# -*- coding: utf-8 -*-
"""
test_agent_eval_harness.py — agent 评测门（L7）harness 自检

评测门的可信度取决于判分与 gate 逻辑本身：scripted「理想模型」必须得满分（判分不冤枉好人），
「坏模型」必须掉分（判分抓得住坏行为），gate 对硬不变量与 δ 语义判得准。
CI 全程零外网零 DB（scripted provider + 内存 store）。
"""
import json
from pathlib import Path

from eval_harness.agent import runner as R


def test_scripted_ideal_scores_full_marks():
    """理想脚本 provider 全金集满分——判分器不产生假阴性。"""
    report = R.run(provider="scripted")
    m = report["metrics"]
    assert m["tool_trigger_rate"] == 1.0
    assert m["tool_query_relevance"] == 1.0     # 理想 query=原问题 → 期望词必然命中
    assert m["no_tool_rate"] == 1.0
    assert m["write_propose_rate"] == 1.0
    assert m["approval_suspend_rate"] == 1.0    # 硬不变量：HIGH_WRITE 提案在生产 Policy 路径必挂起
    assert m["grounded_rate"] == 1.0
    assert m["error_count"] == 0
    assert m["n_total"] == 31


class _LazyProvider:
    """坏模型：从不调工具、答非所问——判分必须抓得住。"""

    name = "dashscope"

    def __init__(self, case):
        pass

    def capabilities(self, model):
        return None

    def chat(self, model, req):
        from opensearch_pipeline.agent_runtime import ChatResponse, Usage
        return ChatResponse(text="这个问题很有意思。", tool_calls=[], usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)


def test_lazy_model_fails_tool_families(monkeypatch):
    monkeypatch.setattr(R, "_ScriptedIdealProvider", _LazyProvider)
    report = R.run(provider="scripted", only=["tool_expected", "write_approval", "grounded"])
    m = report["metrics"]
    assert m["tool_trigger_rate"] == 0.0
    assert m["write_propose_rate"] == 0.0
    assert m["grounded_rate"] == 0.0            # 不检索答不出 gold 关键词
    # 没提案 HIGH_WRITE → 挂起硬不变量按「未触发不计罪」记 1.0（那是 write_propose_rate 的失分）
    assert m["approval_suspend_rate"] == 1.0


def test_gate_semantics():
    baseline = {"metrics": {"tool_trigger_rate": 0.9, "tool_query_relevance": 0.9,
                            "no_tool_rate": 0.8, "write_propose_rate": 0.8,
                            "grounded_rate": 0.8}}
    good = {"tool_trigger_rate": 0.9, "tool_query_relevance": 0.85, "no_tool_rate": 0.8,
            "write_propose_rate": 0.75, "grounded_rate": 0.8,
            "approval_suspend_rate": 1.0, "error_count": 0}
    assert R._gate(good, baseline) == []                       # δ=0.10 内 → 过
    drop = dict(good, tool_trigger_rate=0.7)
    assert any("tool_trigger_rate" in b for b in R._gate(drop, baseline))
    hard = dict(good, approval_suspend_rate=0.8)
    assert any("硬不变量" in b for b in R._gate(hard, baseline))   # 硬不变量不吃 δ
    err = dict(good, error_count=2)
    assert any("error_count" in b for b in R._gate(err, baseline))


def test_cli_scripted_and_freeze_baseline(tmp_path, capsys):
    """CLI 全链：scripted 跑通出报告 → 冻结基线 → --gate 对基线过闸。"""
    rep_dir = tmp_path / "reports"
    R.REPORTS_DIR, orig = rep_dir, R.REPORTS_DIR
    try:
        assert R.main(["--provider", "scripted"]) == 0
        reports = list(rep_dir.glob("agent_eval_*.json"))
        assert reports, "必须落 JSON 报告"
        data = json.loads(reports[0].read_text(encoding="utf-8"))
        assert data["metrics"]["n_total"] == 31
        baseline = tmp_path / "baseline.json"
        assert R.main(["--freeze-baseline", str(reports[0]), "--baseline", str(baseline)]) == 0
        assert json.loads(baseline.read_text(encoding="utf-8"))["metrics"]["tool_trigger_rate"] == 1.0
        assert R.main(["--provider", "scripted", "--gate", "--baseline", str(baseline)]) == 0
    finally:
        R.REPORTS_DIR = orig


def test_freeze_writes_regime_fingerprint(tmp_path):
    """RB-05：冻结产物必须带 regime 指纹（金集/模型/臂/prompt/provider + git 信息）。"""
    rep_dir = tmp_path / "reports"
    R.REPORTS_DIR, orig = rep_dir, R.REPORTS_DIR
    try:
        assert R.main(["--provider", "scripted"]) == 0
        report = next(iter(rep_dir.glob("agent_eval_*.json")))
        baseline = tmp_path / "baseline.json"
        assert R.main(["--freeze-baseline", str(report), "--baseline", str(baseline)]) == 0
        regime = json.loads(baseline.read_text(encoding="utf-8"))["regime"]
        for k in R.REGIME_MATCH_KEYS:
            assert regime.get(k) is not None, f"regime 缺匹配键 {k}"
        assert regime["provider"] == "scripted"
        assert len(regime["cases_sha16"]) == 16 and len(regime["prompt_sha16"]) == 16
    finally:
        R.REPORTS_DIR = orig


def test_gate_regime_mismatch_fails_closed(tmp_path):
    """基线 regime 与本次运行不一致（如 prompt 变了）→ gate 拒比对 exit 2。"""
    rep_dir = tmp_path / "reports"
    R.REPORTS_DIR, orig = rep_dir, R.REPORTS_DIR
    try:
        assert R.main(["--provider", "scripted"]) == 0
        report = next(iter(rep_dir.glob("agent_eval_*.json")))
        baseline = tmp_path / "baseline.json"
        assert R.main(["--freeze-baseline", str(report), "--baseline", str(baseline)]) == 0
        base = json.loads(baseline.read_text(encoding="utf-8"))
        base["regime"]["prompt_sha16"] = "0" * 16          # 模拟：冻结后 prompt 已变
        baseline.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        assert R.main(["--provider", "scripted", "--gate", "--baseline", str(baseline)]) == 2
    finally:
        R.REPORTS_DIR = orig


def test_legacy_baseline_and_report_rejected(tmp_path):
    """旧格式（无 regime）基线 gate 必拒；旧格式 report 冻结必拒——过期成绩单不再静默可比。"""
    rep_dir = tmp_path / "reports"
    R.REPORTS_DIR, orig = rep_dir, R.REPORTS_DIR
    try:
        legacy_baseline = tmp_path / "legacy_baseline.json"
        legacy_baseline.write_text(json.dumps(
            {"frozen_at": "2026-07-12", "metrics": {"tool_trigger_rate": 1.0}}), encoding="utf-8")
        assert R.main(["--provider", "scripted", "--gate",
                       "--baseline", str(legacy_baseline)]) == 2
        legacy_report = tmp_path / "legacy_report.json"
        legacy_report.write_text(json.dumps(
            {"provider": "live", "metrics": {"tool_trigger_rate": 1.0}}), encoding="utf-8")
        assert R.main(["--freeze-baseline", str(legacy_report),
                       "--baseline", str(tmp_path / "b.json")]) == 2
    finally:
        R.REPORTS_DIR = orig


def test_cases_file_well_formed():
    data = json.loads(Path(R.DEFAULT_CASES).read_text(encoding="utf-8"))
    fams = {}
    for c in data["cases"]:
        assert c["id"] and c["family"] in ("tool_expected", "no_tool", "write_approval", "grounded")
        fams[c["family"]] = fams.get(c["family"], 0) + 1
        if c["family"] == "grounded":
            assert "corpus" in c and c.get("must_contain_any"), "grounded 用例必须带语料与判分词"
    assert min(fams.values()) >= 4, f"每族至少 4 例才有统计意义：{fams}"
