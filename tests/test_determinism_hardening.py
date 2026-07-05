# -*- coding: utf-8 -*-
"""tests/test_determinism_hardening.py — Phase-1 DET bundle.

Closes the determinism findings beyond the chunk path: pin LLM-classification temperature to 0
(stable category → stable chunk routing), pin the VLM funnel call to temperature 0 (stable
routing/captions), and make both caches atomic (a torn cache write must not force a nondeterministic
full re-eval).
"""
import inspect


def test_classifier_temperature_is_deterministic():
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn)
    assert '"temperature": 0.1' not in src, "classifier temperature must be pinned to 0, not 0.1"
    assert src.count('"temperature": 0') >= 2, "both DashScope + Gemini classify branches must pin temp=0"


def test_vlm_funnel_pins_temperature_zero():
    import opensearch_pipeline.image_funnel_processor as ifp
    assert "temperature=0" in inspect.getsource(ifp), "VLM funnel must pin temperature=0 for determinism"


def test_build_image_chat_payload_propagates_temperature():
    from opensearch_pipeline.vlm_endpoint import build_image_chat_payload
    compat = build_image_chat_payload("m", "p", "b64", "image/png", True, temperature=0)
    assert compat.get("temperature") == 0
    native = build_image_chat_payload("m", "p", "b64", "image/png", False, temperature=0)
    assert native.get("parameters", {}).get("temperature") == 0


def test_embedding_cache_writes_are_crash_safe():
    """perf#18 后 embedding 缓存换 SQLite：崩溃安全不变量从 temp+os.replace 原子写
    转由 WAL 日志 + 逐批 commit 承担——半截写入回滚为上次 commit，绝不产生撕裂缓存。"""
    import opensearch_pipeline.embedding_cache as ec
    src = inspect.getsource(ec)
    assert "journal_mode=WAL" in src, "embedding cache sqlite must run in WAL mode (crash safety)"
    from opensearch_pipeline.pipeline_nodes import node_generate_embeddings
    assert "get_embedding_cache" in inspect.getsource(node_generate_embeddings), (
        "node_generate_embeddings must use the shared crash-safe store"
    )


def test_vlm_cache_writes_atomically():
    """VLM 缓存 SQLite 化（2026-07-03）后：崩溃安全不变量从 temp+os.replace 原子写
    转由共享 SqliteKVStore 的 WAL 日志承担（与 embedding 缓存同一底座、同款语义迁移）。"""
    from opensearch_pipeline.embedding_cache import SqliteKVStore
    from opensearch_pipeline.vlm_cache import VlmCacheStore
    assert issubclass(VlmCacheStore, SqliteKVStore), (
        "VLM cache must reuse the crash-safe (WAL) SqliteKVStore base"
    )
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    assert "save_vlm_cache" in inspect.getsource(UnifiedExtractor._save_vlm_cache), (
        "UnifiedExtractor._save_vlm_cache must delegate to the shared sqlite-backed store"
    )


def test_embedding_cache_hit_rate_and_cap_pressure_signals():
    """P3-13：20k 上限此前是无信号的规模悬崖——超容后跨运行 OSS 镜像停止摊薄、
    重复整付 DashScope，唯一症状是账单变大。节点必须每次运行记命中率，且在容量
    压力（本批需求超上限/存量顶上限）时发 ops 告警（dedup 防风暴）。"""
    from opensearch_pipeline.pipeline_nodes import node_generate_embeddings
    src = inspect.getsource(node_generate_embeddings)
    assert "hit-rate" in src, "必须每次运行记录缓存命中率"
    assert "send_ops_alert" in src and "embed-cache-cap" in src, (
        "容量压力必须走 ops 告警（dedup_key=embed-cache-cap）"
    )
    assert "RAG_EMBEDDING_CACHE_MAX_ENTRIES" in src, "上限必须保持 env 可配"
