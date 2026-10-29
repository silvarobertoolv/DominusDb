"""SQL Server 2008 catalog reader, without expression-cast reflection SQL.

Only retry metadata syntax errors (102/156). Never hide permission, connection
or type errors. All identifiers used as filter values are bound parameters.
"""
import sqlalchemy as sa
from compatibility import source_dictionary


def syntax_error(exc):
    original = getattr(exc, 'orig', exc)
    args = getattr(original, 'args', ())
    return isinstance(exc, sa.exc.DBAPIError) and bool(args) and args[0] in (102,156)


def uses_catalog(engine):
    return getattr(engine, '_sqlbridge_metadata_mode', 'auto') == 'catalog' or getattr(engine, '_sqlbridge_catalog_fallback', False)


class CatalogInspector:
    def __init__(self, engine): self.engine = engine

    def rows(self, sql, table=None, schema=None):
        params = {'schema': schema or 'dbo', 'table': table}
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(sa.text(sql), params).mappings()]

    def get_schema_names(self):
        return [r['name'] for r in self.rows('SELECT name FROM sys.schemas ORDER BY name')]

    def get_table_names(self, schema=None):
        return [r['name'] for r in self.rows('''SELECT t.name FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name=:schema AND t.is_ms_shipped=0 ORDER BY t.name''', schema=schema)]

    def get_columns(self, table, schema=None):
        reference = sa.Table(table, sa.MetaData(), schema=schema)
        return source_dictionary(self.engine, reference)

    def get_pk_constraint(self, table, schema=None):
        rows = self.rows('''SELECT kc.name, c.name AS column_name FROM sys.key_constraints kc
            JOIN sys.tables t ON t.object_id=kc.parent_object_id
            JOIN sys.schemas s ON s.schema_id=t.schema_id
            JOIN sys.index_columns ic ON ic.object_id=t.object_id AND ic.index_id=kc.unique_index_id
            JOIN sys.columns c ON c.object_id=t.object_id AND c.column_id=ic.column_id
            WHERE s.name=:schema AND t.name=:table AND kc.type='PK' AND ic.key_ordinal>0
            ORDER BY ic.key_ordinal''', table, schema)
        return dict(name=rows[0]['name'] if rows else None, constrained_columns=[r['column_name'] for r in rows])

    def get_indexes(self, table, schema=None):
        rows = self.rows('''SELECT i.name, i.is_unique, i.type AS index_type, i.filter_definition,
                   c.name AS column_name, ic.is_included_column, ic.key_ordinal, ic.index_column_id
            FROM sys.indexes i JOIN sys.tables t ON t.object_id=i.object_id
            JOIN sys.schemas s ON s.schema_id=t.schema_id
            JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
            JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
            WHERE s.name=:schema AND t.name=:table AND i.is_primary_key=0 AND i.type<>0
            ORDER BY i.index_id, ic.key_ordinal, ic.index_column_id''', table, schema)
        grouped = {}
        for row in rows:
            item = grouped.setdefault(row['name'], dict(name=row['name'], unique=bool(row['is_unique']), column_names=[], include_columns=[], dialect_options={}))
            if row['index_type'] not in (1,2): item['dialect_options']['mssql_columnstore'] = True  # spatial/XML also need manual review
            if row['filter_definition'] is not None: item['dialect_options']['mssql_where'] = row['filter_definition']
            if row['is_included_column']: item['include_columns'].append(row['column_name'])
            else: item['column_names'].append(row['column_name'])
        return list(grouped.values())

    def get_foreign_keys(self, table, schema=None):
        rows = self.rows('''SELECT fk.name, lc.name AS column_name, rs.name AS referred_schema,
                rt.name AS referred_table, rc.name AS referred_column,
                fk.delete_referential_action_desc AS ondelete, fk.update_referential_action_desc AS onupdate
            FROM sys.foreign_keys fk JOIN sys.tables t ON t.object_id=fk.parent_object_id
            JOIN sys.schemas s ON s.schema_id=t.schema_id
            JOIN sys.foreign_key_columns fc ON fc.constraint_object_id=fk.object_id
            JOIN sys.columns lc ON lc.object_id=t.object_id AND lc.column_id=fc.parent_column_id
            JOIN sys.tables rt ON rt.object_id=fc.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id=rt.schema_id
            JOIN sys.columns rc ON rc.object_id=rt.object_id AND rc.column_id=fc.referenced_column_id
            WHERE s.name=:schema AND t.name=:table ORDER BY fk.object_id, fc.constraint_column_id''', table, schema)
        grouped = {}
        for row in rows:
            item = grouped.setdefault(row['name'], dict(name=row['name'], constrained_columns=[], referred_schema=row['referred_schema'], referred_table=row['referred_table'], referred_columns=[], options={key:(row[key] or 'NO_ACTION').replace('_',' ') for key in ('ondelete','onupdate')}))
            item['constrained_columns'].append(row['column_name'])
            item['referred_columns'].append(row['referred_column'])
        return list(grouped.values())

    def get_unique_constraints(self, table, schema=None):
        rows = self.rows('''SELECT kc.name, c.name AS column_name FROM sys.key_constraints kc
            JOIN sys.tables t ON t.object_id=kc.parent_object_id JOIN sys.schemas s ON s.schema_id=t.schema_id
            JOIN sys.index_columns ic ON ic.object_id=t.object_id AND ic.index_id=kc.unique_index_id
            JOIN sys.columns c ON c.object_id=t.object_id AND c.column_id=ic.column_id
            WHERE s.name=:schema AND t.name=:table AND kc.type='UQ' AND ic.key_ordinal>0
            ORDER BY kc.object_id, ic.key_ordinal''', table, schema)
        grouped = {}
        for row in rows:
            grouped.setdefault(row['name'], dict(name=row['name'], column_names=[]))['column_names'].append(row['column_name'])
        return list(grouped.values())

    def get_check_constraints(self, table, schema=None):
        return self.rows('''SELECT cc.name, cc.definition AS sqltext FROM sys.check_constraints cc
            JOIN sys.tables t ON t.object_id=cc.parent_object_id JOIN sys.schemas s ON s.schema_id=t.schema_id
            WHERE s.name=:schema AND t.name=:table ORDER BY cc.name''', table, schema)


class AdaptiveInspector:
    def __init__(self, engine):
        self.engine = engine
        self.catalog = CatalogInspector(engine)
        self.native = None

    def __getattr__(self, name):
        def read(*args, **kwargs):
            if uses_catalog(self.engine): return getattr(self.catalog, name)(*args, **kwargs)
            try:
                if self.native is None: self.native = sa.inspect(self.engine)
                return getattr(self.native, name)(*args, **kwargs)
            except sa.exc.DBAPIError as exc:
                if not syntax_error(exc): raise
                self.engine._sqlbridge_catalog_fallback = True
                return getattr(self.catalog, name)(*args, **kwargs)
        return read


def source_inspector(engine):
    return AdaptiveInspector(engine) if getattr(getattr(engine,'dialect',None),'name',None)=='mssql' else sa.inspect(engine)


def catalog_table(engine, name, schema, metadata, resolve_type):
    reader = CatalogInspector(engine)
    definitions = reader.get_columns(name, schema)
    if not definitions: raise sa.exc.NoSuchTableError(f'{schema}.{name}: sem colunas visíveis no catálogo.')
    columns=[]
    for item in definitions:
        placeholder=sa.Column(item['column_name'], sa.types.NullType())
        typ=resolve_type(placeholder, item)
        generated=[]
        if item.get('is_computed'):
            if item.get('computed_definition') is None: raise ValueError(f'{schema}.{name}.{item["column_name"]}: definição calculada invisível; confira VIEW DEFINITION.')
            generated.append(sa.Computed(item['computed_definition'], persisted=bool(item.get('is_persisted'))))
        elif item.get('is_identity'): generated.append(sa.Identity())
        options=dict(nullable=bool(item['is_nullable']))
        if item.get('default_definition') is not None and not generated: options['server_default']=sa.text(item['default_definition'])
        columns.append(sa.Column(item['column_name'], typ, *generated, **options))
    table=sa.Table(name, metadata, *columns, schema=schema)
    pk=reader.get_pk_constraint(name, schema)
    if any(col not in table.c for col in pk['constrained_columns']): raise ValueError('Catálogo de PK inconsistente; gere novamente o plano.')
    if pk['constrained_columns']: table.append_constraint(sa.PrimaryKeyConstraint(*pk['constrained_columns'], name=pk['name']))
    # Protect local FK columns even when their referenced table is not selected.
    table.info['source_fk_columns'] = {c for fk in reader.get_foreign_keys(name,schema) for c in fk['constrained_columns']}
    table.info['source_dictionary'] = definitions
    table.info['reflection_mode'] = 'catalog'
    return table


def reflect_source_table(engine, name, schema, metadata, resolve_type):
    mssql = getattr(getattr(engine,'dialect',None),'name',None)=='mssql'
    if mssql and uses_catalog(engine): return catalog_table(engine,name,schema,metadata,resolve_type)
    try:
        return sa.Table(name,metadata,schema=schema,autoload_with=engine,resolve_fks=False)
    except sa.exc.DBAPIError as exc:
        if not mssql or not syntax_error(exc): raise
        # SQLAlchemy may have registered a half-reflected table. Discard it.
        key=f'{schema}.{name}' if schema else name
        if key in metadata.tables: metadata.remove(metadata.tables[key])
        engine._sqlbridge_catalog_fallback = True
        table=catalog_table(engine,name,schema,metadata,resolve_type)
        table.info['reflection_fallback_code'] = exc.orig.args[0]
        return table
