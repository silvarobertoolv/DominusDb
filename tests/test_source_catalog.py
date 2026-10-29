from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from migration import dictionary_type
from source_catalog import reflect_source_table,CatalogInspector,AdaptiveInspector
from connections import engine_for


def error(code=102):
    return sa.exc.ProgrammingError('SELECT fn_listextendedproperty(CAST(:schema AS nvarchar(max)))',{},RuntimeError(code,b"Incorrect syntax near 'CAST'"))


def definitions():
    return [dict(column_name='id',base_type='bigint',declared_type='bigint',is_nullable=False,is_identity=True,is_computed=False),
            dict(column_name='value',base_type='nvarchar',declared_type='custom_text',max_length_bytes=200,is_nullable=True,is_identity=False,is_computed=False,default_definition="(N'default')"),
            dict(column_name='calculated',base_type='int',declared_type='int',is_nullable=True,is_identity=False,is_computed=True,computed_definition='([id]+(1))',is_persisted=True)]


def stub_catalog(monkeypatch):
    import source_catalog
    monkeypatch.setattr(source_catalog,'source_dictionary',lambda *args:definitions())
    monkeypatch.setattr(CatalogInspector,'get_pk_constraint',lambda *args:dict(name='PK_t',constrained_columns=['id']))
    monkeypatch.setattr(CatalogInspector,'get_foreign_keys',lambda *args:[dict(constrained_columns=['value'])])


def test_source_comments_are_disabled_and_catalog_mode_is_validated():
    engine=engine_for(dict(host='localhost',database='source',user='u',metadata_mode='auto'),'source')
    assert engine.dialect.supports_comments is False
    assert engine._sqlbridge_metadata_mode=='auto'
    engine=engine_for(dict(host='localhost',database='source',user='u',metadata_mode='catalog'),'source')
    assert engine._sqlbridge_metadata_mode=='catalog'
    with pytest.raises(ValueError):engine_for(dict(host='localhost',database='source',user='u',metadata_mode='invalid'),'source')


def test_syntax_fallback_discards_partial_table_and_retains_types_pk_and_generated_metadata(monkeypatch):
    import source_catalog
    stub_catalog(monkeypatch)
    real_table=sa.Table;calls=[]
    def fail_native(*args,**kwargs):
        if 'autoload_with' in kwargs:
            real_table(args[0],args[1],sa.Column('incomplete',sa.Integer),schema=kwargs['schema'])
            calls.append('native');raise error()
        return real_table(*args,**kwargs)
    monkeypatch.setattr(source_catalog.sa,'Table',fail_native)
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'))
    metadata=sa.MetaData()
    table=reflect_source_table(engine,'t','dbo',metadata,dictionary_type)
    assert list(table.c.keys())==['id','value','calculated']
    assert list(table.primary_key.columns.keys())==['id']
    assert table.c.value.type.length==100 and table.c.id.identity is not None
    assert table.c.value.server_default is not None and table.c.calculated.computed.persisted
    assert table.info['source_fk_columns']=={'value'}
    assert table.info['reflection_fallback_code']==102
    reflect_source_table(engine,'second','dbo',metadata,dictionary_type)
    assert calls==['native']  # rest of the connection stays in compatible mode
    from sqlalchemy.dialects.mssql import dialect
    assert 'incomplete' not in str(sa.select(table).compile(dialect=dialect()))


def test_explicit_catalog_mode_never_autoloads(monkeypatch):
    import source_catalog
    stub_catalog(monkeypatch)
    real=sa.Table
    def table(*args,**kwargs):
        assert 'autoload_with' not in kwargs
        return real(*args,**kwargs)
    monkeypatch.setattr(source_catalog.sa,'Table',table)
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'),_sqlbridge_metadata_mode='catalog')
    assert reflect_source_table(engine,'t','dbo',sa.MetaData(),dictionary_type).info['reflection_mode']=='catalog'


def test_permission_failure_is_not_hidden_or_retried_as_syntax(monkeypatch):
    import source_catalog
    problem=error(229)
    monkeypatch.setattr(source_catalog.sa,'Table',lambda *args,**kwargs:(_ for _ in ()).throw(problem))
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'))
    with pytest.raises(sa.exc.ProgrammingError) as caught:reflect_source_table(engine,'t','dbo',sa.MetaData(),dictionary_type)
    assert caught.value is problem and not hasattr(engine,'_sqlbridge_catalog_fallback')


def test_inspector_falls_back_for_syntax_but_not_permission_errors(monkeypatch):
    import source_catalog
    class Native:
        def get_table_names(self,schema=None):raise error()
    monkeypatch.setattr(source_catalog.sa,'inspect',lambda _:Native())
    monkeypatch.setattr(CatalogInspector,'get_table_names',lambda self,schema=None:['t'])
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'))
    assert AdaptiveInspector(engine).get_table_names(schema='dbo')==['t']
    assert engine._sqlbridge_catalog_fallback
    class Denied:
        def get_table_names(self,schema=None):raise error(229)
    monkeypatch.setattr(source_catalog.sa,'inspect',lambda _:Denied())
    with pytest.raises(sa.exc.ProgrammingError):AdaptiveInspector(SimpleNamespace(dialect=SimpleNamespace(name='mssql'))).get_table_names(schema='dbo')


def test_composite_fk_and_index_order_includes_and_actions_are_preserved(monkeypatch):
    reader=CatalogInspector(None)
    def rows(sql,*args,**kwargs):
        if 'sys.foreign_keys' in sql:
            return [dict(name='fk',column_name=c,referred_schema='dbo',referred_table='p',referred_column=r,ondelete='SET_NULL',onupdate='NO_ACTION') for c,r in [('b','y'),('a','x')]]
        return [dict(name='ix',is_unique=True,index_type=2,filter_definition=None,column_name=c,is_included_column=inc) for c,inc in [('b',False),('a',False),('memo',True)]]
    monkeypatch.setattr(reader,'rows',rows)
    fk=reader.get_foreign_keys('c','dbo')[0]
    assert fk['constrained_columns']==['b','a'] and fk['referred_columns']==['y','x']
    assert fk['options']==dict(ondelete='SET NULL',onupdate='NO ACTION')
    index=reader.get_indexes('c','dbo')[0]
    assert index['column_names']==['b','a'] and index['include_columns']==['memo'] and index['unique']


def test_catalog_queries_use_parameters_and_no_cast_or_extendedproperty_call():
    calls=[]
    class Result:
        def mappings(self):return []
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params):calls.append((str(sql),params));return Result()
    reader=CatalogInspector(SimpleNamespace(connect=lambda:Conn()))
    name="table'quoted"
    reader.get_pk_constraint(name,'dbo');reader.get_indexes(name,'dbo');reader.get_foreign_keys(name,'dbo');reader.get_unique_constraints(name,'dbo');reader.get_check_constraints(name,'dbo')
    for sql,params in calls:
        assert name not in sql and params['table']==name
        assert 'CAST(' not in sql.upper() and 'fn_listextendedproperty' not in sql


def test_compatible_pk_retains_declared_key_order(monkeypatch):
    import source_catalog
    monkeypatch.setattr(source_catalog,'source_dictionary',lambda *args:[dict(column_name=n,base_type='int',is_nullable=False) for n in ['a','b']])
    monkeypatch.setattr(CatalogInspector,'get_pk_constraint',lambda *args:dict(name='pk',constrained_columns=['b','a']))
    monkeypatch.setattr(CatalogInspector,'get_foreign_keys',lambda *args,**kwargs:[])
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'),_sqlbridge_metadata_mode='catalog')
    table=reflect_source_table(engine,'t','dbo',sa.MetaData(),dictionary_type)
    assert list(table.primary_key.columns.keys())==['b','a']


def test_plan_recovers_from_cast_error_with_compatible_catalog_and_reports_mode(monkeypatch):
    import migration,source_catalog
    stub_catalog(monkeypatch)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='dbo.t',schema='dbo',name='t')],objects=[]))
    monkeypatch.setattr(CatalogInspector,'get_indexes',lambda *args,**kwargs:[])
    monkeypatch.setattr(CatalogInspector,'get_foreign_keys',lambda *args,**kwargs:[])
    monkeypatch.setattr(CatalogInspector,'get_unique_constraints',lambda *args,**kwargs:[])
    monkeypatch.setattr(CatalogInspector,'get_check_constraints',lambda *args,**kwargs:[])
    real=sa.Table
    def table(*args,**kwargs):
        if kwargs.get('autoload_with') is not None:raise error()
        return real(*args,**kwargs)
    monkeypatch.setattr(source_catalog.sa,'Table',table)
    engine=SimpleNamespace(dialect=SimpleNamespace(name='mssql'))
    result=migration.plan(engine,['dbo.t'])
    assert not result['errors'] and 'CREATE TABLE' in result['ddl']
    assert 'BIGINT' in result['ddl'] and 'PRIMARY KEY (id)' in result['ddl']
    assert result['compatibility']['source_reflection']==[dict(table='dbo.t',mode='catalog',fallback_code=102)]
    assert any('recuperada automaticamente' in warning for warning in result['warnings'])


def test_destination_compound_pk_uses_source_declared_order(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('a',sa.Integer),sa.Column('b',sa.Integer),sa.PrimaryKeyConstraint('b','a')).metadata.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    result=migration.plan(engine,['main.t'])
    assert list(result['pairs'][0][1].primary_key.columns.keys())==['b','a']
