# -*- coding: utf-8 -*-
"""性能 backlog A/B/C/D 批次回归测试（2026-07-01 全仓审查产出的第二梯队）。

覆盖：A#3 qa_retrieved_doc 事实行（写侧去重/1146 熔断/读侧 JOIN 二态）、
A#6/7 qa_rollup sargable 区间换算（DST 两侧）、B#13/14 连接池上限推导、
C#16 kb_gaps TTL 缓存原语、C#17 机器人部门解析 TTL 缓存、
C#18 embedding SQLite store（roundtrip/迁移/淘汰/退化）、C#27 VLM 缓存 OSS 降频、
D#34 LLM 超时可配。D#33（流式清洗挪推送线程）由 test_dingtalk_streaming 的
pusher/finalize 不变量测试覆盖；D#30（提取并发）默认关，串行路径由既有管线测试覆盖。
"""
import json

from opensearch_pipeline import qa_facts


# ─── A#3 qa_retrieved_doc 事实行 ─────────────────────────────────────────────

class _FactCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        if self._conn.raise_errno is not None:
            raise Exception(self._conn.raise_errno, "boom")
        self._conn.calls.append((sql, list(rows)))

    def execute(self, sql, params=None):
        if self._conn.raise_errno is not None:
            raise Exception(self._conn.raise_errno, "boom")
        self._conn.calls.append((sql, params))

    def fetchall(self):
        return []


class _FactConn:
    def __init__(self, raise_errno=None):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.raise_errno = raise_errno

    def cursor(self):
        return _FactCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def test_fact_insert_dedupes_chunk_fanout_and_splits_cited():
    qa_facts._fact_state_clear()
    conn = _FactConn()
    retrieved = [{"doc_id": "D1", "chunk_id": "c1"}, {"doc_id": "D1", "chunk_id": "c2"},
                 {"doc_id": "D2"}, {"doc_id": ""}, "not-a-dict"]
    cited = [{"doc_id": "D1"}]
    qa_facts.insert_qa_doc_facts(conn, "msg-1", retrieved, cited)
    assert conn.commits == 1
    (sql, rows), = conn.calls
    assert "INSERT IGNORE" in sql and "qa_retrieved_doc" in sql
    assert sorted(rows) == [("msg-1", "D1", 0), ("msg-1", "D1", 1), ("msg-1", "D2", 0)], \
        "同 doc 多 chunk 去重为一行；cited 独立成行；空/坏 doc_id 丢弃"


def test_fact_insert_1146_disables_further_writes():
    qa_facts._fact_state_clear()
    conn = _FactConn(raise_errno=1146)
    qa_facts.insert_qa_doc_facts(conn, "msg-1", [{"doc_id": "D1"}], None)
    assert conn.rollbacks == 1
    ok_conn = _FactConn()
    qa_facts.insert_qa_doc_facts(ok_conn, "msg-2", [{"doc_id": "D1"}], None)
    assert ok_conn.calls == [], "1146 后进程内熔断，不再逐条尝试"
    qa_facts._fact_state_clear()
    qa_facts.insert_qa_doc_facts(ok_conn, "msg-3", [{"doc_id": "D1"}], None)
    assert len(ok_conn.calls) == 1, "clear 后恢复"


def test_fact_join_sql_defaults_to_json_table(monkeypatch):
    qa_facts._fact_state_clear()
    monkeypatch.delenv("RAG_QA_FACT_JOIN", raising=False)
    sql = qa_facts.qa_docs_join_sql()
    assert "JSON_TABLE" in sql and "CONVERT" in sql, "flag 关 = 既有 JSON_TABLE 行为"
    assert "qa_retrieved_doc" not in sql


def test_fact_join_sql_uses_fact_table_when_enabled(monkeypatch):
    qa_facts._fact_state_clear()
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    monkeypatch.setattr(qa_facts, "_probe_fact_table", lambda: True)
    sql = qa_facts.qa_docs_join_sql()
    assert "qa_retrieved_doc" in sql and "jt.cited=0" in sql and "JSON_TABLE" not in sql
    cited = qa_facts.qa_docs_join_sql(cited=True)
    assert "jt.cited=1" in cited
    # 别名契约：两种形态都必须暴露 jt / m 供调用方 WHERE/GROUP BY 复用
    assert " jt" in sql and "document_meta m" in sql


def test_fact_join_sql_falls_back_when_probe_fails(monkeypatch):
    qa_facts._fact_state_clear()
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    monkeypatch.setattr(qa_facts, "_probe_fact_table", lambda: False)
    assert "JSON_TABLE" in qa_facts.qa_docs_join_sql(), "flag 开但表缺失 → 回退不炸"


# ─── A#6/7 qa_rollup sargable 区间（DST 两侧）────────────────────────────────

def test_rollup_bounds_summer_pdt():
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    # 北京 2026-07-01 00:00 = UTC 6-30 16:00 = PDT(UTC-7) 6-30 09:00
    assert _beijing_day_pacific_bounds("2026-07-01") == (
        "2026-06-30 09:00:00", "2026-07-01 09:00:00")


def test_rollup_bounds_winter_pst():
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    # 北京 2026-01-15 00:00 = UTC 1-14 16:00 = PST(UTC-8) 1-14 08:00 —— 旧 +15h 硬编码在此差 1 小时
    assert _beijing_day_pacific_bounds("2026-01-15") == (
        "2026-01-14 08:00:00", "2026-01-15 08:00:00")


def test_rollup_bounds_invalid_date_falls_back():
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    assert _beijing_day_pacific_bounds("not-a-date") is None


# ─── B#13/14 连接池上限推导 ──────────────────────────────────────────────────

def test_pool_max_default_and_overrides(monkeypatch):
    from opensearch_pipeline.db import _pool_max_connections
    monkeypatch.delenv("RAG_DB_POOL_MAX", raising=False)
    monkeypatch.delenv("RAG_CLASSIFY_CONCURRENCY", raising=False)
    assert _pool_max_connections() == 20
    monkeypatch.setenv("RAG_CLASSIFY_CONCURRENCY", "30")
    assert _pool_max_connections() == 34, "classify 并发抬高默认上限（+4 headroom）"
    monkeypatch.setenv("RAG_DB_POOL_MAX", "12")
    assert _pool_max_connections() == 12, "显式 RAG_DB_POOL_MAX 覆盖推导值"
    monkeypatch.setenv("RAG_DB_POOL_MAX", "not-a-number")
    assert _pool_max_connections() == 34, "非法值回退推导默认"


# ─── C#16 kb_gaps TTL 缓存原语 ───────────────────────────────────────────────

def test_gaps_cache_roundtrip_and_disable(monkeypatch):
    from opensearch_pipeline.routes import contribution as rc
    rc._gaps_cache_clear()
    key = ("hr",)
    rc._gaps_cache_put(key, ([{"hash": "h"}], "summary"))
    assert rc._gaps_cache_get(key) == ([{"hash": "h"}], "summary")
    assert rc._gaps_cache_get(("other",)) is None, "键按部门集隔离"
    monkeypatch.setenv("RAG_KB_GAPS_CACHE_TTL", "0")
    assert rc._gaps_cache_get(key) is None, "TTL=0 关闭"
    rc._gaps_cache_clear()


# ─── C#17 机器人部门解析 TTL 缓存 ────────────────────────────────────────────

def test_bot_dept_cache_hits_definitive_result(monkeypatch):
    from opensearch_pipeline import dingtalk_identity as di
    di._bot_dept_cache_clear()
    calls = {"n": 0}
    monkeypatch.setattr(di, "_resolve_user_dept_live",
                        lambda sid: (calls.__setitem__("n", calls["n"] + 1) or (["hr"], True)))
    assert di._resolve_user_dept("U1") == ["hr"]
    assert di._resolve_user_dept("U1") == ["hr"]
    assert calls["n"] == 1, "确定性结果 TTL 内直接复用，不再打 DB"


def test_bot_dept_cache_skips_non_cacheable(monkeypatch):
    from opensearch_pipeline import dingtalk_identity as di
    di._bot_dept_cache_clear()
    calls = {"n": 0}
    monkeypatch.setattr(di, "_resolve_user_dept_live",
                        lambda sid: (calls.__setitem__("n", calls["n"] + 1) or (["hr"], False)))
    di._resolve_user_dept("U1")
    di._resolve_user_dept("U1")
    assert calls["n"] == 2, "partial/API 失败回退等非确定性结果绝不缓存（保留逐次自愈）"


def test_bot_dept_cache_disabled_by_env(monkeypatch):
    from opensearch_pipeline import dingtalk_identity as di
    di._bot_dept_cache_clear()
    monkeypatch.setenv("RAG_BOT_DEPT_CACHE_TTL_SECONDS", "0")
    calls = {"n": 0}
    monkeypatch.setattr(di, "_resolve_user_dept_live",
                        lambda sid: (calls.__setitem__("n", calls["n"] + 1) or (["hr"], True)))
    di._resolve_user_dept("U1")
    di._resolve_user_dept("U1")
    assert calls["n"] == 2


def test_bot_dept_cache_group_chat_guard():
    from opensearch_pipeline import dingtalk_identity as di
    assert di._resolve_user_dept("$:LWCP_v1:xxx") == [], "群聊合成身份照旧 fail-closed 短路"


# ─── C#18/19 embedding SQLite store ─────────────────────────────────────────

def test_embedding_store_roundtrip_and_eviction(tmp_path):
    from opensearch_pipeline.embedding_cache import EmbeddingCacheStore
    store = EmbeddingCacheStore(str(tmp_path / "emb.sqlite3"))
    assert store.backend == "sqlite"
    dense = [0.1] * 4
    sparse = {"indices": [1, 2], "values": [0.5, 0.6]}
    store.put_many({"k1": dense, "sp_k1": sparse, "k2": [0.2]})
    got = store.get_many(["k1", "sp_k1", "k2", "missing"])
    assert got["k1"] == dense and got["sp_k1"] == sparse and "missing" not in got
    assert store.count() == 3
    store.evict_to(2)
    assert store.count() == 2, "超容量按最旧淘汰"


def test_embedding_store_migrates_legacy_json(tmp_path):
    from opensearch_pipeline.embedding_cache import EmbeddingCacheStore
    legacy = tmp_path / "embedding_cache.json"
    legacy.write_text(json.dumps({"k1": [1.0, 2.0], "sp_k1": {"indices": [3], "values": [0.9]}}),
                      encoding="utf-8")
    store = EmbeddingCacheStore(str(tmp_path / "embedding_cache.sqlite3"))
    assert store.count() == 2, "首开且 sqlite 为空 → 自动导入存量 JSON"
    assert store.get_many(["k1"])["k1"] == [1.0, 2.0]
    assert legacy.exists(), "JSON 原样保留（eval 脚本仍在用）"


def test_embedding_store_falls_back_to_memory_on_bad_path(tmp_path):
    from opensearch_pipeline.embedding_cache import EmbeddingCacheStore
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = EmbeddingCacheStore(str(blocker / "impossible" / "emb.sqlite3"))
    assert store.backend == "memory", "sqlite 打开失败退化 dict，不阻断 embedding 主流程"
    store.put_many({"k": [1.0]})
    assert store.get_many(["k"]) == {"k": [1.0]}


def test_embedding_store_oss_mirror_key_namespaced(tmp_path, monkeypatch):
    """P2-9(2)：OSS 镜像对象键按 模型+维度 命名空间化——改维/换模型后不再 push/pull 同一
    对象（last-writer-wins 互覆盖）。旧共享对象留在原 key 不删，新命名空间冷启动即可。"""
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.embedding_cache import (
        OSS_MIRROR_KEY as LEGACY_SHARED_KEY,
        EmbeddingCacheStore,
    )
    config = get_config()
    monkeypatch.setattr(config.embedding, "model", "text-embedding-v4")
    monkeypatch.setattr(config.embedding, "dimension", 1024)
    s1 = EmbeddingCacheStore(str(tmp_path / "a.sqlite3"))
    assert "text-embedding-v4" in s1.OSS_MIRROR_KEY and "1024" in s1.OSS_MIRROR_KEY
    assert s1.OSS_MIRROR_KEY != LEGACY_SHARED_KEY, "不得再落在历史共享对象上"

    monkeypatch.setattr(config.embedding, "dimension", 512)
    s2 = EmbeddingCacheStore(str(tmp_path / "b.sqlite3"))
    assert "512" in s2.OSS_MIRROR_KEY
    assert s2.OSS_MIRROR_KEY != s1.OSS_MIRROR_KEY, "不同维度必须各占镜像对象"

    # 类属性兜底值保持历史共享键（fail-open 回退 + VLM store 基类不受影响）
    assert EmbeddingCacheStore.OSS_MIRROR_KEY == LEGACY_SHARED_KEY


# ─── C#27 VLM 缓存 OSS 上传降频 ─────────────────────────────────────────────

def test_vlm_cache_oss_sync_throttled(monkeypatch, tmp_path):
    """perf#27 降频节奏在 SQLite 化（2026-07-03）后保持：每 N 次 per-doc 保存推一次
    OSS 镜像（对象从整包 JSON 换成 checkpoint 后的 .sqlite 文件）+ run 结束 flush 尾巴。
    旧测试直接操纵 UnifiedExtractor 类属性（_vlm_cache/_vlm_cache_oss_dirty），
    迁移后状态收敛到 vlm_cache 模块单例——改为对同一批类方法入口打桩单例。"""
    import opensearch_pipeline.vlm_cache as vc
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor as UE
    puts = {"n": 0}

    class _B:
        def put_object_from_file(self, key, path):
            puts["n"] += 1

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (_B(), False))
    monkeypatch.setenv("RAG_VLM_CACHE_OSS_SYNC_EVERY", "3")
    store = vc.VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    cache = vc.TrackedVlmCache(store)
    monkeypatch.setattr(vc, "_cache", cache)
    try:
        cache["h1:pub"] = {"status": "ROUTE_TO_TEXT"}
        UE._save_vlm_cache()
        UE._save_vlm_cache()
        assert puts["n"] == 0, "未满间隔不推 OSS（本地 sqlite 照常增量落）"
        assert store.get_many(["h1:pub"]) == {"h1:pub": {"status": "ROUTE_TO_TEXT"}}, (
            "per-doc 保存即增量落 sqlite")
        UE._save_vlm_cache()
        assert puts["n"] == 1, "第 3 次触发镜像上传"
        UE._save_vlm_cache()
        assert puts["n"] == 1
        UE.flush_vlm_cache_to_oss()
        assert puts["n"] == 2, "run 结束 flush 把尾巴推上去"
        UE.flush_vlm_cache_to_oss()
        assert puts["n"] == 2, "无账不重复推"
    finally:
        try:
            store._conn.close()
        except Exception:
            pass


# ─── D#34 LLM 超时可配 ───────────────────────────────────────────────────────

def test_llm_timeout_default_and_env(monkeypatch):
    from opensearch_pipeline.llm_generator import _llm_request_timeout
    monkeypatch.delenv("RAG_LLM_TIMEOUT_SECONDS", raising=False)
    assert _llm_request_timeout() == 120.0
    monkeypatch.setenv("RAG_LLM_TIMEOUT_SECONDS", "60")
    assert _llm_request_timeout() == 60.0
    monkeypatch.setenv("RAG_LLM_TIMEOUT_SECONDS", "-1")
    assert _llm_request_timeout() == 120.0, "非法/非正值回退默认"
