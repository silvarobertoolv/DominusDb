"""SQL Server inventory and conservative MySQL migration planner."""
import hashlib
from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable, AddConstraint, CreateIndex


def target_name(schema, name):
    raw = f'{schema}__{name}'
    return raw if len(raw) <= 64 else raw[:51] + '_' + hashlib.sha256(raw.encode()).hexdigest()[:12]


def convert_type(t):
    name = type(t).__name__.upper()
    if name in ('TINYINT', 'SMALLINT', 'INTEGER', 'INT', 'BIGINT'):
        return {'TINYINT': mysql.TINYINT(unsigned=True), 'SMALLINT': sa.SmallInteger(), 'INTEGER': sa.Integer(), 'INT': sa.Integer(), 'BIGINT': sa.BigInteger()}[name]
    if name in ('DECIMAL', 'NUMERIC', 'MONEY', 'SMALLMONEY'):
        return sa.Numeric(getattr(t, 'precision', None) or (19 if name == 'MONEY' else 10), getattr(t, 'scale', None) if getattr(t, 'scale', None) is not None else 4)
    if name in ('BIT', 'BOOLEAN'): return sa.Boolean()
    if name in ('FLOAT', 'REAL', 'DOUBLE'): return mysql.DOUBLE()
    if name in ('DATE',): return sa.Date()
    if name in ('DATETIME', 'DATETIME2', 'SMALLDATETIME'): return mysql.DATETIME(fsp=6)
    if name == 'TIME': return mysql.TIME(fsp=6)
    if name == 'DATETIMEOFFSET': raise ValueError('datetimeoffset exige política explícita de fuso horário')
    if name in ('TIMESTAMP', 'ROWVERSION'): return mysql.BINARY(8)
    if name in ('BINARY', 'VARBINARY', 'IMAGE', 'LARGEBINARY'): return mysql.LONGBLOB()
    if name in ('UNIQUEIDENTIFIER', 'UUID'): return sa.String(36)
    if name in ('VARCHAR', 'NVARCHAR', 'CHAR', 'NCHAR', 'TEXT', 'NTEXT', 'XML', 'UNICODE', 'UNICODETEXT', 'STRING'):
        n = getattr(t, 'length', None)
        return sa.String(n) if n and 0 < n <= 4000 else mysql.LONGTEXT()
    raise ValueError(f'tipo {name} requer mapeamento manual')


def inventory(engine):
    inspector = sa.inspect(engine)
    tables = []
    for schema in inspector.get_schema_names():
        if schema.lower() in ('sys', 'information_schema', 'guest'): continue
        for name in inspector.get_table_names(schema=schema):
            tables.append({'schema': schema, 'name': name, 'id': f'{schema}.{name}', 'columns': len(inspector.get_columns(name, schema=schema))})
    with engine.connect() as conn:
        objects = [dict(r) for r in conn.execute(sa.text("""
            SELECT s.name AS [schema], o.name, o.type_desc AS kind, COALESCE(m.definition, cc.definition, dc.definition) AS definition
            FROM sys.objects o JOIN sys.schemas s ON s.schema_id=o.schema_id
            LEFT JOIN sys.sql_modules m ON m.object_id=o.object_id
            LEFT JOIN sys.check_constraints cc ON cc.object_id=o.object_id
            LEFT JOIN sys.default_constraints dc ON dc.object_id=o.object_id
            WHERE o.is_ms_shipped=0 AND o.type IN ('V','P','FN','IF','TF','TR','SO','SN','C','D')
            ORDER BY s.name,o.name
        """)).mappings()]
    return {'tables': tables, 'objects': objects}


def plan(engine, selected):
    catalog = inventory(engine)
    available = {t['id']: t for t in catalog['tables']}
    if not selected or len(selected) != len(set(selected)) or any(x not in available for x in selected):
        raise ValueError('Selecione tabelas válidas, sem duplicatas.')
    source = sa.MetaData()
    destination = sa.MetaData()
    pairs, warnings, errors = [], [], []
    for key in selected:
        item = available[key]
        src = sa.Table(item['name'], source, schema=item['schema'], autoload_with=engine, resolve_fks=False)
        pk = set(c.name for c in src.primary_key.columns)
        columns = []
        for col in src.columns:
            try:
                if len(col.name) > 64: raise ValueError('nome de coluna excede o limite de 64 caracteres do MySQL')
                typ = convert_type(col.type)
                columns.append(sa.Column(col.name, typ, nullable=False if col.name in pk else col.nullable, primary_key=col.name in pk, autoincrement=False))
                if col.default or col.server_default or col.computed or col.identity:
                    warnings.append(f'{key}.{col.name}: valor copiado; default, cálculo ou identity não recriados.')
                if type(col.type).__name__.upper() in ('DATETIME2', 'TIME'):
                    warnings.append(f'{key}.{col.name}: MySQL preserva até 6 casas de segundo; SQL Server pode ter 7.')
            except ValueError as exc:
                errors.append(f'{key}.{col.name}: {exc}')
        dst = sa.Table(target_name(item['schema'], item['name']), destination, *columns, mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')
        pairs.append((src, dst))
    names = [dst.name.casefold() for _, dst in pairs]
    if len(names) != len(set(names)): errors.append('Colisão de nomes no MySQL; renomeie as tabelas de origem.')
    mapping = {(s.schema, s.name): d for s, d in pairs}
    inspector = sa.inspect(engine)
    for src, dst in pairs:
        for index in inspector.get_indexes(src.name, schema=src.schema):
            cols = index.get('column_names', [])
            options = index.get('dialect_options') or {}
            special = any(options.get(k) is not None for k in ('mssql_where',)) or bool(options.get('mssql_include') or index.get('include_columns') or options.get('mssql_columnstore'))
            if special or not cols or any(c not in dst.c for c in cols):
                warnings.append(f'{src.fullname}: índice {index["name"]} exige revisão.'); continue
            sa.Index(target_name(dst.name, index['name']), *[dst.c[c] for c in cols], unique=index.get('unique', False))
        for fk in inspector.get_foreign_keys(src.name, schema=src.schema):
            ref = mapping.get((fk.get('referred_schema') or src.schema, fk['referred_table']))
            if ref is None:
                warnings.append(f'{src.fullname}: FK {fk["name"]} omitida; tabela referenciada fora da seleção.'); continue
            if any(c not in dst.c for c in fk['constrained_columns']) or any(c not in ref.c for c in fk['referred_columns']): continue
            options = fk.get('options') or {}
            if any(options.get(k, '').upper() == 'SET DEFAULT' for k in ('ondelete', 'onupdate')):
                errors.append(f'{src.fullname}: FK {fk["name"]} usa SET DEFAULT, não suportado no MySQL.'); continue
            dst.append_constraint(sa.ForeignKeyConstraint([dst.c[c] for c in fk['constrained_columns']], [ref.c[c] for c in fk['referred_columns']], name=target_name(dst.name, fk['name'] or 'fk'), use_alter=True, ondelete=options.get('ondelete'), onupdate=options.get('onupdate')))
        # SQLAlchemy's MSSQL dialect exposes UNIQUEs through get_indexes,
        # and does not implement get_unique_constraints/get_check_constraints.
        try:
            uniques = inspector.get_unique_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            uniques = []
        indexed_unique = {tuple(c.name for c in idx.columns) for idx in dst.indexes if idx.unique}
        for unique in uniques:
            cols = unique.get('column_names', [])
            if cols and tuple(cols) not in indexed_unique and all(c in dst.c for c in cols):
                dst.append_constraint(sa.UniqueConstraint(*cols, name=target_name(dst.name, unique['name'] or 'uq')))
        try:
            checks = inspector.get_check_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            checks = []  # CHECK definitions are included in the sys.objects export.
        if checks: warnings.append(f'{src.fullname}: CHECK constraints exigem adaptação manual.')
    warnings.append('Identity, defaults, expressões calculadas e CHECKs não são recriados. PKs, índices comuns, UNIQUEs e FKs entre tabelas selecionadas são incluídos. Revise diferenças de UNIQUE com NULL, collation e limites de índices do MySQL.')
    warnings.append('Collation utf8mb4_bin: ordenação e comparação podem diferir da origem. Revise chaves únicas, texto e regras de negócio.')
    warnings.append('Views, procedures, funções, triggers, sequences e synonyms são exportados para revisão; não executados automaticamente.')
    dialect = mysql.dialect()
    statements = [str(CreateTable(d).compile(dialect=dialect))+';' for _, d in pairs]
    for _, d in pairs:
        statements += [str(CreateIndex(i).compile(dialect=dialect))+';' for i in d.indexes]
        statements += [str(AddConstraint(f).compile(dialect=dialect))+';' for f in d.foreign_key_constraints]
    return {'pairs': pairs, 'ddl': '\n\n'.join(statements), 'warnings': warnings, 'errors': errors, 'objects': catalog['objects']}


class MigrationError(Exception):
    pass


def execute(source, target, prepared, job, batch):
    """Never drops, truncates or reuses destination tables. MySQL DDL auto-commits."""
    job.update(status='running', rows=0, logs=[], tables=[])
    pairs = prepared['pairs']
    try:
        if prepared['errors']: raise MigrationError('Plano contém tipos não suportados.')
        existing = {name.casefold() for name in sa.inspect(target).get_table_names()}
        if any(d.name.casefold() in existing for _, d in pairs): raise MigrationError('Tabela de destino já existe. Use um banco vazio ou remova manualmente apenas os artefatos de uma tentativa anterior.')
        # Create ALL tables before copying. No foreign keys until data is loaded.
        with target.connect() as out:
            for _, dst in pairs:
                job['stage'] = f'Criando {dst.name}'
                out.execute(CreateTable(dst)); out.commit()
        with source.connect().execution_options(stream_results=True) as inc:
            for src, dst in pairs:
                job['stage'] = f'Copiando {src.fullname}'
                copied = 0
                with target.begin() as out:
                    result = inc.execute(sa.select(src))
                    while True:
                        rows = result.fetchmany(batch)
                        if not rows: break
                        payload = []
                        for row in rows:
                            entry = dict(row._mapping)
                            for col in src.columns:
                                if entry[col.name] is not None and type(col.type).__name__.upper() in ('UNIQUEIDENTIFIER', 'UUID'): entry[col.name] = str(entry[col.name])
                            payload.append(entry)
                        out.execute(dst.insert(), payload)
                        copied += len(rows); job['rows'] += len(rows)
                    actual = out.scalar(sa.select(sa.func.count()).select_from(dst))
                    if actual != copied: raise ValueError('Contagem de destino divergente.')
                job['tables'].append({'source': src.fullname, 'target': dst.name, 'copied': copied, 'target_count': actual})
                job['logs'].append(f'{src.fullname}: {copied} registros confirmados.')
        job['stage'] = 'Criando índices e relacionamentos'
        with target.connect() as out:
            for _, dst in pairs:
                for index in dst.indexes: index.create(out); out.commit()
                for fk in dst.foreign_key_constraints: out.execute(AddConstraint(fk)); out.commit()
        job.update(status='completed', stage='Concluído; revise os objetos manuais e valide a aplicação.')
    except Exception as exc:
        # Do not expose driver messages, SQL parameters or migrated records.
        job.update(status='failed', stage='Migração interrompida', error=str(exc) if isinstance(exc, MigrationError) else f'{type(exc).__name__}: falha em {job.get("stage", "preparação")}. Confira compatibilidade, dados e tabelas parciais; a operação não remove tabelas preexistentes.')
    finally:
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
