import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, mssql
from type_mapping import parse_target_type
from migration import convert_type, dictionary_type


@pytest.mark.parametrize('source,expected',[(sa.BigInteger(),'BIGINT'),(sa.SmallInteger(),'SMALLINT'),(mssql.BIGINT(),'BIGINT')])
def test_generic_integer_names_are_automatic(source,expected):
    assert str(convert_type(source).compile(dialect=mysql.dialect()))==expected


def test_bigint_from_sqlserver_dictionary_is_automatic():
    col=sa.Column('id',sa.types.NullType())
    assert str(convert_type(dictionary_type(col,dict(base_type='bigint'))))=='BIGINT'


@pytest.mark.parametrize('value,expected',[('bigint','BIGINT'),('BIGINT UNSIGNED','BIGINT UNSIGNED'),('decimal(18, 2)','DECIMAL(18, 2)'),('varchar(255)','VARCHAR(255)'),('longtext','LONGTEXT'),('datetime(6)','DATETIME(6)'),('int','INTEGER'),('numeric(12)','DECIMAL(12, 0)')])
def test_type_parser_accepts_valid_types(value,expected):
    assert str(parse_target_type(value).compile(dialect=mysql.dialect()))==expected


@pytest.mark.parametrize('value',['BIGINT; DROP TABLE x','VARCHAR(20) NOT NULL','INT DEFAULT 0','ENUM(1)', 'DECIMAL(38,38)','VARCHAR(0)','VARCHAR(20000)','BINARY(256)','DATETIME(7)','INT(11)','TEXT UNSIGNED','VARCHAR(255) COLLATE ascii_bin','DECIMAL(2,3)',None,{}])
def test_type_parser_rejects_raw_sql_and_invalid_parameters(value):
    with pytest.raises(ValueError):parse_target_type(value)


def setup_plan(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True),sa.Column('problem',sa.String(100))).metadata.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    original=migration.dictionary_type
    monkeypatch.setattr(migration,'dictionary_type',lambda col,d: sa.types.NullType() if col.name=='problem' else original(col,d))
    return engine,migration


def test_unmapped_column_stays_editable_and_manual_type_unblocks_plan(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    initial=migration.plan(engine,['main.t'])
    assert initial['errors']
    missing=next(c for c in initial['column_mappings'] if c['column']=='problem')
    assert missing['error'] and missing['target_type'] is None
    corrected=migration.plan(engine,['main.t'],overrides={'main.t':{'problem':'LONGTEXT'}})
    assert not corrected['errors']
    assert 'LONGTEXT' in corrected['ddl']
    assert corrected['column_mappings'][1]['requested_type']=='LONGTEXT'
    assert corrected['compatibility']['manual_mappings']=={'main.t':{'problem':'LONGTEXT'}}


def test_invalid_manual_type_is_a_column_error_not_executable_sql(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    result=migration.plan(engine,['main.t'],overrides={'main.t':{'problem':'INT); DROP TABLE x;--'}})
    assert result['errors'] and result['column_mappings'][1]['error']
    assert 'DROP TABLE' not in result['ddl']


def test_manually_mapping_a_pk_to_text_still_blocks_index_incompatibility(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    result=migration.plan(engine,['main.t'],overrides={'main.t':{'id':'LONGTEXT','problem':'VARCHAR(100)'}})
    assert any('1170' in e for e in result['errors'])


def test_unknown_manual_columns_and_tables_are_not_silently_ignored(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    result=migration.plan(engine,['main.t'],overrides={'main.t':{'absent':'INT','problem':'TEXT'}})
    assert any('inexistentes' in e for e in result['errors'])
    with pytest.raises(migration.PlanError):migration.plan(engine,['main.t'],overrides={'elsewhere':{'id':'INT'}})


def test_manual_varchar_is_not_silently_overwritten_by_row_size_adaptation(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    result=migration.plan(engine,['main.t'],overrides={'main.t':{'problem':'VARCHAR(16383)'}})
    assert result['pairs'][0][1].c.problem.type.length==16383
    assert any('linha estimada' in e for e in result['errors'])


def test_manual_type_flows_into_real_creation_and_batch_copy(monkeypatch):
    engine,migration=setup_plan(monkeypatch)
    with engine.begin() as conn:conn.exec_driver_sql("INSERT INTO main.t VALUES (1, 'Olá')")
    target=sa.create_engine('sqlite://')
    prepared=migration.plan(engine,['main.t'],overrides={'main.t':{'problem':'VARCHAR(100)'}})
    job={}
    migration.execute(engine,target,prepared,job,100)
    assert job['status']=='completed' and job['rows']==1
    with target.connect() as conn:assert conn.exec_driver_sql('SELECT problem FROM main__t').scalar()=='Olá'
