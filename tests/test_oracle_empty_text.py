import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import oracle
from transfer import payload_chunks
from migration import execute


def batch(value,typ=None,nullable=True,policy='null',destination='oracle'):
    counts={}
    values=list(payload_chunks([(value,)],['memo'],[],4096,destination,{'memo':typ or oracle.NVARCHAR2(100)},policy,{'memo':nullable},counts))
    return values,counts


def test_null_policy_only_changes_empty_text_and_reports_column_count():
    values,counts=batch('')
    assert values[0][0]==[{'memo':None}] and counts=={'memo':1}
    for original in (None,' ','  ','NULL','abc'):
        values,counts=batch(original)
        assert values[0][0]==[{'memo':original}] and counts=={}


@pytest.mark.parametrize('typ',[oracle.NVARCHAR2(100),oracle.VARCHAR2(100),oracle.CLOB(),oracle.NCLOB()])
def test_supported_text_types_can_map_empty_to_null(typ):
    assert batch('',typ=typ)[0][0][0][0]['memo'] is None


def test_strict_default_remains_and_constraints_are_not_relaxed():
    with pytest.raises(ValueError,match='texto vazio'):batch('',policy='error')
    with pytest.raises(ValueError,match='NOT NULL/PK'):batch('',nullable=False)
    with pytest.raises(ValueError,match='NOT NULL/PK'):batch('',nullable=None)
    with pytest.raises(ValueError,match='não textual'):batch('',typ=oracle.NUMBER(10,0))


def test_binary_values_never_use_the_text_policy_and_other_destinations_keep_empty():
    with pytest.raises(ValueError,match='binário vazio'):batch(b'',typ=oracle.BLOB())
    for dest in ('mysql','postgresql'):
        values,counts=batch('',destination=dest)
        assert values[0][0]==[{'memo':''}] and counts=={}


def copy_fixture(monkeypatch,rows,mode='table',unique=False):
    import transfer
    source=sa.create_engine('sqlite://');target=sa.create_engine('sqlite://')
    src=sa.Table('source',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),sa.Column('memo',sa.String(100)))
    dst=sa.Table('dest',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),sa.Column('memo',sa.String(100)))
    if unique:dst.append_constraint(sa.UniqueConstraint('memo'))
    src.create(source)
    with source.begin() as conn:conn.execute(src.insert(),[dict(id=i,memo=v) for i,v in enumerate(rows)])
    original=transfer.payload_chunks
    # Exercise the real copy/commit path in SQLite, substituting only the
    # Oracle value-adaptation policy; no claim of Oracle integration testing.
    def oracle_payload(rows,names,uuids,size,destination,types,policy,nullable,counts):
        return original(rows,names,uuids,size,'oracle',types,policy,nullable,counts)
    monkeypatch.setattr(transfer,'payload_chunks',oracle_payload)
    job={}
    execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,2,continue_on_error=True,
            options=dict(oracle_empty_text='null',transaction_mode=mode))
    return target,dst,job


def test_copy_commits_nulls_and_exposes_audit_counts(monkeypatch):
    target,dst,job=copy_fixture(monkeypatch,['',' ','',None])
    assert job['status']=='completed_with_warnings' and job['rows']==4
    metric=job['table_progress'][0]
    assert metric['empty_to_null_prepared']==metric['empty_to_null_confirmed']=={'memo':2}
    assert job['tables'][0]['empty_to_null_confirmed']=={'memo':2}
    event=next(e for e in job['events'] if e['kind']=='data')
    assert event['empty_to_null_confirmed']=={'memo':2}
    with target.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(dst).where(dst.c.memo.is_(None)))==3


@pytest.mark.parametrize('mode,confirmed',[('table',{}),('batch',{'memo':2})])
def test_transformation_counts_distinguish_preparation_from_successful_commits(monkeypatch,mode,confirmed):
    target,dst,job=copy_fixture(monkeypatch,['','','same','same'],mode=mode,unique=True)
    assert job['status']=='completed_with_errors'
    assert job['table_progress'][0]['empty_to_null_prepared']=={'memo':2}
    assert job['table_progress'][0]['empty_to_null_confirmed']==confirmed
    assert job['rows']==(0 if mode=='table' else 3)
