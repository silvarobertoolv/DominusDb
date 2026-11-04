"""SQL Server inventory and conservative MySQL migration planner."""
from diagnostics import diagnostic
from type_mapping import parse_target_type, validate_overrides
from transfer import copy_table, TransferControl, MigrationCancelled, activity
from compatibility import source_dictionary
from source_catalog import source_inspector, reflect_source_table
from target_profiles import destination_kind, destination_schema, dialect_for, identifier, table_identifier, column_identifier, convert_native_type, target_limits, check_and_adapt, KINDS
from datetime import datetime, timezone
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, mssql
from sqlalchemy.schema import CreateTable, AddConstraint, CreateIndex


def target_name(schema, name):
    return table_identifier(name, 'mysql')

def convert_type(t, kind="mysql"):
    if kind != "mysql": return convert_native_type(t, kind)
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
    inspector = source_inspector(engine)
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


def plan(engine, selected, target=None, overrides=None, target_kind=None, table_name_case='preserve', column_name_case='preserve', column_naming='preserve', nullable_overrides=None, oracle_nullable_text=False, foreign_key_policy='compatible'):
    context = {'stage': 'Ler catálogo', 'object': None}
    try:
        return _plan(engine, selected, context, target, validate_overrides(overrides), target_kind, table_name_case, column_name_case, column_naming, nullable_overrides, oracle_nullable_text, foreign_key_policy)
    except Exception as exc:
        raise PlanError(context['stage'], context['object'], exc) from exc


def _plan(engine, selected, context, target=None, overrides=None, target_kind=None, table_name_case='preserve', column_name_case='preserve', column_naming='preserve', nullable_overrides=None, oracle_nullable_text=False, foreign_key_policy='compatible'):
    context.update(stage='Validar padrão dos nomes de tabelas', object=None)
    if table_name_case not in ('preserve', 'upper'):
        raise ValueError('Padrão dos nomes de tabelas inválido. Escolha preservar origem ou maiúsculas.')
    if column_name_case not in ('preserve', 'upper') or column_naming not in ('preserve', 'normalize'):
        raise ValueError('Padrão dos nomes de colunas inválido.')
    if foreign_key_policy not in ('compatible','strict','omit'): raise ValueError('Política de constraints/FKs inválida.')
    if not isinstance(oracle_nullable_text, bool): raise ValueError('Opção Oracle de nulabilidade textual deve ser booleana.')
    if nullable_overrides is None: nullable_overrides = {}
    if not isinstance(nullable_overrides, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(c, str) for c in v) for k,v in nullable_overrides.items()):
        raise ValueError('Ajustes de nulabilidade inválidos; informe tabela e lista de colunas.')
    def case_name(name): return name.upper() if table_name_case == 'upper' else name
    kind = destination_kind(target, target_kind)
    if kind != 'oracle' and (oracle_nullable_text or any(nullable_overrides.values())): raise ValueError('Ajuste de nulabilidade disponível somente para Oracle.')
    label = KINDS[kind]
    dialect = dialect_for(kind)
    schema = destination_schema(target, kind)
    def named(a, b): return identifier(a, b, kind)
    context.update(stage='Ler catálogo', object=None)
    catalog = inventory(engine)
    available = {t['id']: t for t in catalog['tables']}
    if not selected or len(selected) != len(set(selected)) or any(x not in available for x in selected):
        raise ValueError('Selecione tabelas válidas, sem duplicatas.')
    if set(nullable_overrides) - set(selected): raise ValueError('Ajuste de nulabilidade aponta para tabela fora da seleção.')
    limits, limit_warnings = target_limits(target, kind)
    context.update(stage='Definir nomes das tabelas de destino', object=None)
    table_names, owners = {}, {}
    for key in selected:
        name = case_name(table_identifier(case_name(available[key]['name']), kind, 64 if kind == 'mysql' else limits['identifier_bytes']))
        previous = owners.get(name.casefold())
        if previous is not None:
            raise ValueError(f'Colisão de nomes em {label}: {previous} e {key} resultam em {name}. '
                             'O schema de origem não compõe o nome no destino. Renomeie uma das tabelas de origem ou migre-as para destinos separados.')
        owners[name.casefold()] = key
        table_names[key] = name
    overrides = overrides or {}
    unknown = set(overrides) - set(selected)
    if unknown: raise ValueError('Mapeamento aponta para tabela fora da seleção: ' + ', '.join(sorted(unknown)))
    column_mappings = []
    constraint_report = []
    source = sa.MetaData()
    destination = sa.MetaData()
    pairs, warnings, errors = [], [], []
    dictionary = []
    warnings.extend(limit_warnings)
    for key, name in table_names.items():
        if name != case_name(available[key]['name']):
            warnings.append(f'{key}: nome da tabela abreviado para {name} para respeitar o limite do perfil {label}; sufixo hash mantém a abreviação estável.')
    for key in selected:
        item = available[key]
        context.update(stage='Refletir colunas, PKs e relacionamentos', object=key)
        src = reflect_source_table(engine, item['name'], item['schema'], source, dictionary_type)
        if src.info.get('reflection_mode') == 'catalog':
            warnings.append(f'{key}: metadados lidos pelo catálogo compatível sys.*; comentários estendidos não são refletidos. PKs, índices e relacionamentos continuam sendo lidos.')
            if src.info.get('reflection_fallback_code'):
                warnings.append(f'{key}: reflexão SQLAlchemy falhou com sintaxe {src.info["reflection_fallback_code"]}; leitura recuperada automaticamente pelo catálogo compatível.')
        context.update(stage='Consultar dicionário sys.columns/sys.types', object=key)
        definitions = src.info.get('source_dictionary')
        if definitions is None: definitions = source_dictionary(engine, src)
        dictionary.append({'table': key, 'columns': definitions})
        if engine.dialect.name == 'mssql' and len(definitions) != len(src.columns):
            errors.append(f'{key}: dicionário incompleto ou estrutura alterada durante a leitura. Confira permissões de metadados e gere o plano novamente.')
        definitions_by_name = {d['column_name']: d for d in definitions}
        unknown_columns = set(overrides.get(key, {})) - set(src.c.keys())
        if unknown_columns: errors.append(f'{key}: colunas de mapeamento inexistentes: ' + ', '.join(sorted(unknown_columns)))
        allow_null = set(nullable_overrides.get(key, []))
        if allow_null - set(src.c.keys()): errors.append(f'{key}: ajuste de nulabilidade aponta para coluna inexistente.')
        pk = set(c.name for c in src.primary_key.columns)
        columns = []
        used_columns = {}
        for col in src.columns:
            context.update(stage='Converter coluna', object=f'{key}.{col.name}')
            manual = overrides.get(key, {}).get(col.name)
            entry = dict(table=key, column=col.name, source_type=str(col.type),
                         requested_type=manual, target_type=None, automatic_type=None, error=None,
                         source_nullable=col.nullable, primary_key=col.name in pk,
                         allow_null=col.name in allow_null)
            column_mappings.append(entry)
            resolved_type = dictionary_type(col, definitions_by_name.get(col.name, {}))
            try:
                name_limit = 64 if kind == 'mysql' else limits['identifier_bytes']
                final_name = column_identifier(col.name, kind, name_limit, column_name_case, column_naming)
                entry['target_name'] = final_name
                name_length = len(final_name) if kind == 'mysql' else len(final_name.encode('utf-8'))
                if name_length > name_limit: raise ValueError(f'nome da coluna excede o limite de {name_limit} do perfil {label}; escolha normalizar para abreviar')
                if final_name.casefold() in used_columns: raise ValueError(f'colisão de nomes: {used_columns[final_name.casefold()]} e {col.name} resultam em {final_name}')
                used_columns[final_name.casefold()] = col.name
                if col.name in allow_null and col.name in pk: raise ValueError('chave primária não pode ser alterada para aceitar NULL')
                try:
                    automatic = convert_type(resolved_type, kind)
                    entry['automatic_type'] = str(automatic.compile(dialect=dialect))
                except ValueError:
                    if manual is None: raise
                if kind == 'postgresql' and final_name in ('ctid','xmin','xmax','cmin','cmax','tableoid'): raise ValueError('nome conflita com coluna de sistema PostgreSQL; renomeação da origem necessária')
                if kind == 'oracle' and final_name == 'ROWID': raise ValueError('ROWID em maiúsculas é reservado pelo Oracle; renomeação da origem necessária')
                typ = parse_target_type(manual, kind) if manual is not None else automatic
                entry['nullable_policy_applied'] = bool(oracle_nullable_text and col.name not in pk and (isinstance(typ, sa.String) or type(typ).__name__.upper() in ('CLOB','NCLOB')))
                if entry['nullable_policy_applied']: allow_null.add(col.name)
                entry['allow_null'] = col.name in allow_null
                entry['target_type'] = str(typ.compile(dialect=dialect))
                entry['target_nullable'] = False if col.name in pk else True if col.name in allow_null else col.nullable
                # Keep the source key for binding rows and resolving constraints;
                # the SQL name is independent and escaped by the dialect.
                columns.append(sa.Column(final_name, typ, key=col.name, quote=True if kind == 'oracle' and column_naming == 'preserve' else None,
                                         nullable=entry['target_nullable'],
                                         autoincrement=False,
                                         info={'manual_type': manual is not None}))
                if col.name in allow_null and not col.nullable:
                    warnings.append(f'{key}.{col.name}: restrição NOT NULL removida SOMENTE no destino por escolha explícita. Com a política Oracle de texto vazio para NULL, valores vazios serão nulos; revise regras de negócio e UNIQUEs.')
                if manual is not None:
                    warnings.append(f'{key}.{col.name}: tipo escolhido manualmente: {entry["target_type"]}. Revise a compatibilidade dos valores; a escolha não transforma os dados de origem.')
                if any(value is not None for value in (col.default, col.server_default, col.computed, col.identity)):
                    warnings.append(f'{key}.{col.name}: valor copiado; default, cálculo ou identity não recriados.')
                if type(resolved_type).__name__.upper() == 'DATETIMEOFFSET':
                    warnings.append(f'{key}.{col.name}: fuso mantido no tipo nativo do destino quando suportado; valide retorno do driver e precisão temporal.')
                if type(resolved_type).__name__.upper() in ('DATETIME2', 'TIME', 'DATETIMEOFFSET'):
                    warnings.append(f'{key}.{col.name}: {label} neste perfil preserva até 6 casas de segundo; SQL Server pode ter 7.')
            except ValueError as exc:
                entry['error'] = str(exc)
                errors.append(f'{key}.{col.name}: {exc}')
        table_options = dict(mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin') if kind == 'mysql' else dict(schema=schema)
        dst = sa.Table(table_names[key], destination, *columns, **table_options)
        pk_order=[c.name for c in src.primary_key.columns]
        if pk_order and all(name in dst.c for name in pk_order): dst.append_constraint(sa.PrimaryKeyConstraint(*pk_order))
        pairs.append((src, dst))
    names = [dst.name.casefold() for _, dst in pairs]
    if len(names) != len(set(names)): errors.append(f'Colisão de nomes em {label}; renomeie as tabelas de origem.')
    mapping = {(s.schema, s.name): d for s, d in pairs}
    inspector = source_inspector(engine)
    for src, dst in pairs:
        context.update(stage='Ler índices', object=src.fullname)
        for index in inspector.get_indexes(src.name, schema=src.schema):
            cols = index.get('column_names', [])
            options = index.get('dialect_options') or {}
            special = any(options.get(k) is not None for k in ('mssql_where',)) or bool(options.get('mssql_include') or index.get('include_columns') or options.get('mssql_columnstore'))
            if special or not cols or any(c not in dst.c for c in cols):
                warnings.append(f'{src.fullname}: índice {index["name"]} exige revisão.'); continue
            if kind == 'oracle' and index.get('unique', False):
                # Oracle FKs require a PK/UNIQUE constraint, not only a unique index.
                dst.append_constraint(sa.UniqueConstraint(*cols, name=named(dst.name,index['name'])))
            else:
                sa.Index(named(dst.name, index['name']), *[dst.c[c] for c in cols], unique=index.get('unique', False))
        context.update(stage='Ler chaves estrangeiras', object=src.fullname)
        for fk in inspector.get_foreign_keys(src.name, schema=src.schema):
            entry = dict(kind='FOREIGN KEY', table=src.fullname, source_name=fk['name'], target_table=dst.name,
                         source_columns=fk['constrained_columns'], referred_table=(fk.get('referred_schema') or src.schema)+'.'+fk['referred_table'],
                         status='planned', reason=None)
            constraint_report.append(entry)
            if foreign_key_policy == 'omit':
                entry.update(status='omitted',reason='Omitida por escolha explícita no plano.')
                continue
            ref = mapping.get((fk.get('referred_schema') or src.schema, fk['referred_table']))
            if ref is None:
                message = f'{src.fullname}: FK {fk["name"]} omitida; tabela referenciada fora da seleção. Selecione também {entry["referred_table"]}.'
                entry.update(status='blocked' if foreign_key_policy=='strict' else 'omitted',reason=message)
                (errors if foreign_key_policy=='strict' else warnings).append(message)
                continue
            if any(c not in dst.c for c in fk['constrained_columns']) or any(c not in ref.c for c in fk['referred_columns']):
                message=f'{src.fullname}: FK {fk["name"]} depende de coluna sem mapeamento válido.'
                errors.append(message);entry.update(status='blocked',reason=message);continue
            options = fk.get('options') or {}
            entry.update(target_name=named(dst.name, fk['name'] or 'fk'), target_columns=[dst.c[c].name for c in fk['constrained_columns']], referred_columns=[ref.c[c].name for c in fk['referred_columns']], ondelete=options.get('ondelete'), onupdate=options.get('onupdate'))
            if any((options.get(k) or '').upper() == 'SET DEFAULT' for k in ('ondelete', 'onupdate')):
                entry.update(status='blocked',reason='SET DEFAULT exige adaptação dos defaults.')
                errors.append(f'{src.fullname}: FK {fk["name"]} usa SET DEFAULT; defaults não são recriados automaticamente no perfil {label}. Revisão manual necessária.'); continue
            if kind == 'oracle':
                if options.get('onupdate') and options['onupdate'].upper() != 'NO ACTION':
                    entry.update(status='blocked',reason='Oracle não suporta a ação ON UPDATE da origem.')
                    errors.append(f'{src.fullname}: Oracle não suporta ON UPDATE {options["onupdate"]} na FK {fk["name"]}.'); continue
                if options.get('ondelete') and options['ondelete'].upper() not in ('NO ACTION','CASCADE','SET NULL'):
                    entry.update(status='blocked',reason='Ação ON DELETE incompatível com Oracle.')
                    errors.append(f'{src.fullname}: ação ON DELETE incompatível com Oracle.'); continue
                delete_action=(options.get('ondelete') or '').upper()
                options = dict(options, onupdate=None, ondelete=delete_action if delete_action not in ('','NO ACTION') else None)
            dst.append_constraint(sa.ForeignKeyConstraint([dst.c[c] for c in fk['constrained_columns']], [ref.c[c] for c in fk['referred_columns']], name=named(dst.name, fk['name'] or 'fk'), use_alter=True, ondelete=options.get('ondelete'), onupdate=options.get('onupdate')))
        # SQLAlchemy's MSSQL dialect exposes UNIQUEs through get_indexes,
        # and does not implement get_unique_constraints/get_check_constraints.
        context.update(stage='Ler restrições UNIQUE', object=src.fullname)
        try:
            uniques = inspector.get_unique_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            uniques = []
        indexed_unique = {tuple(c.key for c in idx.columns) for idx in dst.indexes if idx.unique} | {tuple(c.key for c in constraint.columns) for constraint in dst.constraints if isinstance(constraint,sa.UniqueConstraint)}
        for unique in uniques:
            cols = unique.get('column_names', [])
            if cols and tuple(cols) not in indexed_unique and all(c in dst.c for c in cols):
                dst.append_constraint(sa.UniqueConstraint(*cols, name=named(dst.name, unique['name'] or 'uq')))
        context.update(stage='Ler restrições CHECK', object=src.fullname)
        try:
            checks = inspector.get_check_constraints(src.name, schema=src.schema)
        except NotImplementedError:
            checks = inspector.catalog.get_check_constraints(src.name, schema=src.schema) if engine.dialect.name == 'mssql' else []
        constraint_report.extend(dict(kind='CHECK',table=src.fullname,source_name=c.get('name'),target_table=dst.name,status='manual',reason='Expressão T-SQL exige adaptação manual.',definition=c.get('sqltext')) for c in checks)
        if checks: warnings.append(f'{src.fullname}: CHECK constraints exigem adaptação manual.')
    context.update(stage=f'Checar limites e adaptar DDL para {label}', object=None)
    compatibility = check_and_adapt(pairs, limits)
    for entry in column_mappings:
        dst = mapping.get((available[entry['table']]['schema'], available[entry['table']]['name']))
        if entry['error'] is None and dst is not None and entry['column'] in dst.c:
            entry['target_type'] = str(dst.c[entry['column']].type.compile(dialect=dialect))
    for src,dst in pairs:
        if len(dst.primary_key.columns):
            constraint_report.append(dict(kind='PRIMARY KEY',table=src.fullname,target_table=dst.name,status='planned',target_name=dst.primary_key.name,target_columns=[c.name for c in dst.primary_key.columns],phase='create_table'))
        constraint_report.extend(dict(kind='UNIQUE',table=src.fullname,target_table=dst.name,status='planned',target_name=c.name,target_columns=[col.name for col in c.columns],phase='create_table') for c in dst.constraints if isinstance(c,sa.UniqueConstraint))
        constraint_report.extend(dict(kind='UNIQUE INDEX',table=src.fullname,target_table=dst.name,status='planned',target_name=i.name,target_columns=[c.name for c in i.columns],phase='after_load') for i in dst.indexes if i.unique)
    compatibility['constraints'] = dict(foreign_key_policy=foreign_key_policy, objects=constraint_report,
        order=['Tabelas com PKs/UNIQUEs e NOT NULL efetivo','Carga de todas as tabelas','Índices, inclusive UNIQUEs','FKs com validação dos dados existentes'],
        note='CHECKs e defaults exigem adaptação manual. Alterações explícitas de nulabilidade continuam válidas.')
    if foreign_key_policy=='omit': warnings.append('FKs omitidas por escolha explícita. A integridade referencial não será imposta por esta migração; PKs e UNIQUEs continuam incluídas.')
    compatibility['target_kind'] = kind
    compatibility['target_schema'] = schema
    compatibility['table_name_case'] = table_name_case
    compatibility['column_name_case'] = column_name_case
    compatibility['column_naming'] = column_naming
    compatibility['nullable_overrides'] = nullable_overrides
    compatibility['oracle_nullable_text'] = oracle_nullable_text
    compatibility['nullability_changes'] = [dict(table=e['table'], source_column=e['column'], target_column=e['target_name'], before='NOT NULL', after='NULL', reason='oracle_nullable_text' if e.get('nullable_policy_applied') else 'manual') for e in column_mappings if e.get('target_nullable') is True and e.get('source_nullable') is False and not e['error']]
    compatibility['column_names'] = [{'table': src.fullname, 'source': c.key, 'target': c.name, 'nullable': c.nullable} for src,dst in pairs for c in dst.columns]
    compatibility['table_names'] = [{'source': src.fullname, 'target': dst.name, 'target_schema': dst.schema,
                                     'abbreviated': case_name(src.name) != dst.name} for src, dst in pairs]
    warnings.extend(compatibility.get('warnings', []))
    compatibility['source_dictionary'] = dictionary
    compatibility['source_reflection'] = [{'table': src.fullname, 'mode': src.info.get('reflection_mode','sqlalchemy'), 'fallback_code': src.info.get('reflection_fallback_code')} for src,_ in pairs]
    compatibility['manual_mappings'] = overrides
    errors.extend(compatibility['errors'])
    for change in compatibility['changes']:
        warnings.append(f"{change['table']}.{change['column']}: {change['before']} → {change['after']} automaticamente. {change['reason']}")
    if kind == 'mysql': warnings.append('DATE/DATETIME: SQL Server pode conter anos anteriores a 1000 que não cabem no intervalo documentado do MySQL. O dicionário não valida os valores; datas não são reescritas automaticamente.')
    warnings.append('Identity, defaults, expressões calculadas e CHECKs não são recriados. PKs, índices comuns e UNIQUEs são incluídos; FKs obedecem à política escolhida no plano. Revise diferenças de UNIQUE com NULL, collation e limites de índices do destino.')
    if kind == 'mysql': warnings.append('Collation utf8mb4_bin: ordenação e comparação podem diferir da origem. Revise chaves únicas, texto e regras de negócio.')
    warnings.append('Views, procedures, funções, triggers, sequences e synonyms são exportados para revisão; não executados automaticamente.')
    statements = []
    def compile_ddl(element, stage, name):
        context.update(stage=stage, object=name)
        try:
            statements.append(str(element.compile(dialect=dialect))+';')
        except sa.exc.CompileError as exc:
            detail = diagnostic(exc, stage, name)
            errors.append(f"{name}: {stage}: {detail['message']}")
    for src, d in pairs:
        compile_ddl(CreateTable(d), f'Compilar CREATE TABLE para {label}', src.fullname)
    for src, d in pairs:
        for index in sorted(d.indexes, key=lambda i: i.name):
            compile_ddl(CreateIndex(index), f'Compilar índice para {label}', index.name)
        for fk in sorted(d.foreign_key_constraints, key=lambda f: f.name):
            compile_ddl(AddConstraint(fk), f'Compilar FK para {label}', fk.name)
    return {'foreign_key_policy': foreign_key_policy, 'target_kind': kind, 'target_schema': schema, 'table_name_case': table_name_case, 'column_mappings': column_mappings, 'manual_mappings': overrides, 'compatibility': compatibility, 'pairs': pairs, 'ddl': '\n\n'.join(statements), 'warnings': warnings, 'errors': errors, 'objects': catalog['objects']}


class MigrationError(Exception):
    pass


def destination_objects(target):
    inspector = sa.inspect(target)
    return {name.casefold() for name in inspector.get_table_names(schema=destination_schema(target,destination_kind(target))) + inspector.get_view_names(schema=destination_schema(target,destination_kind(target)))}


def _create_object(target, element, kind, table, name):
    context = dict(stage='Executar DDL no destino', committed=False)
    try:
        return _create_object_impl(target, element, kind, table, name, context)
    except Exception as exc:
        exc.migration_ddl_stage = context['stage']
        exc.migration_ddl_committed = context['committed']
        try: exc.migration_ddl = str(element.compile(dialect=target.dialect))
        except Exception: exc.migration_ddl = None
        raise


def _create_object_impl(target, element, kind, table, name, context):
    """Return server warnings and metadata verification after committed DDL."""
    result = {'message': 'DDL executado; COMMIT confirmado.', 'warnings': []}
    with target.connect() as conn:
        if kind == 'foreign_key' and target.dialect.name == 'mysql':
            conn.exec_driver_sql('SET SESSION foreign_key_checks=1')
            if conn.exec_driver_sql('SELECT @@SESSION.foreign_key_checks').scalar_one() != 1:
                raise MigrationError('Validação de FKs não habilitada nesta sessão MySQL.')
        conn.execute(element)
        if target.dialect.name == 'mysql':
            # SHOW WARNINGS must immediately follow the DDL on the same session.
            result['warnings'] = [diagnostic(RuntimeError(int(r[1]), r[2]), 'Aviso MySQL', name,
                                            (target,), hide_values=True) for r in conn.exec_driver_sql('SHOW WARNINGS').fetchall()]
        context['stage'] = 'Confirmar COMMIT da DDL no destino'
        conn.commit()
        context.update(stage='Verificar metadados após COMMIT da DDL', committed=True)
        inspector = sa.inspect(conn)
        if kind == 'table':
            found = inspector.has_table(table.name, **({'schema': table.schema} if table.schema else {}))
            if found and target.dialect.name == 'mysql':
                quoted = conn.dialect.identifier_preparer.quote(table.name)
                result['mysql_ddl'] = str(conn.exec_driver_sql(f'SHOW CREATE TABLE {quoted}').one()[1])
        elif kind == 'index':
            found = any(item['name'] == name for item in inspector.get_indexes(table.name, **({'schema': table.schema} if table.schema else {})))
        else:
            found = any(item['name'] == name for item in inspector.get_foreign_keys(table.name, **({'schema': table.schema} if table.schema else {})))
        if found and target.dialect.name == 'mysql' and kind != 'table':
            quoted = conn.dialect.identifier_preparer.quote(table.name)
            result['mysql_ddl'] = str(conn.exec_driver_sql(f'SHOW CREATE TABLE {quoted}').one()[1])
        if found and target.dialect.name in ('postgresql','oracle'):
            result['server_metadata'] = dict(schema=table.schema, table=table.name,
                columns=[dict(name=c['name'], type=str(c['type']), nullable=c['nullable']) for c in inspector.get_columns(table.name, schema=table.schema)],
                primary_key=inspector.get_pk_constraint(table.name, schema=table.schema),
                unique_constraints=inspector.get_unique_constraints(table.name, schema=table.schema),
                indexes=[dict(name=i['name'], columns=i.get('column_names'), unique=i.get('unique')) for i in inspector.get_indexes(table.name, schema=table.schema)],
                foreign_keys=[dict(name=f['name'], columns=f['constrained_columns'], referred_table=f['referred_table']) for f in inspector.get_foreign_keys(table.name, schema=table.schema)])
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
    foreign_key_policy = prepared.get('foreign_key_policy', 'compatible')
    planned_fk_count = sum(len(dst.foreign_key_constraints) for _,dst in pairs)
    constraint_objects = (prepared.get('compatibility') or {}).get('constraints', {}).get('objects', [])
    omitted_fk_count = sum(c.get('kind')=='FOREIGN KEY' and c.get('status') in ('omitted','blocked') for c in constraint_objects)
    job['referential_integrity'] = dict(policy=foreign_key_policy, planned=planned_fk_count,
        created=0, failed=0, skipped=0, not_planned=omitted_fk_count, status='pending')
    created, loaded = set(), set()

    def record(kind, name, table, status, message, **extra):
        entry = dict(kind=kind, object=name, table=table, status=status, message=message,
                     at=datetime.now(timezone.utc).isoformat(), **extra)
        job['events'].append(entry)
        job['logs'].append(f'{status.upper()} · {kind} · {name}: {message}')
        if kind == 'foreign_key' and status in ('created','failed','skipped'):
            current = job['referential_integrity']
            job['referential_integrity'] = {**current, status: current[status]+1}
        return entry

    def failure(exc, kind, name, table):
        detail = diagnostic(exc, getattr(exc, 'migration_ddl_stage', job['stage']), name, (source, target), hide_values=kind != 'table')
        extra = {}
        if hasattr(exc, 'migration_ddl_committed'):
            extra = dict(ddl=getattr(exc, 'migration_ddl', None), ddl_committed=exc.migration_ddl_committed)
            if exc.migration_ddl_committed:
                detail['hint'] += ' A DDL já teve COMMIT; a falha ocorreu ao verificar metadados. Inspecione o objeto antes de repetir.'
        record(kind, name, table, 'failed', detail['message'], diagnostic=detail, **extra)
        if not continue_on_error or (kind == 'foreign_key' and foreign_key_policy == 'strict'):
            raise MigrationError(f"{kind} {name}: {detail['category']}. {detail['hint']}") from exc

    try:
        control.checkpoint(job)
        if prepared.get('target_kind') and destination_kind(target) != prepared['target_kind']: raise MigrationError('O plano pertence a outro tipo de destino. Gere novamente.')
        if existing_policy not in ('abort', 'skip'): raise MigrationError('Política de objetos existentes inválida.')
        if foreign_key_policy not in ('compatible','strict','omit'): raise MigrationError('Política de constraints/FKs inválida.')
        if prepared['errors']: raise MigrationError('Plano contém incompatibilidades bloqueantes; revise antes de executar.')
        job['stage'] = 'Verificar objetos existentes no destino'
        existing = destination_objects(target)
        if foreign_key_policy == 'strict' and existing_policy == 'skip':
            participating = {t.name.casefold() for _,dst in pairs for fk in dst.foreign_key_constraints for t in [dst, *[e.column.table for e in fk.elements]]}
            conflicts = existing & participating
            if conflicts:
                raise MigrationError('Modo estrito de FKs: tabelas de relacionamentos já existem e seriam puladas: '+', '.join(sorted(conflicts))+'. Use um destino novo; a migração preserva objetos existentes. Nenhuma DDL nova foi executada.')
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
                code = diagnostic(exc, 'Criar objeto', engines=(target,))['code']
                if existing_policy == 'skip' and code in (1050, '42P07', 'ORA-00955'):
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
                conversions=result.get('empty_to_null_confirmed',{})
                if conversions: message+=f' {sum(conversions.values())} valores de texto vazio convertidos para NULL por opção explícita.'
                record('data', dst.name, dst.name, 'copied', message, rows=result['copied'], empty_to_null_confirmed=conversions)
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
                    if foreign_key_policy == 'strict':
                        failure(MigrationError('Modo estrito: FK não aplicada porque uma tabela dependente/referenciada está sem carga confirmada nesta execução.'), 'foreign_key', fk.name, dst.name)
                    record('foreign_key', fk.name, dst.name, 'skipped', 'FK omitida: tabela dependente ou referenciada pulada/sem carga confirmada. Revisão manual necessária.'); continue
                job['stage'] = f'Criar chave estrangeira {fk.name}'
                try:
                    result = _create_object(target, AddConstraint(fk), 'foreign_key', dst, fk.name)
                    record('foreign_key', fk.name, dst.name, 'created', result.pop('message'), **result)
                except Exception as exc: failure(exc, 'foreign_key', fk.name, dst.name)
        has_errors = any(e['status'] == 'failed' for e in job['events'])
        has_warnings = bool(omitted_fk_count) or any(e['status'] == 'skipped' or e.get('warnings') or e.get('empty_to_null_confirmed') for e in job['events'])
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
        fk_events = [e for e in job['events'] if e['kind']=='foreign_key']
        counts = {s: sum(e['status']==s for e in fk_events) for s in ('created','failed','skipped')}
        integrity_status = ('not_requested' if foreign_key_policy=='omit' else
            'not_confirmed' if counts['failed'] or counts['skipped'] or len(loaded)!=len(pairs) else
            'partial' if omitted_fk_count else
            'confirmed' if counts['created']==planned_fk_count and planned_fk_count else 'no_foreign_keys')
        job['referential_integrity'] = dict(policy=foreign_key_policy,planned=planned_fk_count,
            **counts,not_planned=omitted_fk_count,status=integrity_status)
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
