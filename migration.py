"""SQL Server inventory and conservative MySQL migration planner."""
import hashlib
from diagnostics import diagnostic
from type_mapping import parse_target_type, validate_overrides
from transfer import copy_table, TransferControl, MigrationCancelled, activity
from compatibility import target_limits, source_dictionary, check_and_adapt
from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, mssql
from sqlalchemy.schema import CreateTable, AddConstraint, CreateIndex


def target_name(schema, name):
    raw = f'{schema}__{name}'
    return raw if len(raw) <= 64 else raw[:51] + '_' + hashlib.sha256(raw.encode()).hexdigest()[:12]


def convert_type(t):
    name = type(t).__name__.upper()
    name = {'BIGINTEGER': 'BIGINT', 'SMALLINTEGER': 'SMALLINT', 'DOUBLE_PRECISION': 'DOUBLE'}.get(name, name)
    if name in ('TINYINT', 'SMALLINT', 'INTEGER', 'INT', 'BIGINT'):
        return {'TINYINT': mysql.TINYINT(unsigned=True), 'SMALLINT': sa.SmallInteger(), 'INTEGER': sa.Integer(), 'INT': sa.Integer(), 'BIGINT': sa.BigInteger()}[name]
    if name in ('DECIMAL', 'NUMERIC', 'MONEY', 'SMALLMONEY'):
        precision = getattr(t, 'precision', None) or (19 if name == 'MONEY' else 10)
        scale = getattr(t, 'scale', None)
        if scale is None: scale = 4 if name in ('MONEY', 'SMALLMONEY') else 0
        if precision > 65 or scale > 30 or scale > precision:
            raise ValueError(f'DECIMAL({precision},{scale}) excede os limites MySQL (precisão 65, escala 30); reduzir escala automaticamente perderia dados')
        return sa.Numeric(precision, scale)
    if name in ('BIT', 'BOOLEAN'): return sa.Boolean()
    if name in ('FLOAT', 'REAL', 'DOUBLE'): return mysql.DOUBLE()
    if name in ('DATE',): return sa.Date()
    if name in ('DATETIME', 'DATETIME2', 'SMALLDATETIME'): return mysql.DATETIME(fsp=6)
    if name == 'TIME': return mysql.TIME(fsp=6)
    if name == 'DATETIMEOFFSET': raise ValueError('datetimeoffset exige política explícita de fuso horário')
    if name in ('TIMESTAMP', 'ROWVERSION'): return mysql.BINARY(8)
    if name == 'BINARY' and getattr(t, 'length', None) and t.length <= 255: return mysql.BINARY(t.length)
    if name in ('BINARY', 'VARBINARY') and getattr(t, 'length', None) and 0 < t.length <= 65535: return mysql.VARBINARY(t.length)
    if name in ('BINARY', 'VARBINARY', 'IMAGE', 'LARGEBINARY'): return mysql.LONGBLOB()
    if name in ('UNIQUEIDENTIFIER', 'UUID'): return sa.String(36)
    if name in ('VARCHAR', 'NVARCHAR', 'CHAR', 'NCHAR', 'TEXT', 'NTEXT', 'XML', 'UNICODE', 'UNICODETEXT', 'STRING'):
        n = getattr(t, 'length', None)
        return sa.String(n) if n and 0 < n <= 4000 else mysql.LONGTEXT()
    raise ValueError(f'tipo {name} requer mapeamento manual')


def dictionary_type(column, definition):
    """Resolve declared byte lengths and alias base types using sys.columns."""
    base = str(definition.get('base_type') or '').lower()
    length = definition.get('max_length_bytes')
    if base in ('varchar', 'nvarchar', 'char', 'nchar', 'binary', 'varbinary') and length is not None:
        length = int(length)
        if length < 0: length = None
        elif base in ('nvarchar', 'nchar'): length //= 2  # sys.columns reports UTF-16 bytes.
        return getattr(mssql, base.upper())(length=length)
    if base in ('numeric', 'decimal') and definition.get('precision') is not None and definition.get('scale') is not None:
        return sa.Numeric(int(definition['precision']), int(definition['scale']))
    known = {'int': sa.Integer, 'smallint': sa.SmallInteger, 'bigint': sa.BigInteger,
             'tinyint': mssql.TINYINT, 'bit': mssql.BIT, 'money': mssql.MONEY,
             'smallmoney': mssql.SMALLMONEY, 'text': mssql.TEXT, 'ntext': mssql.NTEXT,
             'image': mssql.IMAGE, 'xml': mssql.XML, 'uniqueidentifier': mssql.UNIQUEIDENTIFIER,
             'timestamp': mssql.TIMESTAMP, 'datetimeoffset': mssql.DATETIMEOFFSET,
             'date': mssql.DATE, 'datetime': mssql.DATETIME, 'datetime2': mssql.DATETIME2,
             'smalldatetime': mssql.SMALLDATETIME, 'time': mssql.TIME, 'float': mssql.FLOAT, 'real': mssql.REAL}
    return known[base]() if base in known else column.type


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


class PlanError(Exception):
    def __init__(self, stage, object_name, cause):
        self.stage, self.object_name, self.cause = stage, object_name, cause
        super().__init__(f'{stage}: {type(cause).__name__}')


def plan(engine, selected, target=None, overrides=None):
    context = {'stage': 'Ler catálogo', 'object': None}
    try:
        return _plan(engine, selected, context, target, validate_overrides(overrides))
    except Exception as exc:
        raise PlanError(context['stage'], context['object'], exc) from exc


def _plan(engine, selected, context, target=None, overrides=None):
    catalog = inventory(engine)
    available = {t['id']: t for t in catalog['tables']}
    if not selected or len(selected) != len(set(selected)) or any(x not in available for x in selected):
        raise ValueError('Selecione tabelas válidas, sem duplicatas.')
    overrides = overrides or {}
    unknown = set(overrides) - set(selected)
    if unknown: raise ValueError('Mapeamento aponta para tabela fora da seleção: ' + ', '.join(sorted(unknown)))
    column_mappings = []
    source = sa.MetaData()
    destination = sa.MetaData()
    pairs, warnings, errors = [], [], []
    dictionary = []
    limits, limit_warnings = target_limits(target)
    warnings.extend(limit_warnings)
    for key in selected:
        item = available[key]
        context.update(stage='Refletir colunas, PKs e relacionamentos', object=key)
        src = sa.Table(item['name'], source, schema=item['schema'], autoload_with=engine, resolve_fks=False)
        context.update(stage='Consultar dicionário sys.columns/sys.types', object=key)
        definitions = source_dictionary(engine, src)
        dictionary.append({'table': key, 'columns': definitions})
        if engine.dialect.name == 'mssql' and len(definitions) != len(src.columns):
            errors.append(f'{key}: dicionário incompleto ou estrutura alterada durante a leitura. Confira permissões de metadados e gere o plano novamente.')
        definitions_by_name = {d['column_name']: d for d in definitions}
        unknown_columns = set(overrides.get(key, {})) - set(src.c.keys())
        if unknown_columns: errors.append(f'{key}: colunas de mapeamento inexistentes: ' + ', '.join(sorted(unknown_columns)))
        pk = set(c.name for c in src.primary_key.columns)
        columns = []
        for col in src.columns:
            context.update(stage='Converter coluna', object=f'{key}.{col.name}')
            manual = overrides.get(key, {}).get(col.name)
            entry = dict(table=key, column=col.name, source_type=str(col.type),
                         requested_type=manual, target_type=None, automatic_type=None, error=None)
            column_mappings.append(entry)
            resolved_type = dictionary_type(col, definitions_by_name.get(col.name, {}))
            try:
                try:
                    automatic = convert_type(resolved_type)
                    entry['automatic_type'] = str(automatic.compile(dialect=mysql.dialect()))
                except ValueError:
                    if manual is None: raise
                if len(col.name) > 64: raise ValueError('nome de coluna excede o limite de 64 caracteres do MySQL')
                typ = parse_target_type(manual) if manual is not None else automatic
                entry['target_type'] = str(typ.compile(dialect=mysql.dialect()))
                columns.append(sa.Column(col.name, typ, nullable=False if col.name in pk else col.nullable,
                                         primary_key=col.name in pk, autoincrement=False,
                                         info={'manual_type': manual is not None}))
                if manual is not None:
                    warnings.append(f'{key}.{col.name}: tipo escolhido manualmente: {entry["target_type"]}. Revise a compatibilidade dos valores; a escolha não transforma os dados de origem.')
                if any(value is not None for value in (col.default, col.server_default, col.computed, col.identity)):
                    warnings.append(f'{key}.{col.name}: valor copiado; default, cálculo ou identity não recriados.')
                if type(resolved_type).__name__.upper() in ('DATETIME2', 'TIME'):
                    warnings.append(f'{key}.{col.name}: MySQL preserva até 6 casas de segundo; SQL Server pode ter 7.')
            except ValueError as exc:
                entry['error'] = str(exc)
                errors.append(f'{key}.{col.name}: {exc}')
        dst = sa.Table(target_name(item['schema'], item['name']), destination, *columns, mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')
        pairs.append((src, dst))
    names = [dst.name.casefold() for _, dst in pairs]
    if len(names) != len(set(names)): errors.append('Colisão de nomes no MySQL; renomeie as tabelas de origem.')
    mapping = {(s.schema, s.name): d for s, d in pairs}
    inspector = sa.inspect(engine)
    for src, dst in pairs:
        context.update(stage='Ler índices', object=src.fullname)
        for index in inspector.get_indexes(src.name, schema=src.schema):
            cols = index.get('column_names', [])
            options = index.get('dialect_options') or {}
            special = any(options.get(k) is not None for k in ('mssql_where',)) or bool(options.get('mssql_include') or index.get('include_columns') or options.get('mssql_columnstore'))
            if special or not cols or any(c not in dst.c for c in cols):
                warnings.append(f'{src.fullname}: índice {index["name"]} exige revisão.'); continue
            sa.Index(target_name(dst.name, index['name']), *[dst.c[c] for c in cols], unique=index.get('unique', False))
        context.update(stage='Ler chaves estrangeiras', object=src.fullname)
        for fk in inspector.get_foreign_keys(src.name, schema=src.schema):
            ref = mapping.get((fk.get('referred_schema') or src.schema, fk['referred_table']))
            if ref is None:
                warnings.append(f'{src.fullname}: FK {fk["name"]} omitida; tabela referenciada fora da seleção.'); continue
            if any(c not in dst.c for c in fk['constrained_columns']) or any(c not in ref.c for c in fk['referred_columns']): continue
            options = fk.get('options') or {}
            if any((options.get(k) or '').upper() == 'SET DEFAULT' for k in ('ondelete', 'onupdate')):
                errors.append(f'{src.fullname}: FK {fk["name"]} usa SET DEFAULT, não suportado no MySQL.'); continue
            dst.append_constraint(sa.ForeignKeyConstraint([dst.c[c] for c in fk['constrained_columns']], [ref.c[c] for c in fk['referred_columns']], name=target_name(dst.name, fk['name'] or 'fk'), use_alter=True, ondelete=options.get('ondelete'), onupdate=options.get('onupdate')))
        # SQLAlchemy's MSSQL dialect exposes UNIQUEs through get_indexes,
        # and does not implement get_unique_constraints/get_check_constraints.
        context.update(stage='Ler restrições UNIQUE', object=src.fullname)
        try:
            uniques = inspector.get_unique_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            uniques = []
        indexed_unique = {tuple(c.name for c in idx.columns) for idx in dst.indexes if idx.unique}
        for unique in uniques:
            cols = unique.get('column_names', [])
            if cols and tuple(cols) not in indexed_unique and all(c in dst.c for c in cols):
                dst.append_constraint(sa.UniqueConstraint(*cols, name=target_name(dst.name, unique['name'] or 'uq')))
        context.update(stage='Ler restrições CHECK', object=src.fullname)
        try:
            checks = inspector.get_check_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            checks = []  # CHECK definitions are included in the sys.objects export.
        if checks: warnings.append(f'{src.fullname}: CHECK constraints exigem adaptação manual.')
    context.update(stage='Checar limites e adaptar DDL para MySQL', object=None)
    compatibility = check_and_adapt(pairs, limits)
    for entry in column_mappings:
        dst = mapping.get((available[entry['table']]['schema'], available[entry['table']]['name']))
        if entry['error'] is None and dst is not None and entry['column'] in dst.c:
            entry['target_type'] = str(dst.c[entry['column']].type.compile(dialect=mysql.dialect()))
    compatibility['source_dictionary'] = dictionary
    compatibility['manual_mappings'] = overrides
    errors.extend(compatibility['errors'])
    for change in compatibility['changes']:
        warnings.append(f"{change['table']}.{change['column']}: {change['before']} → {change['after']} automaticamente. {change['reason']}")
    warnings.append('DATE/DATETIME: SQL Server pode conter anos anteriores a 1000 que não cabem no intervalo documentado do MySQL. O dicionário não valida os valores; datas não são reescritas automaticamente.')
    warnings.append('Identity, defaults, expressões calculadas e CHECKs não são recriados. PKs, índices comuns, UNIQUEs e FKs entre tabelas selecionadas são incluídos. Revise diferenças de UNIQUE com NULL, collation e limites de índices do MySQL.')
    warnings.append('Collation utf8mb4_bin: ordenação e comparação podem diferir da origem. Revise chaves únicas, texto e regras de negócio.')
    warnings.append('Views, procedures, funções, triggers, sequences e synonyms são exportados para revisão; não executados automaticamente.')
    dialect = mysql.dialect()
    statements = []
    def compile_ddl(element, stage, name):
        context.update(stage=stage, object=name)
        try:
            statements.append(str(element.compile(dialect=dialect))+';')
        except sa.exc.CompileError as exc:
            detail = diagnostic(exc, stage, name)
            errors.append(f"{name}: {stage}: {detail['message']}")
    for src, d in pairs:
        compile_ddl(CreateTable(d), 'Compilar CREATE TABLE para MySQL', src.fullname)
    for src, d in pairs:
        for index in sorted(d.indexes, key=lambda i: i.name):
            compile_ddl(CreateIndex(index), 'Compilar índice para MySQL', index.name)
        for fk in sorted(d.foreign_key_constraints, key=lambda f: f.name):
            compile_ddl(AddConstraint(fk), 'Compilar FK para MySQL', fk.name)
    return {'column_mappings': column_mappings, 'manual_mappings': overrides, 'compatibility': compatibility, 'pairs': pairs, 'ddl': '\n\n'.join(statements), 'warnings': warnings, 'errors': errors, 'objects': catalog['objects']}


class MigrationError(Exception):
    pass


def destination_objects(target):
    inspector = sa.inspect(target)
    return {name.casefold() for name in inspector.get_table_names() + inspector.get_view_names()}


def _create_object(target, element, kind, table, name):
    """Return server warnings and metadata verification after committed DDL."""
    result = {'message': 'DDL executado; COMMIT confirmado.', 'warnings': []}
    with target.connect() as conn:
        conn.execute(element)
        if target.dialect.name == 'mysql':
            # SHOW WARNINGS must immediately follow the DDL on the same session.
            result['warnings'] = [diagnostic(RuntimeError(int(r[1]), r[2]), 'Aviso MySQL', name,
                                            (target,), hide_values=True) for r in conn.exec_driver_sql('SHOW WARNINGS').fetchall()]
        conn.commit()
        inspector = sa.inspect(conn)
        if kind == 'table':
            found = inspector.has_table(table.name)
            if found and target.dialect.name == 'mysql':
                quoted = conn.dialect.identifier_preparer.quote(table.name)
                result['mysql_ddl'] = str(conn.exec_driver_sql(f'SHOW CREATE TABLE {quoted}').one()[1])
        elif kind == 'index':
            found = any(item['name'] == name for item in inspector.get_indexes(table.name))
        else:
            found = any(item['name'] == name for item in inspector.get_foreign_keys(table.name))
        if found and target.dialect.name == 'mysql' and kind != 'table':
            quoted = conn.dialect.identifier_preparer.quote(table.name)
            result['mysql_ddl'] = str(conn.exec_driver_sql(f'SHOW CREATE TABLE {quoted}').one()[1])
        if not found: raise MigrationError('DDL confirmado, mas o objeto não foi encontrado no catálogo. Inspecione o destino antes de repetir.')
    result['verified'] = True
    result['message'] += ' Objeto confirmado no catálogo do destino.'
    return result


def execute(source, target, prepared, job, batch, existing_policy='abort', continue_on_error=False, options=None, control=None):
    """Never modifies existing tables. Independent failures may be isolated."""
    options = options or {}
    control = control or TransferControl(batch)
    job.update(status='running', rows=0, rows_in_flight=0, logs=[], tables=[], events=[],
               options={'existing_policy': existing_policy, 'continue_on_error': continue_on_error, 'batch': batch, **options},
               table_progress=[], paused_seconds=0.,
               started_at=datetime.now(timezone.utc).isoformat())
    pairs = prepared['pairs']
    created, loaded = set(), set()

    def record(kind, name, table, status, message, **extra):
        entry = dict(kind=kind, object=name, table=table, status=status, message=message,
                     at=datetime.now(timezone.utc).isoformat(), **extra)
        job['events'].append(entry)
        job['logs'].append(f'{status.upper()} · {kind} · {name}: {message}')
        return entry

    def failure(exc, kind, name, table):
        detail = diagnostic(exc, job['stage'], name, (source, target), hide_values=True)
        record(kind, name, table, 'failed', detail['message'], diagnostic=detail)
        if not continue_on_error: raise MigrationError(f"{kind} {name}: {detail['category']}. {detail['hint']}") from exc

    try:
        control.checkpoint(job)
        if existing_policy not in ('abort', 'skip'): raise MigrationError('Política de objetos existentes inválida.')
        if prepared['errors']: raise MigrationError('Plano contém incompatibilidades bloqueantes; revise antes de executar.')
        job['stage'] = 'Verificar objetos existentes no destino'
        existing = destination_objects(target)
        if existing_policy == 'abort' and any(d.name.casefold() in existing for _, d in pairs):
            for _, d in pairs:
                if d.name.casefold() in existing:
                    record('table', d.name, d.name, 'failed', 'Já existe no destino. A política escolhida exige interromper antes de criar objetos.')
            raise MigrationError('Tabela de destino já existe. Escolha pular tabelas existentes ou use outro banco.')
        for _, dst in pairs:
            control.checkpoint(job)
            activity(job, f'Criando/verificando tabela {dst.name}')
            if dst.name.casefold() in existing:
                record('table', dst.name, dst.name, 'skipped', 'Já existe no destino; estrutura, dados, índices e FKs preservados. Conteúdo não validado.')
                continue
            job['stage'] = f'Criar tabela {dst.name}'
            try:
                result = _create_object(target, CreateTable(dst), 'table', dst, dst.name)
                created.add(dst.name)
                record('table', dst.name, dst.name, 'created', result.pop('message'), **result)
            except Exception as exc:
                original = getattr(exc, 'orig', exc)
                args = getattr(original, 'args', ())
                if existing_policy == 'skip' and args and args[0] == 1050:
                    record('table', dst.name, dst.name, 'skipped', 'Objeto passou a existir após a inspeção. Preservado sem carga nem alterações.')
                else:
                    failure(exc, 'table', dst.name, dst.name)
        for src, dst in pairs:
            control.checkpoint(job)
            if dst.name not in created:
                record('data', dst.name, dst.name, 'skipped', 'Carga omitida: tabela não criada e verificada nesta execução.')
                continue
            job['stage'] = f'Copiar dados de {src.fullname}'
            try:
                result = copy_table(source, target, src, dst, job, batch, options, control)
                loaded.add(dst.name)
                job['tables'].append(result)
                message = f"{result['copied']} registros confirmados. " + (f"Contagem no destino: {result['target_count']}." if result['target_count'] is not None else 'COUNT(*) dispensado por opção; validação limitada aos COMMITs.')
                record('data', dst.name, dst.name, 'copied', message, rows=result['copied'])
            except MigrationCancelled:
                record('data', dst.name, dst.name, 'cancelled', 'Carga cancelada; transação pendente revertida. COMMITs anteriores, se houver, permanecem. Confira métricas e destino.')
                raise
            except Exception as exc: failure(exc, 'data', dst.name, dst.name)
            finally: job['rows_in_flight'] = 0
        for _, dst in pairs:
            for index in sorted(dst.indexes, key=lambda i: i.name):
                control.checkpoint(job)
                activity(job, f'Criando/verificando índice {index.name}')
                if dst.name not in loaded:
                    record('index', index.name, dst.name, 'skipped', 'Tabela sem carga confirmada nesta execução.'); continue
                job['stage'] = f'Criar índice {index.name}'
                try:
                    result = _create_object(target, CreateIndex(index), 'index', dst, index.name)
                    record('index', index.name, dst.name, 'created', result.pop('message'), **result)
                except Exception as exc: failure(exc, 'index', index.name, dst.name)
        for _, dst in pairs:
            for fk in sorted(dst.foreign_key_constraints, key=lambda f: f.name):
                control.checkpoint(job)
                activity(job, f'Criando/verificando FK {fk.name}')
                references = {element.column.table.name for element in fk.elements}
                if dst.name not in loaded or not references.issubset(loaded):
                    record('foreign_key', fk.name, dst.name, 'skipped', 'FK omitida: tabela dependente ou referenciada pulada/sem carga confirmada. Revisão manual necessária.'); continue
                job['stage'] = f'Criar chave estrangeira {fk.name}'
                try:
                    result = _create_object(target, AddConstraint(fk), 'foreign_key', dst, fk.name)
                    record('foreign_key', fk.name, dst.name, 'created', result.pop('message'), **result)
                except Exception as exc: failure(exc, 'foreign_key', fk.name, dst.name)
        has_errors = any(e['status'] == 'failed' for e in job['events'])
        has_warnings = any(e['status'] == 'skipped' or e.get('warnings') for e in job['events'])
        status = 'completed_with_errors' if has_errors else 'completed_with_warnings' if has_warnings else 'completed'
        with control.condition:
            if control.cancelled: raise MigrationCancelled('Cancelamento solicitado.')
            job.update(status=status, stage='Processamento encerrado. Confira resultados por objeto e pendências manuais.')
    except MigrationCancelled:
        with control.condition: job.update(status='cancelled', stage='Cancelado. Transações anteriores permanecem; revise tabelas parciais antes de repetir.')
    except Exception as exc:
        detail = diagnostic(exc, job.get('stage', 'Preparação'), None, (source, target), hide_values=True)
        if not any(e['status'] == 'failed' for e in job['events']):
            record('execution', job.get('stage', 'Preparação'), '', 'failed', detail['message'], diagnostic=detail)
        with control.condition: job.update(status='failed', error=detail['message'], diagnostic=detail, stage='Migração interrompida; confira o relatório.')
    finally:
        if job['status'] in ('failed', 'cancelled'):
            attempted = {(e['kind'], e['table'], e['object']) for e in job['events']}
            for _, dst in pairs:
                pending = [('table', dst.name), ('data', dst.name)] + [('index', i.name) for i in dst.indexes] + [('foreign_key', f.name) for f in dst.foreign_key_constraints]
                for kind, name in pending:
                    if (kind, dst.name, name) not in attempted:
                        record(kind, name, dst.name, 'skipped', 'Não executado: processamento interrompido. Verifique o destino antes de repetir.')
        job['summary'] = {status: sum(e['status'] == status for e in job['events']) for status in ('created', 'copied', 'skipped', 'failed', 'cancelled') if status != 'cancelled' or any(e['status'] == 'cancelled' for e in job['events'])}
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
