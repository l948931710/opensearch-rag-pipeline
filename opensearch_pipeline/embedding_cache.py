# -*- coding: utf-8 -*-
"""
embedding_cache.py — 摄取侧 embedding 持久缓存（SQLite KV，perf#18/#19/#20）

替换 scratch/embedding_cache.json 的整读整写形态。旧形态的三宗罪：
  1. 每次 node_generate_embeddings 都 json.load 整个文件（注释自述上限 ~220MB）——
     orchestrator drain 循环里每批重复付百 MB 级解析 CPU + 峰值内存；
  2. _save_cache 全量重写——每批一次 220MB dumps + 落盘；
  3. 无跨运行持久层——生产 DataWorks Serverless 每次运行都是新 pod，本地文件永远冷，
     重灌/重跑全额重付 DashScope embedding 调用费。

新形态：
  · SQLite 单文件（scratch/embedding_cache.sqlite3），WAL 日志 —— 按需点查/增量写，
    崩溃安全由 SQLite 日志原生保证（等价旧实现的 temp+os.replace 原子写不变量）；
  · 进程级单例（get_embedding_cache）——drain 循环同进程反复进 node 只打开一次；
  · 首次打开自动从存量 embedding_cache.json 迁移（仅当 sqlite 为空；JSON 原样保留，
    tests/eval 脚本仍可独立读写它——两边都是 advisory 缓存，允许分叉）；
  · OSS 镜像（processing/cache/embedding_cache.sqlite3，默认开，RAG_EMBED_CACHE_OSS_MIRROR=false
    关闭）：打开时本地缺文件则从 OSS 拉，finalize 且本次有增量时 checkpoint 后整文件推回——
    serverless 跨运行复用已算 embedding。simulate 模式经 clients._get_oss_bucket 的 is_sim
    短路，绝不触网。

键/值契约与旧 JSON 完全一致（调用方零迁移）：
  key   = md5(f"{model}_{text}")，sparse 条目前缀 "sp_"
  value = dense: List[float]；sparse: {"indices": [...], "values": [...]}（JSON 编码存储）

任何 SQLite/文件系统故障 → 退化为进程内 dict（no-persist），绝不阻断 embedding 主流程
（graceful degradation 项目惯例）。
"""

import json
import logging
import os
import threading
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

OSS_MIRROR_KEY = "processing/cache/embedding_cache.sqlite3"


def _mirror_enabled() -> bool:
    return os.environ.get("RAG_EMBED_CACHE_OSS_MIRROR", "true").strip().lower() in (
        "1", "true", "yes")


class EmbeddingCacheStore:
    """线程安全 SQLite KV（内部单锁串行化——embedding 负载是网络瓶颈，锁开销可忽略）。

    SQLite 打开/建表失败时自动退化为内存 dict（backend='memory'，无持久化）。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._dirty_puts = 0          # 本进程累计新写条数（决定 finalize 是否推 OSS 镜像）
        self._mem: Optional[dict] = None   # 退化后端
        self._conn = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._pull_oss_mirror_if_absent()
            import sqlite3
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS embedding_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            self._conn.commit()
            self._migrate_legacy_json_if_empty()
        except Exception as e:
            logger.warning("embedding cache sqlite 打开失败，退化为进程内 dict（本次不持久化）: %s", e)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._mem = {}

    # ── 基本操作 ────────────────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        return "sqlite" if self._conn is not None else "memory"

    def count(self) -> int:
        with self._lock:
            if self._conn is None:
                return len(self._mem)
            return int(self._conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])

    def get_many(self, keys: Iterable[str]) -> Dict[str, object]:
        """批量点查；只返回命中项（值已 json 解码）。"""
        keys = [k for k in keys if k]
        if not keys:
            return {}
        with self._lock:
            if self._conn is None:
                return {k: self._mem[k] for k in keys if k in self._mem}
            out: Dict[str, object] = {}
            CHUNK = 500   # SQLite 变量数上限（默认 999）以内分批
            for i in range(0, len(keys), CHUNK):
                part = keys[i:i + CHUNK]
                ph = ",".join(["?"] * len(part))
                try:
                    rows = self._conn.execute(
                        f"SELECT k, v FROM embedding_cache WHERE k IN ({ph})", part).fetchall()
                except Exception as e:
                    logger.warning("embedding cache 读失败（视为 miss）: %s", e)
                    return out
                for k, v in rows:
                    try:
                        out[k] = json.loads(v)
                    except Exception:
                        continue   # 单条坏值当 miss，绝不拖垮整批
            return out

    def put_many(self, mapping: Dict[str, object]) -> None:
        """批量写入（INSERT OR REPLACE，一次 commit）。失败仅告警——缓存是 advisory。"""
        if not mapping:
            return
        with self._lock:
            if self._conn is None:
                self._mem.update(mapping)
                self._dirty_puts += len(mapping)
                return
            try:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embedding_cache (k, v) VALUES (?, ?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in mapping.items()])
                self._conn.commit()
                self._dirty_puts += len(mapping)
            except Exception as e:
                logger.warning("embedding cache 写失败（跳过 %d 条，不影响主流程）: %s",
                               len(mapping), e)

    def evict_to(self, max_entries: int) -> int:
        """裁到容量上限，淘汰最旧（rowid 最小；REPLACE 会刷新 rowid → 近似 LRU-on-write）。"""
        if max_entries <= 0:
            return 0
        with self._lock:
            if self._conn is None:
                excess = len(self._mem) - max_entries
                for k in list(self._mem.keys())[:max(0, excess)]:
                    del self._mem[k]
                return max(0, excess)
            try:
                n = self.count() - max_entries
                if n <= 0:
                    return 0
                self._conn.execute(
                    "DELETE FROM embedding_cache WHERE rowid IN "
                    "(SELECT rowid FROM embedding_cache ORDER BY rowid ASC LIMIT ?)", (n,))
                self._conn.commit()
                print(f"    └─ Evicted {n} oldest embedding cache entries (cap: {max_entries})")
                return n
            except Exception as e:
                logger.warning("embedding cache 淘汰失败（忽略）: %s", e)
                return 0

    def finalize(self, max_entries: int) -> None:
        """run 收尾：容量裁剪 + （有增量时）checkpoint 并推 OSS 镜像。幂等可多次调用。"""
        self.evict_to(max_entries)
        if self._dirty_puts > 0:
            self._push_oss_mirror()

    # ── 存量 JSON 迁移 ──────────────────────────────────────────────────────

    def _migrate_legacy_json_if_empty(self) -> None:
        if self._conn is None or self.count() > 0:
            return
        legacy = os.path.join(os.path.dirname(self.path), "embedding_cache.json")
        if not os.path.exists(legacy):
            return
        try:
            size_mb = os.path.getsize(legacy) / 1024 / 1024
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                return
            with self._lock:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embedding_cache (k, v) VALUES (?, ?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()])
                self._conn.commit()
            print(f"    └─ Migrated {len(data)} entries from legacy embedding_cache.json "
                  f"({size_mb:.1f}MB) → sqlite（JSON 原样保留供 eval 脚本使用）")
        except Exception as e:
            logger.warning("legacy embedding_cache.json 迁移失败（从空缓存开始）: %s", e)

    # ── OSS 镜像（serverless 跨运行持久化；仿 VLM cache 模式）────────────────

    def _pull_oss_mirror_if_absent(self) -> None:
        """本地缺文件时从 OSS 拉镜像；任何失败静默（首次运行/无镜像属正常）。"""
        if os.path.exists(self.path) or not _mirror_enabled():
            return
        try:
            from opensearch_pipeline.clients import _get_oss_bucket
            bucket, is_sim = _get_oss_bucket()
            if is_sim or bucket is None:
                return
            bucket.get_object_to_file(OSS_MIRROR_KEY, self.path)
            size_mb = os.path.getsize(self.path) / 1024 / 1024
            print(f"    └─ Pulled embedding cache mirror from OSS {OSS_MIRROR_KEY} ({size_mb:.1f}MB)")
        except Exception:
            # OSS 上不存在或拉取失败 → 冷启动；确保不留半截文件坑掉 sqlite open
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except Exception:
                pass

    def _push_oss_mirror(self) -> None:
        """checkpoint（把 WAL 折进主文件）后整文件推 OSS；失败仅告警，本地副本仍在。"""
        if self._conn is None or not _mirror_enabled():
            return
        try:
            from opensearch_pipeline.clients import _get_oss_bucket
            bucket, is_sim = _get_oss_bucket()
            if is_sim or bucket is None:
                return
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
                bucket.put_object_from_file(OSS_MIRROR_KEY, self.path)
                self._dirty_puts = 0
            size_mb = os.path.getsize(self.path) / 1024 / 1024
            print(f"    └─ Pushed embedding cache mirror to OSS {OSS_MIRROR_KEY} ({size_mb:.1f}MB)")
        except Exception as e:
            logger.warning("embedding cache OSS 镜像上传失败（本地副本无损）: %s", e)


# ── 进程级单例（perf#19：drain 循环同进程反复进 node 只打开/迁移一次）────────────
_store: Optional[EmbeddingCacheStore] = None
_store_lock = threading.Lock()


def default_cache_path() -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "scratch", "embedding_cache.sqlite3")


def get_embedding_cache() -> EmbeddingCacheStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = EmbeddingCacheStore(default_cache_path())
        return _store


def _reset_embedding_cache() -> None:
    """测试隔离用：关闭并丢弃单例。"""
    global _store
    with _store_lock:
        if _store is not None:
            try:
                if _store._conn is not None:
                    _store._conn.close()
            except Exception:
                pass
            _store = None
