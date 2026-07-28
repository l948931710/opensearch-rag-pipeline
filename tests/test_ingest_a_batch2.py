# -*- coding: utf-8 -*-
"""摄取管线 A 类修复 batch-2 回归（A5 / A11 / A12 / A13，2026-07-25）。

A5：OCR 页缓存接 OSS 镜像 + 绝对路径 + 真单例锁 + run 收尾 flush。
A11：解码失败不再伪装成装饰图 —— 唯一筛查入口 screen_static；DISCARD_UNREADABLE
     既不进 assets 也不进 VLM 缓存；四格式经 stats 出参留痕；独立坏图落 0-chunk 疑似失败闭环。
A12：GET_LOCK 单实例互斥，三态 fail-closed，SIM/flag-off 零连接，取锁早于 run_start。
A13：独立图片文档接入跨文档 VLM 缓存（与嵌入图共用写入门与条目形态）。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import pytest

from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor


# ═══════════════════ A11：screen_static 是唯一筛查入口 ═══════════════════

def _proc():
    return object.__new__(ImageFunnelProcessor)   # 绕开 __init__（不需要 OCR/VLM 客户端）


def test_a11_unreadable_carries_exception_type(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not really a png")
    s = _proc().screen_static(str(bad))
    assert s["verdict"] == "UNREADABLE"
    assert s["error"] and ":" in s["error"], "必须保留异常类型（旧实现压成 (0,0,0.0) 后彻底丢失）"


def test_a11_missing_file_is_unreadable_not_decorative(tmp_path):
    s = _proc().screen_static(str(tmp_path / "nope.png"))
    assert s["verdict"] == "UNREADABLE"


def test_a11_real_small_image_still_decorative(tmp_path):
    """真的很小的图仍归 DECORATIVE —— 不能把既有判据一起改掉。"""
    pytest.importorskip("PIL")
    from PIL import Image
    p = tmp_path / "tiny.png"
    Image.new("RGB", (10, 10)).save(p)
    assert _proc().screen_static(str(p))["verdict"] == "DECORATIVE"


def _noise_png(path, w=400, h=300):
    """纯色图会被 PNG 压到 <3KB 从而命中 MIN_FILE_KB → 必须用噪声图才是"正常图"。"""
    from PIL import Image
    import os as _os
    img = Image.frombytes("RGB", (w, h), _os.urandom(w * h * 3))
    img.save(path)
    return path


def test_a11_normal_image_is_ok(tmp_path):
    pytest.importorskip("PIL")
    p = _noise_png(tmp_path / "ok.png")
    s = _proc().screen_static(str(p))
    assert s["verdict"] == "OK" and s["width"] == 400 and s["file_size_kb"] > 3.0


def test_a11_static_heuristics_stays_a_thin_wrapper(tmp_path):
    """旧三元组契约保留（非测试调用方不受影响）。"""
    pytest.importorskip("PIL")
    p = _noise_png(tmp_path / "ok.png")
    w, h, kb = _proc()._static_heuristics(str(p))
    assert (w, h) == (400, 300) and kb > 0


def test_a11_process_image_returns_discard_unreadable(monkeypatch, tmp_path):
    p = _proc()
    monkeypatch.setattr(p, "screen_static", lambda path: {
        "width": 0, "height": 0, "file_size_kb": 0.0, "aspect_ratio": 0.0,
        "verdict": "UNREADABLE", "error": "OSError: cannot identify image file"})
    res = p.process_image(str(tmp_path / "x.png"), "D1")
    assert res["status"] == "DISCARD_UNREADABLE"
    assert "OSError" in res["reason"]


def _code_only(src: str) -> str:
    """剥掉整行注释（注释里会引用被禁用的写法，断言只看代码）。"""
    return "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))


def test_a11_thresholds_have_a_single_source():
    """阈值不得再有第二份拷贝（旧代码在 unified_extractor Phase-1 内联了一份）。"""
    import opensearch_pipeline.extraction.unified_extractor as ue
    code = _code_only(inspect.getsource(ue.UnifiedExtractor._process_embedded_images))
    assert "screen_static" in code
    assert "< 50" not in code and "8.0" not in code, "Phase-1 不得再内联 Funnel-1 阈值"
    # 阈值现在是漏斗类的常量
    assert (ImageFunnelProcessor.MIN_SIDE_PX, ImageFunnelProcessor.MIN_FILE_KB,
            ImageFunnelProcessor.MAX_ASPECT_RATIO) == (50, 3.0, 8.0)


# ═══════════════════ A11：不进 assets、不进缓存 ═══════════════════

def test_a11_unreadable_excluded_from_vlm_cache():
    from opensearch_pipeline.extraction.unified_extractor import _vlm_cache_put_allowed
    ok = {"status": "ROUTE_TO_VECTOR"}
    assert _vlm_cache_put_allowed(False, ok, "abc123") is True
    # 解码失败：与 degraded 同理，一次性故障不得被固化成跨文档永久标签
    assert _vlm_cache_put_allowed(False, {"status": "DISCARD_UNREADABLE"}, "abc") is False
    assert _vlm_cache_put_allowed(False, {"status": "ROUTE_TO_TEXT", "degraded": True}, "a") is False
    assert _vlm_cache_put_allowed(True, ok, "abc") is False              # simulate 不入缓存
    assert _vlm_cache_put_allowed(False, ok, "fallback_123") is False    # 不稳定键不入缓存


def test_a11_unreadable_skipped_from_assets():
    """必须是精确等值枚举 —— 这条链路上不做 DISCARD 前缀匹配。"""
    import opensearch_pipeline.extraction.unified_extractor as ue
    src = " ".join(inspect.getsource(ue.UnifiedExtractor._process_embedded_images).split())
    assert 'if status in ("DISCARD_DECORATIVE", "DISCARD_UNREADABLE"):' in src


def test_a11_all_four_formats_propagate_stats():
    import opensearch_pipeline.extraction.unified_extractor as ue
    for name in ("_extract_pdf", "_extract_docx", "_extract_xlsx", "_extract_pptx"):
        src = " ".join(inspect.getsource(getattr(ue.UnifiedExtractor, name)).split())
        assert "stats=_funnel_stats" in src, f"{name} 未接 stats 出参"
        assert "_warn_unreadable_images(" in src, f"{name} 未留痕"


def test_a11_warning_text_hits_zero_chunk_failure_keywords():
    """文案与 node_write_chunk_meta 的关键词表是**载荷级耦合**：命中才会落
    chunk_status='NEEDS_REVIEW' + content_process_status='FAILED'。改文案先看这里。"""
    from opensearch_pipeline.extraction.unified_extractor import _warn_unreadable_images
    warns = []
    notes = _warn_unreadable_images(warns, {"unreadable_names": ["a.png: OSError: bad"]})
    assert notes and warns
    keywords = ("fail", "error", "cannot", "无法", "错误", "失败")
    assert any(k in warns[0].lower() for k in keywords), \
        "warning 必须命中 0-chunk 疑似失败判据的关键词表"


def test_a11_no_unreadable_means_no_note():
    from opensearch_pipeline.extraction.unified_extractor import _warn_unreadable_images
    warns = []
    assert _warn_unreadable_images(warns, {}) == [] and warns == []


def test_a11_standalone_bad_image_carries_partial_loss_note():
    import opensearch_pipeline.extraction.unified_extractor as ue
    src = " ".join(inspect.getsource(ue.UnifiedExtractor._extract_image).split())
    assert 'if status == "DISCARD_UNREADABLE":' in src
    assert "partial_loss_notes=_unreadable_notes" in src


# ═══════════════════ A13：独立图片接缓存 ═══════════════════

def test_a13_standalone_image_uses_shared_cache_semantics():
    import opensearch_pipeline.extraction.unified_extractor as ue
    src = " ".join(inspect.getsource(ue.UnifiedExtractor._extract_image).split())
    assert "_vlm_cache_lookup(" in src and "_vlm_cache_put_allowed(" in src
    assert "_vlm_cache_entry(" in src, "条目形态必须走单一来源，防两条路径各写各的"
    assert "sanitize_ocr_text(" in src, "命中也要过反幻觉复洗（与嵌入图同款）"
    # 回归红线：oss_key 必须仍是 raw_key（构造 processing/assets/ 路径对 ROUTE_TO_TEXT 是 403 死图）
    assert '"oss_key": task.get("raw_key", "")' in src


def test_a13_cache_entry_shape_is_shared():
    from opensearch_pipeline.extraction.unified_extractor import _vlm_cache_entry
    e = _vlm_cache_entry({"status": "ROUTE_TO_VECTOR", "visual_summary": "s", "ocr_text": "t"})
    assert set(e) == {"status", "visual_summary", "image_category", "vlm_annotation_map",
                      "reason", "width", "height", "file_size_kb", "ocr_text"}


# ═══════════════════ A5：OCR 页缓存 ═══════════════════

def test_a5_page_cache_lock_is_real():
    """旧代码是 `_page_cache_lock = None` 且从未使用 → 并发下每线程各造一个 store。"""
    import threading
    from opensearch_pipeline.extraction.ocr_client import OCRClient
    assert isinstance(OCRClient._page_cache_lock, type(threading.Lock()))
    src = inspect.getsource(OCRClient._get_page_cache)
    assert "with cls._page_cache_lock:" in src and src.count("if cls._page_cache is None") == 2


def test_a5_cache_path_is_absolute():
    from opensearch_pipeline.extraction.ocr_client import _ocr_page_cache_path
    assert os.path.isabs(_ocr_page_cache_path()), \
        "相对路径依赖 cwd —— DataWorks 在临时解压目录跑，缓存会落到每次都不同的位置"


def test_a5_mirror_wired(monkeypatch, tmp_path):
    import opensearch_pipeline.extraction.ocr_client as oc
    # 路径已是绝对的 → 必须显式改到 tmp，否则测试会在仓库 scratch/ 里留下真缓存文件
    monkeypatch.setattr(oc, "_ocr_page_cache_path",
                        lambda: str(tmp_path / "scratch" / "ocr_page_cache.sqlite3"))
    store = oc._make_ocr_page_cache()
    assert store.OSS_MIRROR_KEY == "processing/cache/ocr_page_cache.sqlite3"
    assert store.MIRROR_ENV == "RAG_OCR_PAGE_CACHE_OSS_MIRROR"
    assert str(tmp_path) in store.path


def test_a5_flush_is_wired_and_fail_open(monkeypatch):
    from opensearch_pipeline.extraction.ocr_client import OCRClient
    calls = []

    class _Store:
        def finalize(self, n):
            calls.append(n)

    monkeypatch.setattr(OCRClient, "_page_cache", _Store())
    OCRClient.flush_page_cache_to_oss()
    assert calls and calls[0] > 0

    class _Boom:
        def finalize(self, n):
            raise RuntimeError("oss down")

    monkeypatch.setattr(OCRClient, "_page_cache", _Boom())
    OCRClient.flush_page_cache_to_oss()      # 缓存是 advisory，绝不上抛

    monkeypatch.setattr(OCRClient, "_page_cache", None)
    OCRClient.flush_page_cache_to_oss()      # 未初始化 → no-op


def test_a5_flush_called_at_run_end():
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn.node_extract_text_with_ocr)
    assert "OCRClient.flush_page_cache_to_oss()" in src, \
        "底座只从 finalize 推镜像；没有调用点镜像永远不会产生"


# ═══════════════════ A12：单实例互斥 ═══════════════════

def _cfg(db="fuling_knowledge"):
    import types
    rds = types.SimpleNamespace(
        host="h", port=3306, user="u", password="p", database=db, charset="utf8mb4",
        connect_timeout=10, read_timeout=30,
        pymysql_ssl_args=lambda: {"ssl_disabled": True})
    return types.SimpleNamespace(rds=rds)


def test_a12_lock_name_is_namespaced_by_database():
    from opensearch_pipeline.dataworks_orchestrator import _stage_lock_name
    assert _stage_lock_name(3, _cfg("fuling_knowledge")) == "rag_ingest:fuling_knowledge:stage3"
    # staging/prod 共用 RDS 实例时不得互相阻塞（GET_LOCK 名字是实例级的）
    assert _stage_lock_name(3, _cfg("fuling_knowledge_stg")) != _stage_lock_name(3, _cfg())


def test_a12_simulate_opens_no_connection(monkeypatch):
    """B1a：改成句柄后，SIM 仍必须零连接、零 holder、零 release 副作用。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.setattr("pymysql.connect",
                        lambda **kw: pytest.fail("SIM 承诺零外部服务"), raising=False)
    h = orch._acquire_stage_lock(1, _cfg(), simulate=True)
    assert h.conn is None and h.acquired_here is False
    assert orch._HELD_STAGE_LOCKS == {}
    orch._release_stage_lock(h)          # no-op，不得抛


def test_a12_flag_off_opens_no_connection(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.setenv("RAG_INGEST_SINGLETON_LOCK", "false")
    monkeypatch.setattr("pymysql.connect",
                        lambda **kw: pytest.fail("显式关闭时不得连库"), raising=False)
    h = orch._acquire_stage_lock(1, _cfg(), simulate=False)
    assert h.conn is None and h.acquired_here is False
    assert orch._HELD_STAGE_LOCKS == {}


class _LockConn:
    def __init__(self, ret):
        self.ret, self.sqls, self.closed = ret, [], False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sqls.append((sql, params))

    def fetchone(self):
        return (self.ret,)

    def close(self):
        self.closed = True


def _patch_connect(monkeypatch, conn):
    import pymysql
    monkeypatch.setattr(pymysql, "connect", lambda **kw: conn)
    return conn


@pytest.fixture(autouse=True)
def _clean_lock_registry():
    """每个用例前后清空进程级持锁表（否则可重入判定会跨用例串味）。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    orch._HELD_STAGE_LOCKS.clear()
    yield
    orch._HELD_STAGE_LOCKS.clear()


def test_a12_acquired_returns_handle(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    conn = _patch_connect(monkeypatch, _LockConn(1))
    h = orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert h.conn is conn and h.acquired_here is True
    assert "GET_LOCK" in conn.sqls[0][0] and not conn.closed
    assert orch._HELD_STAGE_LOCKS[h.name] is h


def test_a12_reentrant_same_thread_does_not_relock(monkeypatch):
    """CLI 路径：main() 外层持锁 → 嵌套的 run_stage_drained 不得再发一次 GET_LOCK，
    返回时也不得释放外层锁（否则后半程裸奔）。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    conn = _patch_connect(monkeypatch, _LockConn(1))
    outer = orch._acquire_stage_lock(2, _cfg(), simulate=False)
    inner = orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert len([s for s in conn.sqls if "GET_LOCK" in s[0]]) == 1, "嵌套层不得重复 GET_LOCK"
    assert inner.acquired_here is False and inner.conn is conn
    orch._release_stage_lock(inner)
    assert not conn.closed and orch._HELD_STAGE_LOCKS, "嵌套层绝不释放外层锁"
    orch._release_stage_lock(outer)
    assert conn.closed and orch._HELD_STAGE_LOCKS == {}
    assert sum(1 for s in conn.sqls if "RELEASE_LOCK" in s[0]) == 1, "恰好释放一次"


def test_a12_other_thread_is_not_reentrant(monkeypatch):
    """同进程另一线程不算可重入 —— 否则两个 drain 会在同一进程并发跑，正是本锁要防的事。"""
    import threading
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    conn = _patch_connect(monkeypatch, _LockConn(1))
    orch._acquire_stage_lock(2, _cfg(), simulate=False)
    seen = {}

    def _worker():
        try:
            orch._acquire_stage_lock(2, _cfg(), simulate=False)
            seen["relocked"] = True
        except SystemExit as e:
            seen["exit"] = e.code

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert len([s for s in conn.sqls if "GET_LOCK" in s[0]]) == 2, "另一线程必须真发 GET_LOCK"


def test_a12_lock_names_do_not_cross_stages(monkeypatch):
    """同进程持有 stage-1 时，stage-2 不得被误判成可重入。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    conn = _patch_connect(monkeypatch, _LockConn(1))
    orch._acquire_stage_lock(1, _cfg(), simulate=False)
    h2 = orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert h2.acquired_here is True
    assert len([s for s in conn.sqls if "GET_LOCK" in s[0]]) == 2


def test_a12_lock_is_taken_inside_run_stage_drained():
    """现网入口是三个 DataWorks 节点直接调 run_stage_drained（不经 main）——
    a2f1a37 只在 main() 取锁，等于现网零保护。取锁必须在 drained 里。"""
    import inspect
    import opensearch_pipeline.dataworks_orchestrator as orch
    src = " ".join(inspect.getsource(orch.run_stage_drained).split())
    assert "_acquire_stage_lock(" in src and "_release_stage_lock(" in src


def test_a12_contended_exits_zero(monkeypatch):
    """另一实例在跑不是错误 —— 不该让 DataWorks 标红。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    conn = _patch_connect(monkeypatch, _LockConn(0))
    with pytest.raises(SystemExit) as ei:
        orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert ei.value.code == 0 and conn.closed


def test_a12_null_result_is_fail_closed(monkeypatch):
    """锁机制本身异常 → 拒绝在无互斥保护下运行（fail-closed）。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    _patch_connect(monkeypatch, _LockConn(None))
    with pytest.raises(SystemExit) as ei:
        orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert ei.value.code == 1


def test_a12_connect_failure_is_fail_closed(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch
    import pymysql
    monkeypatch.delenv("RAG_INGEST_SINGLETON_LOCK", raising=False)
    monkeypatch.setattr(pymysql, "connect",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("rds down")))
    with pytest.raises(SystemExit) as ei:
        orch._acquire_stage_lock(2, _cfg(), simulate=False)
    assert ei.value.code == 1


def test_a12_release_is_fail_open(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch

    class _Boom(_LockConn):
        def execute(self, sql, params=None):
            raise RuntimeError("gone")

    c = _Boom(1)
    h = orch._StageLock(c, "rag_ingest:db:stage2", 1, True)
    orch._HELD_STAGE_LOCKS[h.name] = h
    orch._release_stage_lock(h)               # 不得上抛
    assert c.closed
    assert orch._HELD_STAGE_LOCKS == {}, "异常路径也必须清 holder（线程 ident 会被复用）"
    orch._release_stage_lock(None)            # None 直接返回


def test_a12_lock_acquired_before_run_start():
    """竞争失败必须在写 pipeline_run 之前退出，否则会留下 RUNNING 记录。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    src = inspect.getsource(orch.main)
    assert src.index("_acquire_stage_lock(") < src.index("run_start(")
    assert "_release_stage_lock(" in src


def test_a12_bare_connect_has_explicit_ssl_and_timeouts():
    """裸连接绕过 GuardedDBConnection，必须显式复用 SSL/超时配置（另有全仓扫描门守着）。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    src = " ".join(inspect.getsource(orch._acquire_stage_lock).split())
    assert "**rds.pymysql_ssl_args()" in src
    assert "connect_timeout=rds.connect_timeout" in src
    assert "read_timeout=rds.read_timeout" in src
