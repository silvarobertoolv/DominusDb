"""Destination dialects and conservative metadata checks (no data scans)."""
import hashlib
import math
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, oracle
from compatibility import target_limits as mysql_limits, check_and_adapt as mysql_check
from diagnostics import diagnostic

KINDS = {'mysql': 'MySQL', 'postgresql': 'PostgreSQL', 'oracle': 'Oracle'}


class OracleTimestampTZ(sa.types.UserDefinedType):
    # SQLAlchemy's default oracledb TIMESTAMP bind uses DB_TYPE_TIMESTAMP,
    # which can discard tzinfo. Bind explicitly as TIMESTAMP WITH TIME ZONE.
    def get_col_spec(self, **kwargs):
        return 'TIMESTAMP WITH TIME ZONE'

    cache_ok = True
    timezone = True

    def get_dbapi_type(self, dbapi):
        return dbapi.DB_TYPE_TIMESTAMP_TZ


def destination_kind(engine=None, requested=None):
    actual = getattr(getattr(engine, 'dialect', None), 'name', None)
    if requested is not None and requested not in KINDS: raise ValueError('Destino inválido.')
    if requested and actual in KINDS and actual != requested:
        raise ValueError('Conecte o banco de destino selecionado e gere novamente o plano.')
    return requested or (actual if actual in KINDS else 'mysql')


def destination_schema(engine, kind):
    return getattr(engine, '_sqlbridge_schema', 'public' if kind == 'postgresql' else None)


def dialect_for(kind):
    return {'mysql': mysql, 'postgresql': postgresql, 'oracle': oracle}[kind].dialect()


def identifier(schema, name, kind='mysql'):
    raw = f'{schema}__{name}'
    if kind == 'oracle': raw = raw.lower()
    limit = {'mysql':64, 'postgresql':63, 'oracle':30}[kind]
    encoded = raw.encode('utf-8')
    return raw if len(encoded) <= limit else encoded[:limit-13].decode('utf-8', errors='ignore') + '_' + hashlib.sha256(encoded).hexdigest()[:12]


def convert_native_type(t, kind):
    name = type(t).__name__.upper()
    name = {'BIGINTEGER':'BIGINT', 'SMALLINTEGER':'SMALLINT', 'INT':'INTEGER', 'DOUBLE_PRECISION':'DOUBLE'}.get(name,name)
    pg = kind == 'postgresql'
    if name in ('TINYINT','SMALLINT','INTEGER','BIGINT'):
        if not pg: return oracle.NUMBER({'TINYINT':3,'SMALLINT':5,'INTEGER':10,'BIGINT':19}[name],0)
        return {'TINYINT':sa.SmallInteger, 'SMALLINT':sa.SmallInteger, 'INTEGER':sa.Integer, 'BIGINT':sa.BigInteger}[name]()
    if name in ('DECIMAL','NUMERIC','MONEY','SMALLMONEY'):
        precision = getattr(t,'precision',None) or (19 if name == 'MONEY' else 10)
        scale = getattr(t,'scale',None)
        if scale is None: scale = 4 if name in ('MONEY','SMALLMONEY') else 0
        if not 1 <= precision <= (1000 if pg else 38) or not 0 <= scale <= precision:
            raise ValueError(f'Precisão/escala incompatível com {KINDS[kind]}; escolha um tipo manual sem arredondamento implícito.')
        return sa.Numeric(precision,scale) if pg else oracle.NUMBER(precision,scale)
    if name in ('BIT','BOOLEAN'): return sa.Boolean() if pg else oracle.NUMBER(1,0)
    if name in ('FLOAT','REAL','DOUBLE'): return postgresql.DOUBLE_PRECISION() if pg else oracle.BINARY_DOUBLE()
    if name == 'DATE': return sa.Date() if pg else oracle.DATE()
    if name in ('DATETIME','DATETIME2','SMALLDATETIME'): return postgresql.TIMESTAMP(precision=6) if pg else oracle.TIMESTAMP()
    if name == 'DATETIMEOFFSET': return postgresql.TIMESTAMP(timezone=True,precision=6) if pg else OracleTimestampTZ()
    if name == 'TIME': return postgresql.TIME(precision=6) if pg else oracle.INTERVAL(day_precision=1,second_precision=6)
    if name in ('TIMESTAMP','ROWVERSION'): return postgresql.BYTEA() if pg else oracle.RAW(8)
    if name in ('BINARY','VARBINARY','IMAGE','LARGEBINARY'):
        length = getattr(t,'length',None)
        return postgresql.BYTEA() if pg else oracle.RAW(length) if length and 0 < length <= 2000 else oracle.BLOB()
    if name in ('UNIQUEIDENTIFIER','UUID'): return postgresql.UUID(as_uuid=False) if pg else oracle.NVARCHAR2(36)
    if name in ('VARCHAR','NVARCHAR','CHAR','NCHAR','TEXT','NTEXT','XML','UNICODE','UNICODETEXT','STRING'):
        length = getattr(t,'length',None)
        if pg: return sa.String(length) if length and length>0 else sa.Text()
        return oracle.NVARCHAR2(length) if length and 0 < length <= 1000 else oracle.NCLOB()
    raise ValueError(f'tipo {name} requer mapeamento manual para {KINDS[kind]}')


def target_limits(engine=None, kind='mysql'):
    if kind == 'mysql':
        limits,warnings = mysql_limits(engine)
        limits.update(kind=kind,label=KINDS[kind],identifier_bytes=64)
        return limits,warnings
    limits = dict(kind=kind,label=KINDS[kind],verified=False,version=None,
                  identifier_bytes=63 if kind=='postgresql' else 30,
                  max_columns=1600 if kind=='postgresql' else 1000,
                  max_index_columns=32, index_bytes=2602 if kind=='postgresql' else 3840, page_size_verified=False, charset='UTF8' if kind=='postgresql' else 'nacional (NVARCHAR2/NCLOB)',
                  bytes_per_char=4, page_size=8192)
    warnings=[]
    if engine is not None and getattr(engine.dialect,'name',None)==kind:
        try:
            with engine.connect() as conn:
                if kind=='postgresql':
                    row=conn.execute(sa.text("SELECT current_setting('server_version') AS version, current_setting('block_size') AS block_size, current_setting('server_encoding') AS encoding")).mappings().one()
                    limits.update(version=row['version'],page_size=int(row['block_size']),charset=row['encoding'],verified=True,page_size_verified=True)
                    # B-tree entry budget leaves room for tuple/index overhead.
                    limits['index_bytes']=max(512,int(row['block_size'])//3-128)
                    if row['encoding']!='UTF8': warnings.append('Destino PostgreSQL não usa UTF8; valide a representação dos caracteres da origem.')
                else:
                    rows=conn.execute(sa.text("SELECT parameter, value FROM nls_database_parameters WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET')")).all()
                    nls=dict(rows)
                    limits.update(charset=nls.get('NLS_CHARACTERSET','desconhecido'),national_charset=nls.get('NLS_NCHAR_CHARACTERSET','desconhecido'),verified=bool(rows),version='.'.join(map(str,getattr(engine.dialect,'server_version_info',None) or ())))
                    try:
                        page=conn.execute(sa.text("SELECT value FROM v$parameter WHERE name='db_block_size'")).scalar_one()
                        limits.update(page_size=int(page),index_bytes=max(512,int(page)//2-256),page_size_verified=True)
                    except Exception:
                        warnings.append('Oracle: db_block_size não consultável; orçamento de chave assume bloco de 8 KiB. Nenhum privilégio extra é solicitado.')
        except Exception as exc:
            warnings.append('Limites do destino não confirmados: '+diagnostic(exc,'Consultar limites de '+KINDS[kind],engines=(engine,))['message'])
    if not limits['verified']: warnings.append(f'{KINDS[kind]}: estimativas conservadoras assumidas; valide versão, encoding e limites com o destino conectado.')
    if kind=='oracle':
        warnings += ['Oracle: nomes gerados limitados a 30 bytes; VARCHAR2/RAW usam limites STANDARD. Nenhum parâmetro do servidor é alterado.',
                     'Oracle trata string vazia como NULL, inclusive em NVARCHAR2/CLOB. A carga interrompe se encontrar texto vazio; defina uma transformação de negócio fora da aplicação.',
                     'Oracle: TIME torna-se INTERVAL DAY TO SECOND (dias=0). Revise consultas e regras que esperem um tipo hora.']
    else:
        warnings.append('PostgreSQL: TEXT/VARCHAR/BYTEA usam TOAST automaticamente. Não existe a regra MySQL de 65.535 bytes; strings com caractere NUL são rejeitadas na carga.')
    warnings.append('Collation, comparação, UNIQUE com NULL e comportamento de texto diferem do SQL Server. Revise antes de usar o destino.')
    return limits,warnings


def sql_type(typ,kind): return str(typ.compile(dialect=dialect_for(kind)))


def native_lob(typ,kind):
    name=type(typ).__name__.upper()
    return name in ('CLOB','NCLOB','BLOB') if kind=='oracle' else False


def index_width(typ,kind):
    name=type(typ).__name__.upper()
    if isinstance(typ,sa.String) and not isinstance(typ,sa.Text) and getattr(typ,'length',None): return typ.length*4
    if name=='RAW': return typ.length
    if isinstance(typ,sa.Numeric): return 24
    if name=='UUID': return 16
    if isinstance(typ,(sa.Text,sa.LargeBinary)) or name in ('BYTEA','TEXT'): return None
    return 16


def postgres_inline_estimate(table):
    size = 32 + math.ceil(len(table.columns)/8)
    for col in table.columns:
        typ=col.type;name=type(typ).__name__.upper()
        if isinstance(typ,(sa.String,sa.LargeBinary)) or name in ('BYTEA','TEXT'):
            length=getattr(typ,'length',None)
            width=min(18,length*4+1) if length else 18
        elif name=='UUID': width=16
        elif isinstance(typ,sa.Numeric) and not isinstance(typ,sa.Float): width=8+2*math.ceil((typ.precision or 38)/4)
        else:width={'SMALLINTEGER':2,'SMALLINT':2,'INTEGER':4,'BIGINTEGER':8,'BIGINT':8,'BOOLEAN':1,'DATE':4,'REAL':4}.get(name,8)
        alignment=min(8,width) if width in (2,4,8,16) else 4
        size += (-size)%alignment+width
    return size


def check_and_adapt(pairs,limits):
    kind=limits.get('kind','mysql')
    if kind=='mysql': return mysql_check(pairs,limits)
    errors,checks,warnings=[],[],[]
    for src,dst in pairs:
        if len(dst.columns)>limits['max_columns']:errors.append(f'{src.fullname}: excede {limits["max_columns"]} colunas suportadas pelo perfil {KINDS[kind]}.')
        if kind=='postgresql' and postgres_inline_estimate(dst)>limits['page_size']-256:
            errors.append(f'{src.fullname}: registro local PostgreSQL estimado em {postgres_inline_estimate(dst)} bytes após ponteiros TOAST; orçamento {limits["page_size"]-256}. Muitos campos curtos/fixos; revisão estrutural necessária.')
        protected=set(c.name for c in dst.primary_key.columns)
        keys=[('PRIMARY',list(dst.primary_key.columns))] if len(dst.primary_key.columns) else []
        keys += [(i.name,list(i.columns)) for i in dst.indexes]
        keys += [(c.name,list(c.columns)) for c in dst.constraints if isinstance(c,sa.UniqueConstraint)]
        keys += [(f.name,list(f.columns)) for f in dst.foreign_key_constraints]
        for name,columns in keys:
            protected.update(c.name for c in columns)
            if len(columns)>limits['max_index_columns']: errors.append(f'{src.fullname}: {name} excede o limite de campos de índice do perfil.')
            if any(native_lob(c.type,kind) for c in columns): errors.append(f'{src.fullname}: {name} usa LOB (CLOB/NCLOB/BLOB), incompatível com índice/FK convencional Oracle. Ajuste manualmente sem truncar chaves.')
            widths=[index_width(c.type,kind) for c in columns]
            if any(w is None for w in widths):
                warnings.append(f'{src.fullname}: tamanho real de {name} sobre TEXT/BYTEA não é conhecido pelo dicionário; valide o limite btree do PostgreSQL com dados reais.')
            elif sum(widths)>limits['index_bytes']: errors.append(f'{src.fullname}: {name} estima {sum(widths)} bytes de chave; orçamento conservador {limits["index_bytes"]} no perfil {KINDS[kind]}. Não será encurtada automaticamente.')
        for fk in dst.foreign_key_constraints:
            for el in fk.elements:
                left,right=el.parent.type,el.column.type
                compatible_strings=all(isinstance(t,sa.String) and not native_lob(t,kind) for t in (left,right))
                if kind=='oracle' and compatible_strings:
                    national=lambda t:type(t).__name__.upper() in ('NVARCHAR2','NVARCHAR','NCHAR')
                    compatible_strings=national(left)==national(right)
                if not compatible_strings and sql_type(left,kind)!=sql_type(right,kind): errors.append(f'{src.fullname}: FK {fk.name} tem tipos diferentes: {sql_type(left,kind)} / {sql_type(right,kind)}. Revise a compatibilidade.')
        checks.append(dict(table=src.fullname,target=dst.fullname,protected_columns=sorted(protected), inline_bytes_estimate=postgres_inline_estimate(dst) if kind=='postgresql' else None))
    return dict(limits=limits,changes=[],tables=checks,errors=errors,warnings=warnings,
                note='Análise de metadados específica do destino, sem ler registros. Índices sobre TEXT/BYTEA no PostgreSQL ainda dependem do tamanho real dos valores; erros de carga ou DDL continuam possíveis.')
