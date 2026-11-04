"""Explicit plan policy for the empty values reported by local migrations."""
from types import SimpleNamespace
import pytest
import sqlalchemy as sa

from migration import plan, PlanError
from target_profiles import dialect_for
from transfer import copy_table, TransferControl, OracleEmptyNotNullError
from diagnostics import diagnostic


@pytest.mark.parametrize('table,column', [
    ('Afast_Medico_2017','SEI'),
    ('IPERON_ATUARIAL_2018_2019_Aposentados','DtNascConjuge'),
    ('PSMDFichaInscricao','NmEquipe'),
])
def test_bulk_policy_updates_ddl_and_converts_reported_columns_without_touching_source(monkeypatch,table,column):
    import migration
    source = sa.create_engine('sqlite://')
    src = sa.Table(table, sa.MetaData(schema='main'),
        sa.Column('id',sa.Integer,primary_key=True),
        sa.Column(column,sa.String(80),nullable=False),
        sa.Column('Quantidade',sa.Integer,nullable=False),
        sa.Column('Dia',sa.Date,nullable=False))
    src.create(source)
    from datetime import date
    with source.begin() as conn:conn.execute(src.insert(),dict(id=1,**{column:''},Quantidade=5,Dia=date(2026,1,1)))
    monkeypatch.setattr(migration,'inventory',lambda _: dict(tables=[dict(id='main.'+table,schema='main',name=table)],objects=[]))
    strict = plan(source,['main.'+table],target_kind='oracle')
    assert strict['pairs'][0][1].c[column].nullable is False
    ready = plan(source,['main.'+table],target_kind='oracle',oracle_nullable_text=True,column_name_case='upper')
    assert not ready['errors']
    reflected,dst = ready['pairs'][0]
    assert dst.c[column].nullable is True
    assert all(dst.c[c].nullable is False for c in ('id','Quantidade','Dia'))
    report = ready['compatibility']
    assert report['nullable_overrides'] == {}  # Turning bulk off doesn't leave manual changes.
    assert report['nullability_changes'] == [dict(table='main.'+table,source_column=column,
        target_column=column.upper(),before='NOT NULL',after='NULL',reason='oracle_nullable_text')]
    assert reflected.c[column].nullable is False
    statements=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,stmt,rows):statements.extend(rows)
        def commit(self):pass
    # Real source reader and value/commit path with an Oracle driver stand-in;
    # no claim of testing against a live Oracle instance.
    target=SimpleNamespace(dialect=dialect_for('oracle'),connect=lambda:Conn())
    result=copy_table(source,target,reflected,dst,{'rows':0},2,
        dict(verify_count=False,oracle_empty_text='null'),TransferControl(2))
    assert statements[0]['sb_1'] is None
    assert result['empty_to_null_confirmed'] == {column:1}
    with source.connect() as conn:assert conn.scalar(sa.select(src.c[column])) == ''
    assert sa.inspect(source).get_columns(table,schema='main')[1]['nullable'] is False


def test_bulk_policy_keeps_text_primary_keys_required_and_rejects_wrong_targets(monkeypatch):
    import migration
    source=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('SEI',sa.String(40),primary_key=True)).metadata.create_all(source)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    result=plan(source,['main.t'],target_kind='oracle',oracle_nullable_text=True)
    assert result['pairs'][0][1].c.SEI.nullable is False
    assert not result['compatibility']['nullability_changes']
    for kind in ('mysql','postgresql'):
        with pytest.raises(PlanError):plan(source,['main.t'],target_kind=kind,oracle_nullable_text=True)
    for invalid in ('true',1,None):
        with pytest.raises(PlanError):plan(source,['main.t'],target_kind='oracle',oracle_nullable_text=invalid)


def test_empty_not_null_diagnostic_has_specific_code_and_preserves_safe_guidance():
    detail=diagnostic(OracleEmptyNotNullError('SEI: conversão bloqueada; coluna NOT NULL/PK.'),
                      'Copiar dados de dbo.Afast_Medico_2017',hide_values=True)
    assert detail['code']=='ORACLE_EMPTY_NOT_NULL'
    assert 'coluna obrigatória' in detail['category']
    assert 'SEI' in detail['message'] and 'Preparar textos vazios' in detail['hint']
    assert 'pular existente' in detail['hint']
