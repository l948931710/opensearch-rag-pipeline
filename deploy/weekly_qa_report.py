#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weekly_qa_report.py — 富岭 RAG 每周问答情况报告 + Claude 叙述式分析.

读取 fuling_operation.qa_daily_metrics(本周 vs 上周聚合)+ qa_session_log(拒答/无结果样本、部门分布),
生成 Markdown 周报,调用本地 claude CLI 写中文分析+建议,再用 pandoc+xelatex 渲染中文 PDF。
只读生产(RAG_ENV=metrics 的 fuling_metrics 账号对这两张表有 SELECT);本脚本不写任何生产数据。

输出: reports/qa_weekly_<最新周一>.{md,pdf}。配套 LaunchAgent: deploy/com.fuling.qa-weekly-report.plist。
env 覆盖: RAG_CLAUDE_BIN / RAG_PANDOC_BIN / RAG_TEX_DIR / RAG_TZ_SHIFT_HOURS(默认 15)。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CLAUDE = os.environ.get("RAG_CLAUDE_BIN", "/Users/laijunchen/.local/bin/claude")
PANDOC = os.environ.get("RAG_PANDOC_BIN", "/Users/laijunchen/opt/anaconda3/envs/stack-test/bin/pandoc")
TEX_DIR = os.environ.get("RAG_TEX_DIR", "/Library/TeX/texbin")
TZ = int(os.environ.get("RAG_TZ_SHIFT_HOURS", "15"))
# Output dir: default to the repo's reports/ for manual runs; the LaunchAgent overrides RAG_REPORTS_DIR
# to a NON-~/Downloads path (e.g. ~/fuling-rag-reports) so the pandoc/claude child processes — which
# are NOT individually FDA-granted — never touch the TCC-protected Downloads folder under launchd.
REPORTS = os.environ.get("RAG_REPORTS_DIR") or os.path.join(ROOT, "reports")


def _rows(cur, sql, args=()):
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _agg(days):
    """Aggregate a list of qa_daily_metrics dict rows → week summary."""
    tot = sum(d["total_queries"] or 0 for d in days)
    suc = sum(d["success_count"] or 0 for d in days)
    nr = sum(d["no_result_count"] or 0 for d in days)
    ref = sum(d["refusal_count"] or 0 for d in days)
    err = sum(d["error_count"] or 0 for d in days)
    p95s = [d["p95_latency_ms"] for d in days if d["p95_latency_ms"]]
    breaches = sum(1 for d in days if d["slo_ok"] == 0)
    return {
        "days": len(days), "total": tot, "success": suc, "no_result": nr, "refusal": ref, "error": err,
        "answer_rate": round(suc / tot, 4) if tot else None,
        "no_result_rate": round(nr / tot, 4) if tot else None,
        "error_rate": round(err / tot, 4) if tot else None,
        "p95_avg": round(sum(p95s) / len(p95s)) if p95s else None,
        "p95_worst": max(p95s) if p95s else None,
        "slo_breach_days": breaches,
    }


def _delta(cur_v, prev_v, pct=False):
    if cur_v is None or prev_v is None:
        return "—"
    d = cur_v - prev_v
    if pct:
        return f"{d:+.1%}".replace("%", "pp")
    return f"{d:+,}" if isinstance(d, int) else f"{d:+.4f}"


def build():
    from opensearch_pipeline.config import load_config
    import opensearch_pipeline.config as c
    c._config = load_config()
    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    conn = _get_db_conn(select_db=False)
    try:
        with conn.cursor() as cur:
            dm = _rows(cur, """SELECT metric_date,total_queries,success_count,refusal_count,
                               no_result_count,error_count,answer_rate,no_result_rate,p95_latency_ms,
                               slo_ok,slo_breaches_json FROM fuling_operation.qa_daily_metrics
                               ORDER BY metric_date DESC LIMIT 14""")
            if not dm:
                print("[weekly_report] no qa_daily_metrics rows — nothing to report"); return None
            dm.sort(key=lambda r: r["metric_date"])
            this_wk = dm[-7:]
            last_wk = dm[-14:-7]
            wk_start, wk_end = this_wk[0]["metric_date"], this_wk[-1]["metric_date"]
            cur_a, prev_a = _agg(this_wk), _agg(last_wk) if last_wk else None
            # qualitative samples from qa_session_log for this week's Beijing days
            samp = _rows(cur, f"""SELECT answer_status, risk_blocked, opensearch_hit_count,
                                  LEFT(query_text,80) q, user_dept,
                                  DATE(DATE_ADD(created_at, INTERVAL {TZ} HOUR)) d
                                  FROM fuling_operation.qa_session_log
                                  WHERE DATE(DATE_ADD(created_at, INTERVAL {TZ} HOUR)) BETWEEN %s AND %s""",
                         (wk_start, wk_end))
    finally:
        conn.close()

    refusals = [s for s in samp if (s["answer_status"] or "").upper() == "REFUSAL" or s["risk_blocked"]]
    noresults = [s for s in samp if (s["answer_status"] or "").upper() == "NO_RESULT"
                 or (s["opensearch_hit_count"] == 0 and (s["answer_status"] or "").upper() != "LLM_ERROR")]
    dept = {}
    for s in samp:
        dept[s["user_dept"] or "?"] = dept.get(s["user_dept"] or "?", 0) + 1
    dept_top = sorted(dept.items(), key=lambda x: -x[1])[:8]

    # ── Markdown ──
    L = []
    L.append(f"# 富岭 RAG 问答周报\n\n**周期**: {wk_start} ~ {wk_end}（北京时区）  ·  **生成**: {date.today()}\n")
    L.append("## 本周概览\n")
    L.append("| 指标 | 本周 | 上周 | 变化 |")
    L.append("|---|--:|--:|--:|")
    def row(label, k, pct=False, fmt=None):
        cv, pv = cur_a.get(k), (prev_a.get(k) if prev_a else None)
        f = (lambda x: "—" if x is None else (fmt(x) if fmt else x))
        L.append(f"| {label} | {f(cv)} | {f(pv)} | {_delta(cv, pv, pct)} |")
    row("提问总数", "total", fmt=lambda x: f"{x:,}")
    row("成功应答", "success", fmt=lambda x: f"{x:,}")
    row("应答率", "answer_rate", pct=True, fmt=lambda x: f"{x:.1%}")
    row("无结果率", "no_result_rate", pct=True, fmt=lambda x: f"{x:.1%}")
    row("错误率", "error_rate", pct=True, fmt=lambda x: f"{x:.1%}")
    row("p95 延迟(均/ms)", "p95_avg", fmt=lambda x: f"{x:,}")
    row("p95 最差(ms)", "p95_worst", fmt=lambda x: f"{x:,}")
    row("SLO 破线天数", "slo_breach_days")
    L.append(f"\n本周 {cur_a['days']} 天有数据；拒答 {cur_a['refusal']}、无结果 {cur_a['no_result']}、错误 {cur_a['error']}。\n")

    L.append("## 逐日明细\n\n| 日期 | 提问 | 应答率 | 无结果率 | p95(ms) | SLO |")
    L.append("|---|--:|--:|--:|--:|:-:|")
    for d in this_wk:
        ar = f"{float(d['answer_rate']):.0%}" if d["answer_rate"] is not None else "—"
        nrr = f"{float(d['no_result_rate']):.0%}" if d["no_result_rate"] is not None else "—"
        L.append(f"| {d['metric_date']} | {d['total_queries']} | {ar} | {nrr} | "
                 f"{d['p95_latency_ms'] or '—'} | {'✅' if d['slo_ok'] else '⚠️'} |")

    L.append("\n## 拒答样本（本周，最多 12 条）\n")
    L += [f"- `{s['d']}` [{s['user_dept'] or '?'}] {s['q']}" for s in refusals[:12]] or ["- （无）"]
    L.append("\n## 无结果样本（本周，最多 12 条）\n")
    L += [f"- `{s['d']}` [{s['user_dept'] or '?'}] {s['q']}" for s in noresults[:12]] or ["- （无）"]
    L.append("\n## 提问部门分布（本周样本）\n")
    L += [f"- {d}: {n}" for d, n in dept_top] or ["- （无）"]

    data_md = "\n".join(L)

    # ── Claude 叙述式分析 ──
    narrative = claude_narrative(data_md)
    full_md = data_md + "\n\n## 分析与建议（Claude）\n\n" + narrative + "\n"

    os.makedirs(REPORTS, exist_ok=True)
    base = os.path.join(REPORTS, f"qa_weekly_{wk_end}")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(full_md)
    print(f"[weekly_report] wrote {base}.md")
    render_pdf(base + ".md", base + ".pdf")
    return base


def claude_narrative(data_md: str) -> str:
    prompt = (
        "你是富岭塑胶企业 RAG 问答系统的质量分析师。基于下面这一周的问答指标与样本，"
        "用中文写一段 250–450 字的周度分析：点出关键趋势与周环比变化、SLO 风险（尤其 p95 延迟与无结果率）、"
        "值得关注的拒答/无结果模式或部门信号，并给出 2–4 条可执行建议。"
        "只输出分析正文（可用简短小标题/项目符号），不要复述数据表。\n\n数据：\n\n" + data_md
    )
    try:
        # cwd=/tmp so the headless agent doesn't TCC-probe ~/Downloads; data is inline in the prompt.
        r = subprocess.run([CLAUDE, "-p", prompt], cwd="/tmp", capture_output=True, text=True, timeout=240)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
        return f"*(Claude 分析不可用：rc={r.returncode} {((r.stderr or '')[:200])})*"
    except Exception as e:  # noqa: BLE001 — fail-open: still emit the data report
        return f"*(Claude 分析跳过：{type(e).__name__}: {e})*"


def render_pdf(md_path: str, pdf_path: str) -> None:
    env = dict(os.environ, PATH=TEX_DIR + ":" + os.environ.get("PATH", ""))
    try:
        subprocess.run([PANDOC, md_path, "-o", pdf_path, "--pdf-engine=xelatex",
                        "-V", "CJKmainfont=PingFang SC", "-V", "mainfont=PingFang SC",
                        "-V", "geometry=margin=2cm"], env=env, capture_output=True, text=True, timeout=180, check=True)
        print(f"[weekly_report] wrote {pdf_path}")
    except Exception as e:  # noqa: BLE001 — MD already written; PDF is best-effort
        err = getattr(e, "stderr", "") or str(e)
        print(f"[weekly_report] PDF render failed (MD still available): {str(err)[:200]}")


if __name__ == "__main__":
    build()
