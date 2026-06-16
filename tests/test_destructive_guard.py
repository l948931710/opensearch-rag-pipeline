# -*- coding: utf-8 -*-
"""
test_destructive_guard.py — 运行时破坏性操作守卫（env_guard.py）

覆盖：production 放行 / 非指纹目标放行 / 当日 ack 放行 / 过期 ack 拒绝 /
RAG_READONLY 一律拒绝 / STAGING _stg 资源放行 / GuardedBucket 写拦读传。
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import opensearch_pipeline.env_guard as eg
from opensearch_pipeline.env_guard import (DestructiveOpBlocked, GuardedBucket,
                                           assert_destructive_write_allowed)

PROD_HA3 = "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"


def _cfg(environment="development", readonly=False, rds_db="fuling_knowledge",
         ha3_table="", oss_bucket="fuling-knowledge-base"):
    return SimpleNamespace(
        environment=environment, readonly=readonly,
        rds=SimpleNamespace(host="x", database=rds_db),
        alibaba_vector=SimpleNamespace(table_name=ha3_table),
        oss=SimpleNamespace(bucket_name=oss_bucket),
    )


@pytest.fixture
def patch_cfg(monkeypatch):
    def _apply(cfg):
        monkeypatch.setattr(eg, "get_config", lambda: cfg)
    return _apply


def test_production_always_allowed(patch_cfg):
    patch_cfg(_cfg(environment="production"))
    assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_non_prod_target_allowed(patch_cfg):
    patch_cfg(_cfg())
    assert_destructive_write_allowed("search_delete", "localhost:9200", kind="search")


def test_dev_to_prod_target_blocked(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_same_day_ack_allows(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK",
                       f"search_delete:{date.today().isoformat()}")
    assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_wildcard_same_day_ack_allows(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")
    assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


def test_stale_ack_rejected(patch_cfg, monkeypatch):
    """陈年 export 残留不得长期放行——ack 按日过期。"""
    patch_cfg(_cfg())
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"search_delete:{yesterday}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_wrong_op_ack_rejected(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK",
                       f"some_other_op:{date.today().isoformat()}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("search_delete", PROD_HA3, kind="search")


def test_readonly_blocks_everything(patch_cfg, monkeypatch):
    """RAG_READONLY=true（PROD-RO）：连非生产目标也拒绝，且 ack 无效。"""
    patch_cfg(_cfg(readonly=True))
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("write_chunk_meta", "localhost", kind="rds")


def test_staging_stg_resources_allowed(patch_cfg):
    """STAGING 共享生产实例但写 _stg 资源 = 合法（后缀已被加载期强校验）。"""
    patch_cfg(_cfg(environment="staging", rds_db="fuling_knowledge_stg",
                   ha3_table="fuling_kb_chunks_stg"))
    assert_destructive_write_allowed("write_chunk_meta",
                                     "rm-bp15j7wekd5738f093o.mysql.rds.aliyuncs.com", kind="rds")
    assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


def test_staging_non_stg_table_still_blocked(patch_cfg, monkeypatch):
    """staging 标签但表名不带 _stg（=直指生产活表）仍要拦。"""
    patch_cfg(_cfg(environment="staging", ha3_table="fuling_kb_chunks"))
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    with pytest.raises(DestructiveOpBlocked):
        assert_destructive_write_allowed("push_index", PROD_HA3, kind="search")


class _FakeBucket:
    def __init__(self):
        self.calls = []

    def put_object(self, key, data):
        self.calls.append(("put", key))
        return "put-ok"

    def get_object(self, key):
        self.calls.append(("get", key))
        return "get-ok"

    def sign_url(self, method, key, expires):
        return f"https://signed/{key}"


def test_guarded_bucket_blocks_prod_writes(patch_cfg, monkeypatch):
    patch_cfg(_cfg())
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base")
    with pytest.raises(DestructiveOpBlocked):
        gb.put_object("rag-ready/x/content.md", b"data")


def test_guarded_bucket_reads_and_signing_pass_through(patch_cfg):
    patch_cfg(_cfg())
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base")
    assert gb.get_object("raw/a.pdf") == "get-ok"
    assert gb.sign_url("GET", "processing/assets/i.png", 600).startswith("https://signed/")


def test_guarded_bucket_non_prod_bucket_writes_allowed(patch_cfg):
    """staging/其他桶不命中精确指纹——写放行。"""
    patch_cfg(_cfg(environment="staging", oss_bucket="fuling-knowledge-base-staging"))
    gb = GuardedBucket(_FakeBucket(), "fuling-knowledge-base-staging")
    assert gb.put_object("rag-ready/x/content.md", b"data") == "put-ok"


# ── P4: 连接层裸 cursor 写守卫（GuardedDBConnection / GuardedDBCursor） ──

PROD_RDS = "rm-bp15j7wekd5738f093o.mysql.rds.aliyuncs.com"


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, q, *a, **k):
        self.executed.append(str(q))
        return 1

    def executemany(self, q, *a, **k):
        self.executed.append(("many", str(q)))
        return 2

    def fetchall(self):
        return [("row",)]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_is_write_sql_classification():
    assert eg.is_write_sql("  INSERT INTO t VALUES (1)")
    assert eg.is_write_sql("\n   UPDATE document_version SET x=1")
    assert eg.is_write_sql("-- note\n DELETE FROM chunk_meta")
    assert eg.is_write_sql("/* c */ REPLACE INTO t VALUES (1)")
    assert eg.is_write_sql("TRUNCATE TABLE t")
    assert not eg.is_write_sql("SELECT 1")
    assert not eg.is_write_sql("  SHOW TABLES")
    assert not eg.is_write_sql("SET SESSION TRANSACTION READ ONLY")


def test_is_write_sql_degenerate_forms():
    """加固：可执行注释 /*! */（MySQL 真执行）、前导 (/; 退化前缀都要穿透到真实动词。"""
    # MySQL 可执行注释里的 DML 必须判为写（否则漏守卫）
    assert eg.is_write_sql("/*!40001 DELETE FROM chunk_meta */")
    assert eg.is_write_sql("/*! UPDATE t SET x=1 */")
    # 前导括号/分号穿透：括号写=写，括号读=读（不误伤括号 SELECT）
    assert eg.is_write_sql("(INSERT INTO t VALUES (1))")
    assert eg.is_write_sql("; DELETE FROM t")
    assert not eg.is_write_sql("(SELECT * FROM t) UNION (SELECT * FROM u)")
    # 惰性块注释（非 /*!）仍按惰性剥离
    assert not eg.is_write_sql("/* just a SELECT comment */ SELECT 1")


def test_ack_today_rejects_empty_op():
    """_ack_today 拒退化/typo ack（空 op 段），与文档化的 '<op>:<date>' 契约一致。"""
    today = date.today().isoformat()
    assert eg._ack_today(f"deactivate_old_chunks:{today}")
    assert eg._ack_today(f"*:{today}")
    assert not eg._ack_today(f":{today}")       # 空 op
    assert not eg._ack_today(f"   :{today}")    # 纯空白 op
    assert not eg._ack_today(f"anyop:{(date.today() - timedelta(days=1)).isoformat()}")  # 过期
    assert not eg._ack_today("")


def test_guarded_db_query_method_guarded_under_readonly(patch_cfg):
    """加固：conn.query()（pymysql 底层写口子）在 readonly 下也被拦——堵 __getattr__ 直通。"""
    patch_cfg(_cfg(readonly=True))
    fc = _FakeConn()
    # _FakeConn 需要一个 query 方法供透传
    fc.query = lambda sql, *a, **k: fc.cur.executed.append(("query", str(sql)))
    gc = eg.GuardedDBConnection(fc, "localhost")
    gc.query("SELECT 1")  # 读放行
    with pytest.raises(DestructiveOpBlocked):
        gc.query("DELETE FROM chunk_meta")
    assert all("DELETE" not in str(q) for q in fc.cur.executed)


def test_guarded_db_callproc_guarded_under_readonly(patch_cfg):
    """加固：cursor.callproc()（存储过程可写）在 readonly 下被保守拦截。"""
    patch_cfg(_cfg(readonly=True))
    fc = _FakeConn()
    fc.cur.callproc = lambda name, *a, **k: fc.cur.executed.append(("call", name))
    gc = eg.GuardedDBConnection(fc, "localhost")
    cur = gc.cursor()
    with pytest.raises(DestructiveOpBlocked):
        cur.callproc("some_writing_proc")
    assert ("call", "some_writing_proc") not in fc.cur.executed


def test_guarded_db_callproc_allowed_in_production(patch_cfg):
    patch_cfg(_cfg(environment="production"))
    fc = _FakeConn()
    fc.cur.callproc = lambda name, *a, **k: fc.cur.executed.append(("call", name))
    gc = eg.GuardedDBConnection(fc, PROD_RDS)
    cur = gc.cursor()
    cur.callproc("some_proc")
    assert ("call", "some_proc") in fc.cur.executed


def test_guarded_db_reads_pass_through(patch_cfg):
    """读语句直通——serving 的读路径共用同一池，绝不能被拦。"""
    patch_cfg(_cfg())
    fc = _FakeConn()
    gc = eg.GuardedDBConnection(fc, "localhost")
    with gc.cursor() as cur:
        cur.execute("SELECT doc_id FROM chunk_meta WHERE is_active=1")
        assert cur.fetchall() == [("row",)]
    gc.commit()
    gc.close()
    assert fc.cur.executed == ["SELECT doc_id FROM chunk_meta WHERE is_active=1"]
    assert fc.committed and fc.closed


def test_guarded_db_blocks_write_under_readonly(patch_cfg, monkeypatch):
    """RAG_READONLY=true：裸 cursor 的写被连接层拦下（即便本地 host、即便有 ack）；读仍放行。"""
    patch_cfg(_cfg(readonly=True))
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK", f"*:{date.today().isoformat()}")
    fc = _FakeConn()
    gc = eg.GuardedDBConnection(fc, "localhost")
    cur = gc.cursor()
    cur.execute("SELECT 1")  # 读放行
    with pytest.raises(DestructiveOpBlocked):
        cur.execute("DELETE FROM chunk_meta WHERE doc_id=%s", ("x",))
    # 被拦的写从未抵达底层 cursor
    assert all("DELETE" not in q for q in fc.cur.executed if isinstance(q, str))


def test_guarded_db_blocks_dev_to_prod_write_no_ack(patch_cfg, monkeypatch):
    """development 标签 + 生产 RDS 目标 + 无 ack：裸 cursor 写被拦。"""
    patch_cfg(_cfg())
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    fc = _FakeConn()
    gc = eg.GuardedDBConnection(fc, PROD_RDS)
    cur = gc.cursor()
    with pytest.raises(DestructiveOpBlocked):
        cur.execute("UPDATE document_version SET status='x'")


def test_guarded_db_allows_writes_in_production(patch_cfg):
    """environment=production：写放行（DataWorks/SAE 是合法写方），executemany 也过。"""
    patch_cfg(_cfg(environment="production"))
    fc = _FakeConn()
    gc = eg.GuardedDBConnection(fc, PROD_RDS)
    with gc.cursor() as cur:
        cur.executemany("INSERT INTO chunk_meta (a) VALUES (%s)", [(1,), (2,)])
    assert ("many", "INSERT INTO chunk_meta (a) VALUES (%s)") in fc.cur.executed


def test_guarded_db_any_ack_satisfies_connection_layer(patch_cfg, monkeypatch):
    """连接层用 any_ack：节点级 per-op ack（write_chunk_meta:today）也能放行裸 cursor 写，
    不与节点级显式守卫互相打架。"""
    patch_cfg(_cfg())
    monkeypatch.setenv("RAG_DESTRUCTIVE_PROD_ACK",
                       f"write_chunk_meta:{date.today().isoformat()}")
    fc = _FakeConn()
    gc = eg.GuardedDBConnection(fc, PROD_RDS)
    cur = gc.cursor()
    cur.execute("DELETE FROM chunk_meta WHERE doc_id=%s", ("x",))  # 不应 raise
    assert any("DELETE" in q for q in fc.cur.executed if isinstance(q, str))


# ── P3: node_register_metadata 显式守卫（与其它写节点对齐） ──

def test_node_register_metadata_explicit_guard_blocks_readonly(patch_cfg):
    """P3: node_register_metadata 现有显式 assert_destructive_write_allowed('register_metadata', …)
    —— PROD-RO（RAG_READONLY）会话下在**打开连接之前**就 fail-loud。报错带 op 名
    'register_metadata' 证明触发的是节点级显式守卫（而非连接层兜底的 'rds_write'）。"""
    from opensearch_pipeline import pipeline_nodes

    patch_cfg(_cfg(readonly=True))
    ctx = {"tasks": [{"doc_id": "d1", "version_no": 1}], "simulate_db": False}
    with pytest.raises(DestructiveOpBlocked, match="register_metadata"):
        pipeline_nodes.node_register_metadata(ctx)


def test_node_register_metadata_blocks_dev_to_prod_no_ack(monkeypatch):
    """P3: development 标签指向生产 RDS 且无 ack —— register_metadata 在打开连接前被显式守卫拦下
    （走 dev→prod 分支，op 名证明是节点级显式守卫）。"""
    from opensearch_pipeline import pipeline_nodes

    cfg = SimpleNamespace(
        environment="development", readonly=False, simulate_db=False,
        rds=SimpleNamespace(host=PROD_RDS, database="fuling_knowledge"),
        alibaba_vector=SimpleNamespace(table_name=""),
        oss=SimpleNamespace(bucket_name="fuling-knowledge-base"),
    )
    monkeypatch.setattr(eg, "get_config", lambda: cfg)
    monkeypatch.setattr(pipeline_nodes, "get_config", lambda: cfg)
    monkeypatch.delenv("RAG_DESTRUCTIVE_PROD_ACK", raising=False)
    ctx = {"tasks": [{"doc_id": "d1", "version_no": 1}], "simulate_db": False}
    with pytest.raises(DestructiveOpBlocked, match="register_metadata"):
        pipeline_nodes.node_register_metadata(ctx)
