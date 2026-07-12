# -*- coding: utf-8 -*-
"""485 重灌 docs 的 document_version 双 active 收尾（2026-07-12 盲文档事件附带账面缺口）。

背景：7-06 大量重灌对 485 docs version-bump，但版本级 supersede 整体没跑——每 doc
留下 2 个 status='active' 版本行（chunk 级停用已跑，无双服务，纯台账脏）。
修法同 scratch/hold_reconcile（2026-06-21）：**status-only**——每 doc 仅保留
version_no 最大的 active 行，较旧 active 行置 'superseded'。零 chunk/HA3/ACL 触碰。

范围纪律：默认必须给 --docs-file（本事件=485 清单）；全网存量（含窗口外 104 个
6-22 等批次的同款债）经 --all 显式扩围——两批分开收，先窗口后存量。

Usage:
  python scratch/version_supersede_reconcile_20260712.py --docs-file <485.txt> --check
  python scratch/version_supersede_reconcile_20260712.py --docs-file <485.txt> --commit
  python scratch/version_supersede_reconcile_20260712.py --all --check    # 存量全景
回滚：--commit 前自动导出受影响行快照到 scratch/version_supersede_backup_<ts>.json
（恢复=按快照把 status 写回）。
"""
import argparse
import json
import sys
import time
from datetime import date

sys.path.insert(0, ".")

KN = "fuling_knowledge"
WINDOW = ("2026-07-06 12:00:00", "2026-07-07 12:00:00")   # 重灌版本行 created_at 窗口


def _targets(cur, docs=None):
    """双 active docs：保留 max(version_no)，其余 active 行为收尾目标。docs=范围清单。"""
    scope = ""
    params = []
    if docs:
        scope = f" AND dv.doc_id IN ({','.join(['%s']*len(docs))})"
        params = list(docs)
    cur.execute(f"""
        SELECT dv.doc_id, dv.version_no, dv.status, dv.created_at
        FROM {KN}.document_version dv
        JOIN (SELECT doc_id FROM {KN}.document_version
              WHERE status='active' GROUP BY doc_id HAVING COUNT(*)>1) d
          ON d.doc_id = dv.doc_id
        WHERE dv.status='active'{scope}
        ORDER BY dv.doc_id, dv.version_no""", params)
    rows = cur.fetchall()
    by_doc = {}
    for r in rows:
        by_doc.setdefault(r["doc_id"], []).append(r)
    targets = []
    for doc, vs in by_doc.items():
        keep = max(v["version_no"] for v in vs)
        for v in vs:
            if v["version_no"] != keep:
                targets.append({"doc_id": doc, "version_no": v["version_no"],
                                "keep": keep, "created_at": str(v["created_at"])})
    return targets, len(by_doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--docs-file", help="范围清单（每行一个 doc_id）——本事件=485 盲文档清单")
    ap.add_argument("--all", action="store_true", help="全网存量（须显式；与 --docs-file 互斥）")
    args = ap.parse_args(argv)
    if not (args.check or args.commit):
        ap.error("--check 或 --commit 必选其一")
    if bool(args.docs_file) == bool(args.all):
        ap.error("--docs-file 与 --all 二选一（范围必须显式）")
    docs = None
    if args.docs_file:
        docs = sorted({ln.strip() for ln in open(args.docs_file) if ln.strip()})
        print(f"[SCOPE] 清单 {len(docs)} docs（{args.docs_file}）")

    if args.check or not args.commit:
        from opensearch_pipeline.prod_access import get_prod_readonly_conn
        conn = get_prod_readonly_conn(dict_cursor=True)
    else:
        from opensearch_pipeline.prod_access import get_prod_rw_conn
        conn = get_prod_rw_conn(ack=f"PROD-RW:{date.today().isoformat()}", dict_cursor=True)
    try:
        with conn.cursor() as cur:
            targets, n_docs = _targets(cur, docs)
            print(f"[{'CHECK' if not args.commit else 'COMMIT'}] 双 active docs={n_docs}，"
                  f"将置 superseded 的旧版本行={len(targets)}")
            for t in targets[:10]:
                print(f"   {t['doc_id']}  v{t['version_no']} → superseded (keep v{t['keep']})")
            if len(targets) > 10:
                print(f"   …（其余 {len(targets)-10} 行）")
            if not args.commit:
                return 0
            ts = time.strftime("%Y%m%d_%H%M%S")
            bak = f"scratch/version_supersede_backup_{ts}.json"
            json.dump(targets, open(bak, "w"), ensure_ascii=False, indent=1)
            print(f"[BACKUP] {bak}（{len(targets)} 行原状态快照）")
            n = 0
            for t in targets:
                cur.execute(
                    f"UPDATE {KN}.document_version SET status='superseded' "
                    "WHERE doc_id=%s AND version_no=%s AND status='active'",
                    (t["doc_id"], t["version_no"]))
                n += cur.rowcount
            conn.commit()
            print(f"[COMMIT] 置 superseded {n} 行")
            targets2, n_docs2 = _targets(cur, docs)
            print(f"[AFTER] 双 active docs={n_docs2}（应为 0）")
            return 0 if n_docs2 == 0 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
