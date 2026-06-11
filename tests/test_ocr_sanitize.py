# -*- coding: utf-8 -*-
"""OCR 反幻觉清洗 + VLM 缓存遗留 key 回退 —— 2026-06-10 known-open 修复回归。

Qwen-VL OCR 对低文本/照片类输入会编造内容（同一表格行重复几十次、尾部短语
死循环），编造文本曾直接进入索引 chunk；遗留裸 MD5 缓存条目（命名空间化
之前写入，OSS 上 ~1500 条）用带后缀 key 永远查不到 → 回灌全量重打 VLM。
"""

import pytest

from opensearch_pipeline.extraction.ocr_client import OCRClient, sanitize_ocr_text
from opensearch_pipeline.extraction.unified_extractor import _vlm_cache_lookup


# ═══════════════ sanitize_ocr_text ═══════════════

def test_repeated_lines_collapsed():
    """编造表格的典型形态：同一行连续重复几十次 → 只留前 2 次。"""
    text = "\n".join(["检验项目 外观 结论 合格"] * 20)
    clean, meta = sanitize_ocr_text(text)
    assert meta["sanitized"]
    assert clean.splitlines().count("检验项目 外观 结论 合格") == 2


def test_interleaved_dominant_line_collapsed():
    """穿插重复（A B A C A…）连续规则抓不到，主导行规则兜底。"""
    rows = []
    for i in range(8):
        rows.append("| 外观检查 | 目视 | 合格 |")
        rows.append(f"| 第{i}项 | 卡尺读数 | {i}mm |")
    clean, meta = sanitize_ocr_text("\n".join(rows))
    assert meta["sanitized"]
    assert clean.splitlines().count("| 外观检查 | 目视 | 合格 |") == 2
    assert "| 第7项 | 卡尺读数 | 7mm |" in clean, "非主导行必须全保留"


def test_single_line_short_cell_repeats_untouched():
    """检验记录单行『合格 合格 合格』是合法表格内容（<6 字符短语下限保护）。"""
    text = "外观 合格 合格 合格 合格 合格"
    clean, meta = sanitize_ocr_text(text)
    assert clean == text and not meta["sanitized"]


def test_tail_phrase_loop_trimmed():
    """VLM 退化的尾部短语死循环 → 截到 2 次。"""
    text = "操作完成后保存单据。" + "点击确定按钮系统提示成功" * 8
    clean, meta = sanitize_ocr_text(text)
    assert meta["sanitized"] and "tail-loop" in meta["reason"]
    assert clean.count("点击确定按钮系统提示成功") == 2
    assert clean.startswith("操作完成后保存单据。")


def test_density_bound_drops_fabricated_text_on_tiny_image():
    """23×31 小图物理上装不下 420 字 → 整体编造，返回空。"""
    clean, meta = sanitize_ocr_text("编造表格内容" * 60, width=23, height=31)
    assert clean == "" and meta["sanitized"]
    assert meta["reason"].startswith("density")


def test_density_bound_spares_plausible_text():
    clean, meta = sanitize_ocr_text("交货单编号 FL-2026-0610", width=23, height=31)
    assert clean == "交货单编号 FL-2026-0610" and not meta["sanitized"]


def test_page_render_never_density_dropped():
    """整页 OCR 不传尺寸 → 密度规则永不触发，正常长文原样保留。"""
    long_text = "\n".join(f"第{i}行 互不相同的真实内容句子。" for i in range(400))
    clean, meta = sanitize_ocr_text(long_text)
    assert clean == long_text and not meta["sanitized"]


def test_never_raises_on_garbage():
    for garbage in (None, "", 123, b"bytes"):
        clean, meta = sanitize_ocr_text(garbage)
        assert clean == garbage and not meta["sanitized"]


def test_simulate_ocr_untouched():
    """simulate 返回的字面量不经过清洗（测试契约依赖它）。"""
    r = OCRClient(simulate=True).ocr_image("/nonexistent.png", "DOC1")
    assert r.combined_text == "[OCR: image content recognized]"


def test_real_image_ocr_path_sanitizes(monkeypatch, tmp_path):
    """真实路径接线：嵌入图自测尺寸 → 编造长文被密度上界拦截。"""
    pil_image = pytest.importorskip("PIL.Image")
    p = tmp_path / "t.png"
    pil_image.new("RGB", (40, 40), (255, 255, 255)).save(str(p))
    client = OCRClient(api_key="fake", simulate=False)
    monkeypatch.setattr(client, "_call_ocr_api", lambda b64, mime: "编造的内容句子" * 50)
    r = client._real_image_ocr(str(p), "DOC1")
    assert r.combined_text == "", "40×40 图上 350 字应被密度上界判为编造"


# ═══════════════ VLM 缓存遗留裸 MD5 回退 ═══════════════

def _legacy_entry(status="ROUTE_TO_VECTOR", **kw):
    e = {"status": status, "visual_summary": "U8登录界面截图",
         "image_category": "step_screenshot", "vlm_annotation_map": {},
         "reason": "", "width": 640, "height": 480, "file_size_kb": 55.0,
         "ocr_text": "用户名 密码 登录"}
    e.update(kw)
    return e


def test_legacy_bare_key_hits_for_public():
    cache = {"abc123": _legacy_entry()}
    hit = _vlm_cache_lookup(cache, "abc123", is_public=True)
    assert hit is not None and hit["status"] == "ROUTE_TO_VECTOR"
    assert "abc123:pub" in cache, "命中后应迁移到带后缀 key（裸 key 自然老化）"


def test_legacy_bare_key_never_used_for_sec():
    """裸 key 全部产生于 public-bypass 时代，:sec 复用会跳过敏感审计。"""
    cache = {"abc123": _legacy_entry()}
    assert _vlm_cache_lookup(cache, "abc123", is_public=False) is None


def test_suffixed_key_preferred_over_legacy():
    cache = {"abc123": _legacy_entry(visual_summary="旧条目"),
             "abc123:pub": _legacy_entry(visual_summary="新条目")}
    assert _vlm_cache_lookup(cache, "abc123", True)["visual_summary"] == "新条目"


def test_unsafe_legacy_entries_rejected():
    cache = {
        "k1": _legacy_entry(status="QUARANTINE_SENSITIVE"),
        "k2": _legacy_entry(visual_summary="[Simulated] VLM caption"),
        "k3": _legacy_entry(status="UNKNOWN_STATUS"),
        "k4": "not-a-dict",
    }
    for k in cache:
        assert _vlm_cache_lookup(cache, k, True) is None, f"{k} 不应被复用"
    assert "k1:pub" not in cache and "k2:pub" not in cache


def test_cache_miss_returns_none():
    assert _vlm_cache_lookup({}, "nope", True) is None
