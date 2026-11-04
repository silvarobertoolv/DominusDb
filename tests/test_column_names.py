import pytest
import sqlalchemy as sa
from migration import plan, execute, PlanError, _create_object
from target_profiles import column_identifier, dialect_for
from transfer import copy_table, TransferControl


def source_fixture(monkeypatch):
    import migration
    source = sa.create_engine('sqlite://')
    metadata = sa.MetaData(schema='main')
    parent = sa.Table('Pessoas', metadata,
        sa.Column('Código pessoa', sa.Integer, primary_key=True),
        sa.Column('Descrição Médica', sa.String(40), nullable=False),
        sa.Column('Uma "observação"', sa.String(30)))
    child = sa.Table('Pedidos', metadata,
        sa.Column('ID Pedido', sa.Integer, primary_key=True),
        sa.Column('Código pessoa', sa.Integer),
        sa.ForeignKeyConstraint(['Código pessoa'], [parent.c['Código pessoa']], name='fk'))
    sa.Index('ix_pessoa', child.c['Código pessoa'])
    metadata.create_all(source)
    monkeypatch.setattr(migration, 'inventory', lambda _: dict(tables=[dict(
        id='main.'+t.name, schema='main', name=t.name) for t in (parent, child)], objects=[]))
    with source.begin() as conn:
        conn.execute(parent.insert(), [{'Código pessoa': 1, 'Descrição Médica': 'Saúde', 'Uma "observação"': 'certo'}])
        conn.execute(child.insert(), [{'ID Pedido': 10, 'Código pessoa': 1}])
    return source


@pytest.mark.parametrize('kind', ['mysql', 'postgresql', 'oracle'])
@pytest.mark.parametrize('naming', ['preserve', 'normalize'])
@pytest.mark.parametrize('name_case', ['preserve', 'upper'])
def test_ddl_pk_fk_indexes_and_values_keep_source_mapping(monkeypatch, kind, naming, name_case):
    source = source_fixture(monkeypatch)
    result = plan(source, ['main.Pessoas','main.Pedidos'], target_kind=kind,
                  column_name_case=name_case, column_naming=naming)
    assert not result['errors']
    parent, child = [dst for _,dst in result['pairs']]
    expected = column_identifier('Código pessoa', kind, 30 if kind=='oracle' else 63, name_case, naming)
    assert parent.c['Código pessoa'].name == expected
    fk = next(iter(child.foreign_key_constraints))
    assert next(iter(fk.elements)).column is parent.c['Código pessoa']
    assert list(parent.primary_key.columns) == [parent.c['Código pessoa']]
    assert next(iter(child.indexes)).columns[0] is child.c['Código pessoa']
    assert result['column_mappings'][1]['target_name'] == parent.c['Descrição Médica'].name
    assert result['compatibility']['column_name_case'] == name_case
    if kind == 'oracle' and naming == 'preserve':
        assert '"'+expected+'"' in result['ddl']
        assert '""' in result['ddl']  # Embedded identifier quotes are doubled.
    if kind == 'mysql':
        # Real SQLite DDL, INSERT, index and FK declarations with MySQL's
        # portable type subset; verifies renamed columns don't lose values.
        target = sa.create_engine('sqlite://'); job = {'rows':0}
        for src,dst in result['pairs']:
            dst.create(target)
            copied = copy_table(source, target, src, dst, job, 2, {}, TransferControl(2))
            assert copied['copied'] == 1
        assert job['rows'] == 2
        with target.connect() as conn:
            row = conn.execute(sa.select(parent)).one()
            assert tuple(row) == (1, 'Saúde', 'certo')


def test_normalization_handles_digits_accents_and_long_names():
    assert column_identifier('123 Situação!', 'oracle', 30, 'upper', 'normalize') == 'C_123_SITUACAO'
    name = column_identifier('Descrição '*20, 'oracle', 30, 'upper', 'normalize')
    assert len(name.encode()) <= 30 and name == name.upper()
    assert column_identifier('rowid','oracle',30,'upper','normalize') == 'C_ROWID'
    assert column_identifier('ctid','postgresql',63,'preserve','normalize') == 'C_ctid'


def test_normalization_collision_is_blocked(monkeypatch):
    import migration
    source = sa.create_engine('sqlite://')
    sa.Table('t', sa.MetaData(schema='main'), sa.Column('Situação', sa.Integer), sa.Column('Situacao', sa.Integer)).metadata.create_all(source)
    monkeypatch.setattr(migration, 'inventory', lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    result = plan(source, ['main.t'], column_naming='normalize')
    assert any('colisão de nomes' in e for e in result['errors'])
    job = {}; target = sa.create_engine('sqlite://')
    execute(source, target, result, job, 2)
    assert job['status'] == 'failed' and not sa.inspect(target).get_table_names()


def test_oracle_nullable_override_is_explicit_audited_and_pk_protected(monkeypatch):
    source = source_fixture(monkeypatch)
    normal = plan(source, ['main.Pessoas'], target_kind='oracle')
    assert not normal['pairs'][0][1].c['Descrição Médica'].nullable
    result = plan(source, ['main.Pessoas'], target_kind='oracle', column_naming='normalize', column_name_case='upper',
                  nullable_overrides={'main.Pessoas':['Descrição Médica']})
    assert not result['errors']
    dst = result['pairs'][0][1]
    assert dst.c['Descrição Médica'].nullable and not dst.c['Código pessoa'].nullable
    assert any('NOT NULL removida SOMENTE' in w for w in result['warnings'])
    assert result['compatibility']['nullable_overrides'] == {'main.Pessoas':['Descrição Médica']}
    blocked = plan(source, ['main.Pessoas'], target_kind='oracle',nullable_overrides={'main.Pessoas':['Código pessoa']})
    assert any('chave primária' in e for e in blocked['errors'])
    with pytest.raises(PlanError):plan(source, ['main.Pessoas'], target_kind='mysql',nullable_overrides={'main.Pessoas':['Descrição Médica']})
    with pytest.raises(PlanError):plan(source, ['main.Pessoas'], column_name_case='invalid')


@pytest.mark.parametrize('post_commit', [False,True])
def test_ddl_failures_distinguish_execution_from_metadata_and_keep_ddl(monkeypatch, post_commit):
    import migration
    source = sa.create_engine('sqlite://'); target = sa.create_engine('sqlite://')
    table = sa.Table('new_table', sa.MetaData(), sa.Column('Espaço Á', sa.Integer))
    if post_commit:
        original = migration.sa.inspect
        class BrokenInspector:
            def has_table(self,*a,**kw):raise RuntimeError('ORA-03001: recurso não implementado')
        monkeypatch.setattr(migration.sa, 'inspect', lambda c: BrokenInspector() if isinstance(c,sa.Connection) else original(c))
    else:
        def fail(conn, cursor, statement, parameters, context, executemany):
            raise RuntimeError('ORA-03001: recurso não implementado')
        sa.event.listen(target,'before_cursor_execute',fail)
    with pytest.raises(RuntimeError) as caught:
        _create_object(target, sa.schema.CreateTable(table), 'table', table, table.name)
    assert caught.value.migration_ddl_committed == post_commit
    assert 'CREATE TABLE' in caught.value.migration_ddl
    assert ('metadados' in caught.value.migration_ddl_stage) == post_commit


def test_failed_ddl_event_exposes_phase_sql_and_commit_state():
    source = sa.create_engine('sqlite://'); target = sa.create_engine('sqlite://')
    table = sa.Table('t',sa.MetaData(),sa.Column('Espaço',sa.Integer))
    def fail(conn,cursor,statement,parameters,context,executemany):
        if statement.lstrip().startswith('CREATE TABLE'):
            raise RuntimeError('ORA-03001: recurso não implementado')
    sa.event.listen(target,'before_cursor_execute',fail)
    job = {}
    execute(source,target,dict(pairs=[(table,table)],errors=[]),job,2,continue_on_error=True)
    event = next(e for e in job['events'] if e['kind']=='table')
    assert event['status']=='failed' and event['ddl_committed'] is False
    assert 'CREATE TABLE' in event['ddl']
    assert event['diagnostic']['stage']=='Executar DDL no destino'
    assert event['diagnostic']['code']=='ORA-03001'


@pytest.mark.parametrize('empty_text', [False,True])
def test_oracle_insert_uses_ascii_bind_names_with_quoted_identifiers(monkeypatch, empty_text):
    from types import SimpleNamespace
    import transfer
    source = source_fixture(monkeypatch)
    result = plan(source, ['main.Pessoas'], target_kind='oracle',
                  nullable_overrides={'main.Pessoas':['Descrição Médica']} if empty_text else None)
    src,dst = result['pairs'][0]
    if empty_text:
        with source.begin() as conn:conn.execute(src.update().values({'Descrição Médica':''}))
    statements=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self, stmt, rows):
            statements.append((stmt,rows))
        def commit(self):pass
    target = SimpleNamespace(dialect=dialect_for('oracle'),connect=lambda:Conn())
    result = copy_table(source,target,src,dst,{'rows':0},2,dict(verify_count=False,oracle_empty_text='null'),TransferControl(2))
    stmt,rows=statements[0]
    compiled=stmt.compile(dialect=dialect_for('oracle'))
    assert set(compiled.params) == {'sb_0','sb_1','sb_2'}
    assert rows == [{'sb_0':1,'sb_1':None if empty_text else 'Saúde','sb_2':'certo'}]
    assert result['empty_to_null_confirmed'] == ({'Descrição Médica':1} if empty_text else {})
    assert '"Código pessoa"' in str(compiled)
