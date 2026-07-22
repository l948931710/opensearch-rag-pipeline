#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 RDS 真相重建 HA3 缺失 chunk 的补救 runner（P2-3：有检测、无补救的缺失半环）。

reconcile.py (CS3) 能【发现】「RDS active+INDEXED 但 HA3 缺失」的召回丢失
（rds_active_missing / vanished_docs），但 stage-3 loader 只认领
chunk_meta.index_status IN ('NOT_INDEXED','FAILED')（dataworks_orchestrator.py:429/647）
——卡在 INDEXED 的 chunk 永远不会被重推。本脚本把目标 chunk 的
chunk_meta.index_status 复位为 NOT_INDEXED（与 ACL outbox 标脏同款，见
allowed_depts_reconcile / access_grants），下一次 stage-3 drain 重嵌入重推即自愈。

**绝不直接写/删 HA3**——推送仍走既有 stage-3 管线（含 embedding zombie 闸、parity 校验
与 deactivate 不变量）。document_version 不需要复位：stage-3 锁对 SUCCESS 有 relock 分支
（pipeline_nodes.node_acquire_index_lock），残留 NOT_INDEXED chunk 会被照常认领。

四种目标来源（互斥）：
  --from-reconcile parity.json   吃 reconcile CLI --json 报告（顶层 {"ha3": {...}} 或裸
                                 parity 报告均可）：rds_active_missing[].id ∪
                                 vanished_docs[].doc_id 的全部 active+INDEXED chunk
  --docs doc1,doc2               显式文档集：这些文档全部 active+INDEXED chunk 复位
  --probe                        现场只读跑一遍 reconcile.run_parity_check() 再按报告定位
  --stale-embedding              嵌入制度漂移选择器（盲区审计 P3-7 的"读者"半边）：
                                 行级真值列 embedding_model/embedding_dimension 与当前
                                 config 解析值不一致的 active+INDEXED chunk。模型/维度
                                 升级后跑一遍即得精确重嵌入范围（有意不比对
                                 embedding_version 指纹串——历史行是旧常量格式，
                                 按串比对会把全语料误判为陈旧）

守卫（house style = reset_for_rechunk 的 prod_access 门 + retention 的双门）：
  · dry-run 默认：只读预览将复位的行（逐文档计数 + skip 原因），不写任何东西；
  · --commit 需 (1) PROD_RW_ACK=PROD-RW:<today>（prod_access RW 当日令牌），
             (2) env_guard.assert_destructive_write_allowed 通过
                 （PROD-RO 拒；非生产→生产目标需当日 RAG_DESTRUCTIVE_PROD_ACK）；
  · 行级资格闸（读侧分类 + 写侧 UPDATE 重断言，TOCTOU 只朝「少写」偏）：
      is_active=1                       —— 绝不复活退役/隔离 chunk（PII 隔离文档的
                                           chunk 全是 is_active=0，重推 = 泄漏）
      AND index_status='INDEXED'        —— NOT_INDEXED/FAILED 本就可被 stage-3 认领，
                                           报 already_claimable 即可，不重复写
      AND document_meta.status='active' —— 绝不复活退役文档
      AND document_version.index_status ∉ (PENDING_DELETE, DELETED)
      （PROCESSING 的文档 stage-3 正在跑：跳过并提示稍后重跑，避免与在途推送互踩）

用法：
  python scripts/rebuild_from_rds.py --probe                        # 现场比对，只读
  python scripts/rebuild_from_rds.py --from-reconcile parity.json   # 预览（dry-run）
  PROD_RW_ACK=PROD-RW:$(date +%F) \\
      python scripts/rebuild_from_rds.py --from-reconcile parity.json --commit
"""
import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opensearch_pipeline.reindex_states import (  # noqa: E402
    ChunkIndexStatus,
    DocVersionIndexStatus,
)

WRITE_BATCH = 500

# document_version 侧不允许复活的终态（HA3 删除在途/已删——重推会跟删除赛跑）
_DV_SKIP_STATUS = (DocVersionIndexStatus.PENDING_DELETE, DocVersionIndexStatus.DELETED)


def _targets_from_parity_report(report: Dict[str, Any]) -> Tuple[List[int], List[str]]:
    """解析 reconcile parity 报告 → (缺失 chunk 的 chunk_meta.id 列表, vanished doc_id 列表)。

    兼容两种形态：reconcile CLI --json 的顶层 {"ha3": {...}, "oss": ..., "raw": ...}，
    或 run_parity_check 返回的裸报告。error/skipped 报告返回空目标集。

    终局(2026-07-21「存在性判定唯 fetch 为准」)：报告带 fetch 二次定性且成功时，目标 =
    missing_confirmed ∪ unclassified 的 **PK 精确集**，坚决排除 query_invisible（fetch 已
    证实在场，重推是安慰剂——历次"疗法"即此）；doc 级扩张一律不用——confirmed PK 已精确
    命名丢失面，纯 NOT_INDEXED 的 vanished 行本就处于 stage-3 可认领状态，整篇扩张只会把
    盲区伪影行也复位重推。无 fetch 定性（旧版报告 / fetch 失败）→ 维持 query 单口径目标
    （含 doc 扩张）+ 醒目 WARNING——这是 fetch 端点不可用且真丢失时的灾备路径，写入仍受
    dry-run 默认 + PROD_RW_ACK 双门保护。
    """
    if isinstance(report.get("ha3"), dict):
        report = report["ha3"]
    if report.get("error") or report.get("skipped"):
        return [], []
    fr = report.get("fetch_reclassify") or {}
    if fr.get("ok"):
        pks = sorted({int(x) for x in (fr.get("missing_confirmed") or [])}
                     | {int(x) for x in (fr.get("unclassified") or [])})
        n_uncls = len(fr.get("unclassified") or [])
        if n_uncls:
            print(f"[rebuild] ⚠️ 目标含 {n_uncls} 个 fetch 未定性 PK（fetch 批次异常，"
                  f"无法证实在场）——复核 fetch_errors 后再 --commit。")
        n_inv = len(fr.get("query_invisible") or [])
        if n_inv:
            print(f"[rebuild] ℹ️ 已排除 {n_inv} 个 query 枚举盲区 PK（fetch 复核在场，"
                  f"数据无缺失，无需重推）。")
        return pks, []
    if report.get("rds_active_missing") or report.get("vanished_docs"):
        print("[rebuild] ⚠️ 报告无 fetch 二次定性（旧版报告或 fetch 失败）——以下目标为 "
              "query 单口径，可能包含枚举盲区伪影（数据实际在场）；仅在确认 fetch 不可用"
              "且需灾备恢复时 --commit。")
    pks = sorted({int(m["id"]) for m in (report.get("rds_active_missing") or [])
                  if isinstance(m, dict) and m.get("id") is not None})
    docs = sorted({v["doc_id"] for v in (report.get("vanished_docs") or [])
                   if isinstance(v, dict) and v.get("doc_id")})
    return pks, docs


def _fetch_candidate_rows(cur, pks: List[int], doc_ids: List[str]) -> List[Dict[str, Any]]:
    """只读拉取候选 chunk 行（含资格判定所需的三方状态列）。DictCursor / tuple 游标均兼容。"""
    cols = ("id", "chunk_id", "doc_id", "version_no", "is_active", "index_status",
            "doc_status", "dv_index_status")
    base = """SELECT cm.id, cm.chunk_id, cm.doc_id, cm.version_no, cm.is_active,
                     cm.index_status,
                     dm.status AS doc_status, dv.index_status AS dv_index_status
                FROM chunk_meta cm
                LEFT JOIN document_meta dm ON dm.doc_id = cm.doc_id
                LEFT JOIN document_version dv
                       ON dv.doc_id = cm.doc_id AND dv.version_no = cm.version_no"""
    rows: Dict[int, Dict[str, Any]] = {}

    def _absorb(raw):
        for r in raw:
            d = r if isinstance(r, dict) else dict(zip(cols, r))
            rows[int(d["id"])] = d

    if pks:
        for i in range(0, len(pks), WRITE_BATCH):
            sub = pks[i:i + WRITE_BATCH]
            ph = ",".join(["%s"] * len(sub))
            cur.execute(f"{base} WHERE cm.id IN ({ph})", tuple(sub))
            _absorb(cur.fetchall())
    if doc_ids:
        ph = ",".join(["%s"] * len(doc_ids))
        cur.execute(f"{base} WHERE cm.doc_id IN ({ph})", tuple(doc_ids))
        _absorb(cur.fetchall())
    return [rows[k] for k in sorted(rows)]


def classify_candidates(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按资格闸把候选行分成 eligible / 各类 skip。纯函数（单测入口）。"""
    eligible: List[Dict[str, Any]] = []
    skipped: Counter = Counter()
    for r in rows:
        if r.get("is_active") != 1:
            skipped["inactive"] += 1                 # 退役/隔离 chunk，绝不复活
        elif r.get("index_status") in (ChunkIndexStatus.NOT_INDEXED, ChunkIndexStatus.FAILED):
            skipped["already_claimable"] += 1        # 下一次 drain 本就会重推
        elif r.get("index_status") != ChunkIndexStatus.INDEXED:
            skipped["chunk_status_other"] += 1       # DELETED 等其他终态
        elif r.get("doc_status") != "active":
            skipped["doc_not_active"] += 1           # 退役文档
        elif r.get("dv_index_status") in _DV_SKIP_STATUS:
            skipped["version_deleting"] += 1         # HA3 删除在途/已删
        elif r.get("dv_index_status") == DocVersionIndexStatus.PROCESSING:
            skipped["stage3_running"] += 1           # 在途推送，稍后重跑本脚本
        else:
            eligible.append(r)
    return {"eligible": eligible, "skipped": dict(skipped)}


def mark_chunks_not_indexed(conn, pk_list: List[int], batch: int = WRITE_BATCH) -> int:
    """复位 chunk_meta.index_status → NOT_INDEXED（ACL outbox 同款标脏）。

    UPDATE 里重断言资格谓词（is_active=1 AND index_status='INDEXED'）：预览与提交之间
    行状态若已变化（stage-3 并发跑完 / 文档被退役），写侧自动少写，绝不会把非 INDEXED
    行错误重置。返回实际改行数。调用方须已通过 PROD_RW_ACK + env_guard 双门。
    """
    n = 0
    for i in range(0, len(pk_list), batch):
        sub = pk_list[i:i + batch]
        ph = ",".join(["%s"] * len(sub))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE chunk_meta SET index_status = '{ChunkIndexStatus.NOT_INDEXED}' "
                f"WHERE id IN ({ph}) AND is_active = 1 "
                f"AND index_status = '{ChunkIndexStatus.INDEXED}'",
                tuple(sub))
            n += cur.rowcount
        conn.commit()
    return n


def _print_preview(verdict: Dict[str, Any]) -> None:
    eligible = verdict["eligible"]
    by_doc: Counter = Counter(r["doc_id"] for r in eligible)
    print(f"[preview] 将复位为 NOT_INDEXED 的 chunk：{len(eligible)} 行 / {len(by_doc)} 篇文档")
    for doc, cnt in sorted(by_doc.items())[:20]:
        print(f"   {str(doc)[:48]:48} {cnt} chunk(s)")
    if len(by_doc) > 20:
        print(f"   ... 另有 {len(by_doc) - 20} 篇")
    if verdict["skipped"]:
        print(f"[preview] 跳过（资格闸）：{verdict['skipped']}")
    if verdict["skipped"].get("stage3_running"):
        print("[preview] ⚠️ stage3_running 的文档正在被 stage-3 推送，稍后重跑本脚本再收尾。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="从 RDS 真相重建 HA3 缺失 chunk（标脏 NOT_INDEXED → stage-3 重推；默认 dry-run）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-reconcile", metavar="PARITY_JSON",
                     help="reconcile --json 报告文件（吃 rds_active_missing + vanished_docs）")
    src.add_argument("--docs", help="逗号分隔的显式 doc_id 集（全部 active+INDEXED chunk 复位）")
    src.add_argument("--probe", action="store_true",
                     help="现场只读跑 reconcile.run_parity_check() 再按报告定位")
    src.add_argument("--stale-embedding", action="store_true",
                     help="选出 embedding_model/dimension 与当前 config 不一致的 "
                          "active+INDEXED chunk（模型/维度升级后的重嵌入范围）")
    ap.add_argument("--commit", action="store_true",
                    help="真写（默认只预览；需 PROD_RW_ACK=PROD-RW:<today> + env_guard 通过）")
    args = ap.parse_args(argv)

    # ── 定位目标 ──
    pks: List[int] = []
    doc_ids: List[str] = []
    if args.from_reconcile:
        with open(args.from_reconcile, encoding="utf-8") as f:
            report = json.load(f)
        pks, doc_ids = _targets_from_parity_report(report)
        if report.get("error") or (isinstance(report.get("ha3"), dict)
                                   and report["ha3"].get("error")):
            print("[rebuild] ✗ parity 报告带 error，拒绝按残缺报告定位目标")
            return 3
    elif args.docs:
        doc_ids = sorted({d.strip() for d in args.docs.split(",") if d.strip()})
    elif args.stale_embedding:
        # P3-7 读者半边：此前 embedding_version/dimension 只写不读，模型/维度换代后
        # 无任何机制能推导重索引范围。按行级真值列 vs 当前 config 比对（NULL = 老到
        # 没记录 → 一并算陈旧），走与其余来源相同的资格闸 + dry-run + 双门。
        from opensearch_pipeline.config import get_config
        _emb_cfg = get_config().embedding
        cur_model, cur_dim = _emb_cfg.model, int(_emb_cfg.dimension)
        print(f"[rebuild] 当前嵌入制度：model={cur_model} dimension={cur_dim}")
        from opensearch_pipeline import prod_access
        ro0 = prod_access.get_prod_readonly_conn()
        try:
            with ro0.cursor() as cur:
                cur.execute(
                    "SELECT id FROM chunk_meta WHERE is_active = 1 "
                    f"AND index_status = '{ChunkIndexStatus.INDEXED}' "
                    "AND (embedding_model IS NULL OR embedding_model <> %s "
                    "     OR embedding_dimension IS NULL OR embedding_dimension <> %s)",
                    (cur_model, cur_dim))
                pks = sorted(int(r["id"] if isinstance(r, dict) else r[0])
                             for r in cur.fetchall())
        finally:
            ro0.close()
        if not pks:
            print("[rebuild] 全部 active+INDEXED chunk 均为当前嵌入制度——无事可做 ✅")
            return 0
    else:  # --probe：现场只读比对（reconcile 自身 fail-open、simulate-safe）
        from opensearch_pipeline.reconcile import run_parity_check
        report = run_parity_check()
        if report.get("skipped") or report.get("error"):
            print(f"[rebuild] ✗ probe 不可用：skipped={report.get('skipped')} "
                  f"error={report.get('error')}")
            return 3
        if report.get("complete") is False:
            print("[rebuild] ✗ probe 报告 incomplete（HA3 桶截断）——按残缺枚举重推会放大误判，中止")
            return 3
        pks, doc_ids = _targets_from_parity_report(report)

    if not pks and not doc_ids:
        print("[rebuild] 目标集为空（无缺失 chunk / 无 vanished 文档）——无事可做 ✅")
        return 0
    print(f"[rebuild] 目标：{len(pks)} 个缺失 chunk PK + {len(doc_ids)} 篇文档")

    # ── 只读预览（prod_access read-only；house style = reset_for_rechunk）──
    from opensearch_pipeline import prod_access
    ro = prod_access.get_prod_readonly_conn()
    try:
        with ro.cursor() as cur:
            rows = _fetch_candidate_rows(cur, pks, doc_ids)
    finally:
        ro.close()
    verdict = classify_candidates(rows)
    _print_preview(verdict)
    eligible_pks = [int(r["id"]) for r in verdict["eligible"]]

    if not args.commit:
        print("\n[preview] DRY RUN——加 --commit（并带 PROD_RW_ACK=PROD-RW:<today>）才会写。")
        return 0
    if not eligible_pks:
        print("[rebuild] 没有可复位的行（全部被资格闸跳过）——不写。")
        return 0

    # ── 双门（retention 同款纪律）：prod_access RW 当日令牌 + env_guard ──
    ack = os.environ.get("PROD_RW_ACK") or os.environ.get("RAG_PROD_RW_ACK")
    if not ack:
        raise SystemExit("--commit 需要环境变量 PROD_RW_ACK=PROD-RW:<today>")
    try:
        _host = prod_access.load_prod_env(".env.production").get("RAG_RDS_HOST", "")
    except Exception:  # noqa: BLE001 — 无 env 文件（非典型环境）→ 用 config 池目标做守卫判定
        from opensearch_pipeline.config import get_config
        _host = get_config().rds.host
    from opensearch_pipeline.env_guard import assert_destructive_write_allowed
    assert_destructive_write_allowed("chunk_reindex_reset", _host, kind="rds")

    print(f"[commit] 即将把 {len(eligible_pks)} 行 chunk_meta.index_status 复位为 "
          f"'{ChunkIndexStatus.NOT_INDEXED}'（绝不直接写 HA3）...")
    rw = prod_access.get_prod_rw_conn(ack=ack)
    try:
        n = mark_chunks_not_indexed(rw, eligible_pks)
    finally:
        rw.close()
    print(f"[commit] 实际复位 {n} 行（预览 {len(eligible_pks)} 行"
          + ("，差额=预览后状态已变化，写侧谓词自动少写" if n != len(eligible_pks) else "")
          + "）。下一次 stage-3 drain（dataworks_orchestrator --stage 3）将重嵌入重推。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
