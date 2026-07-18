# -*- coding: utf-8 -*-
"""VLM 标注缓存 SQLite 化（全维度复审「VLM 缓存 SQLite 化」，2026-07-03）回归。

覆盖迁移自 scratch/vlm_cache.json 整读整写形态后的关键不变量：
  · 存量 JSON（本地/遗留 OSS）一次性自动迁移，JSON 原样保留；
  · OSS .sqlite 镜像 warm-start（拉取顺序：本地 sqlite > OSS 镜像 > legacy JSON）；
  · dict 门面命中/写入语义与旧 in-memory dict 一致（含 _vlm_cache_lookup 的
    遗留裸 key 迁移在 per-doc save 后持久化）；
  · 8 线程并发写安全（RAG_VLM_CONCURRENCY/RAG_EXTRACT_CONCURRENCY 形态）；
  · RAG_VLM_CACHE_MAX_ENTRIES 容量上限（近似 LRU-on-write 淘汰最旧）；
  · 坏库文件 fail-open：退化内存 dict，绝不阻断提取主流程；
  · simulate 契约：无 per-doc save 则 run 结束 flush 零动作（不落盘不推 OSS）。

全部用 tmp_path 独立 store——不碰仓库 scratch/、不真连 OSS
（OSS 经 monkeypatch opensearch_pipeline.pipeline_nodes._get_oss_bucket 打桩，
与既有 test_perf_backlog_abcd 同一 seam）。
"""

import json
import os
import threading

import pytest

import opensearch_pipeline.vlm_cache as vc
from opensearch_pipeline.vlm_cache import TrackedVlmCache, VlmCacheStore


def _entry(summary="U8登录界面截图", status="ROUTE_TO_VECTOR"):
    return {"status": status, "visual_summary": summary, "image_category": "step_screenshot",
            "vlm_annotation_map": {}, "reason": "", "width": 640, "height": 480,
            "file_size_kb": 55.0, "ocr_text": "用户名 密码 登录"}


@pytest.fixture()
def _no_oss(monkeypatch):
    """默认打桩成 simulate OSS（(None, True)）——单测绝不触网。"""
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (None, True))


# ─── 存量迁移 ────────────────────────────────────────────────────────────────

def test_migrates_legacy_local_json(_no_oss, tmp_path):
    legacy = tmp_path / "vlm_cache.json"
    data = {"abc123:pub": _entry(), "def456": _entry("裸 MD5 遗留条目")}
    legacy.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    store = VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    assert store.backend == "sqlite"
    assert store.count() == 2, "首开且 sqlite 为空 → 自动导入存量 JSON"
    assert store.get_many(["abc123:pub"])["abc123:pub"] == data["abc123:pub"]
    assert legacy.exists(), "JSON 原样保留（tests/eval 脚本仍在用，embedding 缓存同约定）"
    # 二次打开不重复迁移（幂等）
    store2 = VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    assert store2.count() == 2


def test_migrates_legacy_oss_json_when_all_empty(monkeypatch, tmp_path):
    """新 pod 冷启动窗口（本地空 + OSS 尚无 sqlite 镜像）→ 回退拉遗留 OSS JSON，
    保住旧实现的跨机器 warm-start 语义。"""
    blob = json.dumps({"aaa:pub": _entry()}, ensure_ascii=False).encode("utf-8")

    class _Obj:
        def read(self):
            return blob

    class _B:
        def get_object(self, key):
            assert key == vc.LEGACY_OSS_JSON_KEY
            return _Obj()
        # 无 get_object_to_file → sqlite 镜像拉取按「OSS 上不存在」失败，走 JSON 兜底

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (_B(), False))
    store = VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    assert store.count() == 1
    assert store.get_many(["aaa:pub"])["aaa:pub"]["visual_summary"] == "U8登录界面截图"


def test_pulls_sqlite_mirror_from_oss(monkeypatch, tmp_path):
    """本地缺 sqlite 时优先拉 OSS .sqlite 镜像（新形态 warm-start 主路径）。"""
    # 先造一个有内容的镜像文件（checkpoint 后主文件自洽）；造源期间 OSS 打桩为 sim
    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (None, True))
    src = VlmCacheStore(str(tmp_path / "mirror_src.sqlite3"))
    src.put_many({"k1:pub": _entry()})
    src._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    src._conn.close()
    mirror_bytes = (tmp_path / "mirror_src.sqlite3").read_bytes()

    class _B:
        def get_object_to_file(self, key, path):
            assert key == vc.OSS_MIRROR_KEY
            with open(path, "wb") as f:
                f.write(mirror_bytes)

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (_B(), False))
    store = VlmCacheStore(str(tmp_path / "fresh" / "vlm_cache.sqlite3"))
    assert store.count() == 1
    assert store.get_many(["k1:pub"])["k1:pub"]["status"] == "ROUTE_TO_VECTOR"


# ─── dict 门面：命中语义 + 脏键增量持久化 ───────────────────────────────────

def test_tracked_cache_hit_semantics_and_incremental_persist(_no_oss, tmp_path):
    path = str(tmp_path / "vlm_cache.sqlite3")
    store = VlmCacheStore(path)
    store.put_many({"pre:pub": _entry("预置条目")})
    cache = TrackedVlmCache(store)
    assert cache.get("pre:pub")["visual_summary"] == "预置条目", "预载命中 = 旧 dict 语义"
    assert cache.get("miss:pub") is None

    # _vlm_cache_lookup 的遗留裸 key 迁移经 []= 记脏，save 后持久化（旧契约：
    # 「真实运行随 _save_vlm_cache 持久化」）。B4 P2-03 后迁移键带默认版本（:pub:2），
    # 断言动态取 ns——版本再升不再改判。
    from opensearch_pipeline.extraction.unified_extractor import (
        _vlm_cache_lookup,
        _vlm_cache_ns,
    )
    _migrated = f"bare123:{_vlm_cache_ns(True)}"
    cache["bare123"] = _entry("遗留裸条目")
    hit = _vlm_cache_lookup(cache, "bare123", is_public=True)
    assert hit is not None and _migrated in cache
    cache.save()

    store._conn.close()
    reopened = VlmCacheStore(path)
    got = reopened.get_many(["bare123", _migrated])
    assert got[_migrated]["visual_summary"] == "遗留裸条目", "迁移键随 save 落盘"


def test_concurrent_writes_under_funnel_concurrency(_no_oss, tmp_path):
    """8 线程（RAG_VLM_CONCURRENCY 形态）并发写 + 并发 per-doc save 不丢条目不撕裂。"""
    path = str(tmp_path / "vlm_cache.sqlite3")
    store = VlmCacheStore(path)
    cache = TrackedVlmCache(store)
    N_THREADS, N_KEYS = 8, 50
    errors = []

    def _worker(tid):
        try:
            for i in range(N_KEYS):
                cache[f"t{tid}k{i}:pub"] = _entry(f"thread{tid} img{i}")
            cache.save()   # 模拟每文档保存
        except Exception as e:   # pragma: no cover - 失败时收集证据
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache.save()   # 收尾 flush 残余脏键
    assert not errors
    expected = N_THREADS * N_KEYS
    assert len(cache) == expected
    assert store.count() == expected, "并发写全部持久化，无丢失"


def test_eviction_cap_env(_no_oss, monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_VLM_CACHE_MAX_ENTRIES", "10")
    store = VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    cache = TrackedVlmCache(store)
    for i in range(25):
        cache[f"k{i:02d}:pub"] = _entry(f"img{i}")
        cache.save()   # 逐文档保存 → 落盘顺序 = 写入顺序（rowid 序确定可断言）
    assert store.count() == 10, "超上限裁到 RAG_VLM_CACHE_MAX_ENTRIES"
    kept = store.get_many([f"k{i:02d}:pub" for i in range(25)])
    assert set(kept) == {f"k{i:02d}:pub" for i in range(15, 25)}, "淘汰最旧、保留最新"


def test_fail_open_on_corrupted_db(_no_oss, tmp_path):
    """坏库文件 → 退化内存 dict（fail-open）：提取主流程照常读写，绝不抛错。"""
    bad = tmp_path / "vlm_cache.sqlite3"
    bad.write_bytes(b"this is not a sqlite database " * 10)
    store = VlmCacheStore(str(bad))
    assert store.backend == "memory"
    cache = TrackedVlmCache(store)
    cache["k:pub"] = _entry()
    cache.save()   # 不抛
    assert cache.get("k:pub")["status"] == "ROUTE_TO_VECTOR"


# ─── simulate / 未加载 契约 ─────────────────────────────────────────────────

def test_flush_noop_without_doc_saves(monkeypatch, tmp_path):
    """simulate 契约：全程无 per-doc save（旧 _vlm_cache_oss_dirty==0）→ run 结束
    flush 零动作：不落 sqlite、不推 OSS——「simulate 不落盘」逐字保留。"""
    puts = {"n": 0}

    class _B:
        def put_object_from_file(self, key, path):
            puts["n"] += 1

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_oss_bucket",
                        lambda ctx=None: (_B(), False))
    store = VlmCacheStore(str(tmp_path / "vlm_cache.sqlite3"))
    cache = TrackedVlmCache(store)
    monkeypatch.setattr(vc, "_cache", cache)
    cache["sim:pub"] = _entry("simulate 期间的内存写")
    vc.flush_vlm_cache_to_oss()
    assert puts["n"] == 0 and store.count() == 0, "无账（simulate）→ flush 零动作"


def test_save_and_flush_noop_when_never_loaded(monkeypatch):
    """缓存从未加载（_cache=None）时 save/flush 不隐式创建单例、不触盘。"""
    monkeypatch.setattr(vc, "_cache", None)
    vc.save_vlm_cache()
    vc.flush_vlm_cache_to_oss()
    assert vc._cache is None


def test_default_path_in_scratch():
    p = vc.default_cache_path()
    assert os.path.basename(p) == "vlm_cache.sqlite3"
    assert os.path.basename(os.path.dirname(p)) == "scratch", "与 embedding_cache 同目录"
