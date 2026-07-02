# -*- coding: utf-8 -*-
"""mm_answer_metrics referenced-only 口径对齐（#F-mm1a, 2026-07-01）回归测试。

背景：生产 build_content_blocks 早已是 referenced-only（LLM 无有效 <<IMG:N>> →
返回 []），但 mm_answer_metrics 仍按旧「未引用图追加末尾」模型算
n_shown=min(available, cap) —— 「答案写『如图所示』但零标记」这一实卡片零图的
真 dangling 被判 n_shown>0 → dangling=False，dangling≤0.05 硬闸对最常见失败形态
恒 PASS。本文件钉死新口径：
  - n_shown 从 referenced 集出发按生产轮转配额模拟（plan_image_rotation 共享语义
    + conformance 测试防两侧漂移）；
  - 零有效引用 → n_shown=0、strategy=referenced_none、图指代短语 → dangling=True；
  - rendered_any / answer_image_rate 新主指标；
  - 旧存档 dets（strategy='appended'、无 rendered_any）回放兼容。
"""

import opensearch_pipeline.content_blocks_builder as cb
from opensearch_pipeline.content_blocks_builder import plan_image_rotation
import eval_harness.mm_answer_metrics as M
from eval_harness.mm_answer_metrics import aggregate, analyze_answer


def _map(counts):
    """{1: [img]*counts[0], 2: [...], ...}"""
    return {
        i: [{"visual_summary": f"图{i}-{j}"} for j in range(c)]
        for i, c in enumerate(counts, 1)
    }


# ═══════════════ plan_image_rotation 本体 ═══════════════

def test_rotation_round_robin_semantics():
    # 每轮各取 1 张：counts=[3,1,2] cap=4 → 第一轮 [1,1,1]，第二轮 doc1/doc3 → [2,1,1]
    assert plan_image_rotation([3, 1, 2], 4) == [2, 1, 1]
    assert plan_image_rotation([3, 1, 2], 10) == [3, 1, 2]   # 配额富余取全
    assert plan_image_rotation([5], 3) == [3]
    assert plan_image_rotation([], 6) == []
    assert plan_image_rotation([2, 2], 0) == [0, 0]


def test_rotation_conformance_with_production_builder(monkeypatch):
    """conformance：plan_image_rotation 的计划数 == build_content_blocks 实渲数。
    改生产配额循环必须让本测试跟着变——这是两侧共用语义的防漂移钉子。"""
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "https://oss/" + key)
    # doc1: 3 图 step_card；doc2: 1 图 image chunk；doc3: 2 图 step_card。全被引用，cap=4。
    chunks = [
        {"chunk_type": "step_card", "doc_id": "D1", "title": "a",
         "image_refs": [{"oss_key": f"d1-{j}.png", "caption": f"c{j}"} for j in range(3)]},
        {"chunk_type": "image", "doc_id": "D2", "source_image": "d2.png",
         "visual_summary": "唯一一张服务器机房照片带有独特描述文字避免判重", "title": "b"},
        {"chunk_type": "step_card", "doc_id": "D3", "title": "c",
         "image_refs": [{"oss_key": f"d3-{j}.png", "caption": f"e{j}"} for j in range(2)]},
    ]
    answer = "步骤一 <<IMG:1>>，配图 <<IMG:2>>，收尾 <<IMG:3>>。"
    blocks = cb.build_content_blocks(answer, chunks, max_images=4)
    n_rendered = sum(1 for b in blocks if b["type"] == "image")
    assert n_rendered == sum(plan_image_rotation([3, 1, 2], 4)) == 4
    det = analyze_answer(answer, chunks, max_images=4)
    assert det["n_shown"] == n_rendered


# ═══════════════ referenced-only analyze_answer ═══════════════

def test_zero_markers_means_zero_shown_and_dangling(monkeypatch):
    """THE 核心修复：有图可用 + 图指代短语 + 零标记 = 实卡片 0 图 = 真 dangling。
    旧口径 n_shown=min(available,cap)>0 → dangling=False（硬闸致盲）。"""
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: _map([2, 1]))
    det = analyze_answer("操作如下图所示，先进入界面。", used_chunks=[])
    assert det["n_shown"] == 0
    assert det["rendered_any"] is False
    assert det["strategy"] == "referenced_none"
    assert det["dangling_ref"] is True          # 旧口径此处为 False
    assert det["interleaved"] is False


def test_invalid_only_markers_render_nothing(monkeypatch):
    """纯越界标记渲染不出图：strategy=referenced_none 而非旧版 interleaved。"""
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: _map([1]))
    det = analyze_answer("见 <<IMG:9>>，如图所示。", used_chunks=[])
    assert det["n_markers"] == 1 and det["n_invalid_markers"] == 1
    assert det["n_shown"] == 0 and det["rendered_any"] is False
    assert det["strategy"] == "referenced_none"
    assert det["dangling_ref"] is True
    assert det["marker_validity"] == 0.0


def test_valid_reference_shows_rotation_quota(monkeypatch):
    """n_shown = 被引用文档图片的轮转配额结果，不再数未引用的可用图。"""
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: _map([3, 5, 2]))
    # 只引用 doc1（3 图）与 doc3（2 图），doc2 的 5 图是 orphan 不渲染
    det = analyze_answer("先 <<IMG:1>> 再 <<IMG:3>>", used_chunks=[], max_images=4)
    assert det["n_referenced_images"] == 5
    assert det["n_shown"] == 4                       # cap=4 截断
    assert det["over_cap"] is True                   # referenced 5 > cap 4
    assert det["rendered_any"] is True
    assert det["strategy"] == "interleaved"
    assert det["n_orphan"] == 1 and det["orphan_idxs"] == [2]


def test_over_cap_counts_referenced_not_available(monkeypatch):
    """over_cap 按被引用文档的图数判定：可用图很多但只引用了少量 → 不算 over_cap。"""
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: _map([1, 9]))
    det = analyze_answer("见 <<IMG:1>>", used_chunks=[], max_images=6)
    assert det["n_shown"] == 1
    assert det["over_cap"] is False                  # 旧口径 available=10>6 会误判 True
    assert det["dangling_ref"] is False


def test_text_only_when_no_images(monkeypatch):
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: {})
    det = analyze_answer("纯文字回答。", used_chunks=[])
    assert det["strategy"] == "text_only"
    assert det["n_shown"] == 0 and det["rendered_any"] is False
    assert det["dangling_ref"] is False              # 无图可用也无指代


def test_default_max_images_matches_production():
    """默认 cap=6 对齐生产 config.rag.max_answer_images（旧默认 3 口径错位）。"""
    import inspect
    assert inspect.signature(analyze_answer).parameters["max_images"].default == 6


# ═══════════════ aggregate：answer_image_rate + legacy 回放 ═══════════════

def test_aggregate_answer_image_rate(monkeypatch):
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: _map([2]))
    dets = [
        analyze_answer("见 <<IMG:1>>", []),          # rendered
        analyze_answer("如图所示但没放标记", []),      # not rendered (dangling)
    ]
    monkeypatch.setattr(M, "_extract_image_chunks", lambda c: {})
    dets.append(analyze_answer("纯文字", []))         # no images → 不进分母
    agg = aggregate(dets)
    assert agg["n_answers_with_images"] == 2
    assert agg["answer_image_rate"] == 0.5
    assert agg["dangling_ref_rate"] == 1 / 3


def test_aggregate_legacy_dets_replay():
    """旧存档 dets（无 rendered_any、strategy='appended'）回放：
    rendered 从 in-range 标记数推导；appended 计入 referenced_none 桶。"""
    legacy = [
        # 旧「appended」det：有图无标记（新语义=referenced_none，未渲染）
        {"n_available": 3, "n_markers": 0, "n_invalid_markers": 0, "n_valid_markers": 0,
         "interleaved": False, "dangling_ref": False, "over_cap": False, "n_orphan": 3,
         "strategy": "appended", "n_shown": 3},
        # 旧 interleaved det：2 个在界标记（新语义=已渲染）
        {"n_available": 4, "n_markers": 2, "n_valid_markers": 1, "n_invalid_markers": 0,
         "interleaved": True, "dangling_ref": False, "over_cap": False, "n_orphan": 0,
         "strategy": "interleaved", "n_shown": 2},
    ]
    agg = aggregate(legacy)
    assert agg["answer_image_rate"] == 0.5           # 从 n_markers-n_invalid 推导
    assert agg["n_referenced_none_strategy"] == 1
    assert agg["n_appended_strategy"] == 1           # 旧键名保留，值含两代标签
    assert agg["marker_validity"] == 1.0             # 既有 legacy 回放语义不回退
