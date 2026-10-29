import threading
import time
import pytest
import sqlalchemy as sa
from migration import execute
from transfer import TransferControl, MigrationCancelled, payload_chunks, WarningTrackingCursor, source_estimate


def fixture_pair(rows=20):
    source=sa.create_engine('sqlite://')
    target=sa.create_engine('sqlite://')
    src=sa.Table('data',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),sa.Column('text',sa.String(1000)))
    dst=sa.Table('dbo__data',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),sa.Column('text',sa.String(1000)))
    src.create(source)
    with source.begin() as conn:conn.execute(src.insert(),[dict(id=i,text='ação '*10) for i in range(rows)])
    return source,target,src,dst


def test_chunking_is_bounded_and_never_drops_a_large_row():
    rows=[('a'*30,),('b'*30,),('x'*1000,),('c'*30,)]
    chunks=list(payload_chunks(rows,['text'],[],100))
    assert sum(len(payload) for payload,size in chunks)==4
    assert all(size<=100 or len(payload)==1 for payload,size in chunks)
    assert chunks[1][0][0]['text']=='x'*1000


def test_uuid_conversion_does_not_scan_column_types_per_record():
    import uuid
    value=uuid.uuid4()
    payload,size=next(payload_chunks([(value,'text')],['id','text'],['id'],4096))
    assert payload==[{'id':str(value),'text':'text'}]


@pytest.mark.parametrize('mode',['table','batch'])
def test_copy_reports_timings_counts_and_batches(mode):
    source,target,src,dst=fixture_pair()
    job={};execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,4,options=dict(transaction_mode=mode,verify_count=True,max_batch_mb=1))
    assert job['status']=='completed' and job['rows']==20
    metrics=job['table_progress'][0]
    assert metrics['confirmed_rows']==20 and metrics['pending_rows']==0
    assert metrics['batches']>1 and metrics['target_count']==20
    assert metrics['rows_per_second']>0 and metrics['elapsed_seconds']>0
    assert metrics['read_seconds']>=0 and metrics['commit_seconds']>=0
    assert metrics['estimated_rows'] is None and metrics['eta_seconds'] is None


class CancelAfterTwoBatches(TransferControl):
    def checkpoint(self,job):
        if job.get('table_progress') and job['table_progress'][-1]['batches']>=2:
            self.request('cancel')
        super().checkpoint(job)


@pytest.mark.parametrize('mode,expected',[('table',0),('batch',1)])
def test_cancellation_rolls_back_pending_and_preserves_only_committed_batches(mode,expected):
    source,target,src,dst=fixture_pair()
    job={};execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,3,
                   options=dict(transaction_mode=mode),control=CancelAfterTwoBatches(3))
    assert job['status']=='cancelled' and job['rows']==expected
    assert job['table_progress'][0]['status']=='cancelled'
    assert not any(e['status']=='copied' for e in job['events'])
    with target.connect() as conn: assert conn.scalar(sa.select(sa.func.count()).select_from(dst))==expected


def test_pause_resume_and_cancel_are_cooperative_without_database_calls():
    control=TransferControl(1000);job=dict(status='running')
    control.request('pause')
    reached=threading.Event()
    def worker():control.checkpoint(job);reached.set()
    thread=threading.Thread(target=worker);thread.start()
    deadline=time.monotonic()+2
    while job['status']!='paused' and time.monotonic()<deadline:threading.Event().wait(.005)
    assert job['status']=='paused' and not reached.is_set()
    control.request('resume');thread.join(timeout=2)
    assert reached.is_set() and job['status']=='running' and job['paused_seconds']>=0
    control.request('cancel')
    with pytest.raises(MigrationCancelled):control.checkpoint(job)


def test_cancel_wakes_paused_worker():
    control=TransferControl(10);control.request('pause');job={'status':'running'};cancelled=threading.Event()
    def worker():
        try:control.checkpoint(job)
        except MigrationCancelled:cancelled.set()
    thread=threading.Thread(target=worker);thread.start()
    control.request('cancel');thread.join(timeout=2)
    assert cancelled.is_set()


def test_count_can_be_disabled_without_reporting_an_exact_count():
    source,target,src,dst=fixture_pair()
    calls=[]
    sa.event.listen(target,'before_cursor_execute',lambda conn,cursor,statement,params,context,many:calls.append(statement))
    job={};execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,5,options=dict(verify_count=False))
    assert job['status']=='completed'
    assert job['tables'][0]['target_count'] is None and job['tables'][0]['validation']=='commits_only'
    assert not any('count(' in stmt.lower() for stmt in calls)


def test_batch_commit_mode_reports_partial_rows_on_insert_error():
    source,target,src,dst=fixture_pair(5)
    with source.begin() as conn:conn.execute(src.update().where(src.c.id==3).values(text='same'));conn.execute(src.update().where(src.c.id==4).values(text='same'))
    # IDs 0..2 need unique texts to avoid an earlier failure.
    with source.begin() as conn:
        for i in range(3):conn.execute(src.update().where(src.c.id==i).values(text=str(i)))
    dst.append_constraint(sa.UniqueConstraint('text'))
    job={};execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,2,continue_on_error=True,options=dict(transaction_mode='batch'))
    assert job['status']=='completed_with_errors' and job['rows']==3
    assert job['table_progress'][0]['status']=='failed'
    with target.connect() as conn:assert conn.scalar(sa.select(sa.func.count()).select_from(dst))==3


def test_live_tuning_changes_next_fetch_size():
    source,target,src,dst=fixture_pair(11)
    class Tune(TransferControl):
        def checkpoint(self,job):
            if job.get('table_progress') and job['table_progress'][-1]['batches']==1:self.request('tune',batch=2)
            super().checkpoint(job)
    job={};execute(source,target,dict(pairs=[(src,dst)],errors=[]),job,10,control=Tune(10))
    assert job['status']=='completed' and job['table_progress'][0]['batches']==6


def test_warning_tracking_keeps_warning_from_earlier_driver_packet(monkeypatch):
    from pymysql.cursors import Cursor
    from types import SimpleNamespace
    conn=SimpleNamespace()
    cursor=WarningTrackingCursor(conn)
    def get_result(self):self.warning_count=conn.warning
    def executemany(self,query,args):
        for warning in (1,0,0):conn.warning=warning;self._do_get_result()
        return 3
    monkeypatch.setattr(Cursor,'_do_get_result',get_result)
    monkeypatch.setattr(Cursor,'executemany',executemany)
    assert cursor.executemany('INSERT',[])==3
    assert conn._sqlbridge_warning_count==1


def test_source_estimate_uses_metadata_only_and_parameters():
    from types import SimpleNamespace
    calls=[]
    class Result:
        def scalar(self):return 10000000
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params):calls.append((str(sql),params));return Result()
    source=SimpleNamespace(dialect=SimpleNamespace(name='mssql'),connect=lambda:Connection())
    table=sa.Table('orders',sa.MetaData(schema='dbo'),sa.Column('id',sa.Integer))
    assert source_estimate(source,table)==10000000
    assert 'sys.partitions' in calls[0][0] and 'COUNT(' not in calls[0][0]
    assert calls[0][1]==dict(schema='dbo',table='orders')
