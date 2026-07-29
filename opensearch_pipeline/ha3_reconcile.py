# -*- coding: utf-8 -*-
"""
ha3_reconcile.py — HA3↔RDS 物理行对账：删除 chunk_meta 已不认账的过时 PK（自愈式）。

成因（2026-06-15 事故根因）：`node_write_chunk_meta` 对 chunk_id 做 DELETE→INSERT，
每次重切片给同一 chunk_id 重新分配 `chunk_meta.id`（= HA3 主键）；新 chunk 以新 id 推进 HA3
（新物理行），旧 id 的 HA3 行只有当存在"更旧版本"时才会被 `node_deactivate_old_chunks` 删除。
**同版本（v3→v3）重灌、或 chunk_meta 被清后重灌**时，没有"更旧版本"可删 → 旧 PK 成为孤儿，
与新 PK 在 HA3 并存（同 chunk_id 双行）。这是 spot_checker.reconcile_* 家族的新成员，定期自愈。

唯一安全删除身份 = **HA3 主键 id（INT，非 chunk_id）**——按 chunk_id 删会把新好行一起删掉。
  stale = HA3 物理行的 id ∉ chunk_meta.id(is_active=1)

与 `reconcile_stranded_versions` 同型：只读真相(RDS) → 删 HA3 → **永不抛异常、fail-open**。
三道安全闸（每次实时复算，绝不信任落盘）：
  G1  目标 id ∈ rds_active 一律不删（live kept）—— 还有删除集硬断言兜底
  G3  dup 子类：其 chunk_id 当前 active id 必须已在 HA3，否则跳过（不丢"替换尚未落地"的 chunk）
  G-env `assert_destructive_write_allowed`（PROD-RO 拒、非生产需当日 ack；production 放行）

用法（standalone，默认 dry-run 只读统计）：
  RAG_ENV=... python -m opensearch_pipeline.ha3_reconcile            # dry-run
  RAG_ENV=... python -m opensearch_pipeline.ha3_reconcile --commit   # 真删（受 G-env 守卫）
集成：spot_checker.run_spot_check_pipeline 在 reconcile 段调用 reconcile_ha3_orphan_pks()。
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 100
_ID_SCAN_BUCKET = 500          # PK 区间扫描桶大小（≤500/桶 → 桶内召回完整，不受 ANN 上限影响）
_ID_SCAN_HEADROOM = 1000       # 扫到 MAX(chunk_meta.id)+headroom，兜住边界


def _duration_alert_threshold_s() -> float:
    """P2-29：全 id 空间扫描时长告警阈值（秒），与 reconcile.py 共用同一 env——
    RAG_RECONCILE_DURATION_ALERT_S，默认 1800；<=0 停用。（刻意本地小函数而非 import
    reconcile：本模块保持 standalone、fail-open，不引入横向依赖。）"""
    try:
        return float(os.environ.get("RAG_RECONCILE_DURATION_ALERT_S", "1800"))
    except (TypeError, ValueError):
        return 1800.0


def _alert_scan_duration(elapsed_s: float, buckets: int) -> None:
    """扫描耗时超阈值 → 一条 warning 级 ops 告警（fail-open；send_ops_alert 自身受配置门控）。"""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        send_ops_alert(
            "reconcile 扫描超时长阈值（ha3-orphan）",
            f"elapsed={elapsed_s:.0f}s > 阈值 {_duration_alert_threshold_s():.0f}s；"
            f"buckets_scanned={buckets}。id 空间随重切 churn 单调增长（MAX(id) 永增）——"
            f"孤儿方向必须全扫（见 reconcile_ha3_orphan_pks 注释），扫描成本失控时应"
            f"清理孤儿收敛 id 空间，或调大 RAG_RECONCILE_DURATION_ALERT_S。",
            severity="warning", dedup_key="reconcile:duration:ha3-orphan")
    except Exception:  # noqa: BLE001 — 告警失败绝不影响对账本体
        logger.warning("[RECONCILE-HA3] duration-alert dispatch failed (non-fatal)", exc_info=True)


def _classify_stale(ha3_map: dict, rds_active_ids: set, rds_active_chunkid: dict):
    """PURE（无 I/O，单测入口）。判定哪些 HA3 物理行该删。

    Args:
        ha3_map:            {pk:int -> (chunk_id:str, doc_id:str)} HA3 全量物理行
        rds_active_ids:     chunk_meta.id where is_active=1（应在 HA3 的 id 真相集）
        rds_active_chunkid: {chunk_id -> id} where is_active=1（G3 用：chunk_id→当前 active id）

    Returns:
        (delete_pks: sorted list[int], skipped: dict)
    """
    ha3_pks = set(ha3_map)
    delete_pks = []
    skipped = {"dup_replacement_absent": 0}
    for pk, (chunk_id, _doc) in ha3_map.items():
        if pk in rds_active_ids:                       # G1：live kept，绝不删
            continue
        cur = rds_active_chunkid.get(chunk_id)
        if cur is not None and cur not in ha3_pks:     # G3：该 chunk_id 的新 id 还没进 HA3 → 别删旧载体
            skipped["dup_replacement_absent"] += 1
            continue
        delete_pks.append(pk)
    # G1 硬不变量：删除集与 active 集必须无交集
    assert not (set(delete_pks) & rds_active_ids), "SAFETY: active id leaked into delete set"
    return sorted(delete_pks), skipped


class Ha3EnumerationUnhealthy(RuntimeError):
    """倒排枚举协议不健康（见 clients.ha3_enumerate_bucket 的健康判据）。

    **故意抛异常而非返回结构体**：保持返回契约为普通 dict（调用方可直接 `set(...)`），
    结构体会把 dict 的键当 PK 用。

    唯一生产消费方 = `reconcile_ha3_orphan_pks`：捕获本异常后 **零删除**（不可信的候选集 ×
    不可逆删除 = 绝不允许）。
    注（2026-07-22 C2）：stage-3 推送后校验此前也消费本函数作"廉价 hint"、并靠 except
    降级为逐 PK point-read——该分支已随 C2 删除（改为对全部已知 PK 直接官方 fetch）。"""


def _enumerate_ha3_pks(client, cfg, parse, output_fields, query_cls, id_hi: int,
                       bucket: int = _ID_SCAN_BUCKET, max_rounds: int = 3,
                       id_lo: int = 0) -> dict:
    """PK 区间扫描 → {pk:int -> (chunk_id, doc_id)}。**纯倒排单页枚举**（2026-07-22 起）。

    ⚠️ 旧实现是零向量 + 小区间 filter，并靠 loop-until-stable 兜 G30 的欠返回。
    2026-07-22 终局定性推翻：零向量与任何向量内积恒为 0 ⇒ 全部得分并列 ⇒ 返回哪个子集
    由 ANN 遍历/剪枝路径决定，是**任意但确定的子集**（重扫恒缺同一批）——loop-until-stable
    对确定性欠返回完全无效。现改用 clients.ha3_enumerate_bucket（锚点 is_active 双态、
    单页不翻页、桶宽 ≤500）。`parse`/`query_cls`/`max_rounds` 保留仅为签名兼容并被忽略。

    **存在性判定**：确认某行 IS present 一律用官方 `/vector-service/fetch`
    （clients.ha3_fetch_by_pks）——**不要用零向量 point-read**：实测 `id=28681` 零向量
    +filter 返回 0 命中而 fetch 正常取回，那种 point-read 会把在场行误判为缺失。
    （原注释称 point-read "authoritative" 是错的，已按实证更正。）

    id_lo: 起始 PK（默认 0）。当前唯一生产消费方 reconcile_ha3_orphan_pks 走 id_lo=0
    全扫（孤儿 PK 可能低于当前 MIN(chunk_meta.id)）。

    Raises: Ha3EnumerationUnhealthy —— 任一桶协议不健康。**绝不回退零向量**。
    """
    from opensearch_pipeline.clients import HA3_ENUM_BUCKET, ha3_enumerate_bucket

    bucket = min(int(bucket or HA3_ENUM_BUCKET), HA3_ENUM_BUCKET)
    out = {}
    start = id_lo
    while start < id_hi:
        end = min(start + bucket, id_hi)      # 严格半开，末桶不越界
        res = ha3_enumerate_bucket(client, cfg.table_name, start, end, output_fields)
        if not res["healthy"]:
            raise Ha3EnumerationUnhealthy(f"bucket [{start},{end}): {res['reason']}")
        for pk, r in res["rows"].items():
            out[pk] = (r.get("chunk_id", ""), r.get("doc_id", ""))
        start = end
    return out


def reconcile_ha3_orphan_pks(simulate: bool = None, dry_run: bool = True,
                             batch: int = DEFAULT_BATCH) -> dict:
    """对账 HA3 物理行 vs chunk_meta.id(is_active=1)，删除过时 PK。**永不抛异常**。

    ⚠️ `dry_run` 默认 **True**（2026-07-22 起，此前是 False）：HA3 删除不可逆，无参调用
    绝不能真删——任何删除入口必须**显式**传 `dry_run=False`，且经既有 gate
    `RAG_STAGE3_ORPHAN_PURGE`（orchestrator 入口）授权。

    枚举 fail-closed：底层倒排枚举任一桶协议不健康 → `Ha3EnumerationUnhealthy` →
    本函数捕获后 **deleted=0** 并记 error，绝不在不可信的枚举结果上执行不可逆删除。

    Returns: {"checked": int, "stale": int, "deleted": int, "skipped": dict, "errors": [str]}
    """
    from opensearch_pipeline.config import get_config

    result = {"checked": 0, "stale": 0, "deleted": 0, "skipped": {}, "errors": []}
    config = get_config()
    if simulate is None:
        simulate = config.simulate_opensearch
    if simulate:
        logger.info("[RECONCILE-HA3] simulate=True → no-op")
        return result

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn, _get_opensearch_client
        # 解析器/字段清单取自 clients（HA3 客户端层）；对账扫描 pin 自己的最小字段集
        # （HA3_PARITY_OUTPUT_FIELDS：id/chunk_id/doc_id 为主），serving 调整默认清单不影响
        # orphan 判定与删除口径（见 clients.py 常量注释）。
        from opensearch_pipeline.clients import (
            HA3_PARITY_OUTPUT_FIELDS as _PARITY_OUTPUT_FIELDS,
            parse_ha3_response as _parse_ha3_response,
        )
        from alibabacloud_ha3engine_vector.models import QueryRequest, PushDocumentsRequest
    except Exception as e:  # 依赖缺失：fail-open
        result["errors"].append(f"import failed: {e}")
        return result

    client = _get_opensearch_client()
    if client == "MOCK_HA3_CLIENT" or not hasattr(client, "push_documents"):
        # 标准 OpenSearch 走 delete_by_query 的 version 路径，无此孤儿问题；mock 直接跳过
        result["errors"].append("non-HA3/mock client; reconcile is HA3-only, skipped")
        return result

    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            # perf#90：消费方 _classify_stale 只需 active 集合（rds_active_ids/rds_active_chunkid），
            # 历史 inactive 行拉回来只是被下面的 r[2]==1 丢弃 → 在 SQL 侧过滤，与 ~L190 的
            # 二次确认读对齐。单测 fake cursor 按 SELECT 列形态区分两次读（本读三列，二次读两列）。
            cur.execute("SELECT id, chunk_id, is_active FROM chunk_meta WHERE is_active=1")
            rows = cur.fetchall()
            # MAX(id) 仍取全表：孤儿 PK 可能大于 max(active id)，扫描上界必须覆盖 inactive 行
            cur.execute("SELECT MAX(id) FROM chunk_meta")
            max_id = int(cur.fetchone()[0] or 0)
    except Exception as e:
        result["errors"].append(f"RDS read failed: {e}")
        if conn:
            conn.close()
        return result

    rds_active_ids = {int(r[0]) for r in rows if r[2] == 1}
    rds_active_chunkid = {r[1]: int(r[0]) for r in rows if r[2] == 1}
    cfg = config.alibaba_vector

    # P2-29：孤儿方向必须保持 id_lo=0 全扫——孤儿 PK 正是 DELETE→INSERT churn 后【低于】
    # 当前 MIN(chunk_meta.id) 的旧 id（node_write_chunk_meta 每次重切给同 chunk_id 分配新 id），
    # 从 MIN(id) 起扫会永久漏掉本工具存在的意义（低位孤儿永不清）。上界已按 MAX(id)+headroom
    # 有界；无界的是耗时随 id 空间线性增长 → 记录桶数/耗时进 result，超阈值发 warning 告警。
    _id_hi = max_id + _ID_SCAN_HEADROOM
    _t0 = time.monotonic()
    try:
        ha3_map = _enumerate_ha3_pks(client, cfg, _parse_ha3_response, _PARITY_OUTPUT_FIELDS,
                                     QueryRequest, id_hi=_id_hi)
    except Ha3EnumerationUnhealthy as e:
        # fail-closed：枚举不可信时**零删除**（不可信的候选集 × 不可逆删除 = 绝不允许）
        result["errors"].append(f"HA3 enumerate unhealthy → 本轮零删除: {e}")
        result["enum_health"] = "unhealthy"
        result["elapsed_s"] = round(time.monotonic() - _t0, 3)
        conn.close()
        return result
    except Exception as e:
        result["errors"].append(f"HA3 enumerate failed: {e}")
        result["elapsed_s"] = round(time.monotonic() - _t0, 3)
        conn.close()
        return result
    _elapsed = time.monotonic() - _t0
    result["buckets_scanned"] = len(range(0, _id_hi, _ID_SCAN_BUCKET))
    result["elapsed_s"] = round(_elapsed, 3)
    _thr = _duration_alert_threshold_s()
    if _thr > 0 and _elapsed > _thr:
        _alert_scan_duration(_elapsed, result["buckets_scanned"])

    result["checked"] = len(ha3_map)
    delete_pks, skipped = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    result["stale"] = len(delete_pks)
    result["skipped"] = skipped

    if dry_run or not delete_pks:
        conn.close()
        logger.info("[RECONCILE-HA3] checked=%d stale=%d skipped=%s (dry_run=%s)",
                    result["checked"], result["stale"], skipped, dry_run)
        return result

    # G-env：破坏性写守卫（PROD-RO 拒；非生产需当日 ack；production 放行）
    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
    try:
        assert_destructive_write_allowed("search_delete", cfg.endpoint or cfg.instance_id, kind="search")
    except Exception as e:
        result["errors"].append(f"destructive guard blocked: {e}")
        conn.close()
        return result

    # TOCTOU 二次确认：枚举 HA3 期间（loop-until-stable，可达数十秒）Stage-3 可能并发推入新 chunk
    # —— 新 id 已进 HA3 但不在【枚举前】拍的 chunk_meta 快照里 → 被误判 orphan 删掉（在线 chunk 凭空
    # 消失，召回丢失）。删除前用【最新】chunk_meta 重算 active 真相再判一次，剔除窗口内新增/复活的 id
    # 与 chunk_id。残余窗口（重读→push 删除）仅毫秒级，且只朝"少删"偏（fail-closed）。
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, chunk_id FROM chunk_meta WHERE is_active=1")
            fresh = cur.fetchall()
        fresh_active_ids = {int(r[0]) for r in fresh}
        fresh_active_chunkid = {r[1]: int(r[0]) for r in fresh}
    except Exception as e:
        result["errors"].append(f"RDS re-read failed (fail-closed, skip delete): {e}")
        conn.close()
        return result
    delete_pks, skipped = _classify_stale(ha3_map, fresh_active_ids, fresh_active_chunkid)
    born = result["stale"] - len(delete_pks)
    if born > 0:
        skipped["born_during_scan"] = born
        logger.info("[RECONCILE-HA3] 二次确认：剔除 %d 个枚举窗口内复活/新增的 PK（不删在线 chunk）", born)
    result["stale"] = len(delete_pks)
    result["skipped"] = skipped
    if not delete_pks:
        conn.close()
        return result

    for i in range(0, len(delete_pks), batch):
        sub = delete_pks[i:i + batch]
        body = [{"cmd": "delete", "fields": {cfg.pk_field: pk}} for pk in sub]
        try:
            resp = client.push_documents(cfg.table_name, cfg.pk_field, PushDocumentsRequest(body=body))
            sc = getattr(resp, "status_code", 200)
            msg = (str(getattr(resp, "body", "")) + str(getattr(resp, "text", ""))).lower()
            ok = (200 <= sc < 300) or any(k in msg for k in ("not_found", "no_op", "no-op"))
            if not ok:
                raise RuntimeError(f"status={sc} body={msg[:160]}")
            result["deleted"] += len(sub)
        except Exception as e:
            result["errors"].append(f"delete batch {i // batch}: {e}")

    conn.close()
    logger.info("[RECONCILE-HA3] checked=%d stale=%d deleted=%d skipped=%s errors=%d",
                result["checked"], result["stale"], result["deleted"], skipped, len(result["errors"]))
    return result


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="HA3 orphan-PK reconcile (default dry-run)")
    ap.add_argument("--commit", action="store_true", help="真正删除（默认 dry-run 只统计）")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = ap.parse_args()
    rep = reconcile_ha3_orphan_pks(dry_run=not args.commit, batch=args.batch)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
