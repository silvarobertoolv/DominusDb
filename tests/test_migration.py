import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.schema import CreateTable
from migration import convert_type, target_name, execute

@pytest.mark.parametrize('source,expected', [(mssql.TINYINT(),'TINYINT UNSIGNED'),(mssql.NVARCHAR(None),'LONGTEXT'),(mssql.MONEY(),'NUMERIC(19, 4)'),(mssql.TIMESTAMP(),'BINARY(8)'),(mssql.UNIQUEIDENTIFIER(),'VARCHAR(36)'),(mssql.DATETIME2(),'DATETIME(6)'),(mssql.VARBINARY(None),'LONGBLOB')])
def test_type_conversion(source, expected):
    assert str(convert_type(source).compile(dialect=mysql.dialect())) == expected


def test_unsupported_type_is_blocked():
    with pytest.raises(ValueError): convert_type(mssql.DATETIMEOFFSET())
    with pytest.raises(ValueError): convert_type(sa.types.NullType())


def test_names_have_stable_hash_and_do_not_exceed_mysql_limit():
    a=target_name('s'*70,'t'); b=target_name('s'*70,'u')
    assert len(a)==64 and a!=b and a==target_name('s'*70,'t')


def pair():
    source=sa.create_engine('sqlite://')
    target=sa.create_engine('sqlite://')
    src=sa.Table('people',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),sa.Column('name',sa.String(80)))
    dst=sa.Table('dbo__people',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True,autoincrement=False),sa.Column('name',sa.String(80)))
    src.create(source)
    with source.begin() as conn: conn.execute(src.insert(),[{'id':i,'name':f'José {i}'} for i in range(7)])
    return source,target,src,dst


def test_real_batch_copy_and_counts():
    source,target,src,dst=pair(); job={}
    execute(source,target,{'pairs':[(src,dst)],'errors':[]},job,3)
    assert job['status']=='completed' and job['rows']==7
    assert job['tables'][0]['target_count']==7
    with target.connect() as conn: assert conn.scalar(sa.select(dst.c.name).where(dst.c.id==3))=='José 3'


def test_existing_table_is_never_changed():
    source,target,src,dst=pair(); dst.create(target)
    with target.begin() as conn: conn.execute(dst.insert(),{'id':99,'name':'keep'})
    job={}; execute(source,target,{'pairs':[(src,dst)],'errors':[]},job,2)
    assert job['status']=='failed' and 'já existe' in job['error']
    with target.connect() as conn: assert conn.scalar(sa.select(dst.c.name))=='keep'


def test_unsupported_plan_does_not_create_destination():
    source,target,src,dst=pair(); job={}
    execute(source,target,{'pairs':[(src,dst)],'errors':['blocked']},job,3)
    assert job['status']=='failed' and not sa.inspect(target).get_table_names()


def test_foreign_keys_are_deferred_in_mysql_ddl():
    meta=sa.MetaData()
    parent=sa.Table('parent',meta,sa.Column('id',sa.Integer,primary_key=True))
    child=sa.Table('child',meta,sa.Column('pid',sa.Integer),sa.ForeignKeyConstraint(['pid'],[parent.c.id],use_alter=True,name='fk'))
    assert 'FOREIGN KEY' not in str(CreateTable(child).compile(dialect=mysql.dialect()))


def test_plan_reflection_includes_columns_pk_and_fk(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    meta=sa.MetaData(schema='main')
    parent=sa.Table('parent',meta,sa.Column('id',sa.Integer,primary_key=True))
    child=sa.Table('child',meta,sa.Column('id',sa.Integer,primary_key=True),sa.Column('pid',sa.Integer),sa.ForeignKeyConstraint(['pid'],['main.parent.id'],ondelete='CASCADE'))
    sa.Index('ix_pid',child.c.pid)
    meta.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _: {'tables':[{'id':'main.parent','schema':'main','name':'parent'},{'id':'main.child','schema':'main','name':'child'}],'objects':[]})
    prepared=migration.plan(engine,['main.parent','main.child'])
    assert prepared['errors']==[]
    assert 'ON DELETE CASCADE' in prepared['ddl']
    assert 'CREATE INDEX' in prepared['ddl']
    assert 'PRIMARY KEY' in prepared['ddl']
    prepared=migration.plan(engine,['main.child'])
    assert 'FOREIGN KEY' not in prepared['ddl']
    assert any('fora da seleção' in w for w in prepared['warnings'])


def test_plan_handles_mssql_unsupported_reflection_methods(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    sa.Table('data',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True)).metadata.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _: {'tables':[{'id':'main.data','schema':'main','name':'data'}],'objects':[]})
    original_inspect=sa.inspect
    inspector=original_inspect(engine)
    def unsupported(*args,**kwargs): raise NotImplementedError()
    inspector.get_unique_constraints=unsupported
    inspector.get_check_constraints=unsupported
    monkeypatch.setattr(migration.sa,'inspect',lambda _: inspector)
    prepared=migration.plan(engine,['main.data'])
    assert not prepared['errors'] and 'CREATE TABLE' in prepared['ddl']
