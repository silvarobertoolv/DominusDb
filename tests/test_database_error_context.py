from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from diagnostics import diagnostic, register_error_context
from migration import plan


def test_real_dbapi_failure_records_role_driver_and_operation_without_bound_values():
    source=sa.create_engine('sqlite://')
    register_error_context(source,'source')
    with pytest.raises(sa.exc.DBAPIError) as caught:
        with source.connect() as conn:
            conn.execute(sa.text('SELECT :private_value FROM missing_table'),{'private_value':'DO_NOT_EXPOSE'})
    detail=diagnostic(caught.value,'Copiar dados',engines=(source,),hide_values=True)
    assert detail['database_context']==dict(role='source',dialect='sqlite',driver='pysqlite',
        server_version='.'.join(map(str,source.dialect.server_version_info)),operation='SELECT')
    assert detail['connections'][0]['role']=='source'
    serialized=str(detail)
    assert 'DO_NOT_EXPOSE' not in serialized and 'private_value' not in serialized


@pytest.mark.parametrize('dialect,mismatch',[('oracle',True),('mssql',True),('mysql',False)])
def test_innodb_error_is_not_misclassified_as_native_oracle_error(dialect,mismatch):
    original=RuntimeError(1118,'Row size too large (> 8126). BLOB prefix of 0 bytes is stored inline.')
    original.migration_database_context=dict(role='target' if dialect!='mssql' else 'source',
        dialect=dialect,driver='example_driver',server_version=None,operation='CREATE')
    exc=sa.exc.ProgrammingError('PRIVATE SQL',{'private':'DO_NOT_EXPOSE'},original)
    detail=diagnostic(exc,'Criar tabela',hide_values=True)
    assert detail['code']==1118 and detail['reported_family']=='mysql/innodb'
    assert detail['context_mismatch'] is mismatch
    assert detail['database_context']['dialect']==dialect
    assert 'DO_NOT_EXPOSE' not in str(detail) and 'PRIVATE SQL' not in str(detail)
    if mismatch:assert 'gateways/servidores vinculados' in detail['hint']


def test_native_oracle_error_keeps_native_code():
    error=RuntimeError('ORA-12899: texto longo demais')
    error.migration_database_context=dict(role='target',dialect='oracle',driver='oracledb')
    detail=diagnostic(error,'Copiar dados',hide_values=True)
    assert detail['code']=='ORA-12899' and detail['reported_family']=='oracle'
    assert not detail['context_mismatch']


@pytest.mark.parametrize('kind,expected',[('oracle','NCLOB'),('postgresql','VARCHAR(4000)')])
def test_native_destinations_do_not_inherit_innodb_row_size_rules(monkeypatch,kind,expected):
    import migration
    source=sa.create_engine('sqlite://')
    sa.Table('wide',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True),
        *[sa.Column('c'+str(i),sa.String(4000)) for i in range(200)]).metadata.create_all(source)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.wide',schema='main',name='wide')],objects=[]))
    result=plan(source,['main.wide'],target_kind=kind)
    assert not result['errors'] and expected in result['ddl']
    assert 'ROW_FORMAT' not in result['ddl'] and 'InnoDB' not in str(result['errors'])
    assert result['compatibility']['connection_context']['planned_target']==kind
    assert result['compatibility']['connection_context']['target'] is None
