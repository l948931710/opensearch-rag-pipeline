# -*- coding: utf-8 -*-
"""评测第二包（#F-mm13）回归测试：judge_mm 语义通道 + history 臂 mimic 口径。

- judge_bundle_mm 此前每轮落盘却零消费（run_judge 无 mm rubric、gate 不跑）：
  现在 MM_RUBRIC + merge_mm_panel + report advisory 闸 + baseline trend 全链接通，
  与 answer_image_rate 组成「出没出图 × 出得对不对」双指标闭环。
- history 臂：follow-up 噪声图回归钉，mimic 的固定操作性定义 =
  本轮答案标记 N 集合 ∩ 注入历史 assistant 文本的标记 N 集合 非空。
"""

from unittest.mock import patch

import os as _os

# ⚠️ envboot 隔离（#F-mm13 排雷）：eval_harness.run_eval / layers.* 顶部 import envboot,
# boot() on-import 会把进程 env 强制成 live（RAG_SIMULATE=false + 公网 HA3 端点）——
# 测试进程内一旦泄漏,任何后续从 env 重建 config 的测试（test_config_loading/guards 族）
# 都会被 conftest PROD-GUARD 拒跑,且重建出的毒化单例会级联炸掉整个套件。
# 快照-导入-回滚：envboot 只 boot 一次（模块缓存）,env 写入立即撤销;本文件全部用
# mock 检索/生成,不需要 live env。
_env_snapshot = dict(_os.environ)
from eval_harness.judge import MM_VERDICT_ITEM_SCHEMA, merge_mm_panel  # noqa: E402
from eval_harness.layers import l4_multimodal  # noqa: E402
from eval_harness.report import build_gates  # noqa: E402

for _k in set(_os.environ) - set(_env_snapshot):
    del _os.environ[_k]
_os.environ.update(_env_snapshot)
del _env_snapshot


def _panels():
    def v(qid, ir, iem, noise):
        return {"qid": qid, "image_relevance": ir, "image_expected_match": iem,
                "noise_image": noise, "rationale": "r"}
    return [
        {"judge": "j1", "verdicts": [v("Q1", 5, 4, False), v("Q2", 2, 2, True)]},
        {"judge": "j2", "verdicts": [v("Q1", 4, 4, False), v("Q2", 3, 2, False)]},
    ]


def test_merge_mm_panel_aggregates():
    out = merge_mm_panel([{"qid": "Q1"}, {"qid": "Q2"}], _panels())
    agg = out["aggregate"]
    assert agg["n"] == 2 and agg["n_judges"] == 2
    assert agg["image_relevance"]["mean"] == round((4.5 + 2.5) / 2, 3)
    assert agg["noise_image_rate"] == 0.5          # Q2 任一评委判噪声 → any
    per = {r["qid"]: r for r in out["per_query"]}
    assert per["Q1"]["image_relevance"] == 4.5 and per["Q1"]["noise_any"] is False
    assert per["Q2"]["noise_any"] is True


def test_mm_schema_required_keys():
    assert set(MM_VERDICT_ITEM_SCHEMA["required"]) == {
        "qid", "image_relevance", "image_expected_match", "noise_image", "rationale"}


def test_report_mm_gate_advisory_never_blocks():
    from eval_harness.run_eval import _strict_failures  # noqa: E402
    r = {"judge_mm": merge_mm_panel([{"qid": "Q1"}, {"qid": "Q2"}], _panels())}
    g = build_gates(r)
    gate = g["image relevance (judge-mm, advisory)"]
    assert gate["advisory"] is True
    assert gate["pass"] is True                    # mean 3.5 恰好过线
    # advisory 即使 FAIL 也不阻断 strict
    r2 = {"judge_mm": {"aggregate": {"image_relevance": {"mean": 2.0}, "n": 5,
                                     "image_expected_match": {"mean": 2.0},
                                     "noise_image_rate": 0.6}}}
    g2 = build_gates(r2)
    assert g2["image relevance (judge-mm, advisory)"]["pass"] is False
    assert not any("judge-mm" in f for f in _strict_failures(g2, {}))


def test_run_judge_mm_rubric_wiring():
    """run_judge 的 mm rubric 用自己的 schema keys（不再复用 answer schema）。"""
    import eval_harness.run_judge as rj
    rub, schema, id_key = rj._RUBRICS["mm"]
    assert schema is MM_VERDICT_ITEM_SCHEMA and id_key == "qid"
    assert "image_relevance" in rub


# ═══════════════ history 臂 ═══════════════

def _mt_case(qid="MT1", hist_markers="<<IMG:2>>"):
    return {
        "qid": qid, "query": "第二步在哪个界面操作？", "expect_images": True,
        "live_scorable": True, "multi_turn": True,
        "history": [
            {"role": "user", "content": "销售出库单怎么开？"},
            {"role": "assistant", "content": f"第1步登录。{hist_markers} 第2步录入。"},
        ],
    }


def _fake_ret(query, top_k=7, user_dept=None, cosurface_images=False, **kw):
    return [{"chunk_type": "step_card", "doc_id": "D", "title": "t",
             "image_refs": [{"oss_key": "a.png", "caption": "图"}]},
            {"chunk_type": "step_card", "doc_id": "D", "title": "t",
             "image_refs": [{"oss_key": "b.png", "caption": "图2"}]}]


def _run_hist(answer, cases):
    def _gen(query, chunks, pure_text=False, history=None):
        assert history, "history 臂必须把注入历史传给生成"
        return {"answer": answer}
    with patch("opensearch_pipeline.retriever.retrieve_and_enrich", _fake_ret), \
         patch("eval_harness.layers.l4_multimodal.generate_answer_nothink", _gen):
        return l4_multimodal._run_history_arm(cases, top_k=7, max_images=6)


def test_mimic_definition_overlap(monkeypatch):
    """mimic = 答案标记 N ∩ 历史标记 N 非空（固定口径单测 1）。"""
    out = _run_hist("接着操作 <<IMG:2>>。", [_mt_case()])
    assert out["history_marker_mimic_rate"] == 1.0
    assert out["per_query"][0]["mimic_overlap"] is True


def test_mimic_definition_no_overlap(monkeypatch):
    """答案标记与历史标记无交集 → 非 mimic（固定口径单测 2）。"""
    out = _run_hist("接着操作 <<IMG:1>>。", [_mt_case(hist_markers="<<IMG:2>>")])
    assert out["history_marker_mimic_rate"] == 0.0


def test_mimic_definition_no_markers(monkeypatch):
    """答案零标记 → 非 mimic（固定口径单测 3）。"""
    out = _run_hist("接着在界面上直接操作即可。", [_mt_case()])
    assert out["history_marker_mimic_rate"] == 0.0


def test_history_arm_requires_multi_turn_cases(monkeypatch):
    out = _run_hist("x", [{"qid": "Q", "query": "q", "expect_images": True,
                           "live_scorable": True}])   # 无 multi_turn/history
    assert out["applicable"] is False


def test_multi_turn_excluded_from_main_arms(monkeypatch):
    """multi_turn case 不进主臂/对照臂（保护已锁定的 answer_image_rate 基线）。"""
    monkeypatch.setenv("EVAL_L4_COSURFACE_ARM", "false")
    monkeypatch.delenv("EVAL_L4_HISTORY_ARM", raising=False)
    def _gen(query, chunks, pure_text=False, history=None):
        return {"answer": "如图 <<IMG:1>>"}
    cases = [_mt_case()]   # 只有 multi_turn case
    with patch("opensearch_pipeline.retriever.retrieve_and_enrich", _fake_ret), \
         patch("eval_harness.layers.l4_multimodal.generate_answer_nothink", _gen):
        out = l4_multimodal.run(cases)
    assert out["applicable"] is False               # 主臂无可用 case（multi_turn 被排除）


def test_history_arm_env_gated(monkeypatch):
    monkeypatch.setenv("EVAL_L4_COSURFACE_ARM", "false")
    monkeypatch.setenv("EVAL_L4_HISTORY_ARM", "true")
    def _gen(query, chunks, pure_text=False, history=None):
        return {"answer": "如图 <<IMG:2>>"}
    cases = [
        {"qid": "REG1", "query": "怎么操作？", "expect_images": True, "live_scorable": True},
        _mt_case(),
    ]
    with patch("opensearch_pipeline.retriever.retrieve_and_enrich", _fake_ret), \
         patch("eval_harness.layers.l4_multimodal.generate_answer_nothink", _gen):
        out = l4_multimodal.run(cases)
    assert out["aggregate"]["n_answers"] == 1       # 主臂只有 REG1
    hist = out["arms"]["history"]
    assert hist["applicable"] is True and hist["n_cases"] == 1
    assert hist["history_marker_mimic_rate"] == 1.0
