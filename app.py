import io
import json
import secrets
import re
import threading
import zipfile
from functools import wraps
from flask import Flask, request, session, jsonify, render_template, send_file
import sqlalchemy as sa
from sqlalchemy.engine import URL
from migration import inventory, plan, execute, PlanError

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', TRUSTED_HOSTS=['127.0.0.1', 'localhost', '[::1]'])
state = {'source': None, 'target': None, 'plan': None, 'job': None}
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
            if state['job'] and state['job']['status'] in ('queued', 'running'): return error('Aguarde a migração em andamento.', 409)
            return fn(*args, **kwargs)
    return wrapped


def engine_for(data, kind):
    port = int(data.get('port') or (1433 if kind == 'source' else 3306))
    if not 1 <= port <= 65535: raise ValueError('Porta inválida.')
    for key in ('host', 'database', 'user'):
        if not isinstance(data.get(key), str) or not data[key].strip(): raise ValueError(f'Preencha {key}.')
    url = URL.create('mssql+pymssql' if kind == 'source' else 'mysql+pymysql', username=data['user'], password=data.get('password', ''), host=data['host'].strip(), port=port, database=data['database'])
    args = {'login_timeout': 10, 'timeout': 60, 'charset': 'UTF-8', 'encryption': 'require' if data.get('tls', True) else 'off', 'tds_version': '7.3', 'use_datetime2': True} if kind == 'source' else {'connect_timeout': 10, 'read_timeout': 60, 'write_timeout': 60, 'charset': 'utf8mb4'}
    if kind == 'target' and data.get('tls'):
        args['ssl'] = {'check_hostname': True}
        args['ssl_verify_cert'] = True
        args['ssl_verify_identity'] = True
    return sa.create_engine(url, connect_args=args, pool_pre_ping=True, hide_parameters=True, echo=False)


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
            conn.execute(sa.text('SELECT 1'))
        if state[kind]: state[kind].dispose()
        state[kind] = engine
        state['plan'] = None
        return jsonify(ok=True)
    except Exception:
        if engine: engine.dispose()
        return error('Não foi possível conectar. Confira endereço, porta, banco, credenciais, TLS e permissão de rede. O banco de destino precisa existir.')


@app.post('/api/inventory')
@idle
def scan():
    if state['source'] is None: return error('Conecte a origem primeiro.')
    try: return jsonify(inventory(state['source']))
    except Exception: return error('Falha na leitura do catálogo. Confira permissões de metadados (VIEW DEFINITION).')



def plan_diagnostic(exc):
    cause = exc.cause
    original = getattr(cause, 'orig', cause)
    # Never stringify a SQLAlchemy DBAPIError: it includes SQL and parameters.
    message = str(original)
    if getattr(original, 'args', None):
        message = ' | '.join(arg.decode('utf-8', errors='replace') if isinstance(arg, bytes) else str(arg) for arg in original.args)
    engine = state['source']
    url = getattr(engine, 'url', None)
    for secret in (getattr(url, 'password', None), getattr(url, 'username', None)):
        if secret:
            message = message.replace(str(secret), '[oculto]')
    message = re.sub(r'(?i)(?:mssql|mysql|https?)://[^\s]+', '[conexão oculta]', message)
    message = re.sub(r'(?i)((?:password|pwd|senha)\s*[=:]\s*)([^;\s]+)', r'\1[oculto]', message)
    message = message.split('[SQL:')[0].split('[parameters:')[0][:3000]
    reference = secrets.token_hex(6)
    detail = {'reference': reference, 'stage': exc.stage, 'object': exc.object_name,
              'exception': type(cause).__name__, 'driver_exception': type(original).__name__,
              'message': message or 'Exceção sem mensagem.'}
    text = (f"Falha ao gerar plano [{reference}]. Etapa: {exc.stage}. "
            f"Objeto: {exc.object_name or 'catálogo'}. "
            f"{type(cause).__name__} / {type(original).__name__}: {detail['message']}")
    app.logger.error('%s', text)
    return jsonify(error=text, diagnostic=detail), 400


@app.post('/api/plan')
@idle
def make_plan():
    state['plan'] = None
    if state['source'] is None: return error('Conecte a origem primeiro.')
    try:
        selected = (request.get_json() or {}).get('tables')
        if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected): return error('Seleção inválida.')
        prepared = plan(state['source'], selected)
        prepared['id'] = secrets.token_urlsafe(24)
        response = jsonify(id=prepared['id'], ddl=prepared['ddl'], warnings=prepared['warnings'], errors=prepared['errors'], objects=prepared['objects'], tables=[{'source': s.fullname, 'target': d.name, 'columns': [{'name': c.name, 'type': str(c.type.compile(dialect=sa.dialects.mysql.dialect()))} for c in d.columns]} for s,d in prepared['pairs']])
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
    try: batch = int(data.get('batch', 1000))
    except (ValueError, TypeError): return error('Lote inválido.')
    if not 1 <= batch <= 10000: return error('O lote deve ter entre 1 e 10000 linhas.')
    job = {'status': 'queued', 'rows': 0, 'logs': [], 'tables': [], 'stage': 'Preparando'}
    state['job'] = job
    threading.Thread(target=execute, args=(state['source'], state['target'], prepared, job, batch), daemon=True).start()
    return jsonify(ok=True)


@app.get('/api/job')
def job_status(): return jsonify(state['job'] or {'status': 'idle'})


@app.get('/api/export')
def export():
    # Downloads require the session created by the local UI.
    if not session.get('csrf'): return error('Abra a página primeiro.', 403)
    with lock:
        prepared = state['plan']
        if not prepared: return error('Gere um plano primeiro.')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('mysql-plan.sql', prepared['ddl'])
            archive.writestr('objetos-origem.json', json.dumps(prepared['objects'], ensure_ascii=False, indent=2))
            archive.writestr('revisao.json', json.dumps({'warnings': prepared['warnings'], 'errors': prepared['errors'], 'job': state['job']}, ensure_ascii=False, indent=2))
        buffer.seek(0)
        return send_file(buffer, mimetype='application/zip', as_attachment=True, download_name='sqlbridge-plano.zip')


if __name__ == '__main__':
    print('SQLBridge: http://127.0.0.1:8000 — mantenha este terminal aberto')
    app.run(host='127.0.0.1', port=8000, debug=False, use_reloader=False)
