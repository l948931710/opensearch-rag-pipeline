# -*- coding: utf-8 -*-
"""L4-serving：orphan 分母收窄到「模型实际看得见的图」（2026-07-27）。

起因：`orphan_rate = 0.5921` 对 soft 门槛 0.30。查下去发现 `available` 数的是**全部**
带图 chunk，而 `_format_context` 有 `max_context_chars`（6000）预算，尾部 chunk 会被截掉
—— 它们的 `<<IMG:N>>` 根本没进 context，模型看不见、引用不了，却照样进 orphan 分母。

实测 35 题：available **152** / 可见仅 **110** ⇒ **27.6% 的分母是模型无从引用的图**。
最典型 QA-94：9 个可用、只有 1 个可见、模型引用了那 1 个 —— 实际 **1/1 全对**，
旧口径记成 1/9。这与模块文档串自称的语义（"the model isn't *placing* images"）直接矛盾。

⚠️ 我在定这条之前先否掉了自己的两个错判：
  · "闸门结构上不可达"——错。`orphans = available - referenced` 里**根本没有 cap**；
    且我当时用的 cap=3 也不是生产值（真实 `max_answer_images=6`）。
  · "分母该按文档去重"——也错。步骤卡每张图不同（第1步截图 vs 第2步截图），
    BIND-04 引用 8/8 正是要的行为，按文档去重会把它压成 1/1 看不出好坏。

收窄后 0.6184 → 0.4727，**仍高于 0.30** —— 不是往绿了调。旧口径保留为 `orphan_rate_all`。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

from eval_harness.mm_answer_metrics import (  # noqa: E402
    L4SRV_EVALUATOR_VERSION,
    _visible_indices,
    aggregate,
    analyze_answer,
)


def _chunks(n_img=3, n_text=2):
    """前 n_img 个带图、后 n_text 个纯文本。

    形态必须能通过生产的 `renderable_image_refs`（只收带 oss_key 的图，且按
    chunk_type 分支）—— 第一版随手造的 dict 过不了它，`n_available` 恒 0，
    测试全红。造数据要按生产的可渲染判据来，不能自己想当然。
    """
    out = []
    for i in range(n_img):
        out.append({"chunk_id": f"i{i}", "chunk_text": f"图文{i}",
                    "chunk_type": "step_card",
                    "image_refs": [{"oss_key": f"k{i}", "source_image": f"s{i}.png",
                                    "visual_summary": f"图{i}"}]})
    for i in range(n_text):
        out.append({"chunk_id": f"t{i}", "chunk_text": f"纯文本{i}"})
    return out


def test_visible_set_is_the_markers_actually_in_context():
    imap = {1: [{}], 2: [{}], 3: [{}]}
    ctx = "[文档1] a <<IMG:1>>\n[文档3] c <<IMG:3>>\n"   # 文档2 被截掉了
    assert _visible_indices(imap, ctx) == {1, 3}


def test_subindex_form_counts_as_visible():
    """RAG_IMG_SUBINDEX 开时 context 发的是 `<<IMG:3.1>>`，不能漏判成"看不见"。"""
    assert _visible_indices({3: [{}]}, "[文档3] x <<IMG:3.1>> <<IMG:3.2>>") == {3}


def test_none_context_falls_back_to_full_set():
    """老调用方 / 回放旧 det：不给 context 时必须是老口径，不得静默改数。"""
    imap = {1: [{}], 2: [{}]}
    assert _visible_indices(imap, None) == {1, 2}


def test_truncated_images_no_longer_count_as_orphans():
    """核心用例，取自实测的 QA-94 形态：9 可用 / 1 可见 / 引用了那 1 个。"""
    chunks = _chunks(n_img=3, n_text=0)
    ctx = "[文档1] a <<IMG:1>>\n"          # 文档 2、3 被 max_context_chars 截掉
    det = analyze_answer("正文 <<IMG:1>>", chunks, max_images=6, context=ctx)
    assert det["n_available"] == 1 and det["n_orphan"] == 0
    assert det["orphan_rate"] == 0.0, "模型把能看见的都放了 ⇒ 不该判 orphan"
    # 旧口径同时保留，可对照收窄了多少
    assert det["n_available_all"] == 3 and det["orphan_rate_all"] == pytest.approx(2 / 3)


def test_model_that_ignores_visible_images_is_still_penalised():
    """收窄不能把真问题也抹掉：看得见却不放，照样是 orphan（实测 J-r120_23 形态）。"""
    chunks = _chunks(n_img=3, n_text=0)
    ctx = "[文档1] a <<IMG:1>>\n[文档2] b <<IMG:2>>\n[文档3] c <<IMG:3>>\n"
    det = analyze_answer("正文里一个标记都没有", chunks, max_images=6, context=ctx)
    assert det["n_available"] == 3 and det["n_orphan"] == 3
    assert det["orphan_rate"] == 1.0


def test_legacy_behavior_preserved_without_context():
    chunks = _chunks(n_img=3, n_text=0)
    det = analyze_answer("正文 <<IMG:1>>", chunks, max_images=6)
    assert det["n_available"] == 3 and det["orphan_rate"] == pytest.approx(2 / 3)
    assert det["orphan_rate_all"] == det["orphan_rate"], "无 context 时两个口径必须一致"


def test_marker_validity_is_unaffected_by_the_narrowing():
    """只动 orphan 分母，不得波及 marker_validity（那是另一条闸，别混改）。"""
    chunks = _chunks(n_img=2, n_text=1)
    ctx = "[文档1] a <<IMG:1>>\n"
    det = analyze_answer("x <<IMG:1>> y <<IMG:9>>", chunks, max_images=6, context=ctx)
    assert det["n_markers"] == 2 and det["n_invalid_markers"] == 1
    assert det["marker_validity"] == 0.5


def test_aggregate_reports_both_denominators():
    chunks = _chunks(n_img=3, n_text=0)
    ctx = "[文档1] a <<IMG:1>>\n"
    dets = [analyze_answer("正文 <<IMG:1>>", chunks, max_images=6, context=ctx)]
    agg = aggregate(dets)
    assert agg["orphan_rate"] == 0.0
    assert agg["orphan_rate_all"] == pytest.approx(2 / 3)
    assert agg["n_available_visible"] == 1 and agg["n_available_all"] == 3


def test_aggregate_tolerates_legacy_dets_without_the_new_keys():
    """回放老 det（缺 n_available_all/n_orphan_all）不得炸，也不得给出假数。"""
    legacy = {"n_available": 3, "n_orphan": 2, "n_markers": 1, "n_invalid_markers": 0,
              "n_inrange_markers": 1, "n_distinct_markers": 1, "n_referenced": 1,
              "n_shown": 1, "rendered_any": True, "over_cap": False,
              "dangling_ref": False, "strategy": "interleaved", "interleaved": True,
              "has_figure_phrase": False}
    agg = aggregate([legacy])
    assert agg["orphan_rate"] == pytest.approx(2 / 3)
    assert agg["orphan_rate_all"] == pytest.approx(2 / 3), "缺键时退回新口径值，不臆造"


def test_evaluator_version_is_in_regime_keys():
    """改**尺子**必须与"模型放图变好了"可区分 —— 同 l4_ingestion / l6 / l1 三条先例。"""
    from eval_harness.baseline import _LENIENT_REGIME_KEYS, _REGIME_KEYS
    assert L4SRV_EVALUATOR_VERSION
    assert "l4_serving_evaluator_version" in _REGIME_KEYS
    assert "l4_serving_evaluator_version" not in _LENIENT_REGIME_KEYS


def test_l4_layer_passes_the_context_through():
    """接线断言：l4_multimodal 必须把生成方返回的 context 传给 analyze_answer。

    不传就静默退回老口径 —— 这正是本轮 #F-mm14 假 ON 的同型陷阱（改了判据但喂不到）。
    """
    import ast
    import inspect

    from eval_harness.layers import l4_multimodal
    tree = ast.parse(inspect.getsource(l4_multimodal))
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and getattr(c.func, "attr", None) == "analyze_answer"]
    assert calls, "找不到 analyze_answer 调用"
    for c in calls:
        assert any(k.arg == "context" for k in c.keywords), \
            "analyze_answer 调用缺 context= —— 会静默退回老口径"
