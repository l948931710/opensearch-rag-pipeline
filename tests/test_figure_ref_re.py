# -*- coding: utf-8 -*-
"""
test_figure_ref_re.py — 校准后 _FIGURE_REF_RE 的覆盖与误报边界。

背景:scripts/audit_figure_ref_phrases.py 在 68 条 SUCCESS 生产答案上发现原版正则
只命中 top 120 短语里的 2 条 = 装饰性闸。2026-06-12 扩充 6 类指代后,此测试钉死
新覆盖 + 显式标定不该入正则的名词性短语,防止后续打回旧版无声装饰化。

跑测:pytest tests/test_figure_ref_re.py -v
"""
import pytest
# mm_answer_metrics moved in-repo 2026-06-18 (eval_harness/mm_answer_metrics.py) so these tests run in
# CI sim without the out-of-repo data repo. The data-repo copy is now a shim re-exporting this module.
from eval_harness.mm_answer_metrics import (
    _FIGURE_REF_RE,
    _FIGURE_REF_FALSE_POSITIVES,
    dump_dangling_cases,
)


# ── 新覆盖 — 真生产高频短语必须命中 ──────────────────────────────

@pytest.mark.parametrize("phrase", [
    # 截图相关(audit 实测 8+ 命中)
    "界面截图显示菜单结构",
    "统界面截图与局部功能",
    "参考截图未成功上传",
    "系统截图显示路径",
    # 图示
    "但图示及常规配置",
    "参考图示显示需要",
    "如参考图所示,",
    # 图片显示/描述/提示
    "文档9的图片显示在",
    "见文档3图片描述,",
    "文档1图片提示,",
    "参考图片及常规逻辑",
    # 原版必须仍命中(回归保护)
    "操作如下图所示",
    "如图所示进入界面",
    "请见上图",
    # 英文
    "see the figure below",
    "see picture",
    "see screenshot",
])
def test_dangling_phrases_caught(phrase):
    assert _FIGURE_REF_RE.search(phrase), f"{phrase!r} 应该被识别为图指代"


# ── 不该命中 — 纯文字、无图指代 ────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "请按操作流程提交申请",
    "联系行政部确认",
    "登录系统后填写表单",
    "审核通过后开始执行",
])
def test_non_dangling_phrases_skipped(phrase):
    assert not _FIGURE_REF_RE.search(phrase), f"{phrase!r} 不应被识别为图指代"


# ── 已知误报候选(在 _FIGURE_REF_FALSE_POSITIVES 集) ─────────────
# 当前不在正则里做硬排除(过严容易漏真 dangling),改由 dump_dangling_cases
# 的 is_likely_false_positive 标记,让人工抽检时一眼分桶。

def test_known_false_positives_flagged_by_dumper():
    """name-like 短语(图标/组织架构图)被 dumper 标 is_likely_false_positive=True。"""
    cases = [
        {"qid": "F1", "query": "U8 怎么登录", "answer": "桌面快捷方式图标双击进入"},
        {"qid": "F2", "query": "组织架构是?", "answer": "参见组织架构图,文中已展示"},
    ]
    # 注:_FIGURE_REF_RE 不在 F1 上命中(无图指代触发短语),F2 上「参见」也不命中
    # 这两个其实不会触发 dangling = 验证:误报集仅在真触发时才相关
    per_answer = [
        {"n_available": 0, "dangling_ref": False},
        {"n_available": 0, "dangling_ref": False},
    ]
    dumped = dump_dangling_cases(per_answer, cases)
    assert dumped == [], "非 dangling case 不应被 dump"


def test_dumper_outputs_context_and_phrase():
    """dump 输出包含 matched_phrase 和上下文。"""
    cases = [
        {"qid": "T1", "query": "扫码报检步骤?",
         "answer": "登录 U8 系统,如截图显示菜单结构,选择"},
    ]
    per_answer = [{"n_available": 0, "dangling_ref": True}]
    dumped = dump_dangling_cases(per_answer, cases)
    assert len(dumped) == 1
    d = dumped[0]
    assert d["qid"] == "T1"
    assert "截图显示" in d["matched_phrase"] or "界面截图" in d["matched_phrase"] or "截图" in d["matched_phrase"]
    assert "menu" not in d["answer_excerpt"]  # 中文内容
    assert d["n_available"] == 0
    assert isinstance(d["is_likely_false_positive"], bool)


def test_dumper_flags_false_positive_context():
    """answer 含「图标」名词时 dumper 标 is_likely_false_positive。"""
    cases = [{"qid": "FP1", "query": "?",
              "answer": "请点击桌面快捷方式图标,如下图所示,登录界面"}]
    per_answer = [{"n_available": 0, "dangling_ref": True}]
    dumped = dump_dangling_cases(per_answer, cases)
    assert len(dumped) == 1
    # 上下文里同时有「如下图所示」(真命中) 和「图标」(误报候选)
    # is_likely_false_positive 标 True 让人工抽检时优先看
    assert dumped[0]["is_likely_false_positive"] is True


def test_dumper_limit_respected():
    cases = [{"qid": f"L{i}", "query": "", "answer": "如图所示"} for i in range(10)]
    per_answer = [{"n_available": 0, "dangling_ref": True} for _ in range(10)]
    dumped = dump_dangling_cases(per_answer, cases, limit=3)
    assert len(dumped) == 3
    assert [d["qid"] for d in dumped] == ["L0", "L1", "L2"]


def test_false_positive_constant_documented():
    """显式标定:_FIGURE_REF_FALSE_POSITIVES 至少含图标/图样/图片参数(已知误报候选)。"""
    for word in ("图标", "图样", "图片参数"):
        assert word in _FIGURE_REF_FALSE_POSITIVES


# ── marker_validity redefinition (2026-06-18) ────────────────────────────────────
# validity = IN-RANGE marker occurrences / total occurrences. Reuse of a VALID marker is NOT invalid
# (that's marker_distinctness). Regression for the J-water_soak 0.8 artifact (it placed <<IMG:1>> twice
# across two steps → old distinct/total formula scored 0.5 even though zero markers were out-of-range).
import eval_harness.mm_answer_metrics as _M  # noqa: E402
from eval_harness.mm_answer_metrics import analyze_answer, aggregate  # noqa: E402

_MAP3 = {1: [{"visual_summary": "a"}], 2: [{"visual_summary": "b"}], 3: [{"visual_summary": "c"}]}


def test_marker_validity_valid_reuse_not_penalised(monkeypatch):
    monkeypatch.setattr(_M, "_extract_image_chunks", lambda c: _MAP3)
    det = analyze_answer("第1步 <<IMG:1>> 第2步 <<IMG:1>>", used_chunks=[])
    assert det["n_markers"] == 2 and det["n_invalid_markers"] == 0
    assert det["marker_validity"] == 1.0          # was 0.5 under the old distinct/total formula
    assert det["marker_distinctness"] == 0.5      # reuse surfaces here (advisory), not as invalidity


def test_marker_validity_invalid_out_of_range(monkeypatch):
    monkeypatch.setattr(_M, "_extract_image_chunks", lambda c: _MAP3)
    det = analyze_answer("占位 <<IMG:5>>", used_chunks=[])   # 5 not in {1,2,3}
    assert det["n_markers"] == 1 and det["n_invalid_markers"] == 1
    assert det["marker_validity"] == 0.0
    assert det["marker_distinctness"] is None     # no in-range markers


def test_marker_validity_mixed(monkeypatch):
    monkeypatch.setattr(_M, "_extract_image_chunks", lambda c: _MAP3)
    det = analyze_answer("<<IMG:1>> <<IMG:2>> <<IMG:9>>", used_chunks=[])  # 1,2 valid; 9 out-of-range
    assert det["n_markers"] == 3 and det["n_invalid_markers"] == 1
    assert abs(det["marker_validity"] - 2 / 3) < 1e-9
    assert det["marker_distinctness"] == 1.0      # 2 distinct / 2 in-range


def test_aggregate_marker_validity_and_distinctness(monkeypatch):
    monkeypatch.setattr(_M, "_extract_image_chunks", lambda c: _MAP3)
    dets = [
        analyze_answer("<<IMG:1>> <<IMG:1>>", []),            # reuse: inrange2 distinct1 markers2
        analyze_answer("<<IMG:1>> <<IMG:2>> <<IMG:9>>", []),  # mixed: inrange2 distinct2 markers3
        analyze_answer("占位 <<IMG:5>>", []),                 # invalid: inrange0 distinct0 markers1
    ]
    agg = aggregate(dets)
    assert agg["total_markers"] == 6 and agg["total_invalid_markers"] == 2
    assert abs(agg["marker_validity"] - 4 / 6) < 1e-9        # 4 in-range / 6 total occurrences
    assert abs(agg["marker_distinctness"] - 3 / 4) < 1e-9    # 3 distinct / 4 in-range


def test_aggregate_validity_recomputes_from_legacy_dets():
    """Older stored dets (pre-2026-06-18) lack n_inrange/n_distinct → aggregate must recompute from
    n_markers/n_invalid. Proves the baseline marker_validity can be recomputed: the J-water_soak det
    (markers 2, invalid 0, distinct 1) → 1.0 under the new formula (was 0.5)."""
    legacy = [{"n_available": 4, "n_markers": 2, "n_valid_markers": 1, "n_invalid_markers": 0,
               "interleaved": True, "dangling_ref": False, "over_cap": False, "n_orphan": 0,
               "strategy": "interleaved", "n_shown": 2}]
    agg = aggregate(legacy)
    assert agg["marker_validity"] == 1.0
    assert agg["marker_distinctness"] == 0.5
