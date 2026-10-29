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


def test_reflection_failure_preserves_stage_table_and_original_error(monkeypatch):
    import migration
    monkeypatch.setattr(migration, 'inventory', lambda _: {'tables':[{'id':'dbo.orders','schema':'dbo','name':'orders'}], 'objects':[]})
    def fail(*args, **kwargs): raise RuntimeError('metadata reflection failed')
    monkeypatch.setattr(migration.sa, 'Table', fail)
    with pytest.raises(migration.PlanError) as caught:
        migration.plan(object(), ['dbo.orders'])
    assert caught.value.object_name == 'dbo.orders'
    assert caught.value.stage == 'Refletir colunas, PKs e relacionamentos'
    assert str(caught.value.cause) == 'metadata reflection failed'


def test_nullable_foreign_key_actions_do_not_crash_plan(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    meta=sa.MetaData(schema='main')
    parent=sa.Table('parent',meta,sa.Column('id',sa.Integer,primary_key=True))
    sa.Table('child',meta,sa.Column('pid',sa.Integer),sa.ForeignKeyConstraint(['pid'],[parent.c.id]))
    meta.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _: {'tables':[{'id':'main.parent','schema':'main','name':'parent'},{'id':'main.child','schema':'main','name':'child'}],'objects':[]})
    inspector=sa.inspect(engine)
    get_fks=inspector.get_foreign_keys
    def null_actions(*args, **kwargs):
        result=get_fks(*args, **kwargs)
        for fk in result: fk['options']={'ondelete':None,'onupdate':None}
        return result
    inspector.get_foreign_keys=null_actions
    monkeypatch.setattr(migration.sa,'inspect',lambda _:inspector)
    prepared=migration.plan(engine,['main.parent','main.child'])
    assert not prepared['errors'] and 'FOREIGN KEY' in prepared['ddl']


def add_second_table(source):
    src=sa.Table('more', sa.MetaData(), sa.Column('id', sa.Integer, primary_key=True))
    src.create(source)
    with source.begin() as conn: conn.execute(src.insert(), {'id':42})
    dst=sa.Table('dbo__more', sa.MetaData(), sa.Column('id', sa.Integer, primary_key=True))
    return src,dst


def test_skip_existing_preserves_data_and_continues_with_new_table():
    source,target,src,dst=pair()
    dst.create(target)
    with target.begin() as conn: conn.execute(dst.insert(), {'id':99,'name':'keep'})
    src2,dst2=add_second_table(source)
    job={}
    execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_warnings'
    assert job['rows']==1
    assert any(e['object']==dst.name and e['status']=='skipped' for e in job['events'])
    with target.connect() as conn:
        assert conn.scalar(sa.select(dst.c.name))=='keep'
        assert conn.scalar(sa.select(dst2.c.id))==42


def test_creation_failure_does_not_prevent_other_table_migration():
    source,target,src,dst=pair()
    # MySQL-specific type unsupported by SQLite simulates a DDL incompatibility.
    dst.c.name.type=mysql.LONGTEXT()
    src2,dst2=add_second_table(source)
    job={}
    execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_errors'
    assert job['rows']==1
    failed=next(e for e in job['events'] if e['status']=='failed')
    assert failed['object']==dst.name and failed['diagnostic']['exception']=='CompileError'
    assert not sa.inspect(target).has_table(dst.name)
    assert any(e['kind']=='data' and e['object']==dst.name and e['status']=='skipped' for e in job['events'])


def test_copy_failure_rolls_back_table_and_continues():
    source,target,src,dst=pair()
    dst.c.name.type=sa.Integer()  # SQLite dynamic types do not reject this.
    # Force a duplicate unique key halfway into the batch copy instead.
    with source.begin() as conn: conn.execute(src.update().values(name='duplicate'))
    dst.append_constraint(sa.UniqueConstraint('name'))
    src2,dst2=add_second_table(source)
    job={}
    execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_errors' and job['rows']==1
    assert job['rows_in_flight']==0
    with target.connect() as conn: assert conn.scalar(sa.select(sa.func.count()).select_from(dst))==0
    assert not any(e['kind']=='data' and e['object']==dst.name and e['status']=='copied' for e in job['events'])


def test_stop_after_error_does_not_attempt_other_tables():
    source,target,src,dst=pair();dst.c.name.type=mysql.LONGTEXT()
    src2,dst2=add_second_table(source);job={}
    execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',False)
    assert job['status']=='failed' and not sa.inspect(target).has_table(dst2.name)
    assert any(e['object']==dst2.name and e['status']=='skipped' for e in job['events'])


def test_successful_ddl_and_index_are_verified_in_catalog():
    source,target,src,dst=pair();sa.Index('ix_name',dst.c.name);job={}
    execute(source,target,{'pairs':[(src,dst)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed'
    created=[e for e in job['events'] if e['status']=='created']
    assert len(created)==2 and all(e['verified'] for e in created)
    assert job['summary']=={'created':2,'copied':1,'failed':0,'skipped':0}


def test_skipped_reference_prevents_fk_creation_on_new_table():
    source,target,src,dst=pair();dst.create(target)
    src2,dst2=add_second_table(source)
    dst2.append_constraint(sa.ForeignKeyConstraint([dst2.c.id],[dst.c.id],name='fk_people',use_alter=True))
    job={};execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_warnings'
    fk=next(e for e in job['events'] if e['kind']=='foreign_key')
    assert fk['status']=='skipped' and 'referenciada' in fk['message']


def test_index_error_does_not_prevent_other_index_creation(monkeypatch):
    import migration
    source,target,src,dst=pair()
    sa.Index('a_bad',dst.c.name);sa.Index('b_good',dst.c.id)
    original=migration._create_object
    def create(target,element,kind,table,name):
        if name=='a_bad': raise sa.exc.OperationalError('hidden SQL',{},RuntimeError(1071,'key too long'))
        return original(target,element,kind,table,name)
    monkeypatch.setattr(migration,'_create_object',create)
    job={};execute(source,target,{'pairs':[(src,dst)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_errors'
    error=next(e for e in job['events'] if e['object']=='a_bad')
    assert error['diagnostic']['code']==1071 and error['diagnostic']['category']=='limite de índice'
    assert any(e['object']=='b_good' and e['status']=='created' for e in job['events'])


def test_mysql_data_error_does_not_disclose_row_values():
    from diagnostics import diagnostic
    exc=sa.exc.IntegrityError('SQL SECRET',{'record':'PARAM SECRET'},RuntimeError(1062,"Duplicate entry 'customer-secret' for key 'unique'"))
    detail=diagnostic(exc,'Copiar dados','dbo__clients',hide_values=True)
    assert detail['code']==1062 and 'duplicados' in detail['hint']
    assert all(secret not in str(detail) for secret in ('SQL SECRET','PARAM SECRET','customer-secret'))


def test_mysql_ddl_captures_server_warnings_and_show_create(monkeypatch):
    import migration
    source,target,src,dst=pair()
    class Result:
        def __init__(self,rows): self.rows=rows
        def fetchall(self): return self.rows
        def one(self): return self.rows[0]
    class Connection:
        dialect=mysql.dialect()
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,element): calls.append('DDL')
        def commit(self): calls.append('COMMIT')
        def exec_driver_sql(self,sql):
            calls.append(sql)
            if sql=='SHOW WARNINGS': return Result([('Warning',1681,'deprecated feature')])
            assert sql.startswith('SHOW CREATE TABLE ')
            return Result([(dst.name,'CREATE TABLE returned_by_mysql (id int)')])
    class Target:
        dialect=mysql.dialect()
        def connect(self): return Connection()
    class Inspector:
        def has_table(self,name): return True
    calls=[]
    monkeypatch.setattr(migration.sa,'inspect',lambda conn:Inspector())
    result=migration._create_object(Target(),CreateTable(dst),'table',dst,dst.name)
    assert calls[:3]==['DDL','SHOW WARNINGS','COMMIT']
    assert result['verified'] and 'returned_by_mysql' in result['mysql_ddl']
    assert result['warnings'][0]['code']==1681


def test_mysql_ddl_is_not_reported_successful_if_verification_fails(monkeypatch):
    import migration
    source,target,src,dst=pair()
    class Inspector:
        def has_table(self,name): return False
    monkeypatch.setattr(migration.sa,'inspect',lambda conn:Inspector())
    with pytest.raises(migration.MigrationError,match='não foi encontrado'):
        migration._create_object(target,CreateTable(dst),'table',dst,dst.name)


def test_existing_view_is_also_preserved_when_skipping():
    source,target,src,dst=pair()
    with target.begin() as conn: conn.exec_driver_sql('CREATE VIEW dbo__people AS SELECT 99 AS id')
    src2,dst2=add_second_table(source);job={}
    execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_warnings' and job['rows']==1
    with target.connect() as conn: assert conn.exec_driver_sql('SELECT id FROM dbo__people').scalar()==99


def test_skip_policy_handles_table_created_after_preflight(monkeypatch):
    import migration
    source,target,src,dst=pair();src2,dst2=add_second_table(source)
    original=migration._create_object
    def concurrent_create(target,element,kind,table,name):
        if name==dst.name:
            dst.create(target)
            raise sa.exc.OperationalError('hidden SQL',{},RuntimeError(1050,'table already exists'))
        return original(target,element,kind,table,name)
    monkeypatch.setattr(migration,'_create_object',concurrent_create)
    job={};execute(source,target,{'pairs':[(src,dst),(src2,dst2)],'errors':[]},job,3,'skip',True)
    assert job['status']=='completed_with_warnings' and job['rows']==1
    assert not any(e['status']=='failed' for e in job['events'])
    with target.connect() as conn: assert conn.scalar(sa.select(sa.func.count()).select_from(dst))==0
