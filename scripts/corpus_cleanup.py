#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corpus_cleanup.py — 一次性语料清理（2026-06-10 计划批准执行）

两个独立部分：

【B1 注册卫生 — 可执行】
  目标：**零 active chunk** 且属于垃圾/遗留/策略排除的注册行 → 置 status='inactive'。
  选择条件（必须同时满足）：
    - 该 doc_id 在 chunk_meta 无任何 is_active=1 行（硬断言，逐行复核）
    - 且满足其一：raw_key 未过 ingest_policy.should_ingest_raw_key（遗留格式/
      垃圾文件/归档路径）；或 title 以 ~$ 开头（Office 临时文件）
  明确不碰：可处理格式且策略放行但历史抽取失败的 ~7 个"真缺失"文档（留待回灌）。
  审计：变更清单写 docs/cleanup_manifest_<date>.json（doc_id/title/raw_key/reason）。

【B2 近重家族 — 仅预览，绝不执行】
  解析 docs/corpus_cleanup_worklist.md 的三类表格（keep-pdf / keep-hr / keep-newest），
  解析 (title, dept) → doc_id，输出每家族 keep/drop + 5 维 serving 风险评估
  （真实引用量 / 内容覆盖率 / 金集影响 / 未来 ACL 部门暴露 / 检索排名连续性）
  → docs/dedup_families_preview.md。本模式不写任何数据。

用法（RAG_ENV=test = 生产 RDS/HA3，谨慎）：
  python scripts/corpus_cleanup.py --hygiene            # B1 预览（默认只读）
  python scripts/corpus_cleanup.py --hygiene --commit   # B1 执行
  python scripts/corpus_cleanup.py --families-preview   # B2 预览报告（只读）
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("RAG_ENV", "test")

WORKLIST = os.path.join(REPO, "docs", "corpus_cleanup_worklist.md")
GOLDEN = os.path.join(REPO, "eval_harness", "goldset", "golden_full.json")


def _conn(database=None):
    import pymysql
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=database or cfg.rds.database,
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


# ═══════════════════════════ B1 注册卫生 ═══════════════════════════

def hygiene(commit: bool):
    from opensearch_pipeline.ingest_policy import should_ingest_raw_key

    conn = _conn()
    with conn.cursor() as cur:
        # 全部注册行 + active chunk 计数 + 当前版本 raw_key
        cur.execute("""
            SELECT dm.doc_id, dm.title, dm.status AS meta_status,
                   dv.version_no, dv.raw_key, dv.status AS ver_status,
                   dv.content_process_status,
                   (SELECT COUNT(*) FROM chunk_meta cm
                     WHERE cm.doc_id = dm.doc_id AND cm.is_active = 1) AS n_active
            FROM document_meta dm
            JOIN document_version dv
              ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
        """)
        rows = cur.fetchall()

    targets, skipped_missing = [], []
    for r in rows:
        if r["meta_status"] != "active" and r["ver_status"] != "active":
            continue   # 已是终态
        ok, reason = should_ingest_raw_key(r["raw_key"] or "")
        is_temp = os.path.basename(r["raw_key"] or r["title"] or "").startswith("~$")
        if ok and not is_temp:
            if r["n_active"] == 0:
                skipped_missing.append(r)   # 真缺失：策略放行但无 chunk —— 留待回灌
            continue
        if r["n_active"] > 0:
            # 策略排除却有 active chunk —— 绝不自动下线（需人工裁决），只警示
            print(f"  ⚠️ 策略排除但有 {r['n_active']} 个 active chunk，跳过: "
                  f"{r['doc_id']} {r['title'][:40]} ({reason})")
            continue
        targets.append(dict(r, reason=("temp file (~$)" if is_temp else reason)))

    by_reason = defaultdict(int)
    for t in targets:
        by_reason[t["reason"]] += 1
    print(f"[hygiene] 候选 {len(targets)} 行（零 active chunk 且 垃圾/遗留/策略排除）:")
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    {n:4d} × {reason}")
    print(f"[hygiene] 真缺失（策略放行、零 chunk，**不动**，留待回灌）: {len(skipped_missing)}")
    for r in skipped_missing[:10]:
        print(f"    keep: {r['title'][:56]}")

    if not commit:
        print("\n[hygiene] PREVIEW 模式 — 加 --commit 执行")
        return

    # ── 执行：逐行硬断言零 active chunk 后置 inactive ──
    manifest = []
    with conn.cursor() as cur:
        for t in targets:
            cur.execute("SELECT COUNT(*) AS c FROM chunk_meta WHERE doc_id=%s AND is_active=1",
                        (t["doc_id"],))
            assert cur.fetchone()["c"] == 0, f"active chunks appeared for {t['doc_id']} — abort"
            cur.execute("UPDATE document_version SET status='inactive' WHERE doc_id=%s",
                        (t["doc_id"],))
            cur.execute("UPDATE document_meta SET status='inactive' WHERE doc_id=%s",
                        (t["doc_id"],))
            manifest.append({k: t[k] for k in ("doc_id", "title", "raw_key", "reason")})
    conn.commit()

    out = os.path.join(REPO, "docs", f"cleanup_manifest_{datetime.now():%Y%m%d}.json")
    json.dump({"executed_at": datetime.now().isoformat(), "count": len(manifest),
               "action": "document_meta/version status -> inactive", "rows": manifest},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[hygiene] ✅ 已置 inactive: {len(manifest)} 行；清单: {out}")

    # 复核
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS c FROM document_meta dm
            JOIN document_version dv ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
            WHERE dm.status='active'""")
        print(f"[hygiene] 剩余 active 注册: {cur.fetchone()['c']}")
    conn.close()


# ═══════════════════════════ B2 家族预览 ═══════════════════════════

_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\w+)/(\w+)\s*\|\s*(\d+)/(\d+)\s*\|")


def _parse_worklist():
    """解析三类表格 → [{rule, keep_title, drop_title, keep_dept, drop_dept, keep_n, drop_n}]"""
    families, rule = [], None
    rule_map = {"## 1.": "keep-pdf", "## 2.": "keep-hr", "## 3.": "keep-newest",
                "## 4.": None, "## 5.": None}
    for line in open(WORKLIST, encoding="utf-8"):
        for prefix, r in rule_map.items():
            if line.startswith(prefix):
                rule = r
        if rule is None:
            continue
        m = _ROW_RE.match(line.strip())
        if m and m.group(1) != "保留":
            families.append({
                "rule": rule, "keep_title": m.group(1), "drop_title": m.group(2),
                "keep_dept": m.group(3), "drop_dept": m.group(4),
                "keep_n_wl": int(m.group(5)), "drop_n_wl": int(m.group(6)),
            })
    return families


def _resolve_doc(cur, title, dept, exclude_doc_id=None):
    """(title, dept) → (doc_id, n_active_chunks)；同名多行时取 active chunk 最多者。"""
    cur.execute("""
        SELECT dm.doc_id, COUNT(cm.chunk_id) AS n
        FROM document_meta dm
        LEFT JOIN chunk_meta cm ON cm.doc_id = dm.doc_id AND cm.is_active = 1
        WHERE dm.title = %s AND dm.owner_dept = %s AND dm.status = 'active'
        GROUP BY dm.doc_id ORDER BY n DESC""", (title, dept))
    for r in cur.fetchall():
        if r["doc_id"] != exclude_doc_id:
            return r["doc_id"], int(r["n"])
    return None, 0


def _shingles(text, k=8):
    s = re.sub(r"\s+", "", text or "")
    return {s[i:i + k] for i in range(len(s) - k + 1)}


def _coverage(cur, drop_id, keep_id):
    """drop 侧 chunk 文本被 keep 侧覆盖的比例 + 双方 image 资产计数。"""
    def fetch(doc_id):
        cur.execute("""SELECT chunk_text, chunk_type FROM chunk_meta
                       WHERE doc_id=%s AND is_active=1""", (doc_id,))
        rows = cur.fetchall()
        text = "\n".join(r["chunk_text"] or "" for r in rows)
        n_img = sum(1 for r in rows if r["chunk_type"] in ("image", "visual_knowledge"))
        return text, n_img
    drop_text, drop_img = fetch(drop_id)
    keep_text, keep_img = fetch(keep_id)
    ds, ks = _shingles(drop_text), _shingles(keep_text)
    cov = (len(ds & ks) / len(ds)) if ds else 1.0
    return cov, drop_img, keep_img


def _usage(op_cur, doc_id, days=30):
    op_cur.execute(
        "SELECT COUNT(*) AS c FROM qa_session_log "
        "WHERE created_at >= NOW() - INTERVAL %s DAY AND cited_docs_json LIKE %s",
        (days, f"%{doc_id}%"))
    return int(op_cur.fetchone()["c"])


def _rank_probe(query, keep_id, drop_id):
    """对高使用家族做一次真实检索，报告 keep/drop 当前名次（fail-open）。"""
    try:
        from opensearch_pipeline.retriever import search_chunks
        chunks = search_chunks(query, top_k=10)
        ranks = {}
        for i, c in enumerate(chunks, 1):
            d = c.get("doc_id", "")
            if d in (keep_id, drop_id) and d not in ranks:
                ranks[d] = i
        return ranks.get(keep_id), ranks.get(drop_id)
    except Exception as e:
        print(f"    (rank probe failed: {e})")
        return None, None


def families_preview():
    families = _parse_worklist()
    print(f"[families] 解析工单: {len(families)} 个可执行家族")
    golden = json.load(open(GOLDEN, encoding="utf-8"))
    gold_doc_ids = set()
    for case in (golden if isinstance(golden, list) else golden.get("cases", [])):
        gold_doc_ids.update(case.get("expected_doc_ids") or [])

    conn = _conn()
    op_conn = _conn("fuling_operation")
    lines = [
        "# 近重复家族下线预览 + Serving 风险评估",
        "",
        f"生成: {datetime.now():%Y-%m-%d %H:%M} · 只读分析，未做任何变更 · 执行须另行批准",
        "",
        "风险因子: usage=近30天答案引用次数(drop侧) · coverage=drop侧文本被keep侧覆盖率 ·",
        "imgs=图片chunk数(drop/keep) · gold=金集expected_doc命中 · ACL=跨部门(权限上线后暴露) ·",
        "rank=高使用家族的当前检索名次(keep/drop)",
        "",
        "| # | 规则 | 保留 (dept) | 下线 (dept) | chunks 留/撤 | usage | coverage | imgs d/k | gold | ACL | 风险 | 理由 |",
        "|---|------|------------|------------|--------------|-------|----------|----------|------|-----|------|------|",
    ]
    summary = defaultdict(int)
    with conn.cursor() as cur, op_conn.cursor() as op_cur:
        for i, f in enumerate(families, 1):
            keep_id, keep_n = _resolve_doc(cur, f["keep_title"], f["keep_dept"])
            drop_id, drop_n = _resolve_doc(cur, f["drop_title"], f["drop_dept"],
                                           exclude_doc_id=keep_id)
            if not keep_id or not drop_id:
                lines.append(f"| {i} | {f['rule']} | {f['keep_title'][:36]} | {f['drop_title'][:36]} "
                             f"| — | — | — | — | — | — | ⚠️unresolved | doc_id 解析失败，需人工核对 |")
                summary["unresolved"] += 1
                continue
            cov, d_img, k_img = _coverage(cur, drop_id, keep_id)
            usage = _usage(op_cur, drop_id)
            gold_hit = drop_id in gold_doc_ids
            acl = f["keep_dept"] != f["drop_dept"]

            reasons, risk = [], "低"
            if usage > 0:
                # 高使用家族做一次真实检索探针：keep/drop 当前名次（排名连续性证据）
                kq = re.sub(r"[《》().docxpdf]+", " ", f["keep_title"]).strip()[:24]
                rk, rd = _rank_probe(kq, keep_id, drop_id)
                reasons.append(f"当前检索名次 keep@{rk or '∅'} / drop@{rd or '∅'}")
            if cov < 0.90:
                risk = "高"
                reasons.append(f"keep 侧仅覆盖 drop 内容 {cov:.0%}（下线丢内容）")
            if d_img > k_img:
                risk = "高"
                reasons.append(f"drop 侧图片更多 ({d_img}>{k_img})，下线丢图")
            if gold_hit:
                risk = "高" if risk == "高" else "中"
                reasons.append("金集 expected_doc 命中 → 执行前需改指向 keep 侧")
            if usage > 5 and risk == "低":
                risk = "中"
                reasons.append(f"近30天被引用 {usage} 次（高使用）")
            if acl and risk == "低":
                risk = "中"
            if acl:
                reasons.append(f"跨部门 {f['drop_dept']}→{f['keep_dept']}：ACL 上线后 "
                               f"{f['drop_dept']} 用户可能失去访问，执行前确认归属")
            if not reasons:
                reasons.append("内容全覆盖、零引用、同部门 — 安全")
            summary[risk] += 1

            lines.append(
                f"| {i} | {f['rule']} | {f['keep_title'][:36]} ({f['keep_dept']}) "
                f"| {f['drop_title'][:36]} ({f['drop_dept']}) "
                f"| {keep_n}/{drop_n} | {usage} | {cov:.0%} | {d_img}/{k_img} "
                f"| {'是' if gold_hit else '—'} | {'是' if acl else '—'} | {risk} "
                f"| {'；'.join(reasons)} |")
            print(f"  [{i:2d}/{len(families)}] {f['drop_title'][:38]:40s} risk={risk} "
                  f"cov={cov:.0%} usage={usage}")

    lines += [
        "",
        f"## 汇总：高={summary['高']} 中={summary['中']} 低={summary['低']} "
        f"未解析={summary['unresolved']}",
        "",
        "## 排除项（按工单/计划）",
        "- 场区变体 2 对（保安巡查记录表 北门/总表、外来人员入厂告知书 新/松门）— 待业务定夺",
        "- 假阳性 5 组 — 保持不动",
        "",
        "## 执行前置条件（本预览不执行）",
        "1. Part A 本地 E2E 报告评审通过（keep-pdf 的选择在新管线下复核 — docx 绑定质量已大幅提升）",
        "2. 金集命中的家族先把 golden_full.json expected_doc_ids 改指 keep 侧",
        "3. 跨部门家族确认 ACL 归属（或等权限系统用权限解决共享，不靠重复注册）",
        "4. 执行顺序：HA3 删除（PENDING_DELETE 兜底）→ chunk_meta.is_active=0 → L0/L1 验证",
    ]
    out = os.path.join(REPO, "docs", "dedup_families_preview.md")
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"[families] ✅ 预览写入 {out}（高={summary['高']} 中={summary['中']} 低={summary['低']}）")
    conn.close()
    op_conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hygiene", action="store_true", help="B1 注册卫生（默认预览）")
    ap.add_argument("--commit", action="store_true", help="执行 B1（仅与 --hygiene 联用）")
    ap.add_argument("--families-preview", action="store_true", help="B2 近重家族预览（只读）")
    args = ap.parse_args()
    if args.hygiene:
        hygiene(commit=args.commit)
    if args.families_preview:
        families_preview()
    if not (args.hygiene or args.families_preview):
        print(__doc__)


if __name__ == "__main__":
    main()
