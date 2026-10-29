"""Local connection configuration. Credentials stay in Engine URLs in memory."""
import sqlalchemy as sa
from sqlalchemy.engine import URL
from transfer import WarningTrackingCursor
from target_profiles import KINDS


def engine_for(data, kind):
    destination = data.get('db_type','mysql') if kind=='target' else 'mssql'
    if kind=='target' and destination not in KINDS: raise ValueError('Destino inválido.')
    ports={'mssql':1433,'mysql':3306,'postgresql':5432,'oracle':1521}
    port=int(data.get('port') or ports[destination])
    timeout=int(data.get('query_timeout') or 300)
    if not 1<=port<=65535: raise ValueError('Porta inválida.')
    if not 10<=timeout<=3600: raise ValueError('Timeout deve ser entre 10 e 3600 segundos.')
    for key in ('host','database','user'):
        if not isinstance(data.get(key),str) or not data[key].strip():raise ValueError(f'Preencha {key}.')
    drivers={'mssql':'mssql+pymssql','mysql':'mysql+pymysql','postgresql':'postgresql+psycopg','oracle':'oracle+oracledb'}
    query={'service_name':data['database'].strip()} if destination=='oracle' else {}
    url=URL.create(drivers[destination],username=data['user'],password=data.get('password',''),
                   host=data['host'].strip(),port=port,database=None if destination=='oracle' else data['database'],query=query)
    extra={}
    if destination=='mssql':
        args=dict(login_timeout=10,timeout=timeout,charset='UTF-8',encryption='require' if data.get('tls',True) else 'off',tds_version='7.3',use_datetime2=True)
    elif destination=='mysql':
        args=dict(connect_timeout=10,read_timeout=timeout,write_timeout=timeout,charset='utf8mb4',cursorclass=WarningTrackingCursor)
        if data.get('tls'):args.update(ssl={'check_hostname':True},ssl_verify_cert=True,ssl_verify_identity=True)
    elif destination=='postgresql':
        args=dict(connect_timeout=10,sslmode='verify-full' if data.get('tls') else 'prefer',options=f'-c statement_timeout={timeout*1000}')
        if data.get('ssl_ca'):args['sslrootcert']=data['ssl_ca']
        extra['insertmanyvalues_page_size']=1000
    else:
        args=dict(tcp_connect_timeout=10)
        if data.get('tls'):args.update(protocol='tcps',ssl_server_dn_match=True)
        extra['max_identifier_length']=30
    engine=sa.create_engine(url,connect_args=args,pool_pre_ping=True,hide_parameters=True,echo=False,**extra)
    schema=data.get('schema','').strip() if kind=='target' else ''
    engine._sqlbridge_schema=(schema or 'public') if destination=='postgresql' else (schema.upper() or None) if destination=='oracle' else None
    if destination=='oracle':
        engine.dialect.include_set_input_sizes = set(engine.dialect.include_set_input_sizes or ()) | {engine.dialect.dbapi.DB_TYPE_TIMESTAMP_TZ}
        @sa.event.listens_for(engine,'connect')
        def call_timeout(dbapi_connection,record):dbapi_connection.call_timeout=timeout*1000
    return engine
