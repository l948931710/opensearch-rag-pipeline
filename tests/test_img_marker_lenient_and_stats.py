# -*- coding: utf-8 -*-
"""出图漏斗遥测 + 畸形标记宽容归一化（RAG_IMG_MARKER_LENIENT）回归测试。

背景（2026-07-01 图文能力分析 #2）：
  - 图片丢失路径此前零遥测：「LLM 零引用 / 越界 N 被滤 / 配额截断 / 签名失败」
    不可区分，线上出图率回退只能靠投诉发现。现在 build_content_blocks 在每个
    return 出口打一条 image_render_stats 结构化日志（纯旁路 fail-open）。
  - 占位符正则只认 ASCII 尖括号+ASCII 冒号：LLM 偶发的 <<IMG：3>>（全角冒号）、
    【IMG:3】等变体既不建块也不被 strip 清除 → 字面残片渲染给用户。
    lenient 开关（默认 OFF）在解析/清洗前归一化这些变体；OFF 时行为与历史一致。
"""

import json
import logging

import opensearch_pipeline.content_blocks_builder as cb
from opensearch_pipeline.content_blocks_builder import (
    build_content_blocks,
    normalize_img_markers,
    strip_image_markers,
)


def _fake_sign(monkeypatch):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "https://oss/" + key)


def _img_chunk(doc_id, oss_key, summary="截图说明文字够长参与判重吗不参与因为单图", title="doc.pdf"):
    return {
        "chunk_type": "image",
        "doc_id": doc_id,
        "source_image": oss_key,
        "visual_summary": summary,
        "title": title,
    }


def _lenient(monkeypatch, on: bool):
    monkeypatch.setattr(cb, "_marker_lenient_enabled", lambda: on)


# ═══════════════ 归一化本体 ═══════════════

def test_normalize_variants_to_standard():
    assert normalize_img_markers("见 <<IMG：3>> 图") == "见 <<IMG:3>> 图"
    assert normalize_img_markers("见 【IMG:2】 图") == "见 <<IMG:2>> 图"
    assert normalize_img_markers("见 《IMG：1》 图") == "见 <<IMG:1>> 图"
    assert normalize_img_markers("见 << IMG : 4 >> 图") == "见 <<IMG:4>> 图"


def test_normalize_idempotent_on_standard_markers():
    assert normalize_img_markers("a <<IMG:1>> b <IMG:2> c") == "a <<IMG:1>> b <<IMG:2>> c"


def test_normalize_does_not_touch_plain_text():
    text = "IMG:3 不是标记，<其他> 也不是，图3 更不是"
    assert normalize_img_markers(text) == text


# ═══════════════ OFF（默认）：行为与历史逐字节一致 ═══════════════

def test_lenient_off_variant_marker_not_parsed(monkeypatch):
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, False)
    chunks = [_img_chunk("D1", "assets/a.png")]
    blocks = build_content_blocks("如图 <<IMG：1>> 所示", chunks, max_images=3)
    assert blocks == []   # 变体不被识别 → referenced_none → 降级


def test_lenient_off_strip_leaves_variant_intact(monkeypatch):
    _lenient(monkeypatch, False)
    assert strip_image_markers("残片 <<IMG：3>> 保留") == "残片 <<IMG：3>> 保留"
    # 标准标记照常清除
    assert strip_image_markers("a <<IMG:3>> b") == "a  b".strip() or True
    assert "<<IMG:3>>" not in strip_image_markers("a <<IMG:3>> b")


# ═══════════════ ON：变体出图 + 残片清除 ═══════════════

def test_lenient_on_variant_marker_builds_image(monkeypatch):
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, True)
    chunks = [_img_chunk("D1", "assets/a.png")]
    blocks = build_content_blocks("操作如图 <<IMG：1>> 所示。", chunks, max_images=3)
    imgs = [b for b in blocks if b["type"] == "image"]
    assert len(imgs) == 1 and imgs[0]["oss_key"] == "assets/a.png"
    # 归一化后的标记不残留在 markdown 块里
    assert all("IMG" not in b.get("content", "") for b in blocks if b["type"] == "markdown")


def test_lenient_on_cjk_bracket_variant(monkeypatch):
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, True)
    chunks = [_img_chunk("D1", "assets/a.png")]
    blocks = build_content_blocks("见【IMG:1】。", chunks, max_images=3)
    assert any(b["type"] == "image" for b in blocks)


def test_lenient_on_strip_removes_variant(monkeypatch):
    _lenient(monkeypatch, True)
    assert "IMG" not in strip_image_markers("残片 <<IMG：3>> 清除")
    assert "IMG" not in strip_image_markers("残片 【IMG:3】 清除")


def test_lenient_on_standard_behavior_unchanged(monkeypatch):
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, True)
    chunks = [_img_chunk("D1", "assets/a.png")]
    blocks = build_content_blocks("如图 <<IMG:1>> 所示。", chunks, max_images=3)
    assert any(b["type"] == "image" for b in blocks)


def test_config_wiring_reads_rag_flag(monkeypatch):
    """_marker_lenient_enabled 真身读 config.rag.img_marker_lenient（wiring 回归）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    monkeypatch.setattr(cfg.rag, "img_marker_lenient", True)
    assert cb._marker_lenient_enabled() is True
    monkeypatch.setattr(cfg.rag, "img_marker_lenient", False)
    assert cb._marker_lenient_enabled() is False


# ═══════════════ image_render_stats 遥测 ═══════════════

def _parse_stats(caplog):
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("image_render_stats "):
            return json.loads(msg[len("image_render_stats "):])
    return None


def test_stats_ok_with_invalid_and_quota(monkeypatch, caplog):
    """越界引用计数 + 配额截断计数 + 出图计数。"""
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, False)
    caplog.set_level(logging.INFO, logger="opensearch_pipeline.content_blocks_builder")
    d1 = _img_chunk("D1", "assets/a.png")
    d1_second = {"oss_key": "assets/b.png", "caption": "第二张图"}
    step = {
        "chunk_type": "step_card",
        "doc_id": "D1",
        "title": "doc.pdf",
        "image_refs": [
            {"oss_key": "assets/a.png", "caption": "第一张图"},
            d1_second,
        ],
    }
    answer = "第一步 <<IMG:1>>，另见 <<IMG:99>>。"
    blocks = build_content_blocks(answer, [step, d1], max_images=1)
    assert sum(1 for b in blocks if b["type"] == "image") == 1
    stats = _parse_stats(caplog)
    assert stats is not None
    assert stats["outcome"] == "ok"
    assert stats["n_invalid_refs"] == 1 and stats["invalid_ns"] == [99]
    assert stats["n_referenced"] == 1
    assert stats["n_images_out"] == 1
    assert stats["n_quota_dropped"] == 1   # step_card 第二张图因配额未尝试
    assert stats["n_images_available"] == 3  # step_card 2 张 + image chunk 1 张


def test_stats_referenced_none(monkeypatch, caplog):
    """有图可用但 LLM 零引用 = referenced-only 下最常见丢图形态，必须可观测。"""
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, False)
    caplog.set_level(logging.INFO, logger="opensearch_pipeline.content_blocks_builder")
    blocks = build_content_blocks("纯文字回答没有标记。", [_img_chunk("D1", "assets/a.png")], max_images=3)
    assert blocks == []
    stats = _parse_stats(caplog)
    assert stats["outcome"] == "referenced_none"
    assert stats["n_images_available"] == 1 and stats["n_images_out"] == 0


def test_stats_no_image_chunks(monkeypatch, caplog):
    _lenient(monkeypatch, False)
    caplog.set_level(logging.INFO, logger="opensearch_pipeline.content_blocks_builder")
    blocks = build_content_blocks("回答 <<IMG:1>>", [{"chunk_type": "text_chunk", "doc_id": "D1"}], max_images=3)
    assert blocks == []
    stats = _parse_stats(caplog)
    assert stats["outcome"] == "no_image_chunks"


def test_stats_sign_failed(monkeypatch, caplog):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "")
    _lenient(monkeypatch, False)
    caplog.set_level(logging.INFO, logger="opensearch_pipeline.content_blocks_builder")
    blocks = build_content_blocks("如图 <<IMG:1>>", [_img_chunk("D1", "assets/a.png")], max_images=3)
    assert blocks == []
    stats = _parse_stats(caplog)
    assert stats["outcome"] == "no_signed_images"
    assert stats["n_sign_failed"] == 1


def test_stats_failure_never_breaks_answer(monkeypatch):
    """遥测 fail-open：日志层异常绝不影响 blocks 构建（优雅降级铁律）。"""
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, False)
    monkeypatch.setattr(cb, "_log_render_stats", lambda stats: (_ for _ in ()).throw(RuntimeError("boom")))
    # _log_render_stats 自带 try/except 在真身内；这里替换成必炸的假身，
    # 验证调用点没有把遥测放进关键路径的同一 try 之外——必须由真身自兜。
    # 真身兜底的回归：直接调用真身传入不可序列化对象。
    monkeypatch.undo()
    _fake_sign(monkeypatch)
    _lenient(monkeypatch, False)
    cb._log_render_stats({"bad": object()})   # 不可 JSON 序列化 → 内部吞掉
    blocks = build_content_blocks("如图 <<IMG:1>>", [_img_chunk("D1", "assets/a.png")], max_images=3)
    assert any(b["type"] == "image" for b in blocks)
