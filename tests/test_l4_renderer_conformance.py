# -*- coding: utf-8 -*-
"""L4-serving evaluator ↔ 生产渲染器的 conformance（L4SRV 3.0.0，codex 复核后）。

2.0.0 只把 **orphan 分母**收窄到可见集，`marker_validity`/`n_shown`/`dangling` 仍按全量
`image_map` 算。后果（可执行反例）：ctx 只含图1、答案只引 `<<IMG:2>>` 时
    evaluator: marker_validity=1.0, n_shown=1, rendered_any=True
    生产 build_content_blocks(..., included_indices=[1]): **0 张图**
⇒ 硬闸读数高报。3.0.0 把**全部** serving 主指标改按可见集判。

本组测试的核心是 **两侧同输入必须同结论**——单侧断言拦不住口径漂移（2.0.0 的单侧测试
当时是全绿的）。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

from eval_harness.mm_answer_metrics import aggregate, analyze_answer  # noqa: E402


def _ch(i, n_img=1):
    return {"chunk_id": f"c{i}", "chunk_type": "step_card", "chunk_text": f"文本{i}",
            "image_refs": [{"oss_key": f"k{i}_{m}", "source_image": f"s{i}_{m}.png",
                            "visual_summary": f"图{i}-{m}"} for m in range(n_img)]}


def _rendered(monkeypatch, answer, chunks, included):
    """生产渲染器实际出图张数（打桩签名——测试环境无 OSS 凭据会让所有分支都渲染 0 张，
    那样断言就成了空断言）。"""
    import opensearch_pipeline.content_blocks_builder as B
    monkeypatch.setattr(B, "generate_signed_url", lambda key, expires=None: f"https://x/{key}")
    blocks = B.build_content_blocks(answer, chunks, included_indices=included)
    return sum(1 for b in blocks if str(b.get("type", "")).lower() in ("image", "img"))


@pytest.mark.parametrize("answer,included,desc", [
    ("正文 <<IMG:2>>", [1], "只引被截断的图"),
    ("正文 <<IMG:1>>", [1], "只引可见的图"),
    ("a <<IMG:1>> b <<IMG:2>>", [1], "一可见一截断"),
    ("正文没有标记", [1, 2], "无标记"),
    ("正文 <<IMG:9>>", [1, 2], "越界编号"),
    ("正文 <<IMG:1>>", [], "context 里一篇都没有"),
])
def test_evaluator_n_shown_matches_renderer(monkeypatch, answer, included, desc):
    """**核心 conformance**：evaluator 的 n_shown 必须等于生产实渲张数。"""
    chunks = [_ch(1), _ch(2)]
    det = analyze_answer(answer, chunks, max_images=6, included_doc_indices=included)
    assert det["n_shown"] == _rendered(monkeypatch, answer, chunks, included), desc
    assert det["rendered_any"] is (det["n_shown"] > 0), desc


def test_truncated_reference_is_invalid_not_valid():
    """引用未进 context 的图 = 渲染不出任何东西 ⇒ 必须计为 invalid。

    这正是 2.0.0 漏掉、导致重冻里 marker_validity=1.0 高报的那条。
    """
    det = analyze_answer("正文 <<IMG:2>>", [_ch(1), _ch(2)], max_images=6,
                         included_doc_indices=[1])
    assert det["n_invalid_markers"] == 1
    assert det["marker_validity"] == 0.0
    assert det["n_shown"] == 0 and det["rendered_any"] is False


def test_old_denominator_is_not_polluted_by_the_new_one():
    """codex BLOCKER-1：旧口径必须用独立的 referenced_all，否则改主口径就把对照基准也改了。"""
    det = analyze_answer("正文 <<IMG:2>>", [_ch(1), _ch(2)], max_images=6,
                         included_doc_indices=[1])
    # 新口径：图2 不可见 ⇒ 未被有效引用 ⇒ 可见集只有图1 且是 orphan
    assert det["n_available"] == 1 and det["n_orphan"] == 1
    # 旧口径：全量看,图2 **被引用过** ⇒ orphan 只有图1
    assert det["n_available_all"] == 2 and det["n_orphan_all"] == 1
    assert det["orphan_rate_all"] == 0.5


def test_aggregate_keeps_all_visible_zero_questions_in_the_old_denominator():
    """codex BLOCKER-1 后半：全部图被截断的题不得被旧口径聚合排除。"""
    det = analyze_answer("无标记", [_ch(1), _ch(2)], max_images=6, included_doc_indices=[])
    assert det["n_available"] == 0 and det["orphan_rate"] is None
    assert det["orphan_rate_all"] == 1.0
    agg = aggregate([det])
    assert agg["orphan_rate"] is None, "新口径下该题无可见图,不该进分母"
    assert agg["orphan_rate_all"] == 1.0, "旧口径必须保住该题——否则无法逐轮对照"
    assert agg["n_available_all"] == 2


def test_shown_image_summary_is_post_rotation(monkeypatch):
    """codex BLOCKER-2：MM judge 的 shown_image_captions 必须是**实际展示**的图。

    文档1 带 4 张图、配额 2 ⇒ 只展示前 2 张；全量 image_map_summary 仍是 4 张。
    """
    chunks = [_ch(1, n_img=4)]
    det = analyze_answer("正文 <<IMG:1>>", chunks, max_images=2, included_doc_indices=[1])
    assert det["n_shown"] == 2
    assert len(det["shown_image_summary"][1]) == 2, "shown 必须按配额截断"
    assert len(det["image_map_summary"][1]) == 4, "全量诊断字段保持不变"
    assert det["shown_image_summary"][1] == det["image_map_summary"][1][:2]


def test_l4_layer_feeds_shown_not_full_map():
    """接线断言：judge bundle 里的 shown_image_captions 不得再取 image_map_summary。"""
    import inspect

    from eval_harness.layers import l4_multimodal
    src = inspect.getsource(l4_multimodal)
    assert '"shown_image_captions": det.get("shown_image_summary"' in src, \
        "MM judge 仍在喂全量候选图 —— rubric 说的是卡片实际展示的图"


def test_structured_indices_beat_context_regex():
    """结构化 included_doc_indices 优先于正则扫 context。

    构造冲突：context 串里有 `<<IMG:2>>`（例如文档正文自带该字面量），但结构化可见集
    只有 1 ⇒ 必须以结构化为准。
    """
    det = analyze_answer("正文 <<IMG:2>>", [_ch(1), _ch(2)], max_images=6,
                         context="[文档1] a <<IMG:1>>\n[文档2] 正文里写了 <<IMG:2>> 这几个字\n",
                         included_doc_indices=[1])
    assert det["n_invalid_markers"] == 1, "结构化源说图2 不可见,正则的假信号不得覆盖它"


def test_context_regex_still_works_for_replay():
    """老 det 回放：没有结构化字段时仍按 context 正则判（兼容路径不能断）。"""
    det = analyze_answer("正文 <<IMG:2>>", [_ch(1), _ch(2)], max_images=6,
                         context="[文档1] a <<IMG:1>>\n")
    assert det["n_invalid_markers"] == 1


def test_evaluator_version_bumped_and_in_regime():
    from eval_harness.baseline import _LENIENT_REGIME_KEYS, _REGIME_KEYS
    from eval_harness.mm_answer_metrics import L4SRV_EVALUATOR_VERSION
    assert L4SRV_EVALUATOR_VERSION == "3.0.0"
    for k in ("l4_serving_evaluator_version", "l4_serving_set_sha"):
        assert k in _REGIME_KEYS and k not in _LENIENT_REGIME_KEYS
