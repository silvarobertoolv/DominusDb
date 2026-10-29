from datetime import time,timedelta
from types import SimpleNamespace
import io,json,zipfile
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql,postgresql,oracle
from migration import convert_type,plan,PlanError,destination_objects,_create_object
from target_profiles import dialect_for,identifier,target_limits,check_and_adapt
from type_mapping import parse_target_type
from transfer import payload_chunks
from diagnostics import diagnostic
from connections import engine_for


@pytest.mark.parametrize('source,pg,ora',[
 (sa.BigInteger(),'BIGINT','NUMBER(19, 0)'),
 (mssql.NVARCHAR(None),'TEXT','NCLOB'),
 (mssql.NVARCHAR(250),'VARCHAR(250)','NVARCHAR2(250)'),
 (mssql.DECIMAL(38,38),'NUMERIC(38, 38)','NUMBER(38, 38)'),
 (mssql.UNIQUEIDENTIFIER(),'UUID','NVARCHAR2(36)'),
 (mssql.TIMESTAMP(),'BYTEA','RAW(8)'),
 (mssql.VARBINARY(None),'BYTEA','BLOB'),
 (mssql.VARBINARY(16),'BYTEA','RAW(16)'),
 (mssql.BIT(),'BOOLEAN','NUMBER(1, 0)'),
])
def test_source_types_have_destination_specific_mappings(source,pg,ora):
    assert str(convert_type(source,'postgresql').compile(dialect=dialect_for('postgresql')))==pg
    assert str(convert_type(source,'oracle').compile(dialect=dialect_for('oracle')))==ora


def test_timezone_and_time_have_explicit_native_types():
    assert convert_type(mssql.DATETIMEOFFSET(),'postgresql').timezone
    assert convert_type(mssql.DATETIMEOFFSET(),'oracle').timezone
    assert isinstance(convert_type(mssql.TIME(),'oracle'),oracle.INTERVAL)
    assert isinstance(convert_type(mssql.TIME(),'postgresql'),postgresql.TIME)


@pytest.mark.parametrize('kind,value',[
 ('postgresql','NUMERIC(38,38)'),('postgresql','TIMESTAMP(6) WITH TIME ZONE'),
 ('postgresql','BYTEA'),('postgresql','DOUBLE PRECISION'),('postgresql','UUID'),
 ('oracle','NUMBER(38,38)'),('oracle','NVARCHAR2(1000)'),('oracle','NCLOB'),
 ('oracle','TIMESTAMP(6) WITH TIME ZONE'),('oracle','INTERVAL DAY TO SECOND')])
def test_manual_native_types_compile(kind,value):
    assert str(parse_target_type(value,kind).compile(dialect=dialect_for(kind)))


@pytest.mark.parametrize('kind,value',[
 ('postgresql','BIGINT UNSIGNED'),('postgresql','VARCHAR(5);DROP TABLE x'),('postgresql','NUMBER(19,0)'),
 ('oracle','VARCHAR2(3000)'),('oracle','RAW(3000)'),('oracle','NUMBER(40,0)'),('oracle','TEXT'),
 ('oracle','CLOB NOT NULL'),('postgresql','TIMESTAMP(20)'),('oracle','TIMESTAMP(3)')])
def test_native_manual_types_reject_wrong_dialect_and_sql(kind,value):
    with pytest.raises(ValueError):parse_target_type(value,kind)


def source_fixture(monkeypatch,foreign_action=None):
    import migration
    engine=sa.create_engine('sqlite://')
    metadata=sa.MetaData(schema='main')
    parent=sa.Table('parent',metadata,sa.Column('id',sa.BigInteger,primary_key=True),*[sa.Column('c'+str(i),sa.String(4000)) for i in range(10)])
    child=sa.Table('child',metadata,sa.Column('id',sa.BigInteger,primary_key=True),sa.Column('pid',sa.BigInteger),sa.ForeignKeyConstraint(['pid'],[parent.c.id],name='fk',onupdate=foreign_action,use_alter=True))
    metadata.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.parent',schema='main',name='parent'),dict(id='main.child',schema='main',name='child')],objects=[]))
    return engine


@pytest.mark.parametrize('kind',['mysql','postgresql','oracle'])
def test_plan_uses_correct_ddl_and_compatibility_rules(monkeypatch,kind):
    engine=source_fixture(monkeypatch)
    result=plan(engine,['main.parent','main.child'],target_kind=kind)
    assert result['target_kind']==kind and not result['errors']
    assert result['compatibility']['limits']['kind']==kind
    if kind=='mysql':assert 'ROW_FORMAT=DYNAMIC' in result['ddl'] and result['compatibility']['changes']
    else:
        assert 'ENGINE=' not in result['ddl'] and 'utf8mb4' not in result['ddl'] and 'ROW_FORMAT' not in result['ddl']
        assert not result['compatibility']['changes']
    if kind=='postgresql':assert 'public.main__parent' in result['ddl'] and 'VARCHAR(4000)' in result['ddl']
    if kind=='oracle':assert 'NCLOB' in result['ddl'] and 'NUMBER(19, 0)' in result['ddl']


def test_oracle_does_not_silently_drop_on_update_cascade(monkeypatch):
    source=source_fixture(monkeypatch,'CASCADE')
    assert any('ON UPDATE' in e for e in plan(source,['main.parent','main.child'],target_kind='oracle')['errors'])
    assert 'ON UPDATE CASCADE' in plan(source,['main.parent','main.child'],target_kind='postgresql')['ddl']


@pytest.mark.parametrize('kind,limit',[('mysql',64),('postgresql',63),('oracle',30)])
def test_identifiers_respect_byte_limits_and_remain_deterministic(kind,limit):
    first=identifier('á'*40,'t'*50,kind)
    assert len(first.encode('utf-8'))<=limit
    assert first==identifier('á'*40,'t'*50,kind)
    assert first!=identifier('á'*40,'u'*50,kind)


def test_oracle_index_lob_and_char_family_are_checked():
    meta=sa.MetaData()
    parent=sa.Table('p',meta,sa.Column('id',oracle.NVARCHAR2(20),primary_key=True),sa.Column('memo',oracle.NCLOB()))
    sa.Index('ix_lob',parent.c.memo)
    child=sa.Table('c',meta,sa.Column('pid',oracle.VARCHAR2(20)),sa.ForeignKeyConstraint(['pid'],[parent.c.id],name='fk',use_alter=True))
    result=check_and_adapt([(parent,parent),(child,child)],target_limits(kind='oracle')[0])
    assert any('LOB' in e for e in result['errors'])
    assert any('tipos diferentes' in e for e in result['errors'])


def test_oracle_payload_preserves_hour_as_interval_and_rejects_empty_values():
    value=time(12,34,56,123456)
    payload,size=next(payload_chunks([(value,True)],['hour','flag'],[],4096,'oracle',{'hour':oracle.INTERVAL(),'flag':oracle.NUMBER(1,0)}))
    assert payload==[dict(hour=timedelta(hours=12,minutes=34,seconds=56,microseconds=123456),flag=1)]
    with pytest.raises(ValueError,match='vazio'):list(payload_chunks([('',)],['memo'],[],4096,'oracle'))
    with pytest.raises(ValueError,match='vazio'):list(payload_chunks([(b'',)],['raw'],[],4096,'oracle'))


def test_postgres_payload_rejects_nul_text_and_preserves_binary_nul():
    with pytest.raises(ValueError,match='NUL'):list(payload_chunks([('abc\x00',)],['memo'],[],4096,'postgresql'))
    payload,_=next(payload_chunks([(b'\x00',1)],['bytes','flag'],[],4096,'postgresql',{'flag':sa.Boolean()}))
    assert payload[0]==dict(bytes=b'\x00',flag=True)


@pytest.mark.parametrize('kind,port,schema', [('mysql',3306,None),('postgresql',5432,'public'),('oracle',1521,None)])
def test_connection_drivers_default_ports_schema_and_service(kind,port,schema):
    engine=engine_for(dict(db_type=kind,host='localhost',database='migration',user='user',password='s@fe'), 'target')
    assert engine.dialect.name==kind and engine.url.port==port
    assert engine._sqlbridge_schema==schema
    assert engine.url.password=='s@fe'
    if kind=='oracle':assert engine.url.query['service_name']=='migration' and engine.url.database is None
    else:assert engine.url.database=='migration'


def test_pg_schema_is_used_for_collision_checks(monkeypatch):
    import migration
    calls=[]
    class Inspector:
        def get_table_names(self,schema):calls.append(schema);return ['already_here']
        def get_view_names(self,schema):calls.append(schema);return []
    target=SimpleNamespace(dialect=SimpleNamespace(name='postgresql'),_sqlbridge_schema='migration')
    monkeypatch.setattr(migration.sa,'inspect',lambda _:Inspector())
    assert destination_objects(target)=={'already_here'} and calls==['migration','migration']


def test_diagnostics_extract_sqlstate_and_oracle_code_without_data():
    class PGError(Exception):sqlstate='23505'
    d=diagnostic(sa.exc.IntegrityError('SECRET SQL',{'x':'SECRET DATA'},PGError('duplicate secret row')), 'Insert',hide_values=True)
    assert d['code']=='23505' and d['category']=='dados duplicados' and 'secret' not in d['message']
    error=SimpleNamespace(full_code='ORA-12899')
    d=diagnostic(sa.exc.DataError('SECRET SQL',{},RuntimeError(error)), 'Insert',hide_values=True)
    assert d['code']=='ORA-12899' and d['category']=='texto longo demais'


@pytest.mark.parametrize('kind',['postgresql','oracle'])
def test_native_creation_returns_catalog_metadata(monkeypatch,kind):
    import migration
    metadata=sa.MetaData(schema='migration' if kind=='postgresql' else None)
    table=sa.Table('test',metadata,sa.Column('id',sa.Integer,primary_key=True))
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,statement):pass
        def commit(self):pass
    class Inspector:
        def has_table(self,name,**kw):return True
        def get_columns(self,name,**kw):return [dict(name='id',type=sa.Integer(),nullable=False)]
        def get_pk_constraint(self,name,**kw):return dict(name='pk',constrained_columns=['id'])
        def get_unique_constraints(self,name,**kw):return []
        def get_indexes(self,name,**kw):return []
        def get_foreign_keys(self,name,**kw):return []
    monkeypatch.setattr(migration.sa,'inspect',lambda _:Inspector())
    target=SimpleNamespace(dialect=SimpleNamespace(name=kind),connect=lambda:Conn())
    result=_create_object(target,sa.schema.CreateTable(table),'table',table,table.name)
    assert result['verified'] and result['server_metadata']['columns'][0]['name']=='id'
    assert 'mysql_ddl' not in result


def test_postgres_fixed_width_tuple_limit_is_checked():
    table=sa.Table('wide',sa.MetaData(),*[sa.Column('c'+str(i),sa.BigInteger) for i in range(1200)])
    report=check_and_adapt([(table,table)],target_limits(kind='postgresql')[0])
    assert any('registro local PostgreSQL' in e for e in report['errors'])


def test_native_profile_mismatch_does_not_generate_wrong_ddl(monkeypatch):
    source=source_fixture(monkeypatch)
    target=SimpleNamespace(dialect=SimpleNamespace(name='mysql'))
    with pytest.raises(PlanError,match='ValueError'):plan(source,['main.parent'],target=target,target_kind='postgresql')


def test_oracle_timestamp_with_timezone_uses_timezone_preserving_bind_type():
    import oracledb
    from sqlalchemy.dialects.oracle import oracledb as oracle_driver
    typ=convert_type(mssql.DATETIMEOFFSET(),'oracle')
    adapted=typ.dialect_impl(oracle_driver.dialect(dbapi=oracledb))
    assert adapted.get_dbapi_type(oracledb)==oracledb.DB_TYPE_TIMESTAMP_TZ
    assert str(typ.compile(dialect=dialect_for('oracle')))=='TIMESTAMP WITH TIME ZONE'


def test_oracle_engine_enables_timezone_bind_sizes():
    import oracledb
    engine=engine_for(dict(db_type='oracle',host='localhost',database='service',user='user',password=''), 'target')
    assert oracledb.DB_TYPE_TIMESTAMP_TZ in engine.dialect.include_set_input_sizes


def test_timezone_payload_requires_explicit_offset_and_parses_iso_without_leaking_values():
    from datetime import datetime
    typ=convert_type(mssql.DATETIMEOFFSET(),'oracle')
    payload,_=next(payload_chunks([('2026-10-29T12:30:00-04:00',)],['at'],[],4096,'oracle',{'at':typ}))
    assert payload[0]['at'].utcoffset()==timedelta(hours=-4)
    with pytest.raises(ValueError,match='offset explícito'):list(payload_chunks([(datetime(2026,1,1),)],['at'],[],4096,'oracle',{'at':typ}))
    with pytest.raises(ValueError) as caught:list(payload_chunks([('PRIVATE DATA',)],['at'],[],4096,'oracle',{'at':typ}))
    assert 'PRIVATE DATA' not in str(caught.value)


def test_oracle_unique_index_is_emitted_as_constraint_for_fk_support(monkeypatch):
    import migration
    source=sa.create_engine('sqlite://')
    meta=sa.MetaData(schema='main')
    parent=sa.Table('p',meta,sa.Column('id',sa.Integer,primary_key=True),sa.Column('code',sa.Integer))
    sa.Index('ix_code',parent.c.code,unique=True)
    child=sa.Table('c',meta,sa.Column('parent_code',sa.Integer),sa.ForeignKeyConstraint(['parent_code'],[parent.c.code],name='fk'))
    meta.create_all(source)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.p',schema='main',name='p'),dict(id='main.c',schema='main',name='c')],objects=[]))
    result=plan(source,['main.p','main.c'],target_kind='oracle')
    assert not result['errors']
    assert 'UNIQUE (code)' in result['ddl']
    assert 'FOREIGN KEY(parent_code)' in result['ddl']


@pytest.mark.parametrize('kind,typ',[('postgresql',mssql.DATETIME2()),('postgresql',mssql.TIME()),('postgresql',mssql.DATETIMEOFFSET()),('oracle',mssql.TIME()),('oracle',mssql.DATETIMEOFFSET())])
def test_temporal_auto_type_display_can_be_reused_in_manual_editor(kind,typ):
    rendered=str(convert_type(typ,kind).compile(dialect=dialect_for(kind)))
    assert str(parse_target_type(rendered,kind).compile(dialect=dialect_for(kind)))==rendered


def test_oracle_compiler_setinputsizes_uses_tz_bind_not_plain_timestamp():
    import oracledb
    engine=engine_for(dict(db_type='oracle',host='localhost',database='service',user='user',password=''), 'target')
    table=sa.Table('t',sa.MetaData(),sa.Column('at',convert_type(mssql.DATETIMEOFFSET(),'oracle')))
    compiled=table.insert().values(at=sa.bindparam('at',type_=table.c.at.type)).compile(dialect=engine.dialect)
    sizes=compiled._get_set_input_sizes_lookup()
    assert list(sizes.values())==[oracledb.DB_TYPE_TIMESTAMP_TZ]
