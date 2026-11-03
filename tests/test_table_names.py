"""Naming contracts across plan, DDL and foreign-key references."""
import pytest
import sqlalchemy as sa

from migration import plan, PlanError, target_name
from target_profiles import table_identifier, target_limits
from types import SimpleNamespace
from unittest.mock import MagicMock


@pytest.mark.parametrize('kind,limit', [('mysql',64), ('postgresql',63), ('oracle',30)])
def test_preserves_names_up_to_limit_and_shortens_only_when_needed(kind, limit):
    for name in ('Clientes', 'select', 'Ação', 'nome.com.ponto', 'A'*limit):
        assert table_identifier(name, kind) == name
    long_name = 'Tabela_' + 'á'*70
    short = table_identifier(long_name, kind)
    assert (len(short) if kind == 'mysql' else len(short.encode('utf-8'))) <= limit
    assert short == table_identifier(long_name, kind)
    assert short != table_identifier(long_name+'X', kind)
    assert short.startswith('Tabela_')


def test_mysql_character_limit_does_not_abbreviate_valid_multibyte_name():
    assert target_name('dbo', 'á'*64) == 'á'*64
    assert target_name('outro', 'Clientes') == 'Clientes'


@pytest.mark.parametrize('limit', [30, 128])
def test_oracle_uses_limit_detected_by_initialized_driver(limit):
    conn = MagicMock()
    conn.execute.return_value.all.return_value = [('NLS_CHARACTERSET', 'AL32UTF8')]
    conn.execute.return_value.scalar_one.return_value = 8192
    engine = SimpleNamespace(dialect=SimpleNamespace(name='oracle', max_identifier_length=limit,
                             server_version_info=(19, 0)), connect=MagicMock())
    engine.connect.return_value.__enter__.return_value = conn
    limits, _ = target_limits(engine, 'oracle')
    assert limits['identifier_bytes'] == limit
    name = 'TabelaComNomeMaiorQueTrintaCaracteres'
    assert (table_identifier(name, 'oracle', limits['identifier_bytes']) == name) == (limit == 128)


@pytest.mark.parametrize('kind', ['mysql', 'postgresql', 'oracle'])
def test_plan_uses_original_case_and_new_names_in_indexes_and_fk(monkeypatch, kind):
    import migration
    source = sa.create_engine('sqlite://')
    metadata = sa.MetaData(schema='main')
    parent_name = 'Clientes_' + 'MuitoLongo'*10
    parent = sa.Table(parent_name, metadata, sa.Column('id', sa.Integer, primary_key=True))
    child = sa.Table('Pedidos', metadata, sa.Column('id', sa.Integer, primary_key=True),
                     sa.Column('cliente', sa.Integer),
                     sa.ForeignKeyConstraint(['cliente'], [parent.c.id], name='fk_cliente'))
    sa.Index('ix_cliente', child.c.cliente)
    metadata.create_all(source)
    monkeypatch.setattr(migration, 'inventory', lambda _: dict(tables=[
        dict(id='main.'+t.name, schema='main', name=t.name) for t in (parent, child)], objects=[]))
    result = plan(source, ['main.'+parent_name, 'main.Pedidos'], target_kind=kind)
    assert not result['errors']
    dst_parent, dst_child = [dst for _, dst in result['pairs']]
    assert dst_parent.name == table_identifier(parent_name, kind)
    assert dst_child.name == 'Pedidos'
    fk = next(iter(dst_child.foreign_key_constraints))
    assert next(iter(fk.elements)).column.table is dst_parent
    assert dst_parent.name in result['ddl'] and 'main__' not in result['ddl']
    assert any('abreviado' in w for w in result['warnings'])
    assert result['compatibility']['table_names'][1] == dict(
        source='main.Pedidos', target='Pedidos', target_schema='public' if kind=='postgresql' else None,
        abbreviated=False)


@pytest.mark.parametrize('kind', ['mysql', 'postgresql', 'oracle'])
@pytest.mark.parametrize('second', ['Clientes', 'clientes'])
def test_schema_removal_collision_is_reported_before_reflection(monkeypatch, kind, second):
    import migration
    monkeypatch.setattr(migration, 'inventory', lambda _: dict(tables=[
        dict(id='dbo.Clientes', schema='dbo', name='Clientes'),
        dict(id='rh.'+second, schema='rh', name=second)], objects=[]))
    def unexpected(*args, **kwargs):
        pytest.fail('Names must be checked before any table reflection')
    monkeypatch.setattr(migration, 'reflect_source_table', unexpected)
    with pytest.raises(PlanError) as caught:
        plan(sa.create_engine('sqlite://'), ['dbo.Clientes', 'rh.'+second], target_kind=kind)
    assert caught.value.stage == 'Definir nomes das tabelas de destino'
    assert 'dbo.Clientes' in str(caught.value.cause) and 'rh.'+second in str(caught.value.cause)
    assert 'Colisão' in str(caught.value.cause)
