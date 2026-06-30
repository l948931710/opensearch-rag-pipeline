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

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 100
_ID_SCAN_BUCKET = 500          # PK 区间扫描桶大小（≤500/桶 → 桶内召回完整，不受 ANN 上限影响）
_ID_SCAN_HEADROOM = 1000       # 扫到 MAX(chunk_meta.id)+headroom，兜住边界


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


def _enumerate_ha3_pks(client, cfg, parse, output_fields, query_cls, id_hi: int,
                       bucket: int = _ID_SCAN_BUCKET, max_rounds: int = 3,
                       id_lo: int = 0) -> dict:
    """PK 区间扫描 → {pk:int -> (chunk_id, doc_id)}。零向量 + 小区间 filter。

    ⚠️ G30: a single zero-vector scan is **non-deterministic / incomplete** — it can
    return a different partial subset each call (and right after a realtime push it may
    return nothing at all). So we **loop each bucket until stable**: re-scan and union
    the ids until a round adds nothing new (or max_rounds). Unioning is safe because the
    only consumer (reconcile) deletes PKs absent from chunk_meta(active) under G1/G3
    guards — more-complete enumeration finds more true orphans, never an active id.

    NOTE for *verification* (confirming a doc IS present), do NOT rely on this scan —
    use a per-PK point-read (filter id=<pk>), which is authoritative.

    id_lo: 起始 PK（默认 0）。Stage-3 推送后校验只需扫本批 [min_pk, max_pk] 窗口，
    传 id_lo=min(expected) 避免从 0 扫整个 id 空间（"廉价 hint" 才真的廉价）。
    """
    from opensearch_pipeline.config import get_config
    dim = get_config().embedding.dimension   # 向量维度读配置，勿硬编码 1024
    out = {}
    start = id_lo
    while start < id_hi:
        for _ in range(max(1, max_rounds)):
            before = len(out)
            req = query_cls(table_name=cfg.table_name, vector=[0.0] * dim, top_k=bucket + 100,
                            include_vector=False, output_fields=output_fields,
                            filter=f"id>={start} AND id<{start + bucket}")
            for r in parse(client.query(req)):
                try:
                    out[int(r.get("id"))] = (r.get("chunk_id", ""), r.get("doc_id", ""))
                except (TypeError, ValueError):
                    pass
            if len(out) == before:   # this round surfaced nothing new → bucket stable
                break
        start += bucket
    return out


def reconcile_ha3_orphan_pks(simulate: bool = None, dry_run: bool = False,
                             batch: int = DEFAULT_BATCH) -> dict:
    """对账 HA3 物理行 vs chunk_meta.id(is_active=1)，删除过时 PK。**永不抛异常**。

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
        from opensearch_pipeline.retriever import _parse_ha3_response, _DEFAULT_OUTPUT_FIELDS
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
            cur.execute("SELECT id, chunk_id, is_active FROM chunk_meta")
            rows = cur.fetchall()
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

    try:
        ha3_map = _enumerate_ha3_pks(client, cfg, _parse_ha3_response, _DEFAULT_OUTPUT_FIELDS,
                                     QueryRequest, id_hi=max_id + _ID_SCAN_HEADROOM)
    except Exception as e:
        result["errors"].append(f"HA3 enumerate failed: {e}")
        conn.close()
        return result

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
