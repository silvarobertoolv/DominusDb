import pytest
from app import app, state

@pytest.fixture
def client():
    app.config['TESTING']=True
    state.update(source=None,target=None,plan=None,job=None)
    with app.test_client() as client:
        client.get('/')
        with client.session_transaction() as sess: client.csrf=sess['csrf']
        yield client


def post(client,path,data=None): return client.post(path,json=data or {},headers={'X-CSRF-Token':client.csrf})


def test_home_and_no_credentials_in_cookie(client):
    response=client.get('/')
    assert response.status_code==200 and b'SQLBridge' in response.data
    with client.session_transaction() as session: assert list(session)==['csrf']


def test_csrf_and_host_checks(client):
    assert client.post('/api/inventory',json={}).status_code==403
    assert client.get('/',headers={'Host':'evil.example'}).status_code==400


def test_missing_connections_and_stale_plan_blocked(client):
    assert post(client,'/api/inventory').status_code==400
    assert post(client,'/api/plan',{'tables':['dbo.t']}).status_code==400
    assert post(client,'/api/run',{'plan_id':'old','confirmation':'MIGRAR'}).status_code==400


def test_jobs_lock_mutations(client):
    state['job']={'status':'running'}
    assert post(client,'/api/inventory').status_code==409
    assert post(client,'/api/connect/source',{}).status_code==409


def test_validation_and_errors_do_not_echo_secrets(client):
    response=post(client,'/api/connect/source',{'password':'secret-test'})
    assert response.status_code==400 and b'secret-test' not in response.data


def test_plan_failure_returns_context_and_redacts_driver_credentials(client, monkeypatch):
    import app as module
    import sqlalchemy as sa
    from migration import PlanError
    # A URL-bearing stub avoids making a real network connection.
    from types import SimpleNamespace
    state['source'] = SimpleNamespace(url=sa.URL.create('mssql+pymssql', username='admin-example', password='secret-password', host='localhost'))
    def fail(*args):
        original = RuntimeError(229, b'permission denied admin-example password=secret-password')
        wrapped = sa.exc.OperationalError('SECRET SQL', {'x': 'SECRET DATA'}, original)
        raise PlanError('Ler índices', 'dbo.Clientes', wrapped)
    monkeypatch.setattr(module, 'plan', fail)
    response = post(client, '/api/plan', {'tables':['dbo.Clientes']})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload['diagnostic']['stage'] == 'Ler índices'
    assert payload['diagnostic']['object'] == 'dbo.Clientes'
    assert '229' in payload['error'] and 'permission denied' in payload['error']
    for private in ('secret-password', 'admin-example', 'SECRET SQL', 'SECRET DATA'):
        assert private not in response.get_data(as_text=True)
    assert state['plan'] is None


def test_unexpected_plan_error_is_not_reported_as_permission_problem(client, monkeypatch):
    import app as module
    state['source'] = object()
    def fail(*args): raise TypeError('unexpected metadata value')
    monkeypatch.setattr(module, 'plan', fail)
    response = post(client, '/api/plan', {'tables':['dbo.Example']})
    assert 'TypeError' in response.get_json()['error']
    assert 'unexpected metadata value' in response.get_json()['error']


def test_run_passes_skip_and_continue_options_to_worker(client, monkeypatch):
    import app as module
    state.update(source=object(), target=object(), plan={'id':'current','errors':[],'warnings':[],'pairs':[]})
    captured={}
    class Thread:
        def __init__(self, target, args, daemon): captured.update(target=target,args=args,daemon=daemon)
        def start(self): captured['started']=True
    monkeypatch.setattr(module.threading,'Thread',Thread)
    response=post(client,'/api/run',{'plan_id':'current','confirmation':'MIGRAR','existing_policy':'skip','continue_on_error':True,'batch':500})
    assert response.status_code==200 and captured['started']
    assert captured['args'][4:7]==(500,'skip',True)


@pytest.mark.parametrize('options',[{'existing_policy':'drop'},{'continue_on_error':'false'}])
def test_run_rejects_invalid_execution_options(client, options):
    state.update(source=object(), target=object(), plan={'id':'current','errors':[]})
    assert post(client,'/api/run',dict(plan_id='current',confirmation='MIGRAR',**options)).status_code==400
    assert state['job'] is None


def test_report_survives_new_plan_and_exposes_object_results(client):
    state['plan']=None
    state['job']={'status':'completed_with_errors','events':[{'kind':'index','object':'ix_name','status':'failed','diagnostic':{'code':1071}}]}
    response=client.get('/api/report')
    assert response.status_code==200
    assert response.get_json()['events'][0]['diagnostic']['code']==1071
    assert 'attachment' in response.headers['Content-Disposition']


def test_plan_api_and_zip_include_adapted_ddl_and_dictionary(client, monkeypatch):
    import sqlalchemy as sa
    import migration
    import io,json,zipfile
    source=sa.create_engine('sqlite://')
    sa.Table('wide',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True),*[sa.Column('c'+str(i),sa.String(4000)) for i in range(10)]).metadata.create_all(source)
    state.update(source=source,target=sa.create_engine('sqlite://'))
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.wide',schema='main',name='wide')],objects=[]))
    response=post(client,'/api/plan',dict(tables=['main.wide']))
    assert response.status_code==200
    data=response.get_json()
    assert data['compatibility']['changes'] and not data['errors']
    assert 'TEXT' in data['ddl'] and 'ROW_FORMAT=DYNAMIC' in data['ddl']
    assert data['destination_check']==[dict(table='main__wide',exists=False)]
    archive=zipfile.ZipFile(io.BytesIO(client.get('/api/export').data))
    report=json.loads(archive.read('compatibilidade.json'))
    assert report['changes']==data['compatibility']['changes']
    assert report['source_dictionary'][0]['table']=='main.wide'


def test_manual_mapping_can_unblock_plan_and_run_without_accepting_stale_id(client, monkeypatch):
    import sqlalchemy as sa
    import migration
    import app as module
    source=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True),sa.Column('problem',sa.String(100))).metadata.create_all(source)
    state.update(source=source,target=sa.create_engine('sqlite://'))
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    original=migration.dictionary_type
    monkeypatch.setattr(migration,'dictionary_type',lambda c,d:sa.types.NullType() if c.name=='problem' else original(c,d))
    first=post(client,'/api/plan',dict(tables=['main.t'])).get_json()
    assert first['errors'] and first['column_mappings'][1]['error']
    assert post(client,'/api/run',dict(plan_id=first['id'],confirmation='MIGRAR')).status_code==400
    corrected=post(client,'/api/plan',dict(tables=['main.t'],overrides={'main.t':{'problem':'VARCHAR(100)'}})).get_json()
    assert not corrected['errors'] and corrected['id']!=first['id']
    assert corrected['manual_mappings']=={'main.t':{'problem':'VARCHAR(100)'}}
    assert post(client,'/api/run',dict(plan_id=first['id'],confirmation='MIGRAR')).status_code==400
    captured={}
    class Thread:
        def __init__(self,target,args,daemon):captured['plan']=args[2]
        def start(self):pass
    monkeypatch.setattr(module.threading,'Thread',Thread)
    response=post(client,'/api/run',dict(plan_id=corrected['id'],confirmation='MIGRAR'))
    assert response.status_code==200
    assert captured['plan']['pairs'][0][1].c.problem.type.length==100
    assert state['job']['manual_mappings']==corrected['manual_mappings']


@pytest.mark.parametrize('status',['paused','pause_requested','cancelling'])
def test_mutations_remain_locked_while_job_paused_or_cancelling(client,status):
    state['job']={'status':status}
    assert post(client,'/api/inventory').status_code==409


def test_job_controls_validate_csrf_job_id_and_tuning(client):
    from transfer import TransferControl
    state.update(job=dict(id='run1',status='running'),control=TransferControl(100))
    assert client.post('/api/job/control',json=dict(job_id='run1',action='pause')).status_code==403
    assert post(client,'/api/job/control',dict(job_id='old',action='cancel')).status_code==409
    assert post(client,'/api/job/control',dict(job_id='run1',action='pause')).status_code==200
    assert state['control'].paused and state['job']['status']=='pause_requested'
    assert post(client,'/api/job/control',dict(job_id='run1',action='tune',batch=20000)).status_code==200
    assert state['control'].batch==20000
    assert post(client,'/api/job/control',dict(job_id='run1',action='tune',batch=50001)).status_code==400
    assert post(client,'/api/job/control',dict(job_id='run1',action='resume')).status_code==200
    assert not state['control'].paused
    assert post(client,'/api/job/control',dict(job_id='run1',action='cancel')).status_code==200
    assert state['control'].cancelled and state['job']['status']=='cancelling'


def test_polling_is_incremental_and_omits_large_schema_report(client):
    state['job']=dict(id='j',status='running',compatibility={'huge':'schema'},manual_mappings={},events=[{'object':str(i)} for i in range(250)],logs=['x']*500)
    first=client.get('/api/job?after=0').get_json()
    assert len(first['events'])==200 and first['next_cursor']==200
    assert first['events_total']==250 and 'compatibility' not in first
    assert len(first['logs'])==30
    second=client.get('/api/job?after=200').get_json()
    assert len(second['events'])==50 and second['events'][0]['object']=='200'
    assert client.get('/api/job?after=-1').status_code==400
    assert client.get('/api/job?after=invalid').status_code==400


@pytest.mark.parametrize('options',[{'transaction_mode':'invalid'},{'verify_count':'yes'},{'max_batch_mb':0},{'max_batch_mb':100},{'batch':True}])
def test_invalid_performance_options_do_not_start_worker(client,options):
    state.update(source=object(),target=object(),plan=dict(id='p',errors=[]))
    assert post(client,'/api/run',dict(plan_id='p',confirmation='MIGRAR',**options)).status_code==400
    assert state['job'] is None


@pytest.mark.parametrize('destination',['postgresql','oracle'])
def test_native_plan_api_exports_correct_dialect_types_and_filename(client,monkeypatch,destination):
    import sqlalchemy as sa
    import migration
    import io,zipfile
    source=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('id',sa.BigInteger,primary_key=True),sa.Column('memo',sa.Text)).metadata.create_all(source)
    state['source']=source
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    data=post(client,'/api/plan',dict(tables=['main.t'],target_kind=destination)).get_json()
    assert data['target_kind']==destination and not data['errors']
    assert 'ROW_FORMAT' not in data['ddl']
    assert ('NCLOB' if destination=='oracle' else 'TEXT') in data['ddl']
    assert ('NUMBER(19,0)' if destination=='oracle' else 'UUID') in data['type_examples']
    archive=zipfile.ZipFile(io.BytesIO(client.get('/api/export').data))
    assert destination+'-plan.sql' in archive.namelist()


def test_oracle_connection_probe_uses_dual(client,monkeypatch):
    import app as module
    from types import SimpleNamespace
    calls=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql):calls.append(str(sql))
    engine=SimpleNamespace(dialect=SimpleNamespace(name='oracle'),_sqlbridge_schema=None,connect=lambda:Conn())
    monkeypatch.setattr(module,'engine_for',lambda data,kind:engine)
    assert post(client,'/api/connect/target',dict(db_type='oracle')).status_code==200
    assert calls==['SELECT 1 FROM DUAL']


def test_oracle_empty_text_policy_is_validated_and_passed_to_worker(client,monkeypatch):
    import app as module
    state.update(source=object(),target=object(),plan=dict(id='p',errors=[],target_kind='oracle'))
    captured={}
    class Thread:
        def __init__(self,target,args,daemon):captured['options']=args[7]
        def start(self):pass
    monkeypatch.setattr(module.threading,'Thread',Thread)
    assert post(client,'/api/run',dict(plan_id='p',confirmation='MIGRAR',oracle_empty_text='null')).status_code==200
    assert captured['options']['oracle_empty_text']=='null'
    state['job']=None
    assert post(client,'/api/run',dict(plan_id='p',confirmation='MIGRAR',oracle_empty_text='replace')).status_code==400
    state['plan']['target_kind']='postgresql'
    assert post(client,'/api/run',dict(plan_id='p',confirmation='MIGRAR',oracle_empty_text='null')).status_code==400
