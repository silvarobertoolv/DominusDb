"""Parse a bounded MySQL type vocabulary; never accept arbitrary DDL."""
import re
from sqlalchemy.dialects import mysql, postgresql, oracle
import sqlalchemy as sa

TYPE_EXAMPLES = ['BIGINT', 'INT', 'SMALLINT', 'TINYINT', 'BIGINT UNSIGNED', 'BOOLEAN',
                 'DECIMAL(18,2)', 'DOUBLE', 'FLOAT', 'VARCHAR(255)', 'CHAR(36)',
                 'TEXT', 'MEDIUMTEXT', 'LONGTEXT', 'VARBINARY(255)', 'BINARY(16)',
                 'BLOB', 'MEDIUMBLOB', 'LONGBLOB', 'DATE', 'DATETIME(6)', 'TIME(6)']


def parse_target_type(value, kind="mysql"):
    if kind != "mysql": return parse_native_type(value, kind)
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Informe um tipo MySQL válido.')
    match = re.fullmatch(r'\s*([A-Za-z]+)\s*(?:\(\s*(\d{1,5})\s*(?:,\s*(\d{1,5})\s*)?\))?\s*(UNSIGNED)?\s*', value, re.I)
    if not match: raise ValueError('Use somente tipo e parâmetros, por exemplo BIGINT, VARCHAR(255) ou DECIMAL(18,2).')
    name, first, second, unsigned = match.groups()
    name = {'INTEGER':'INT', 'NUMERIC':'DECIMAL', 'BOOL':'BOOLEAN', 'REAL':'DOUBLE'}.get(name.upper(), name.upper())
    args = [int(x) for x in (first, second) if x is not None]
    if unsigned and name not in ('TINYINT', 'SMALLINT', 'INT', 'BIGINT', 'DECIMAL'):
        raise ValueError('UNSIGNED é permitido apenas em inteiros e DECIMAL.')
    if name in ('TINYINT', 'SMALLINT', 'INT', 'BIGINT') and not args:
        return getattr(mysql, 'INTEGER' if name == 'INT' else name)(unsigned=bool(unsigned))
    if name == 'DECIMAL' and 1 <= len(args) <= 2:
        precision, scale = args[0], args[1] if len(args) == 2 else 0
        if 1 <= precision <= 65 and 0 <= scale <= min(30, precision):
            return mysql.DECIMAL(precision, scale, unsigned=bool(unsigned))
    if name in ('VARCHAR', 'CHAR', 'BINARY', 'VARBINARY') and len(args) == 1:
        cap = {'VARCHAR':16383, 'CHAR':255, 'BINARY':255, 'VARBINARY':65535}[name]
        if 1 <= args[0] <= cap: return getattr(mysql, name)(args[0])
    if name in ('DATETIME', 'TIME') and len(args) <= 1:
        if not args or 0 <= args[0] <= 6: return getattr(mysql, name)(fsp=args[0] if args else 6)
    if name in ('BOOLEAN', 'DOUBLE', 'FLOAT', 'TEXT', 'MEDIUMTEXT', 'LONGTEXT', 'BLOB', 'MEDIUMBLOB', 'LONGBLOB', 'DATE') and not args:
        return getattr(mysql, name)()
    raise ValueError('Tipo/parâmetros inválidos ou não suportados. Revise tamanho, precisão e escala MySQL.')


def validate_overrides(overrides):
    if overrides is None: return {}
    if not isinstance(overrides, dict): raise ValueError('Mapeamentos devem ser um objeto por tabela e coluna.')
    for table, columns in overrides.items():
        if not isinstance(table, str) or not isinstance(columns, dict): raise ValueError('Mapeamento de tabela inválido.')
        for column, value in columns.items():
            if not isinstance(column, str): raise ValueError('Nome de coluna inválido.')
            # Individual invalid types stay in the editable plan as column errors.
            if not isinstance(value, str): raise ValueError('Tipo de destino deve ser texto.')
    return overrides


TYPE_EXAMPLES_BY_KIND = {
    'mysql': TYPE_EXAMPLES,
    'postgresql': ['BIGINT','INTEGER','SMALLINT','BOOLEAN','NUMERIC(38,10)','DOUBLE PRECISION','VARCHAR(255)','TEXT','UUID','BYTEA','DATE','TIME(6)','TIMESTAMP(6)','TIMESTAMP(6) WITH TIME ZONE'],
    'oracle': ['NUMBER(19,0)','NUMBER(38,10)','BINARY_DOUBLE','NVARCHAR2(255)','VARCHAR2(255)','NCLOB','CLOB','RAW(16)','BLOB','DATE','TIMESTAMP(6)','TIMESTAMP(6) WITH TIME ZONE','INTERVAL DAY TO SECOND'],
}


def parse_native_type(value,kind):
    if kind not in ('postgresql','oracle') or not isinstance(value,str) or len(value)>80:raise ValueError('Tipo de destino inválido.')
    normalized=' '.join(value.upper().split())
    if normalized=='DOUBLE PRECISION' and kind=='postgresql':return postgresql.DOUBLE_PRECISION()
    if normalized in ('INTERVAL DAY TO SECOND','INTERVAL DAY(1) TO SECOND(6)') and kind=='oracle':return oracle.INTERVAL(day_precision=1,second_precision=6)
    match=re.fullmatch(r'([A-Z0-9_]+)\s*(?:\(\s*(\d{1,5})\s*(?:,\s*(\d{1,5})\s*)?\))?\s*(WITH(?:OUT)? TIME ZONE)?',normalized)
    if not match:raise ValueError('Informe somente um tipo suportado e seus parâmetros; SQL arbitrário não é permitido.')
    name,first,second,tz=match.groups()
    args=[int(x) for x in (first,second) if x is not None]
    if tz and name not in ('TIME','TIMESTAMP'):raise ValueError('WITH TIME ZONE exige TIME/TIMESTAMP.')
    if kind=='postgresql':
        if name in ('TIMESTAMP','TIME') and len(args)<=1 and (not args or args[0]<=6):return getattr(postgresql,name)(precision=args[0] if args else 6,timezone=tz=='WITH TIME ZONE')
        if tz:raise ValueError('Tipo temporal inválido.')
        if name in ('NUMERIC','DECIMAL') and 1<=len(args)<=2:
            p,s=args[0],args[1] if len(args)==2 else 0
            if 1<=p<=1000 and 0<=s<=p:return sa.Numeric(p,s)
        if name in ('VARCHAR','CHAR') and len(args)==1 and 1<=args[0]<=10485760:return getattr(sa,'String' if name=='VARCHAR' else 'CHAR')(args[0])
        types={'INT':sa.Integer,'INTEGER':sa.Integer,'BIGINT':sa.BigInteger,'SMALLINT':sa.SmallInteger,'BOOLEAN':sa.Boolean,'TEXT':sa.Text,'BYTEA':postgresql.BYTEA,'UUID':lambda:postgresql.UUID(as_uuid=False),'DATE':sa.Date,'DOUBLE':postgresql.DOUBLE_PRECISION,'REAL':sa.REAL}
        if name in types and not args:return types[name]()
    else:
        if name=='TIMESTAMP' and (not args or args==[6]):
            from target_profiles import OracleTimestampTZ
            return OracleTimestampTZ() if tz=='WITH TIME ZONE' else oracle.TIMESTAMP()
        if tz:raise ValueError('Oracle neste perfil suporta TIMESTAMP(6) WITH TIME ZONE.')
        if name in ('NUMBER','NUMERIC','DECIMAL'):
            if not args and name=='NUMBER':return oracle.NUMBER()
            if 1<=len(args)<=2:
                p,s=args[0],args[1] if len(args)==2 else 0
                if 1<=p<=38 and 0<=s<=127:return oracle.NUMBER(p,s)
        if name in ('VARCHAR2','NVARCHAR2','RAW') and len(args)==1:
            cap={'VARCHAR2':1000,'NVARCHAR2':1000,'RAW':2000}[name]
            if 1<=args[0]<=cap:return getattr(oracle,name)(args[0])
        types={'CLOB':oracle.CLOB,'NCLOB':oracle.NCLOB,'BLOB':oracle.BLOB,'DATE':oracle.DATE,'BINARY_DOUBLE':oracle.BINARY_DOUBLE,'BINARY_FLOAT':oracle.BINARY_FLOAT}
        if name in types and not args:return types[name]()
    raise ValueError('Tipo/parâmetros incompatíveis com o destino selecionado. Use as sugestões do perfil.')
