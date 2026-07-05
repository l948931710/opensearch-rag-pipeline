# -*- coding: utf-8 -*-
"""reconcile.py — Phase-3 CS3: read-only RDS↔HA3 parity reconciler.

The ingestion pipeline is laptop/DataWorks-driven and the cross-store reconcilers run only when
invoked — there is no daily self-heal. This module is the standing parity probe that covers the
**silent-recall-loss direction no other check covers**: an RDS chunk that is active+INDEXED but
absent from HA3 (its vector vanished, yet the doc is "served"). It also surfaces the inverse
(HA3 rows with no RDS-active backing — purge lag / zombies) and the worst case (a doc with RDS-active
chunks but ZERO HA3 rows = fully vanished from search).

Design contract (mirrors qa_logger / audit_log / alerting):
  - **Read-only.** RDS access goes through prod_access.get_prod_readonly_conn (fuling_ro). HA3 is
    queried with include_vector=False, no writes. This module NEVER deletes or deactivates.
  - **Deterministic enumeration.** HA3 is scanned by PK range (`id>=lo AND id<hi`, ≤bucket per call)
    — a zero-vector ANN top_k under-enumerates HNSW (the scratch v1 incident); range filter is
    complete per bucket. A bucket that returns ≥ its cap is flagged `truncated` → report.complete=False.
  - **Fail-open.** run_parity_check never raises to its caller; on any error it returns a report with
    ok=False + error set, and (if alert=True) fires one OBS-4 ops alert. Simulate → skipped no-op.

`compute_parity` is a pure function (no DB/HA3) and is the unit-tested core.

CLI:  python -m opensearch_pipeline.reconcile [--alert] [--json] [--hi N]
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from opensearch_pipeline.reindex_states import ChunkIndexStatus

logger = logging.getLogger(__name__)


def _kb_db() -> str:
    """知识库库名（document_meta/version/chunk_meta 所在库）；经 RAG_RDS_DATABASE 配置（STAGING=_stg）。
    惰性读 config（不在 import 期）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database

_DEFAULT_BUCKET = 500
_HI_HEADROOM = 1000  # scan past max(rds.id) so freshly-pushed-but-unrecorded rows still surface
_OSS_IMAGE_PREFIX = "processing/assets/"  # where active-chunk image_refs[].oss_key live
_OSS_RAW_PREFIX = "raw/"                  # where document_version.raw_key source files live (CS4b)
_HEAD_FANOUT_THRESHOLD = 50               # F#58: above this, LIST-build a set instead of per-key HEAD
_RDS_COLS = ("id", "chunk_id", "doc_id", "version_no", "is_active", "index_status", "chunk_type")


# ── cred portability: run from the laptop (prod_access .env files → dedicated read-only fuling_ro)
# OR inside a DataWorks pod / any host with injected RAG_ env vars (no .env files). Prefer the
# read-only path; fall back to the config/env pool when prod_access finds no .env file. On
# RAG_ENV=prod_ro the config pool is itself SESSION READ ONLY, so read-only safety holds on both
# paths; the reconcilers only ever SELECT / HA3-query / OSS-list. ────────────────────────────────

def _rds_conn():
    from opensearch_pipeline.prod_access import ProdAccessError, get_prod_readonly_conn
    try:
        return get_prod_readonly_conn()
    except ProdAccessError:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        return _get_db_conn(select_db=False)


def _oss_bucket():
    from opensearch_pipeline.prod_access import ProdAccessError, get_prod_oss_bucket
    try:
        return get_prod_oss_bucket()
    except ProdAccessError:
        import oss2
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.oss_url import _ensure_public_endpoint
        from opensearch_pipeline.prod_access import _ReadOnlyBucket
        oc = get_config().oss
        auth = oss2.Auth(oc.access_key_id, oc.access_key_secret)
        return _ReadOnlyBucket(oss2.Bucket(auth, _ensure_public_endpoint(oc.endpoint), oc.bucket_name))


def _as_dict_rows(raw, cols):
    """Normalize cursor rows to dicts regardless of cursor class (prod_access=DictCursor, pool=tuple)."""
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in raw]


def compute_parity(rds_rows: List[Dict[str, Any]],
                   ha3_rows: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Pure parity diff. No I/O.

    Args:
        rds_rows: chunk_meta rows; each needs id, chunk_id, doc_id, version_no, is_active,
                  index_status, chunk_type.
        ha3_rows: pk(int) -> {chunk_id, doc_id, chunk_type, version_no} from the HA3 scan.

    Returns a report dict. `ok` is True iff there is NO recall-loss drift — i.e. no
    `rds_active_missing` AND no `vanished_docs`. HA3 stale rows alone do NOT fail `ok` (purge lag is
    expected and harmless to recall); they are reported for cleanup tracking.
    """
    rds_by_id = {int(r["id"]): r for r in rds_rows}
    active = [r for r in rds_rows if r.get("is_active") == 1]
    active_ids = {int(r["id"]) for r in active}
    active_indexed = {int(r["id"]): r for r in active if r.get("index_status") == ChunkIndexStatus.INDEXED}
    active_chunkids = {r["chunk_id"] for r in active}
    active_by_doc: Dict[str, set] = defaultdict(set)
    for r in active:
        active_by_doc[r["doc_id"]].add(int(r["id"]))

    seen_pks = set(ha3_rows)

    # ── DIRECTION 1 (recall loss): RDS active+INDEXED not present in HA3 ──
    rds_active_missing = [
        {"id": pk, "chunk_id": r["chunk_id"], "doc_id": r["doc_id"],
         "version_no": r.get("version_no"), "chunk_type": r.get("chunk_type")}
        for pk, r in active_indexed.items() if pk not in seen_pks
    ]

    # ── DIRECTION 2 (stale / zombie): HA3 PK with no RDS-active backing ──
    ha3_stale = []
    ha3_kept_by_doc: Counter = Counter()
    for pk, h in ha3_rows.items():
        if pk in active_ids:
            ha3_kept_by_doc[h.get("doc_id")] += 1
            continue
        cid = h.get("chunk_id", "")
        subtype = ("dup" if cid in active_chunkids
                   else "rds_inactive" if pk in rds_by_id
                   else "orphan_chunkid")
        ha3_stale.append({"id": pk, "chunk_id": cid, "doc_id": h.get("doc_id"),
                          "chunk_type": h.get("chunk_type"), "subtype": subtype})

    # ── WORST CASE: doc has RDS-active chunks but ZERO HA3 kept rows (fully vanished) ──
    vanished_docs = [
        {"doc_id": d, "rds_active": len(ids), "ha3_kept": ha3_kept_by_doc.get(d, 0)}
        for d, ids in active_by_doc.items()
        if ids and ha3_kept_by_doc.get(d, 0) == 0
    ]

    # ── INFORMATIONAL: HA3 doc_ids with no RDS-active backing at all ──
    ha3_docs = {h.get("doc_id") for h in ha3_rows.values()}
    orphan_docs = sorted(ha3_docs - set(active_by_doc))

    ok = not rds_active_missing and not vanished_docs
    return {
        "ok": ok,
        "counts": {
            "rds_rows": len(rds_rows),
            "rds_active": len(active_ids),
            "rds_active_indexed": len(active_indexed),
            "ha3_pks": len(ha3_rows),
            "rds_active_missing": len(rds_active_missing),
            "ha3_stale": len(ha3_stale),
            "vanished_docs": len(vanished_docs),
            "orphan_docs": len(orphan_docs),
        },
        "stale_subtypes": dict(Counter(s["subtype"] for s in ha3_stale)),
        "rds_active_missing": rds_active_missing,
        "vanished_docs": vanished_docs,
        "ha3_stale_sample": ha3_stale[:50],
        "orphan_docs_sample": orphan_docs[:50],
    }


def _scan_concurrency() -> int:
    """HA3 桶扫描并发度（perf F#51，只读枚举）。RAG_RECONCILE_SCAN_CONCURRENCY，默认 4；<=1 串行。"""
    try:
        return max(1, int(os.environ.get("RAG_RECONCILE_SCAN_CONCURRENCY", "4")))
    except (TypeError, ValueError):
        return 4


def _new_ha3_client():
    """新建一个独立 HA3 client（并发扫描每线程一个，不共享连接状态）。

    与 retriever._get_ha3_client 的进程级单例刻意区分——单例用于 serving 热路径复用，
    这里的对账扫描需要 per-thread 独立实例（SDK client 未声明线程安全）。配置解析与
    clients._get_opensearch_client 的 HA3 分支同源。
    """
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config as _HA3Config
    from opensearch_pipeline.config import get_config
    cfg = get_config().alibaba_vector
    if not cfg.endpoint:
        raise RuntimeError("HA3 endpoint 未配置，无法进行对账扫描")
    clean_endpoint = cfg.endpoint.replace("http://", "").replace("https://", "")
    return Client(_HA3Config(
        endpoint=clean_endpoint,
        instance_id=cfg.instance_id,
        access_user_name=cfg.access_user_name,
        access_pass_word=cfg.access_pass_word,
    ))


def _scan_ha3_pks(cli, table_name: str, hi: int, *,
                  lo: int = 0, bucket: int = _DEFAULT_BUCKET,
                  max_rounds: int = 3, concurrency: Optional[int] = None,
                  client_factory=None) -> Dict[str, Any]:
    """HA3 PK-range enumeration with G30 mitigation. Returns {"rows": {pk: {...}}, "truncated": [...]}.

    ⚠️ G30: a single zero-vector range scan is non-deterministic — it can under-return BELOW the cap
    (a different partial subset each call, ~nothing right after a realtime push). Trusting one pass
    surfaces phantom 'missing'/'vanished' rows → false OBS-4 recall-loss alerts for chunks that are in
    fact indexed and serving. So we **loop each bucket until stable** — re-scan and union the rows until
    a round adds nothing new (or max_rounds), the same fix ha3_reconcile._enumerate_ha3_pks uses.
    Unioning is safe: the consumer only diffs vs RDS, so more-complete enumeration removes false
    positives, never invents rows. A bucket whose single query reaches its cap is still flagged
    truncated (genuinely > cap rows → caller marks the report incomplete).

    perf F#51：桶间无数据依赖 → RAG_RECONCILE_SCAN_CONCURRENCY（默认 4）线程并发扫桶，
    每线程经 client_factory 建独立 HA3 client（factory 缺失/失败退回共享 cli——MOCK/测试
    路径照跑）；桶内 loop-until-stable 仍严格串行，rows 按桶分片后在主线程合并（PK 范围
    互斥，无锁）。concurrency<=1 或单桶时与旧实现逐字节等价。
    """
    from alibabacloud_ha3engine_vector.models import QueryRequest
    # 解析器仍经 retriever 模块动态取名：这是既有测试的 monkeypatch 席位
    # （test_reconcile.py::test_scan_ha3_pks_loops_until_stable patch 的是
    # retriever._parse_ha3_response）。该名如今是 clients.parse_ha3_response 的 re-export
    # 别名（绑定恒等由 tests/test_ha3_client_coupling.py 看住），不再是 serving 私有实现
    # ——保留此间接层只为 patch 席位，不构成对 serving 内部的语义依赖。
    from opensearch_pipeline import retriever as _retriever
    from opensearch_pipeline.clients import HA3_PARITY_OUTPUT_FIELDS
    from opensearch_pipeline.config import get_config

    # #F-recon-vecdim 向量维度读配置、勿硬编码 1024：EMBEDDING_DIMENSION 可设 768/512，
    # 写死 1024 会让维度不匹配的 HA3 query 报错 → CS3 对账整体 fail-open，召回丢失探针失效
    # （与 ha3_reconcile._enumerate_ha3_pks 的 get_config().embedding.dimension 单一来源对齐）。
    _dim = get_config().embedding.dimension

    cap = bucket + 100
    starts = list(range(lo, hi, bucket))

    def _scan_bucket(bcli, start: int) -> Dict[str, Any]:
        """单桶 loop-until-stable（G30 语义不变）；返回本桶 rows 分片 + truncated 标记。"""
        brows: Dict[int, Dict[str, Any]] = {}
        btrunc = False
        for _ in range(max(1, max_rounds)):
            before = len(brows)
            req = QueryRequest(table_name=table_name, vector=[0.0] * _dim, top_k=cap,
                               include_vector=False,
                               # 对账扫描 pin 死自己的最小字段集（消费 id/chunk_id/doc_id/
                               # chunk_type/version_no），serving 调默认清单不影响对账口径。
                               output_fields=HA3_PARITY_OUTPUT_FIELDS,
                               filter=f"id>={start} AND id<{start + bucket}")
            parsed = _retriever._parse_ha3_response(bcli.query(req))
            if len(parsed) >= cap:
                btrunc = True
            for r in parsed:
                try:
                    pk = int(r.get("id"))
                except (TypeError, ValueError):
                    continue
                brows[pk] = {"chunk_id": r.get("chunk_id", ""), "doc_id": r.get("doc_id", ""),
                             "chunk_type": r.get("chunk_type", ""),
                             "version_no": r.get("version_no")}
            if len(brows) == before:   # round surfaced nothing new → bucket stable
                break
        return {"start": start, "rows": brows, "truncated": btrunc}

    conc = concurrency if concurrency is not None else _scan_concurrency()
    conc = max(1, min(int(conc), len(starts) or 1))

    if conc <= 1 or len(starts) <= 1:
        shards = [_scan_bucket(cli, s) for s in starts]
    else:
        # 每线程独立 client（thread-local 懒建）；factory 缺失或建失败 → 退回共享 cli。
        _tls = threading.local()

        def _thread_cli():
            c = getattr(_tls, "cli", None)
            if c is None:
                try:
                    c = client_factory() if client_factory is not None else cli
                except Exception:  # noqa: BLE001 — fail-open：建 client 失败退回共享实例
                    c = cli
                _tls.cli = c
            return c

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=conc) as pool:
            shards = list(pool.map(lambda s: _scan_bucket(_thread_cli(), s), starts))

    rows: Dict[int, Dict[str, Any]] = {}
    truncated: List[int] = []
    for sh in shards:                    # 主线程合并：桶 PK 范围互斥，顺序=桶序（确定性）
        rows.update(sh["rows"])
        if sh["truncated"]:
            truncated.append(sh["start"])
    return {"rows": rows, "truncated": truncated}


def _rds_batch_width() -> int:
    """P2-30：RDS↔HA3 对齐分桶宽度（服务端 id-range 分页）。RAG_RECONCILE_RDS_BATCH，默认 5000。"""
    try:
        return max(_DEFAULT_BUCKET, int(os.environ.get("RAG_RECONCILE_RDS_BATCH", "5000")))
    except (TypeError, ValueError):
        return 5000


def _scan_from_min() -> bool:
    """P2-29 起点优化开关（默认关）。只对 DIRECTION 1（RDS→HA3 缺失/召回丢失）安全：
    HA3 孤儿 PK 恰恰是 DELETE→INSERT churn 后【低于】当前 MIN(chunk_meta.id) 的旧 id，
    从 MIN 起扫会漏掉低位 stale/orphan 检出（报告会标 stale_scan_curtailed）。"""
    return os.environ.get("RAG_RECONCILE_SCAN_FROM_MIN", "").lower() in ("1", "true", "yes")


def _duration_alert_threshold_s() -> float:
    """P2-29：全 id 空间扫描时长告警阈值（秒）。RAG_RECONCILE_DURATION_ALERT_S，默认 1800；
    <=0 停用。id 空间随重切 churn 只增不减（MAX(id) 永增），超阈值说明扫描成本已失控。"""
    try:
        return float(os.environ.get("RAG_RECONCILE_DURATION_ALERT_S", "1800"))
    except (TypeError, ValueError):
        return 1800.0


def _alert_on_duration(check: str, elapsed_s: float, buckets: int) -> None:
    """扫描耗时超阈值 → 一条 warning 级 ops 告警（fail-open；send_ops_alert 自身受配置门控）。"""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        send_ops_alert(
            f"reconcile 扫描超时长阈值（{check}）",
            f"elapsed={elapsed_s:.0f}s > 阈值 {_duration_alert_threshold_s():.0f}s；"
            f"buckets_scanned={buckets}。id 空间随重切 churn 单调增长（MAX(id) 永增）——"
            f"考虑清理孤儿收敛 id 空间，或调大 RAG_RECONCILE_DURATION_ALERT_S。",
            severity="warning", dedup_key=f"reconcile:duration:{check}")
    except Exception:  # noqa: BLE001 — 告警失败绝不影响对账本体
        logger.warning("reconcile: duration-alert dispatch failed (non-fatal)", exc_info=True)


class _ParityAccumulator:
    """P2-30 流式 parity 累积器：与 compute_parity 同语义，但按对齐 id 桶逐桶喂入、逐桶释放。

    等价性依赖两个不变量：
      1. RDS 桶与 HA3 桶共用同一 [start, end) id 区间——pk 数值恒等、跨桶不可能互相匹配，
         故 rds_inactive 子类的「pk 是否存在于 chunk_meta」可用桶内 id 集合等价判定；
      2. dup 子类（chunk_id 是否 active）与 vanished/orphan 判定（每文档 active 计数 vs
         HA3 kept 计数）是跨桶属性，由构造时传入的全局【轻量】真相承担——只驻留 int 集合 /
         chunk_id 字符串集合 / 每文档 Counter，绝不驻留整行 dict（那正是 OOM 的内存大头）。
    峰值内存 O(单桶行数) + O(active 轻量集合)。compute_parity 保留为纯函数单测核心，
    tests/test_reconcile_scan_metrics.py 用同一 fixture 断言两者输出逐键一致。
    """

    def __init__(self, active_ids: set, active_chunkids: set,
                 active_doc_counts: Counter):
        self._active_ids = active_ids
        self._active_chunkids = active_chunkids
        self._active_doc_counts = active_doc_counts
        self._rds_rows = 0
        self._rds_active_indexed = 0
        self._ha3_pks = 0
        self._missing: List[Dict[str, Any]] = []
        self._stale_count = 0
        self._stale_sample: List[Dict[str, Any]] = []
        self._stale_subtypes: Counter = Counter()
        self._ha3_kept_by_doc: Counter = Counter()
        self._ha3_docs: set = set()

    def add_bucket(self, rds_rows: List[Dict[str, Any]],
                   ha3_rows: Dict[int, Dict[str, Any]]) -> None:
        """喂入一个对齐 id 桶的 RDS 行 + HA3 行；调用后两者即可释放。"""
        self._rds_rows += len(rds_rows)
        bucket_rds_ids = set()
        for r in rds_rows:
            pk = int(r["id"])
            bucket_rds_ids.add(pk)
            if r.get("is_active") == 1 and r.get("index_status") == ChunkIndexStatus.INDEXED:
                self._rds_active_indexed += 1
                if pk not in ha3_rows:   # DIRECTION 1：召回丢失
                    self._missing.append({
                        "id": pk, "chunk_id": r["chunk_id"], "doc_id": r["doc_id"],
                        "version_no": r.get("version_no"), "chunk_type": r.get("chunk_type")})
        self._ha3_pks += len(ha3_rows)
        for pk, h in ha3_rows.items():
            self._ha3_docs.add(h.get("doc_id"))
            if pk in self._active_ids:
                self._ha3_kept_by_doc[h.get("doc_id")] += 1
                continue
            cid = h.get("chunk_id", "")
            subtype = ("dup" if cid in self._active_chunkids
                       else "rds_inactive" if pk in bucket_rds_ids
                       else "orphan_chunkid")
            self._stale_count += 1
            self._stale_subtypes[subtype] += 1
            if len(self._stale_sample) < 50:
                self._stale_sample.append({"id": pk, "chunk_id": cid, "doc_id": h.get("doc_id"),
                                           "chunk_type": h.get("chunk_type"), "subtype": subtype})

    def finalize(self) -> Dict[str, Any]:
        """汇总为与 compute_parity 完全同构的报告 dict。"""
        vanished = [
            {"doc_id": d, "rds_active": n, "ha3_kept": self._ha3_kept_by_doc.get(d, 0)}
            for d, n in self._active_doc_counts.items()
            if n and self._ha3_kept_by_doc.get(d, 0) == 0
        ]
        orphan_docs = sorted(self._ha3_docs - set(self._active_doc_counts))
        ok = not self._missing and not vanished
        return {
            "ok": ok,
            "counts": {
                "rds_rows": self._rds_rows,
                "rds_active": len(self._active_ids),
                "rds_active_indexed": self._rds_active_indexed,
                "ha3_pks": self._ha3_pks,
                "rds_active_missing": len(self._missing),
                "ha3_stale": self._stale_count,
                "vanished_docs": len(vanished),
                "orphan_docs": len(orphan_docs),
            },
            "stale_subtypes": dict(self._stale_subtypes),
            "rds_active_missing": self._missing,
            "vanished_docs": vanished,
            "ha3_stale_sample": self._stale_sample,
            "orphan_docs_sample": orphan_docs[:50],
        }


def run_parity_check(*, alert: bool = False, hi: Optional[int] = None,
                     bucket: int = _DEFAULT_BUCKET) -> Dict[str, Any]:
    """Top-level CS3 reconcile: read RDS (read-only) + scan HA3 + diff. Fail-open, simulate-safe.

    P2-30（流式分桶）：不再整表 fetchall + 全量 HA3 map 同持内存——RDS 侧按 id 区间服务端
    分批 SELECT（宽度 RAG_RECONCILE_RDS_BATCH，默认 5000），HA3 侧按同一区间桶扫，对齐桶
    逐桶 diff、桶用完即释放。报告结构与旧实现一致（下游/测试不破），另附
    scan_lo / buckets_scanned / elapsed_s / rds_batch。

    P2-29（扫描界与时长告警）：起点默认仍从 0 全扫——本扫描同时负责 DIRECTION 2 的
    HA3 stale/orphan 检出，而孤儿 PK 恰恰是重切 churn 后低于当前 MIN(chunk_meta.id) 的旧 id，
    从 MIN 起扫会漏检；只关心召回丢失方向时可设 RAG_RECONCILE_SCAN_FROM_MIN=true
    （报告标 stale_scan_curtailed）。耗时超 RAG_RECONCILE_DURATION_ALERT_S（默认 1800s）
    → 一条 warning 级 ops 告警。

    Returns the parity report enriched with `complete` (False if any HA3 bucket truncated)
    and, on failure, `error`. Never raises. When alert=True and drift (recall-loss) is detected — or
    the run errors — fires a single OBS-4 ops alert (itself fail-open / config-gated).
    """
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    if cfg.simulate or cfg.simulate_db or cfg.simulate_opensearch:
        logger.info("reconcile: simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}

    t0 = time.monotonic()
    buckets_scanned = 0
    conn = None
    try:
        from opensearch_pipeline.retriever import _get_ha3_client

        step = _rds_batch_width()
        conn = _rds_conn()
        with conn.cursor() as c:
            # P2-29：上下界改服务端聚合（旧实现整表 fetchall 后在 Python 求 max）。
            c.execute(f"SELECT MIN(id), MAX(id) FROM {_kb_db()}.chunk_meta")
            row = c.fetchone() or (None, None)
            vals = list(row.values()) if isinstance(row, dict) else list(row)
            min_id = int(vals[0] or 0)
            max_id = int(vals[1] or 0)

            # 跨桶全局轻量 active 真相（见 _ParityAccumulator 不变量 2）：同样按 id 区间分批读，
            # 只积累集合/计数——峰值不随全表行数持整行 dict。
            active_ids: set = set()
            active_chunkids: set = set()
            active_doc_counts: Counter = Counter()
            p = min_id
            while max_id and p <= max_id:
                c.execute(
                    f"SELECT id, chunk_id, doc_id FROM {_kb_db()}.chunk_meta"
                    f" WHERE is_active=1 AND id>=%s AND id<%s", (p, p + step))
                for r in _as_dict_rows(c.fetchall(), ("id", "chunk_id", "doc_id")):
                    active_ids.add(int(r["id"]))
                    active_chunkids.add(r["chunk_id"])
                    active_doc_counts[r["doc_id"]] += 1
                p += step

        scan_hi = hi if hi is not None else (max_id + _HI_HEADROOM)
        scan_lo = min_id if (_scan_from_min() and min_id) else 0

        cli = _get_ha3_client()
        acc = _ParityAccumulator(active_ids, active_chunkids, active_doc_counts)
        truncated: List[int] = []

        start = scan_lo
        while start < scan_hi:
            end = min(start + step, scan_hi)
            bucket_rds: List[Dict[str, Any]] = []
            if max_id and start <= max_id and end > min_id:   # 与表 id 域有交集才查 RDS
                with conn.cursor() as c:
                    c.execute(
                        f"""SELECT id, chunk_id, doc_id, version_no, is_active,
                                  index_status, chunk_type
                             FROM {_kb_db()}.chunk_meta WHERE id>=%s AND id<%s""",
                        (start, end))
                    bucket_rds = _as_dict_rows(c.fetchall(), _RDS_COLS)
            # perf F#51：并发扫桶时每线程经 _new_ha3_client 建独立 client（默认并发 4，只读）。
            scan = _scan_ha3_pks(cli, cfg.alibaba_vector.table_name, end, lo=start,
                                 bucket=bucket, client_factory=_new_ha3_client)
            acc.add_bucket(bucket_rds, scan["rows"])
            truncated.extend(scan["truncated"])
            buckets_scanned += len(range(start, end, bucket))
            start = end

        report = acc.finalize()
        report["complete"] = not truncated
        report["truncated_buckets"] = truncated
        report["scan_hi"] = scan_hi
        report["scan_lo"] = scan_lo
        if scan_lo:
            report["stale_scan_curtailed"] = True   # 低于 min_id 的 stale/orphan 本轮未检
        report["rds_batch"] = step
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.exception("reconcile: parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # P2-29：耗时/桶数进报告；超阈值一条 warning 告警（不依赖 --alert，fail-open）。
    elapsed = time.monotonic() - t0
    report["elapsed_s"] = round(elapsed, 3)
    report["buckets_scanned"] = buckets_scanned
    thr = _duration_alert_threshold_s()
    if thr > 0 and elapsed > thr:
        _alert_on_duration("rds-ha3-parity", elapsed, buckets_scanned)

    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_drift(report)
    return report


# ──────────────────────────────────────────────────────────────────────────────
# CS4 — OSS↔RDS image-object parity (the third store)
# ──────────────────────────────────────────────────────────────────────────────

def collect_referenced_image_keys(rds_rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Parse active chunks' extra_json image_refs[].oss_key → {oss_key: sample_chunk_id}.

    Only is_active=1 rows count — an inactive chunk's image being absent is not a serving defect.
    Fail-open per row: a malformed extra_json is skipped, not fatal.
    """
    import json
    out: Dict[str, str] = {}
    for r in rds_rows:
        if r.get("is_active") != 1:
            continue
        raw = r.get("extra_json")
        if not raw or "oss_key" not in raw:
            continue
        try:
            ej = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            continue
        ej_dict = ej if isinstance(ej, dict) else {}
        # 顶层 oss_key：独立 image chunk (pipeline_nodes ~4413) 和 step-card 派生 visual_knowledge
        # (chunker ~1244) 把图键存在 extra 顶层，而非 image_refs[]。这些是非 step DOCX/PDF / 独立
        # 图文档的【主要】图片载体，之前被下面的 image_refs 列表门 (isinstance(refs,list)) 整行跳过
        # → OSS 对账在最易坏图处假性返回 ok=True，坏图告警从不触发。只取 oss_key（present 是 OSS
        # key 集合）；source_image 是 URL，不与 present 同域，加入会误报 missing。
        _top_k = ej_dict.get("oss_key")
        if isinstance(_top_k, str) and _top_k and _top_k not in out:
            out[_top_k] = r.get("chunk_id", "")
        refs = ej_dict.get("image_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, dict):
                k = ref.get("oss_key")
                if k and k not in out:
                    out[k] = r.get("chunk_id", "")
    return out


def compute_oss_parity(referenced: Dict[str, str], present: set, *,
                       verify_fn=None) -> Dict[str, Any]:
    """OSS parity diff. Pure when verify_fn is None.

    Args:
        referenced: {oss_key: sample_chunk_id} that active chunks point at (ANY prefix).
        present: set of oss_keys actually listed in OSS under the image prefix.
        verify_fn: optional callable(key)->bool returning True iff the object EXISTS. CRITICAL:
            `present` only covers the listed prefix, so a referenced key under a DIFFERENT prefix
            (e.g. raw/marketing/*.jpg vs processing/assets/*) would be a FALSE 'missing' on the raw
            set-diff. When verify_fn is given, each set-diff candidate is HEAD-checked and kept only
            if it truly does NOT exist. Without verify_fn this returns the raw candidates (unit tests).

    `ok` is True iff no referenced key is truly missing from OSS (broken-image / serving defect).
    Orphan OSS objects (present but unreferenced) are storage bloat — reported, do NOT fail ok.
    """
    ref_keys = set(referenced)
    candidates = sorted(ref_keys - present)
    if verify_fn is not None:
        missing = [k for k in candidates if not verify_fn(k)]
    else:
        missing = candidates
    orphan = sorted(present - ref_keys)
    return {
        "ok": not missing,
        "counts": {
            "referenced": len(ref_keys),
            "present": len(present),
            "candidates_offprefix": len(candidates) - len(missing) if verify_fn else 0,
            "missing": len(missing),
            "orphan": len(orphan),
        },
        "missing": [{"oss_key": k, "chunk_id": referenced.get(k, "")} for k in missing[:50]],
        "orphan_sample": orphan[:50],
    }


def _list_oss_keys(bucket, prefix: str = _OSS_IMAGE_PREFIX) -> set:
    """Paginated read-only ListObjects under prefix → set of object keys.

    perf#95：max_keys=1000（服务端上限）——ObjectIterator 默认 100/页，全量 LIST 多打 10 倍
    请求数；CS4 图片前缀与 F#57/58 的 raw/、候选前缀 LIST 共用本函数，一并受益。
    """
    import oss2
    keys = set()
    for obj in oss2.ObjectIterator(bucket, prefix=prefix, max_keys=1000):
        keys.add(obj.key)
    return keys


def run_oss_parity_check(*, alert: bool = False,
                         prefix: str = _OSS_IMAGE_PREFIX) -> Dict[str, Any]:
    """CS4: read active-chunk image keys (RDS, read-only) + list OSS image objects + diff.
    Read-only (prod_access read-only bucket blocks all writes), simulate-safe, fail-open.
    """
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    if cfg.simulate or cfg.simulate_db:
        logger.info("reconcile(oss): simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}

    try:
        conn = _rds_conn()
        try:
            with conn.cursor() as c:
                c.execute(f"""SELECT chunk_id, is_active, extra_json
                             FROM {_kb_db()}.chunk_meta
                             WHERE is_active=1 AND extra_json LIKE %s""", ("%oss_key%",))
                rds_rows = _as_dict_rows(c.fetchall(), ("chunk_id", "is_active", "extra_json"))
        finally:
            conn.close()

        referenced = collect_referenced_image_keys(rds_rows)
        bucket = _oss_bucket()
        present = _list_oss_keys(bucket, prefix)

        # perf F#58：差集候选异常放大（>50，如整目录被搬走/前缀漂移）时不再逐 key 串行 HEAD——
        # 与 F#57 同型 list-then-diff：对候选的目录前缀（≥2 个候选共享才值回票价）各做一次分页
        # LIST 建 set，命中即存在；仅剩余零散候选逐个 HEAD 确认。常态（候选个位数）零行为变化。
        candidates = set(referenced) - present
        listed_extra: set = set()
        if len(candidates) > _HEAD_FANOUT_THRESHOLD:
            dir_counts = Counter(k.rsplit("/", 1)[0] + "/" for k in candidates if "/" in k)
            for p in sorted(d for d, n in dir_counts.items() if n >= 2):
                try:
                    listed_extra |= _list_oss_keys(bucket, p)
                except Exception:  # noqa: BLE001 — LIST 失败 → 该前缀退回逐 key HEAD（fail-open）
                    logger.warning("reconcile(oss): candidate-prefix LIST failed for %s (fallback to HEAD)", p)

        # HEAD-verify candidates so a referenced key under a different prefix (e.g. raw/*) is not a
        # false 'missing'; only objects that truly don't exist count. object_exists is read-only.
        def _exists(k):
            if k in listed_extra:
                return True
            try:
                return bool(bucket.object_exists(k))
            except Exception:  # noqa: BLE001 — treat HEAD error conservatively as "exists" (no false alarm)
                return True

        report = compute_oss_parity(referenced, present, verify_fn=_exists)
        report["complete"] = True
        report["prefix"] = prefix
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.exception("reconcile(oss): parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}

    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_oss_drift(report)
    return report


def _alert_on_oss_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing OSS image-object drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"OSS parity check errored: {report['error']}"
        else:
            text = (f"active-chunk image oss_keys missing from OSS: **{c.get('missing', 0)}** "
                    f"(broken images); orphan OSS objects: {c.get('orphan', 0)}")
        send_ops_alert("OSS↔RDS image parity drift", text, severity="critical",
                       dedup_key="reconcile:oss-rds-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile(oss): ops-alert dispatch failed (non-fatal)", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# CS4b — raw_key↔OSS parity (the source-file gap CS4 doesn't cover)
# ──────────────────────────────────────────────────────────────────────────────

def compute_raw_parity(rows: List[Dict[str, Any]], exists_fn) -> Dict[str, Any]:
    """Pure (+ injected exists_fn): of current-version active docs, which raw_key OSS objects are
    MISSING. rows: [{doc_id, version_no, raw_key}]; exists_fn(key)->bool (True iff object exists).
    A NULL raw_key is 'unregistered' (reported separately, not a missing-file). CS4 checks image keys
    only; this closes the raw source-file gap (the DC-3-survey 404). Lower severity: a missing raw does
    NOT break serving (canonical/chunks already exist) — it's a re-ingest/audit concern."""
    null_raw = [r for r in rows if not r.get("raw_key")]
    have = [r for r in rows if r.get("raw_key")]
    missing = [r for r in have if not exists_fn(r["raw_key"])]
    return {
        "ok": not missing,
        "counts": {"total": len(rows), "have_raw_key": len(have),
                   "null_raw_key": len(null_raw), "missing": len(missing)},
        "missing": [{"doc_id": r.get("doc_id"), "version_no": r.get("version_no"),
                     "raw_key": r.get("raw_key")} for r in missing[:50]],
        "null_raw_key_sample": [r.get("doc_id") for r in null_raw[:20]],
    }


def run_raw_parity_check(*, alert: bool = False) -> Dict[str, Any]:
    """CS4b: current-version active docs whose raw_key OSS object is missing. Read-only, simulate-safe,
    fail-open.

    perf F#57（list-then-diff，与 CS4 的 _list_oss_keys 模式对齐）：先按 raw/ 前缀分页 LIST 一次
    拉全 key 集合——差集为空即完成（原实现对每篇 active 文档发一次 OSS HEAD，O(文档数×RTT)）；
    仅对差集候选（含前缀外 raw_key，通常个位数）逐个 HEAD 确认，防误报（与 CS4 verify_fn 同语义）。
    LIST 失败时整体退回逐 key HEAD（现状行为，fail-open）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        logger.info("reconcile(raw): simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}
    try:
        conn = _rds_conn()
        try:
            with conn.cursor() as c:
                c.execute(f"""SELECT v.doc_id, v.version_no, v.raw_key
                             FROM {_kb_db()}.document_version v
                             JOIN {_kb_db()}.document_meta m
                               ON m.doc_id=v.doc_id AND m.current_version_no=v.version_no
                             WHERE m.status='active'""")
                rows = _as_dict_rows(c.fetchall(), ("doc_id", "version_no", "raw_key"))
        finally:
            conn.close()
        bucket = _oss_bucket()

        try:
            listed = _list_oss_keys(bucket, _OSS_RAW_PREFIX)
        except Exception:  # noqa: BLE001 — LIST 失败 → 退回逐 key HEAD（现状行为）
            logger.warning("reconcile(raw): raw/ prefix LIST failed, falling back to per-key HEAD")
            listed = set()

        def _exists(k):
            if k in listed:
                return True     # LIST 集合命中即存在（覆盖绝大多数 raw_key，零 HEAD）
            try:
                return bool(bucket.object_exists(k))
            except Exception:  # noqa: BLE001 — conservative: HEAD error → treat as exists (no false alarm)
                return True

        report = compute_raw_parity(rows, _exists)
        report["complete"] = True
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.exception("reconcile(raw): parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}
    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_raw_drift(report)
    return report


def _alert_on_raw_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing raw_key→OSS drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"raw_key parity check errored: {report['error']}"
        else:
            text = (f"active docs whose raw source file is MISSING from OSS: **{c.get('missing', 0)}** "
                    f"(of {c.get('have_raw_key', 0)} with raw_key; {c.get('null_raw_key', 0)} have none)")
        send_ops_alert("raw_key↔OSS parity drift", text, severity="warning",
                       dedup_key="reconcile:raw-oss-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile(raw): ops-alert dispatch failed (non-fatal)", exc_info=True)


def _alert_on_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing recall-loss drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"parity check errored: {report['error']}"
        else:
            text = (f"RDS-active missing from HA3: **{c.get('rds_active_missing', 0)}** chunks; "
                    f"fully-vanished docs: **{c.get('vanished_docs', 0)}**; "
                    f"HA3 stale: {c.get('ha3_stale', 0)}; "
                    f"complete={report.get('complete')}")
        send_ops_alert("RDS↔HA3 parity drift", text, severity="critical",
                       dedup_key="reconcile:rds-ha3-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile: ops-alert dispatch failed (non-fatal)", exc_info=True)


def _exit_code(report: Dict[str, Any]) -> int:
    """0 = ok (or simulate-skipped); 2 = drift; 3 = error/incomplete."""
    if report.get("skipped"):
        return 0
    if report.get("error") or report.get("complete") is False:
        return 3
    return 0 if report.get("ok") else 2


def _print_ha3(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:ha3] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:ha3] ok={report.get('ok')} complete={report.get('complete')}")
    print(f"  RDS rows={c.get('rds_rows')} active={c.get('rds_active')} "
          f"active_indexed={c.get('rds_active_indexed')} | HA3 pks={c.get('ha3_pks')}")
    print(f"  ⚠️ RDS-active MISSING from HA3 = {c.get('rds_active_missing')} (recall loss)")
    print(f"  ⚠️ fully-VANISHED docs = {c.get('vanished_docs')}")
    print(f"  stale HA3 rows = {c.get('ha3_stale')} {report.get('stale_subtypes', {})}")
    print(f"  orphan HA3 docs = {c.get('orphan_docs')}")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    for m in report.get("rds_active_missing", [])[:10]:
        print(f"    MISSING id={m['id']} {m['chunk_id']} type={m['chunk_type']}")
    for v in report.get("vanished_docs", [])[:10]:
        print(f"    VANISHED {v}")


def _print_oss(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:oss] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:oss] ok={report.get('ok')} complete={report.get('complete')}")
    print(f"  referenced image keys={c.get('referenced')} | OSS objects={c.get('present')}")
    print(f"  ⚠️ referenced MISSING from OSS = {c.get('missing')} (broken images)")
    print(f"  orphan OSS objects = {c.get('orphan')}")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    for m in report.get("missing", [])[:10]:
        print(f"    MISSING {m['oss_key']} (chunk={m['chunk_id']})")


def _print_raw(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:raw] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:raw] ok={report.get('ok')} complete={report.get('complete')}")
    print(f"  current-version active docs={c.get('total')} "
          f"(with raw_key={c.get('have_raw_key')}, null={c.get('null_raw_key')})")
    print(f"  ⚠️ raw source MISSING from OSS = {c.get('missing')} (CS4 covers image keys only)")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    for m in report.get("missing", [])[:10]:
        print(f"    MISSING {m['doc_id']} v{m['version_no']} raw_key={m['raw_key']}")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI. Runs the selected cross-store parity check(s). Exit = worst of the run codes
    (0 = ok / simulate-skipped; 2 = drift; 3 = error/incomplete)."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="read-only cross-store parity reconciler (CS3 + CS4 + CS4b)")
    ap.add_argument("--check", choices=["ha3", "oss", "raw", "all"], default="all",
                    help="which parity check to run (default: all)")
    ap.add_argument("--alert", action="store_true", help="fire an OBS-4 ops alert on drift/error")
    ap.add_argument("--json", action="store_true", help="emit the full report(s) as JSON")
    ap.add_argument("--hi", type=int, default=None, help="override HA3 PK scan upper bound")
    args = ap.parse_args(argv)

    reports: Dict[str, Any] = {}
    if args.check in ("ha3", "all"):
        reports["ha3"] = run_parity_check(alert=args.alert, hi=args.hi)
    if args.check in ("oss", "all"):
        reports["oss"] = run_oss_parity_check(alert=args.alert)
    if args.check in ("raw", "all"):
        reports["raw"] = run_raw_parity_check(alert=args.alert)

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        if "ha3" in reports:
            _print_ha3(reports["ha3"])
        if "oss" in reports:
            _print_oss(reports["oss"])
        if "raw" in reports:
            _print_raw(reports["raw"])

    return max((_exit_code(r) for r in reports.values()), default=0)


if __name__ == "__main__":
    import sys
    sys.exit(main())
