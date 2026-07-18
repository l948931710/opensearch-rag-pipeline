# -*- coding: utf-8 -*-
"""report — 压测报告落盘（stress_harness/reports/run_<ts>_<tag>/report.{json,md}）。

目录/双格式约定与 eval_harness/reports 同款；markdown 侧重门表与 findings，
json 保全量原始指标（含 S3 soak 的 RSS/线程时间线数组）。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from stress_harness.gates import ScenarioResult

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _git_fingerprint() -> Dict[str, Any]:
    """报告↔提交绑定（RB-05）：没有 git 指纹的压测成绩无法证明测的是哪个版本。
    local/staging 两档共用（write_report 是唯一落盘口）；git 不可用降级 unknown 不炸压测。"""
    try:
        root = Path(__file__).resolve().parent.parent

        def _git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=root, capture_output=True,
                                  text=True, timeout=10).stdout.strip()

        # dirty 判定排除 harness 自身输出目录：上一轮落盘的 reports/ 会把后续每轮
        # 都误标 dirty（空目录 porcelain 不显示，首轮恰好逃过——实翻于 2026-07-18 C2）。
        dirty_lines = [ln for ln in _git("status", "--porcelain").splitlines()
                       if "stress_harness/reports/" not in ln]
        return {"git_sha": _git("rev-parse", "--short", "HEAD") or "unknown",
                "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
                "git_dirty": bool(dirty_lines)}
    except Exception:   # noqa: BLE001
        return {"git_sha": "unknown", "git_branch": "unknown", "git_dirty": False}


def _md_gate_table(results: List[ScenarioResult]) -> str:
    lines = ["| 场景 | 门 | 判据 | 阈值 | 实测 | 结果 |",
             "|---|---|---|---|---|---|"]
    for r in results:
        for g in r.gates:
            verdict = ("SKIP" if g.passed is None
                       else ("PASS" if g.passed else "**FAIL**"))
            if g.draft:
                verdict += " (draft)"
            lines.append(f"| {r.scenario} | {g.gate} | {g.description} "
                         f"| {g.threshold} | {g.value} | {verdict} |")
    return "\n".join(lines)


def write_report(results: List[ScenarioResult], tag: str, scope_notes: List[str],
                 env_summary: Dict[str, Any]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outdir = REPORTS_DIR / f"run_{ts}_{tag}"
    outdir.mkdir(parents=True, exist_ok=True)

    git = _git_fingerprint()
    payload = {"generated_at": ts, "tag": tag, "git": git, "env": env_summary,
               "scope_notes": scope_notes,
               "scenarios": [r.as_dict() for r in results],
               "hard_fail": any(r.hard_fail for r in results)}
    (outdir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    findings = [f for r in results for f in r.findings]
    md = ["# Agent 压测报告 — " + tag,
          "",
          f"- 生成时间（UTC）: {ts}",
          f"- 代码指纹: {git['git_sha']}@{git['git_branch']}"
          f"{'（工作树 dirty，结果不可复现）' if git['git_dirty'] else ''}",
          f"- 硬性门失败: {'是' if payload['hard_fail'] else '否'}"
          "（draft 门 FAIL 不计入，见设计文档 §4）",
          "",
          "## 诚实范围（本轮被测/被替身）",
          ""]
    md += [f"- {n}" for n in scope_notes]
    md += ["", "## 门表", "", _md_gate_table(results), "", "## Findings", ""]
    if findings:
        md += [f"- {f}" for f in findings]
    else:
        md += ["-（无）"]
    md += ["", "## 各场景摘要", ""]
    for r in results:
        md.append(f"### {r.scenario} — {r.title}")
        md.append(f"- 时长 {r.duration_s:.1f}s")
        for k, v in r.metrics.items():
            if isinstance(v, (list, dict)) and len(str(v)) > 200:
                md.append(f"- {k}: （数组/明细见 report.json）")
            else:
                md.append(f"- {k}: {v}")
        for n in r.notes:
            md.append(f"- 备注: {n}")
        md.append("")
    (outdir / "report.md").write_text("\n".join(md), encoding="utf-8")
    return outdir
