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
    assert captured['args'][-3:]==(500,'skip',True)


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
