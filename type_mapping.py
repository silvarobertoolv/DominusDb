"""Parse a bounded MySQL type vocabulary; never accept arbitrary DDL."""
import re
from sqlalchemy.dialects import mysql

TYPE_EXAMPLES = ['BIGINT', 'INT', 'SMALLINT', 'TINYINT', 'BIGINT UNSIGNED', 'BOOLEAN',
                 'DECIMAL(18,2)', 'DOUBLE', 'FLOAT', 'VARCHAR(255)', 'CHAR(36)',
                 'TEXT', 'MEDIUMTEXT', 'LONGTEXT', 'VARBINARY(255)', 'BINARY(16)',
                 'BLOB', 'MEDIUMBLOB', 'LONGBLOB', 'DATE', 'DATETIME(6)', 'TIME(6)']


def parse_target_type(value):
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
