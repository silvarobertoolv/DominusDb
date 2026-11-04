from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from migration import plan, execute, PlanError, _create_object
from target_profiles import dialect_for


def fixture(monkeypatch, orphan=False):
    import migration
    source=sa.create_engine('sqlite://')
    meta=sa.MetaData(schema='main')
    parent=sa.Table('Pessoas',meta,sa.Column('Código',sa.Integer,primary_key=True),
        sa.Column('SEI',sa.String(40)),sa.UniqueConstraint('SEI',name='uq_sei'))
    child=sa.Table('Filhos',meta,sa.Column('id',sa.Integer,primary_key=True),
        sa.Column('Código Pessoa',sa.Integer),sa.ForeignKeyConstraint(['Código Pessoa'],[parent.c['Código']],name='fk_pessoa'))
    meta.create_all(source)
    with source.begin() as conn:
        conn.execute(parent.insert(),{'Código':1,'SEI':'123'})
        conn.execute(child.insert(),{'id':10,'Código Pessoa':999 if orphan else 1})
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.'+t.name,schema='main',name=t.name) for t in (parent,child)],objects=[]))
    return source


@pytest.mark.parametrize('kind',['mysql','postgresql','oracle'])
@pytest.mark.parametrize('policy',['compatible','strict','omit'])
def test_plan_records_constraint_policy_and_remapped_keys(monkeypatch,kind,policy):
    source=fixture(monkeypatch)
    result=plan(source,['main.Pessoas','main.Filhos'],target_kind=kind,
                column_name_case='upper',column_naming='normalize',foreign_key_policy=policy)
    assert not result['errors'] and result['foreign_key_policy']==policy
    report=result['compatibility']['constraints']
    assert report['foreign_key_policy']==policy
    assert sum(c['kind']=='PRIMARY KEY' for c in report['objects'])==2
    assert any(c['kind']=='UNIQUE' for c in report['objects'])
    fk=next(c for c in report['objects'] if c['kind']=='FOREIGN KEY')
    if policy=='omit':
        assert 'FOREIGN KEY' not in result['ddl'] and fk['status']=='omitted'
    else:
        assert 'FOREIGN KEY' in result['ddl'] and fk['status']=='planned'
        assert fk['target_columns']==['CODIGO_PESSOA'] and fk['referred_columns']==['CODIGO']


@pytest.mark.parametrize('policy,blocked',[('strict',True),('compatible',False),('omit',False)])
def test_strict_plan_requires_selected_references(monkeypatch,policy,blocked):
    source=fixture(monkeypatch)
    result=plan(source,['main.Filhos'],foreign_key_policy=policy)
    assert bool(result['errors']) is blocked
    entry=next(c for c in result['compatibility']['constraints']['objects'] if c['kind']=='FOREIGN KEY')
    assert entry['status']==('blocked' if blocked else 'omitted')
    if blocked:assert 'main.Pessoas' in result['errors'][0]


def test_strict_skip_existing_relationship_blocks_before_any_new_ddl(monkeypatch):
    source=fixture(monkeypatch)
    result=plan(source,['main.Pessoas','main.Filhos'],foreign_key_policy='strict')
    target=sa.create_engine('sqlite://')
    result['pairs'][0][1].create(target)
    job={}
    execute(source,target,result,job,2,existing_policy='skip',continue_on_error=True)
    assert job['status']=='failed'
    assert 'Modo estrito' in job['error'] and not sa.inspect(target).has_table('Filhos')
    assert job['referential_integrity']['status']=='not_confirmed'


@pytest.mark.parametrize('policy,orphan,status,integrity',[
    ('strict',False,'completed','confirmed'),
    ('strict',True,'failed','not_confirmed'),
    ('compatible',True,'completed_with_errors','not_confirmed'),
    ('omit',True,'completed_with_warnings','not_requested')])
def test_fk_order_validation_failures_and_final_report(monkeypatch,policy,orphan,status,integrity):
    import migration
    source=fixture(monkeypatch,orphan)
    result=plan(source,['main.Pessoas','main.Filhos'],foreign_key_policy=policy)
    target=sa.create_engine('sqlite://')
    original=migration._create_object
    def create(target,element,kind,table,name):
        if kind!='foreign_key':return original(target,element,kind,table,name)
        # SQLite cannot ALTER ADD CONSTRAINT. Substitute only that operation,
        # verifying ordering and rejecting real orphan rows with an anti-join.
        with target.connect() as conn:
            assert conn.scalar(sa.text('SELECT COUNT(*) FROM "Pessoas"'))==1
            assert conn.scalar(sa.text('SELECT COUNT(*) FROM "Filhos"'))==1
            missing=conn.scalar(sa.text('SELECT COUNT(*) FROM "Filhos" f LEFT JOIN "Pessoas" p ON f."Código Pessoa"=p."Código" WHERE p."Código" IS NULL'))
        if missing:raise RuntimeError('ORA-02291: integridade referencial violada')
        return dict(message='FK validada no stand-in de teste.',verified=True,warnings=[])
    monkeypatch.setattr(migration,'_create_object',create)
    job={};execute(source,target,result,job,2,continue_on_error=True)
    assert job['status']==status and job['referential_integrity']['status']==integrity
    assert job['referential_integrity']['planned']==(0 if policy=='omit' else 1)
    if policy=='omit':assert not any(e['kind']=='foreign_key' for e in job['events'])


def test_mysql_enables_foreign_key_checks_before_add_constraint(monkeypatch):
    import migration
    meta=sa.MetaData()
    parent=sa.Table('p',meta,sa.Column('id',sa.Integer,primary_key=True))
    child=sa.Table('c',meta,sa.Column('pid',sa.Integer),sa.ForeignKeyConstraint(['pid'],[parent.c.id],name='fk',use_alter=True))
    operations=[]
    class Result:
        def scalar_one(self):return 1
        def fetchall(self):return []
        def one(self):return ('c','CREATE TABLE c')
    class Conn:
        dialect=dialect_for('mysql')
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def exec_driver_sql(self,sql):operations.append(sql);return Result()
        def execute(self,ddl):operations.append('ADD FK')
        def commit(self):operations.append('COMMIT')
    inspector=SimpleNamespace(get_foreign_keys=lambda *a,**k:[dict(name='fk')])
    monkeypatch.setattr(migration.sa,'inspect',lambda _:inspector)
    target=SimpleNamespace(dialect=dialect_for('mysql'),connect=lambda:Conn())
    fk=next(iter(child.foreign_key_constraints))
    outcome=_create_object(target,sa.schema.AddConstraint(fk),'foreign_key',child,'fk')
    assert operations[:3]==['SET SESSION foreign_key_checks=1','SELECT @@SESSION.foreign_key_checks','ADD FK']
    assert outcome['verified'] is True


def test_invalid_policy_rejected_before_source_access():
    with pytest.raises(PlanError):plan(object(),['dbo.t'],foreign_key_policy='ignore_all')
