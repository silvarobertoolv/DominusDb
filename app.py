import io
import json
from datetime import datetime, timezone
import secrets
import threading
import zipfile
from functools import wraps
from flask import Flask, request, session, jsonify, render_template, send_file
import sqlalchemy as sa
from connections import engine_for
from target_profiles import destination_kind, destination_schema, dialect_for, KINDS
from migration import inventory, plan, execute, PlanError, destination_objects
from diagnostics import diagnostic
from type_mapping import TYPE_EXAMPLES_BY_KIND
from transfer import TransferControl, WarningTrackingCursor

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', TRUSTED_HOSTS=['127.0.0.1', 'localhost', '[::1]'])
state = {'source': None, 'target': None, 'plan': None, 'job': None, 'control': None}
lock = threading.RLock()


def error(message, code=400): return jsonify(error=message), code


@app.before_request
def local_guard():
    if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('csrf', 'missing')):
        return error('Sessão inválida. Recarregue a página.', 403)


def idle(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with lock:
            if state['job'] and state['job']['status'] in ('queued', 'running', 'pause_requested', 'paused', 'cancelling'): return error('Aguarde a migração em andamento.', 409)
            return fn(*args, **kwargs)
    return wrapped


@app.get('/')
def home():
    session.setdefault('csrf', secrets.token_urlsafe(32))
    return render_template('index.html', csrf=session['csrf'])


@app.post('/api/connect/<kind>')
@idle
def connect(kind):
    if kind not in ('source', 'target'): return error('Conexão inválida.')
    engine = None
    try:
        engine = engine_for(request.get_json() or {}, kind)
        with engine.connect() as conn:
            conn.execute(sa.text('SELECT 1 FROM DUAL' if engine.dialect.name == 'oracle' else 'SELECT 1'))
        if state[kind]: state[kind].dispose()
        state[kind] = engine
        state['plan'] = None
        return jsonify(ok=True, target_kind=engine.dialect.name, target_schema=getattr(engine,'_sqlbridge_schema',None))
    except Exception as exc:
        if engine: engine.dispose()
        return request_diagnostic(exc, 'Conectar ' + kind, engines=(engine,))


@app.post('/api/inventory')
@idle
def scan():
    if state['source'] is None: return error('Conecte a origem primeiro.')
    try: return jsonify(inventory(state['source']))
    except Exception as exc: return request_diagnostic(exc, 'Ler catálogo de origem', engines=(state['source'],))



def request_diagnostic(exc, stage, object_name=None, engines=()):
    detail = diagnostic(exc, stage, object_name, engines)
    text = (f"Falha [{detail['reference']}]. Etapa: {stage}. Objeto: {object_name or 'catálogo'}. "
            f"{detail['exception']} / {detail['driver_exception']}: {detail['message']} "
            f"Orientação: {detail['hint']}")
    app.logger.error('%s', text)
    return jsonify(error=text, diagnostic=detail), 400


def plan_diagnostic(exc):
    return request_diagnostic(exc.cause, exc.stage, exc.object_name, (state['source'], state['target']))


@app.post('/api/plan')
@idle
def make_plan():
    state['plan'] = None
    if state['source'] is None: return error('Conecte a origem primeiro.')
    try:
        selected = (request.get_json() or {}).get('tables')
        if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected): return error('Seleção inválida.')
        prepared = plan(state['source'], selected, state['target'], (request.get_json() or {}).get('overrides'), (request.get_json() or {}).get('target_kind'))
        prepared['id'] = secrets.token_urlsafe(24)
        prepared['destination_check'] = []
        if state['target'] is not None:
            try:
                existing = destination_objects(state['target'])
                prepared['destination_check'] = [{'table': d.name, 'exists': d.name.casefold() in existing} for _, d in prepared['pairs']]
            except Exception as exc:
                detail = diagnostic(exc, 'Inspecionar destino', engines=(state['target'],))
                prepared['warnings'].append(f"Não foi possível inspecionar o destino: {detail['message']}. A execução fará nova verificação.")
        response = jsonify(target_kind=prepared.get('target_kind','mysql'), target_schema=prepared.get('target_schema'), type_examples=TYPE_EXAMPLES_BY_KIND[prepared.get('target_kind','mysql')], column_mappings=prepared.get('column_mappings', []), manual_mappings=prepared.get('manual_mappings', {}), compatibility=prepared.get('compatibility'), destination_check=prepared['destination_check'], id=prepared['id'], ddl=prepared['ddl'], warnings=prepared['warnings'], errors=prepared['errors'], objects=prepared['objects'], tables=[{'source': s.fullname, 'target': d.name, 'columns': [{'name': c.name, 'type': str(c.type.compile(dialect=dialect_for(prepared.get('target_kind','mysql'))))} for c in d.columns]} for s,d in prepared['pairs']])
        state['plan'] = prepared
        return response
    except PlanError as exc: return plan_diagnostic(exc)
    except Exception as exc: return plan_diagnostic(PlanError('Preparar resposta do plano', None, exc))


@app.post('/api/run')
@idle
def run():
    data = request.get_json() or {}
    prepared = state['plan']
    if not prepared or data.get('plan_id') != prepared['id']: return error('Gere e revise um plano atualizado.')
    if prepared['errors']: return error('O plano contém bloqueios de conversão.')
    if state['target'] is None: return error('Conecte o destino primeiro.')
    if data.get('confirmation') != 'MIGRAR': return error('Digite MIGRAR para confirmar a criação dos objetos no destino.')
    if isinstance(data.get('batch'), bool): return error('Lote inválido.')
    try: batch = int(data.get('batch', 1000))
    except (ValueError, TypeError): return error('Lote inválido.')
    if not 1 <= batch <= 50000: return error('O lote deve ter entre 1 e 50000 linhas.')
    existing_policy = data.get('existing_policy', 'abort')
    continue_on_error = data.get('continue_on_error', True)
    if existing_policy not in ('abort', 'skip'): return error('Política de objetos existentes inválida.')
    if not isinstance(continue_on_error, bool): return error('Opção de continuar após erros deve ser booleana.')
    transaction_mode = data.get('transaction_mode', 'table')
    verify_count = data.get('verify_count', True)
    max_batch_mb = data.get('max_batch_mb', 8)
    if transaction_mode not in ('table', 'batch'): return error('Modo de transação inválido.')
    if not isinstance(verify_count, bool): return error('Opção de contagem deve ser booleana.')
    if isinstance(max_batch_mb, bool) or not isinstance(max_batch_mb, int) or not 1 <= max_batch_mb <= 64: return error('Limite do lote deve ser entre 1 e 64 MiB.')
    options = dict(transaction_mode=transaction_mode, verify_count=verify_count, max_batch_mb=max_batch_mb)
    control = TransferControl(batch)
    job = {'id': secrets.token_hex(12), 'control_history': [], 'target_kind': prepared.get('target_kind','mysql'), 'target_schema': prepared.get('target_schema'), 'plan_id': prepared['id'], 'plan_warnings': prepared.get('warnings', []), 'compatibility': prepared.get('compatibility'), 'manual_mappings': prepared.get('manual_mappings', {}), 'status': 'queued', 'rows': 0, 'logs': [], 'tables': [], 'stage': 'Preparando'}
    state['job'] = job
    state['control'] = control
    threading.Thread(target=execute, args=(state['source'], state['target'], prepared, job, batch, existing_policy, continue_on_error, options, control), daemon=True).start()
    return jsonify(ok=True)


@app.get('/api/job')
def job_status():
    job = state['job']
    if not job: return jsonify(status='idle')
    try: after = int(request.args.get('after', 0))
    except ValueError: return error('Cursor inválido.')
    if after < 0: return error('Cursor inválido.')
    # Keep large schema dictionaries and repeated SHOW CREATE responses out of polling.
    fields = ('target_kind', 'target_schema', 'id', 'status', 'stage', 'activity', 'activity_started_at', 'rows', 'rows_in_flight',
              'table_progress', 'started_at', 'finished_at', 'summary', 'error', 'diagnostic', 'options')
    result = {k: job[k] for k in fields if k in job}
    events = list(job.get('events', []))
    if 'table_progress' in result: result['table_progress'] = [dict(item) for item in list(result['table_progress'])]
    result.update(events=events[after:after+200], next_cursor=min(len(events), after+200),
                  events_total=len(events), logs=job.get('logs', [])[-30:])
    now = datetime.now(timezone.utc)
    for key, field in (('elapsed_seconds', 'started_at'), ('activity_seconds', 'activity_started_at')):
        stamp = job.get(field)
        if stamp:
            end = datetime.fromisoformat(job.get('finished_at') or now.isoformat())
            result[key] = max(0., (end - datetime.fromisoformat(stamp)).total_seconds())
    return jsonify(result)


@app.post('/api/job/control')
def job_control():
    with lock:
        data = request.get_json() or {}
        job, control = state['job'], state['control']
        if not job or not control: return error('Nenhuma migração ativa.', 409)
        with control.condition:
            active = ('queued', 'running', 'pause_requested', 'paused', 'cancelling')
            if job['status'] not in active: return error('Nenhuma migração ativa.', 409)
            if data.get('job_id') != job.get('id'): return error('Execução desatualizada.', 409)
            action = data.get('action')
            if action not in ('pause', 'resume', 'cancel', 'tune'): return error('Ação inválida.')
            batch = data.get('batch')
            if action == 'tune' and (isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= 50000): return error('Lote deve ser entre 1 e 50000 linhas.')
            if action != 'tune': batch = None
            if job['status'] == 'cancelling': return error('Cancelamento já solicitado.', 409)
            if action == 'pause': job['status'] = 'pause_requested'
            elif action == 'resume': job['status'] = 'running'
            elif action == 'cancel': job['status'] = 'cancelling'
            control.request(action, batch)
            job.setdefault('control_history', []).append(dict(action=action, batch=batch, at=datetime.now(timezone.utc).isoformat()))
            if batch is not None: job.setdefault('options', {})['batch'] = batch
            return jsonify(ok=True, message='Solicitação recebida. Operações SQL em andamento terminam antes do próximo ponto de controle.')


@app.get('/api/report')
def report():
    if not session.get('csrf'): return error('Abra a página primeiro.', 403)
    if not state['job']: return error('Nenhuma execução disponível.')
    content = json.dumps(state['job'], ensure_ascii=False, indent=2).encode('utf-8')
    return send_file(io.BytesIO(content), mimetype='application/json', as_attachment=True, download_name='sqlbridge-execucao.json')


@app.get('/api/export')
def export():
    # Downloads require the session created by the local UI.
    if not session.get('csrf'): return error('Abra a página primeiro.', 403)
    with lock:
        prepared = state['plan']
        if not prepared: return error('Gere um plano primeiro.')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(prepared.get('target_kind','mysql')+'-plan.sql', prepared['ddl'])
            archive.writestr('compatibilidade.json', json.dumps(prepared.get('compatibility'), ensure_ascii=False, indent=2))
            archive.writestr('objetos-origem.json', json.dumps(prepared['objects'], ensure_ascii=False, indent=2))
            archive.writestr('revisao.json', json.dumps({'warnings': prepared['warnings'], 'errors': prepared['errors'], 'job': state['job'] if state['job'] and state['job'].get('plan_id') == prepared['id'] else None}, ensure_ascii=False, indent=2))
        buffer.seek(0)
        return send_file(buffer, mimetype='application/zip', as_attachment=True, download_name='sqlbridge-plano.zip')


if __name__ == '__main__':
    print('SQLBridge: http://127.0.0.1:8000 — mantenha este terminal aberto')
    app.run(host='127.0.0.1', port=8000, debug=False, use_reloader=False)
