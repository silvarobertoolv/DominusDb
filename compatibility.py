"""Metadata-only MySQL sizing checks. Estimates are not a server DDL validator."""
import math
import re
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from diagnostics import diagnostic


def target_limits(engine=None):
    limits = dict(verified=False, version=None, page_size=16384, charset='utf8mb4', bytes_per_char=4,
                  row_format='DYNAMIC', logical_row_budget=60000, logical_row_limit=65535,
                  index_bytes=3072, max_columns=1017, max_index_columns=16)
    warnings = []
    if engine is not None and engine.dialect.name == 'mysql':
        try:
            with engine.connect() as conn:
                row = conn.execute(sa.text('SELECT VERSION() AS version, @@innodb_page_size AS page_size')).mappings().one()
            page = int(row['page_size'])
            if page not in (4096, 8192, 16384, 32768, 65536): raise ValueError('innodb_page_size desconhecido')
            version = str(row['version'])
            if 'mariadb' in version.lower() or not re.match(r'^(8|9)\.', version):
                warnings.append('Destino não é MySQL 8/9: valide suporte a DYNAMIC e limites de índices da versão antes de executar.')
            limits.update(verified=True, version=version, page_size=page,
                          index_bytes=min(3072, page * 3 // 16))
        except Exception as exc:
            detail = diagnostic(exc, 'Consultar limites do MySQL', engines=(engine,))
            warnings.append(f"Limites do destino não confirmados: {detail['message']}. Estimativa assume MySQL 8, página de 16 KiB e InnoDB DYNAMIC.")
    else:
        warnings.append('Estimativa de compatibilidade assume MySQL 8, página de 16 KiB e InnoDB DYNAMIC; conecte o destino e gere novamente para consultar os limites reais.')
    # Large pages still impose a local record limit slightly below 16 KiB.
    limits['inline_budget'] = min(limits['page_size'] // 2, 16384) - 256
    return limits, warnings


def source_dictionary(engine, table):
    if engine.dialect.name == 'mssql':
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(sa.text('''
                SELECT c.name AS column_name, ut.name AS declared_type,
                       TYPE_NAME(c.system_type_id) AS base_type,
                       c.max_length AS max_length_bytes, c.precision AS [precision],
                       c.scale, c.is_nullable, c.is_identity, c.is_computed,
                       c.collation_name, dc.definition AS default_definition,
                       cc.definition AS computed_definition, cc.is_persisted
                FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id
                JOIN sys.columns c ON c.object_id=t.object_id
                JOIN sys.types ut ON ut.user_type_id=c.user_type_id
                LEFT JOIN sys.default_constraints dc ON dc.object_id=c.default_object_id
                LEFT JOIN sys.computed_columns cc ON cc.object_id=c.object_id AND cc.column_id=c.column_id
                WHERE s.name=:schema AND t.name=:table ORDER BY c.column_id
            '''), {'schema': table.schema, 'table': table.name}).mappings()]
    return [dict(column_name=c.name, declared_type=str(c.type), base_type=type(c.type).__name__,
                 length=getattr(c.type, 'length', None), precision=getattr(c.type, 'precision', None),
                 scale=getattr(c.type, 'scale', None), is_nullable=c.nullable) for c in table.columns]


def type_sql(typ):
    return str(typ.compile(dialect=mysql.dialect()))


def decimal_bytes(precision, scale):
    # MySQL packs groups of nine decimal digits into four bytes.
    rest = (0, 1, 1, 2, 2, 3, 3, 4, 4)
    integer = precision - scale
    return 4 * (integer // 9 + scale // 9) + rest[integer % 9] + rest[scale % 9]


LOB_NAMES = {'TEXT', 'TINYTEXT', 'MEDIUMTEXT', 'LONGTEXT', 'BLOB', 'TINYBLOB', 'MEDIUMBLOB', 'LONGBLOB'}


def is_lob(typ):
    return type(typ).__name__.upper() in LOB_NAMES or isinstance(typ, sa.Text)


def index_width(typ):
    if is_lob(typ): return 0  # Reported separately as unsupported without a prefix.
    if isinstance(typ, sa.String): return (typ.length or 65536) * 4
    if type(typ).__name__.upper() in ('BINARY', 'VARBINARY'): return typ.length or 65536
    return storage(typ)[0]


def storage(typ):
    name = type(typ).__name__.upper()
    if is_lob(typ):
        return 12, 20  # Logical descriptor and DYNAMIC external reference (DDL estimate).
    if isinstance(typ, sa.String):
        size = (typ.length or 0) * 4
        return size + (2 if size > 255 else 1), (size if name == 'CHAR' and size < 768 else 20 if size > 255 else size) + 2
    if name in ('BINARY', 'VARBINARY'):
        size = typ.length or 0
        return size + (2 if name == 'VARBINARY' else 0), ((20 if size > 255 else size) + 2 if name == 'VARBINARY' else size)
    if isinstance(typ, sa.Numeric) and not isinstance(typ, sa.Float):
        size = decimal_bytes(typ.precision or 10, typ.scale or 0)
    else:
        size = {'TINYINT':1, 'BOOLEAN':1, 'SMALLINTEGER':2, 'SMALLINT':2, 'INTEGER':4,
                'BIGINTEGER':8, 'BIGINT':8, 'DOUBLE':8, 'FLOAT':8, 'DATE':3,
                'DATETIME':8, 'TIME':6}.get(name, 16)
    return size, size


def row_estimate(table):
    overhead = 128 + math.ceil(len(table.columns) / 8)
    widths = [storage(c.type) for c in table.columns]
    return {'logical_bytes': overhead + sum(w[0] for w in widths),
            'inline_bytes_estimate': overhead + sum(w[1] for w in widths)}


def check_and_adapt(pairs, limits):
    """Only widen non-key variable columns; never truncate data or index keys."""
    changes, errors, checks = [], [], []
    protected = {d.name: set() for _, d in pairs}
    for _, table in pairs:
        protected[table.name].update(c.name for c in table.primary_key.columns)
        for index in table.indexes:
            protected[table.name].update(c.name for c in index.columns)
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint):
                protected[table.name].update(c.name for c in constraint.columns)
        for fk in table.foreign_key_constraints:
            protected[table.name].update(c.name for c in fk.columns)
            for element in fk.elements:
                ref = element.column
                if ref.table.name in protected: protected[ref.table.name].add(ref.name)
    # Preserve local FK columns even when the referenced table was not selected.
    for src, dst in pairs:
        protected[dst.name].update(c.name for c in src.columns if c.foreign_keys)
        protected[dst.name].update(src.info.get('source_fk_columns', set()))

    for src, table in pairs:
        table.dialect_options['mysql']['row_format'] = 'DYNAMIC'
        before = row_estimate(table)
        if len(table.columns) > limits['max_columns']:
            errors.append(f'{src.fullname}: {len(table.columns)} colunas excedem o limite InnoDB de {limits["max_columns"]}. Divida a tabela; conversão de tipos não resolve.')
        candidates = sorted((c for c in table.columns if c.name not in protected[table.name] and not c.info.get('manual_type')
                             and (isinstance(c.type, sa.String) and not is_lob(c.type)
                                  or type(c.type).__name__.upper() == 'VARBINARY')),
                            key=lambda c: storage(c.type)[0], reverse=True)
        for col in candidates:
            if not getattr(col.type, 'length', None): continue
            estimate = row_estimate(table)
            if estimate['logical_bytes'] <= limits['logical_row_budget'] and estimate['inline_bytes_estimate'] <= limits['inline_budget']:
                break
            old = type_sql(col.type)
            old_width = storage(col.type)[0]
            if old_width <= 12: continue  # A LOB descriptor would not reduce the logical row.
            if isinstance(col.type, sa.String):
                max_bytes = col.type.length * 4
                col.type = mysql.TEXT() if max_bytes <= 65535 else mysql.MEDIUMTEXT() if max_bytes <= 16777215 else mysql.LONGTEXT()
            else:
                max_bytes = col.type.length
                col.type = mysql.BLOB() if max_bytes <= 65535 else mysql.MEDIUMBLOB() if max_bytes <= 16777215 else mysql.LONGBLOB()
            changes.append(dict(table=src.fullname, column=col.name, before=old, after=type_sql(col.type),
                                reason='Reduzir tamanho máximo lógico da linha (1118), preservando a capacidade e sem alterar chaves/índices.'))
        after = row_estimate(table)
        if after['logical_bytes'] > limits['logical_row_budget']:
            errors.append(f'{src.fullname}: linha estimada em {after["logical_bytes"]} bytes, acima do orçamento de {limits["logical_row_budget"]} (limite SQL 65535). Não há conversão automática segura restante; revise colunas protegidas por chaves/índices ou divida a tabela.')
        if after['inline_bytes_estimate'] > limits['inline_budget']:
            errors.append(f'{src.fullname}: registro inline estimado em {after["inline_bytes_estimate"]} bytes excede o orçamento InnoDB de {limits["inline_budget"]}. Há muitos campos curtos/fixos; TEXT e DYNAMIC podem não resolver. Revise a estrutura.')
        checks.append(dict(table=src.fullname, target=table.name, before=before, after=after,
                           protected_columns=sorted(protected[table.name]), row_format='DYNAMIC'))
        keys = [('PRIMARY', list(table.primary_key.columns))] if len(table.primary_key.columns) else []
        keys += [(i.name, list(i.columns)) for i in table.indexes]
        keys += [(c.name or 'UNIQUE', list(c.columns)) for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
        keys += [(f.name or 'FK', list(f.columns)) for f in table.foreign_key_constraints]
        for name, columns in keys:
            if len(columns) > limits['max_index_columns']:
                errors.append(f'{src.fullname}: chave/índice {name} tem {len(columns)} colunas; limite InnoDB: {limits["max_index_columns"]}.')
            if any(is_lob(c.type) for c in columns):
                errors.append(f'{src.fullname}: chave/índice {name} usa TEXT/BLOB sem prefixo (1170). Revisão manual; prefixar UNIQUE/PK/FK pode alterar a integridade.')
            size = sum(index_width(c.type) for c in columns)
            if size > limits['index_bytes']:
                errors.append(f'{src.fullname}: chave/índice {name} pode exigir {size} bytes; limite estimado {limits["index_bytes"]} (1071). Não será encurtada automaticamente.')
        for fk in table.foreign_key_constraints:
            for element in fk.elements:
                left, right = element.parent.type, element.column.type
                compatible_strings = all(isinstance(t, sa.String) and not is_lob(t) for t in (left, right))
                # MySQL permits different VARCHAR lengths when charset/collation match.
                # All generated text columns use the same table charset/collation.
                if not compatible_strings and type_sql(left) != type_sql(right):
                    errors.append(f'{src.fullname}: FK {fk.name}, {element.parent.name} ({type_sql(element.parent.type)}) e {element.column.table.name}.{element.column.name} ({type_sql(element.column.type)}) incompatíveis no MySQL (3780).')
    return dict(limits=limits, changes=changes, tables=checks, errors=errors,
                note='Estimativas a partir do dicionário, sem ler valores das tabelas. Não garantem que o servidor aceitará todo registro; DYNAMIC e valores curtos podem afetar o tamanho real.')
