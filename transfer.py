"""Bounded batch copy, cooperative control and per-table timing metrics."""
import threading
import time
from datetime import datetime, timezone, time as time_type, timedelta
import sqlalchemy as sa
from pymysql.cursors import Cursor


class WarningTrackingCursor(Cursor):
    """Retain warnings across PyMySQL's internal executemany packet splits."""
    def _do_get_result(self):
        super()._do_get_result()
        if getattr(self, '_tracking', False):
            self._batch_warnings += self.warning_count
        else:
            self._get_db()._sqlbridge_warning_count = self.warning_count

    def executemany(self, query, args):
        self._tracking, self._batch_warnings = True, 0
        try:
            return super().executemany(query, args)
        finally:
            self._get_db()._sqlbridge_warning_count = self._batch_warnings
            self._tracking = False


def now(): return datetime.now(timezone.utc).isoformat()


class MigrationCancelled(Exception):
    pass


class TransferControl:
    def __init__(self, batch):
        self.condition = threading.Condition()
        self.paused = False
        self.cancelled = False
        self.batch = batch

    def request(self, action, batch=None):
        with self.condition:
            if action == 'pause': self.paused = True
            elif action == 'resume': self.paused = False
            elif action == 'cancel': self.cancelled = True; self.paused = False
            if batch is not None: self.batch = batch
            self.condition.notify_all()

    def checkpoint(self, job):
        with self.condition:
            if self.cancelled: raise MigrationCancelled('Cancelamento solicitado.')
            if self.paused:
                began = time.perf_counter()
                job['status'] = 'paused'
                try:
                    while self.paused and not self.cancelled:
                        self.condition.wait(timeout=1)
                finally:
                    job['paused_seconds'] = job.get('paused_seconds', 0) + time.perf_counter() - began
                if self.cancelled: raise MigrationCancelled('Cancelamento solicitado.')
                job['status'] = 'running'


def activity(job, text):
    job.update(activity=text, activity_started_at=now())


def source_estimate(source, table):
    if source.dialect.name != 'mssql': return None
    # sys.partitions is a metadata estimate; no COUNT(*) or dirty-read hint.
    with source.connect() as conn:
        value = conn.execute(sa.text('''SELECT SUM(CONVERT(bigint,p.rows))
            FROM sys.partitions p JOIN sys.tables t ON p.object_id=t.object_id
            JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name=:schema AND t.name=:table AND p.index_id IN (0,1)'''),
            {'schema': table.schema, 'table': table.name}).scalar()
    return int(value) if value is not None else None


def row_bytes(row):
    # Soft bound for application batches. Driver escaping/packet sizes may differ.
    return sum(len(v.encode('utf-8')) if isinstance(v, str) else len(v) if isinstance(v, (bytes, bytearray, memoryview)) else 24 for v in row) + 16 * len(row)


def payload_chunks(rows, names, uuid_columns, max_bytes, destination=None, column_types=None):
    payload, size = [], 0
    for row in rows:
        cost = row_bytes(row)
        if payload and size + cost > max_bytes:
            yield payload, size
            payload, size = [], 0
        entry = dict(zip(names, row))
        for name in uuid_columns:
            if entry[name] is not None: entry[name] = str(entry[name])
        if destination in ('postgresql','oracle'):
            for name,value in entry.items():
                if value is None: continue
                if destination=='postgresql' and isinstance(value,str) and '\x00' in value:
                    raise ValueError(f'{name}: PostgreSQL não aceita caractere NUL em texto. Transformação manual necessária.')
                if destination=='oracle' and (isinstance(value,str) and value=='' or isinstance(value,(bytes,bytearray,memoryview)) and len(value)==0):
                    raise ValueError(f'{name}: valor vazio não pode ser preservado como distinto de NULL no perfil Oracle; transformação manual necessária.')
                typ=(column_types or {}).get(name)
                if getattr(typ,'timezone',False):
                    if isinstance(value,str):
                        try: value=datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
                        except ValueError: raise ValueError(f'{name}: data/hora com fuso deve ter formato ISO com offset; transformação manual necessária.') from None
                    if not isinstance(value,(datetime,time_type)) or value.tzinfo is None or value.utcoffset() is None:
                        raise ValueError(f'{name}: destino com fuso exige offset explícito; não será assumido o fuso da máquina.')
                    entry[name]=value
                if destination=='oracle' and isinstance(value,time_type) and type(typ).__name__.upper()=='INTERVAL':
                    if value.tzinfo is not None: raise ValueError(f'{name}: hora com fuso exige política explícita para INTERVAL Oracle.')
                    entry[name]=timedelta(hours=value.hour,minutes=value.minute,seconds=value.second,microseconds=value.microsecond)
                elif destination=='oracle' and isinstance(value,bool) and isinstance(typ,sa.Numeric):entry[name]=int(value)
                elif destination=='postgresql' and isinstance(typ,sa.Boolean) and isinstance(value,int):
                    if value not in (0,1):raise ValueError(f'{name}: BOOLEAN exige 0/1 ou bool; conversão de valores necessária.')
                    entry[name]=bool(value)
        payload.append(entry); size += cost
    if payload: yield payload, size


def copy_table(source, target, src, dst, job, batch, options, control):
    began = time.perf_counter()
    paused_at_start = job.get('paused_seconds', 0)
    metrics = dict(source=src.fullname, target=dst.name, status='running', estimated_rows=None,
                   sent_rows=0, confirmed_rows=0, pending_rows=0, batches=0,
                   read_seconds=0., prepare_seconds=0., write_seconds=0., commit_seconds=0.,
                   validation_seconds=0., elapsed_seconds=0., rows_per_second=0., eta_seconds=None,
                   last_batch_rows=0, last_batch_bytes=0, last_progress_at=now(),
                   target_count=None, validation='pending', estimate_note=None,
                   effective_max_batch_bytes=options.get('max_batch_mb', 8)*1024*1024, unconfirmed_rows=0)
    job.setdefault('table_progress', []).append(metrics)
    names = list(src.c.keys())
    uuid_columns = [c.name for c in src.columns if type(c.type).__name__.upper() in ('UNIQUEIDENTIFIER', 'UUID')]
    destination=target.dialect.name
    target_label={'mysql':'MySQL','postgresql':'PostgreSQL','oracle':'Oracle'}.get(destination,'destino')
    column_types={c.name:c.type for c in dst.columns}
    stmt = dst.insert()  # Compile/cache the statement across batches.
    max_bytes = options.get('max_batch_mb', 8) * 1024 * 1024
    per_batch = options.get('transaction_mode', 'table') == 'batch'
    verify_count = options.get('verify_count', True)
    copied, pending = 0, 0
    average_bytes = None

    def refresh():
        elapsed = max(0., time.perf_counter() - began - (job.get('paused_seconds', 0) - paused_at_start))
        speed = copied / elapsed if elapsed else 0.
        total = metrics['estimated_rows']
        metrics.update(sent_rows=copied, pending_rows=pending, elapsed_seconds=elapsed,
                       rows_per_second=speed, eta_seconds=max(0., (total-copied)/speed) if total is not None and speed else None)
        job['rows_in_flight'] = pending

    def commit(out):
        nonlocal pending
        control.checkpoint(job)
        activity(job, 'Aguardando COMMIT no '+target_label)
        stamp = time.perf_counter(); out.commit()
        metrics['commit_seconds'] += time.perf_counter() - stamp
        metrics['confirmed_rows'] += pending
        job['rows'] += pending
        pending = 0
        refresh()

    try:
        control.checkpoint(job)
        activity(job, 'Consultar estimativa de linhas no catálogo')
        try: metrics['estimated_rows'] = source_estimate(source, src)
        except Exception: metrics['estimate_note'] = 'Estimativa indisponível; a cópia continua sem percentual/ETA.'
        control.checkpoint(job)
        with source.connect().execution_options(stream_results=True) as inc, target.connect() as out:
            if target.dialect.name == 'mysql':
                config = out.exec_driver_sql('SELECT @@SESSION.sql_mode AS mode, @@max_allowed_packet AS packet').mappings().one()
                mode = config['mode'] or ''
                max_bytes = min(max_bytes, max(1, int(config['packet']) // 4))
                metrics['effective_max_batch_bytes'] = max_bytes
                modes = set(mode.split(',')) - {''}; modes.add('STRICT_ALL_TABLES')
                out.exec_driver_sql('SET SESSION sql_mode = %s', (','.join(sorted(modes)),))
                out.commit()
            control.checkpoint(job)
            activity(job, 'Aguardando SELECT da origem')
            stamp = time.perf_counter(); result = inc.execute(sa.select(src))
            metrics['read_seconds'] += time.perf_counter() - stamp
            try:
                while True:
                    control.checkpoint(job)
                    # Sample one row first to avoid a large initial fetch of unknown LOBs.
                    fetch_rows = min(control.batch, max(1, int(max_bytes / average_bytes))) if average_bytes else 1
                    activity(job, f'Lendo lote da origem (até {fetch_rows} linhas)')
                    stamp = time.perf_counter(); rows = result.fetchmany(fetch_rows)
                    metrics['read_seconds'] += time.perf_counter() - stamp
                    if not rows: break
                    activity(job, 'Preparando lote em memória')
                    chunks = iter(payload_chunks(rows, names, uuid_columns, max_bytes, destination, column_types))
                    while True:
                        stamp = time.perf_counter()
                        try: payload, size = next(chunks)
                        except StopIteration: break
                        metrics['prepare_seconds'] += time.perf_counter() - stamp
                        control.checkpoint(job)
                        activity(job, f'Gravando lote em {target_label} ({len(payload)} linhas)')
                        stamp = time.perf_counter(); out.execute(stmt, payload)
                        # PyMySQL exposes the warning count from the OK packet. Avoid
                        # a SHOW WARNINGS network round trip on every clean INSERT.
                        if target.dialect.name == 'mysql':
                            cursor_result = getattr(out.connection.driver_connection, '_result', None)
                            warning_count = getattr(out.connection.driver_connection, '_sqlbridge_warning_count', getattr(cursor_result, 'warning_count', None))
                            if warning_count is None or warning_count:
                                warnings = out.exec_driver_sql('SHOW WARNINGS').fetchall()
                                if warnings: raise RuntimeError(int(warnings[0][1]), 'Aviso no INSERT; lote pendente revertido para evitar conversão silenciosa.')
                                if warning_count: raise RuntimeError('Avisos em sublotes do INSERT; mensagem anterior indisponível. Transação pendente revertida para evitar conversão silenciosa.')
                        metrics['write_seconds'] += time.perf_counter() - stamp
                        copied += len(payload); pending += len(payload)
                        metrics.update(batches=metrics['batches']+1, last_batch_rows=len(payload), last_batch_bytes=size, last_progress_at=now())
                        sample = size / len(payload)
                        average_bytes = sample if average_bytes is None else .5 * average_bytes + .5 * sample
                        refresh()
                        if per_batch: commit(out)
                        control.checkpoint(job)
                control.checkpoint(job)
                actual = None
                if verify_count:
                    activity(job, 'Validando COUNT(*) no destino (pode ser demorado)')
                    stamp = time.perf_counter(); actual = out.scalar(sa.select(sa.func.count()).select_from(dst))
                    metrics['validation_seconds'] = time.perf_counter() - stamp
                    if actual != copied: raise RuntimeError('Contagem de destino divergente; inspecione as linhas já confirmadas.')
                commit(out)
                metrics.update(status='completed', target_count=actual, validation='count' if verify_count else 'commits_only', last_progress_at=now())
            finally:
                result.close()
    except Exception as exc:
        metrics.update(status='cancelled' if isinstance(exc, MigrationCancelled) else 'failed', unconfirmed_rows=pending)
        # Connection context rolls back the pending transaction. Earlier batch
        # commits remain and must never be reported as a complete table.
        raise
    finally:
        pending = 0
        refresh()
    return dict(source=src.fullname, target=dst.name, copied=copied, target_count=actual,
                confirmed_rows=metrics['confirmed_rows'], validation=metrics['validation'])
