# -*- coding: utf-8 -*-
"""
vlm_cache.py — 跨文档 VLM 图片标注持久缓存（SQLite KV，全维度复审「VLM 缓存 SQLite 化」）

替换 scratch/vlm_cache.json 的整读整写形态——embedding 缓存 perf#18 之前的同款病灶：
  1. _save_vlm_cache 在每个发生过 VLM 调用的文档后整包 json.dumps + 重写
     整个 ~2.4MB / ~2100 条缓存文件；
  2. OSS 侧每 10 次保存整包上传一次（perf#27 只降了频，没去掉整包形态）；
  3. 无任何淘汰——成本随语料唯一图片数线性膨胀。

设计取舍（extend vs new module）：底座复用 embedding_cache.SqliteKVStore（本次把既有
EmbeddingCacheStore 参数化抽出的通用 WAL KV 基类）——相比在此复刻 ~150 行同构
SQLite/迁移/镜像/退化代码，参数化底座重复最少，崩溃安全与 fail-open 语义只需一处回归。
本模块只保留 VLM 特有的三件事：

  · 读模型 TrackedVlmCache（dict 子类）：调用方（unified_extractor 的
    _vlm_cache_lookup / 直接 []= 写）的 .get()/[]= 用法与命中语义逐字节不变；
    __setitem__ 记脏，per-doc save() 时只把脏键增量 put_many 进 sqlite——
    旧「每文档整包重写」退化为廉价增量 flush；
  · OSS 镜像与降频（跨机器 warm-start 语义保留）：镜像对象改为 .sqlite 文件本身
    （wal_checkpoint 后单个 PUT，OSS 侧单对象写天然原子；processing/cache/
    vlm_cache.sqlite3）。拉取顺序：本地 sqlite > OSS sqlite 镜像 > 本地 legacy
    JSON 迁移 > OSS legacy JSON（processing/cache/vlm_cache.json）迁移——新 pod
    首跑（OSS 侧尚无 sqlite 镜像的窗口期）仍能吃到存量 ~2100 条标注。推送节奏沿用
    perf#27：每 RAG_VLM_CACHE_OSS_SYNC_EVERY（默认 10）次 per-doc 保存推一次 +
    run 结束 flush；上传失败不清计数，下一文档自然重试；
  · 容量上限：RAG_VLM_CACHE_MAX_ENTRIES（默认 50000——条目均长 ~1KB，上限从宽），
    近似 LRU-on-write（同 embedding cache：rowid 序淘汰最旧；命中不刷新位次，
    实际是 FIFO——对一次写入终身只读的 VLM 标注足够）。

键/值契约与旧 JSON 完全一致（unified_extractor 零语义迁移）：
  key   = "{图片MD5}:{pub|sec}[:{RAG_VLM_CACHE_VERSION}]"（遗留裸 MD5 兼容照旧，
          见 unified_extractor._vlm_cache_lookup）
  value = funnel 结果 dict（status/visual_summary/ocr_text/width/...，JSON 编码存储）

legacy scratch/vlm_cache.json 原样保留不删（tests/eval 脚本仍可独立读它——advisory
缓存允许分叉，与 embedding 缓存迁移同一约定）。simulate 不落盘契约保留：per-doc
save 只在真实模式被调用（unified_extractor 侧守卫），run 结束 flush 在零 per-doc
save 时零动作。任何 SQLite/OSS 故障 fail-open：退化为纯内存 dict，绝不阻断提取主流程。
"""

import json
import logging
import os
import threading
from typing import Optional

from opensearch_pipeline.embedding_cache import SqliteKVStore

logger = logging.getLogger(__name__)

OSS_MIRROR_KEY = "processing/cache/vlm_cache.sqlite3"
LEGACY_OSS_JSON_KEY = "processing/cache/vlm_cache.json"


def _max_entries() -> int:
    try:
        return int(os.environ.get("RAG_VLM_CACHE_MAX_ENTRIES", "50000"))
    except ValueError:
        return 50000


def _oss_sync_every() -> int:
    """OSS 镜像同步间隔（每 N 次 per-doc 保存一推；≤1 = 每次都推即 perf#27 之前行为）。"""
    try:
        return max(1, int(os.environ.get("RAG_VLM_CACHE_OSS_SYNC_EVERY", "10")))
    except ValueError:
        return 10


class VlmCacheStore(SqliteKVStore):
    """VLM 专用 store：本地 scratch/vlm_cache.sqlite3 + OSS .sqlite 镜像。"""

    TABLE = "vlm_cache"
    LEGACY_JSON_BASENAME = "vlm_cache.json"
    OSS_MIRROR_KEY = OSS_MIRROR_KEY
    MIRROR_ENV = "RAG_VLM_CACHE_OSS_MIRROR"
    LABEL = "VLM cache"

    def _get_bucket(self):
        # 沿用旧 _save_vlm_cache 的 OSS seam：tests/调用方一直 monkeypatch
        # opensearch_pipeline.pipeline_nodes._get_oss_bucket（clients 的 re-export，
        # 见 CLAUDE.md F-A1——patch either path）。惰性导入避免模块级循环。
        from opensearch_pipeline.pipeline_nodes import _get_oss_bucket
        return _get_oss_bucket()

    def _migrate_legacy_json_if_empty(self) -> None:
        super()._migrate_legacy_json_if_empty()   # 1) 本地 scratch/vlm_cache.json
        if self._conn is None or self.count() > 0:
            return
        # 2) 本地无 sqlite/无 JSON、OSS 也无 sqlite 镜像（迁移上线后的首跑窗口）
        #    → 回退拉遗留 OSS JSON，保住旧实现「新 pod 冷启动吃 OSS 缓存」的 warm-start。
        #    put_many 会记 _dirty_puts → 下一次镜像推送即把它固化成 OSS sqlite 镜像。
        try:
            bucket, is_sim = self._get_bucket()
            if is_sim or bucket is None:
                return
            raw = bucket.get_object(LEGACY_OSS_JSON_KEY).read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                self.put_many(data)
                print(f"    └─ Migrated {len(data)} entries from legacy OSS "
                      f"{LEGACY_OSS_JSON_KEY} → sqlite（OSS JSON 原样保留）")
        except Exception:
            # OSS 上不存在 / 网络失败 → 冷缓存，属正常
            pass


class TrackedVlmCache(dict):
    """dict 门面：读走预载内存（get/[] 命中语义与旧 in-memory dict 一致），
    __setitem__ 记脏；save() 把脏键增量落 sqlite 并按 perf#27 节奏推 OSS 镜像。

    ⚠️ 调用方现状只用 .get()/[]=（unified_extractor / _vlm_cache_lookup）；
    update()/setdefault() 等旁路不做脏追踪——新增写法请走 []=。

    并发：8 线程 VLM funnel 的结果写发生在收集线程，但 RAG_EXTRACT_CONCURRENCY>1
    时多文档线程会并发读写同一实例——dict 读写靠 GIL 原子性（与旧实现相同的假设），
    脏集合与 save 全程分别有锁（save 串行化 = 旧 _vlm_cache_save_lock 的等价物）。
    """

    def __init__(self, store: SqliteKVStore):
        super().__init__(store.load_all())
        self._store = store
        self._dirty_lock = threading.Lock()
        self._dirty_keys = set()
        self._save_lock = threading.Lock()
        self._doc_saves_since_push = 0   # perf#27 降频计数（旧 _vlm_cache_oss_dirty）

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        with self._dirty_lock:
            self._dirty_keys.add(key)

    @property
    def pending_oss_saves(self) -> int:
        return self._doc_saves_since_push

    def save(self, force_oss: bool = False) -> None:
        """per-doc 保存触发（旧 UnifiedExtractor._save_vlm_cache 的替身）。

        - 脏键增量 put_many（WAL 增量写代替旧整包 json 重写；无脏零 SQL 写）；
        - 容量裁剪（RAG_VLM_CACHE_MAX_ENTRIES）；
        - OSS 镜像降频推送：非 force 每次调用计数 +1，满 RAG_VLM_CACHE_OSS_SYNC_EVERY
          推一次；force_oss=True（run 结束 flush）有账即推。推送失败不清计数，
          下一文档自然重试（与 perf#27 逐字节同节奏）。
        任何存储失败 fail-open（store 内部已吞），绝不抛给提取主流程。
        """
        with self._save_lock:
            with self._dirty_lock:
                keys = list(self._dirty_keys)
                self._dirty_keys.clear()
            if keys:
                payload = {}
                for k in keys:
                    v = self.get(k)
                    if v is not None:
                        payload[k] = v
                self._store.put_many(payload)
                self._store.evict_to(_max_entries())
            if not force_oss:
                self._doc_saves_since_push += 1
            if self._doc_saves_since_push <= 0:
                return
            if not force_oss and self._doc_saves_since_push < _oss_sync_every():
                return
            if self._store._push_oss_mirror():
                self._doc_saves_since_push = 0


# ── 进程级单例（等价旧 UnifiedExtractor 类属性 _vlm_cache 的共享语义）──────────
_cache: Optional[TrackedVlmCache] = None
_cache_lock = threading.Lock()


def default_cache_path() -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "scratch", "vlm_cache.sqlite3")


def get_vlm_cache() -> TrackedVlmCache:
    """进程级共享的 VLM 缓存 dict 门面（首次调用打开/迁移/预载）。"""
    global _cache
    with _cache_lock:
        if _cache is None:
            store = VlmCacheStore(default_cache_path())
            _cache = TrackedVlmCache(store)
            print(f"      [VLM Cache] Ready: backend={store.backend}, "
                  f"{len(_cache)} cached entries")
        return _cache


def save_vlm_cache(force_oss: bool = False) -> None:
    """per-doc 保存（旧 _save_vlm_cache 语义）；缓存未加载过则零动作。"""
    with _cache_lock:
        cache = _cache
    if cache is None:
        return
    cache.save(force_oss=force_oss)


def flush_vlm_cache_to_oss() -> None:
    """run 结束 flush（perf#27 的尾巴）：把未满一轮的镜像账目推上去。

    无账（含 simulate 全程——per-doc save 只在真实模式发生）→ 零动作，
    保持旧实现「simulate 不落盘」契约。
    """
    with _cache_lock:
        cache = _cache
    if cache is None or cache.pending_oss_saves <= 0:
        return
    cache.save(force_oss=True)


def _reset_vlm_cache() -> None:
    """测试隔离用：关闭并丢弃单例。"""
    global _cache
    with _cache_lock:
        if _cache is not None:
            try:
                if _cache._store._conn is not None:
                    _cache._store._conn.close()
            except Exception:
                pass
            _cache = None
