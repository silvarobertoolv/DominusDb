import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.schema import CreateTable
from compatibility import target_limits, check_and_adapt, source_dictionary, row_estimate
from migration import convert_type


def wide_pair(count=10, length=4000, binary=False):
    source=sa.Table('wide',sa.MetaData(schema='dbo'),sa.Column('id',sa.Integer,primary_key=True),*[sa.Column(f'c{i}',sa.String(length)) for i in range(count)])
    target=sa.Table('dbo__wide',sa.MetaData(),sa.Column('id',sa.Integer,primary_key=True),*[sa.Column(f'c{i}',mysql.VARBINARY(length) if binary else sa.String(length)) for i in range(count)])
    return source,target


def test_row_1118_is_automatically_adapted_without_truncation():
    src,dst=wide_pair()
    limits,_=target_limits()
    report=check_and_adapt([(src,dst)],limits)
    assert report['tables'][0]['before']['logical_bytes']>65535
    assert report['tables'][0]['after']['logical_bytes']<=60000
    assert len(report['changes'])>0 and report['errors']==[]
    for c in dst.columns:
        if isinstance(c.type,sa.Text): assert type(c.type).__name__=='TEXT'  # 4000*4 fits 65535 bytes
    ddl=str(CreateTable(dst).compile(dialect=mysql.dialect()))
    assert 'TEXT' in ddl and 'ROW_FORMAT=DYNAMIC' in ddl
    assert dst.c.id.primary_key


def test_binary_row_width_uses_blob_without_touching_key():
    src,dst=wide_pair(count=10,length=8000,binary=True)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert not report['errors']
    assert any(c['after']=='BLOB' for c in report['changes'])
    assert dst.c.id.primary_key


def test_small_table_requires_no_change():
    src,dst=wide_pair(count=2,length=20)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert not report['errors'] and not report['changes']


def test_mediumtext_preserves_large_declared_capacity():
    src,dst=wide_pair(count=1,length=20000)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert not report['errors'] and report['changes'][0]['after']=='MEDIUMTEXT'


def test_protected_indexes_are_not_silently_shortened_or_made_text():
    src,dst=wide_pair()
    for c in list(dst.columns)[1:]: sa.Index('ix_'+c.name,c)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert not report['changes']
    assert any('1071' in e for e in report['errors'])
    assert any('linha estimada' in e for e in report['errors'])
    assert all(not isinstance(c.type,sa.Text) for c in dst.columns)


def test_incoming_and_outgoing_fk_columns_are_protected():
    src,parent=wide_pair(count=5,length=4000)
    child=sa.Table('child',parent.metadata,sa.Column('ref',sa.String(4000)))
    child.append_constraint(sa.ForeignKeyConstraint([child.c.ref],[parent.c.c0],name='fk',use_alter=True))
    child_src=sa.Table('child',sa.MetaData(),sa.Column('ref',sa.String(4000)))
    report=check_and_adapt([(src,parent),(child_src,child)],target_limits()[0])
    assert not isinstance(parent.c.c0.type,sa.Text) and not isinstance(child.c.ref.type,sa.Text)
    assert 'c0' in report['tables'][0]['protected_columns']


def test_blob_index_and_fk_mismatches_block_before_execution():
    src,dst=wide_pair(count=1)
    dst.c.c0.type=mysql.LONGTEXT();sa.Index('ix_long',dst.c.c0)
    child=sa.Table('child',dst.metadata,sa.Column('ref',sa.SmallInteger))
    child.append_constraint(sa.ForeignKeyConstraint([child.c.ref],[dst.c.id],name='fk',use_alter=True))
    child_src=sa.Table('child',sa.MetaData(),sa.Column('ref',sa.SmallInteger))
    report=check_and_adapt([(src,dst),(child_src,child)],target_limits()[0])
    assert any('1170' in e for e in report['errors'])
    assert any('3780' in e for e in report['errors'])


def test_key_byte_boundary_uses_bytes_not_varchar_length_header():
    src,dst=wide_pair(count=1,length=768);sa.Index('ix_limit',dst.c.c0)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert not report['errors']  # 768 * 4 = 3072 exactly
    limits=target_limits()[0];limits['index_bytes']=1536
    assert any('1071' in e for e in check_and_adapt([(src,dst)],limits)['errors'])


def test_too_many_columns_and_index_fields_are_reported():
    src,dst=wide_pair(count=20,length=5)
    sa.Index('ix_many',*[c for c in list(dst.columns)[1:]])
    limits=target_limits()[0];limits['max_columns']=20
    report=check_and_adapt([(src,dst)],limits)
    assert any('colunas excedem' in e for e in report['errors'])
    assert any('limite InnoDB: 16' in e for e in report['errors'])


def test_decimal_scale_is_not_silently_reduced():
    with pytest.raises(ValueError,match='escala 30'): convert_type(mssql.DECIMAL(38,38))
    assert str(convert_type(mssql.DECIMAL(38,30)))=='NUMERIC(38, 30)'
    assert str(convert_type(mssql.DECIMAL(18,0)))=='NUMERIC(18, 0)'


def test_finite_binary_types_stay_indexable_and_max_uses_lob():
    assert isinstance(convert_type(mssql.VARBINARY(100)),mysql.VARBINARY)
    assert isinstance(convert_type(mssql.BINARY(16)),mysql.BINARY)
    assert isinstance(convert_type(mssql.VARBINARY(None)),mysql.LONGBLOB)


def test_dictionary_query_is_parameterized_and_reads_declared_byte_length():
    calls=[]
    class Result:
        def mappings(self): return self
        def __iter__(self): return iter([{'column_name':'memo','declared_type':'nvarchar','base_type':'nvarchar','max_length_bytes':8000,'precision':0,'scale':0}])
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql,params): calls.append((str(sql),params));return Result()
    class Engine:
        dialect=mssql.dialect()
        def connect(self): return Connection()
    table=sa.Table('order\'name',sa.MetaData(schema='dbo'),sa.Column('memo',sa.String(4000)))
    result=source_dictionary(Engine(),table)
    assert result[0]['max_length_bytes']==8000
    assert 'sys.columns' in calls[0][0] and 'sys.types' in calls[0][0]
    assert "order'name" not in calls[0][0] and calls[0][1]['table']=="order'name"


def test_target_page_size_sets_real_index_budget():
    class Result:
        def mappings(self): return self
        def one(self): return {'version':'8.0.40','page_size':8192}
    class Connection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,sql): return Result()
    class Engine:
        dialect=mysql.dialect()
        def connect(self): return Connection()
    limits,warnings=target_limits(Engine())
    assert limits['verified'] and limits['index_bytes']==1536
    assert limits['inline_budget']==3840 and warnings==[]


def test_dictionary_failure_preserves_plan_error_stage(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    sa.Table('t',sa.MetaData(schema='main'),sa.Column('id',sa.Integer)).metadata.create_all(engine)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.t',schema='main',name='t')],objects=[]))
    def fail(*args): raise RuntimeError('dictionary not available')
    monkeypatch.setattr(migration,'source_dictionary',fail)
    with pytest.raises(migration.PlanError) as caught: migration.plan(engine,['main.t'])
    assert caught.value.stage=='Consultar dicionário sys.columns/sys.types'


def test_plan_compiles_adapted_types_and_exports_dictionary(monkeypatch):
    import migration
    engine=sa.create_engine('sqlite://')
    src,dst=wide_pair()
    source=sa.Table('wide',sa.MetaData(schema='main'),sa.Column('id',sa.Integer,primary_key=True),*[sa.Column(f'c{i}',sa.String(4000)) for i in range(10)])
    source.create(engine)
    monkeypatch.setattr(migration,'inventory',lambda _:dict(tables=[dict(id='main.wide',schema='main',name='wide')],objects=[]))
    prepared=migration.plan(engine,['main.wide'])
    assert not prepared['errors'] and 'ROW_FORMAT=DYNAMIC' in prepared['ddl']
    assert prepared['compatibility']['changes']
    assert prepared['compatibility']['source_dictionary'][0]['columns'][1]['length']==4000


def test_dictionary_bytes_drive_unicode_capacity_and_resolve_aliases():
    from migration import dictionary_type
    column=sa.Column('alias_note',sa.types.NullType())
    typ=dictionary_type(column,{'base_type':'nvarchar','max_length_bytes':8000,'declared_type':'custom_note'})
    assert typ.length==4000
    assert str(convert_type(typ))=='VARCHAR(4000)'
    typ=dictionary_type(column,{'base_type':'nvarchar','max_length_bytes':-1})
    assert str(convert_type(typ).compile(dialect=mysql.dialect()))=='LONGTEXT'
    typ=dictionary_type(column,{'base_type':'decimal','precision':38,'scale':38})
    with pytest.raises(ValueError): convert_type(typ)


def test_inline_limit_converts_unindexed_short_strings_too():
    src,dst=wide_pair(count=100,length=50)
    report=check_and_adapt([(src,dst)],target_limits()[0])
    assert report['tables'][0]['before']['inline_bytes_estimate']>report['limits']['inline_budget']
    assert report['changes'] and not report['errors']
    assert report['tables'][0]['after']['inline_bytes_estimate']<=report['limits']['inline_budget']


def test_target_limit_failure_is_visible_and_uses_documented_fallback():
    class Engine:
        dialect=mysql.dialect()
        def connect(self): raise RuntimeError('metadata unavailable')
    limits,warnings=target_limits(Engine())
    assert not limits['verified'] and limits['page_size']==16384
    assert 'metadata unavailable' in warnings[0]


def test_mysql_fk_allows_different_varchar_lengths_with_same_charset():
    src,parent=wide_pair(count=1,length=100)
    child=sa.Table('child',parent.metadata,sa.Column('ref',sa.String(50)))
    child.append_constraint(sa.ForeignKeyConstraint([child.c.ref],[parent.c.c0],name='fk',use_alter=True))
    child_src=sa.Table('child',sa.MetaData(),sa.Column('ref',sa.String(50)))
    report=check_and_adapt([(src,parent),(child_src,child)],target_limits()[0])
    assert not any('3780' in e for e in report['errors'])
