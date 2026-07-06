# -*- coding: utf-8 -*-
"""G12-OCR：页级 OCR 文本的页眉/页脚裁剪（G6 tier-3 决策抓获②）回归测试。

原生 pdfplumber 路径按 y 边界裁页眉，OCR 兜底文本无坐标 → 用 Pass-1 跨页
词集（词级+数字归一）做行级过滤。见 unified_extractor._strip_hf_lines_from_ocr。
"""
from opensearch_pipeline.extraction.schema import ExtractedBlock
from opensearch_pipeline.extraction.unified_extractor import _strip_hf_lines_from_ocr

# 模拟 Pass-1 词集（词级 + 数字归一，与 pdf_extractor top_candidates 键同构；
# 注意 \d+ 是整串替换："FL-CW-SW-003"→"FL-CW-SW-#"，"2022"→"#"）
HF = {"富岭科技股份有限公司", "作业指导书", "文件编号：", "FL-CW-SW-#",
      "文件版本号：", "生效日期：", "#"}


def _blk(text, page=1):
    return ExtractedBlock(block_type="ocr_text", text=text, page_num=page, source="ocr")


def test_header_lines_dropped_body_kept():
    ocr = [_blk("富岭科技股份有限公司\n"
                "文件编号： FL-CW-SW-003\n"
                "文件版本号： A / 0\n"
                "生效日期： 2022 年 7 月 1 日\n"
                "作业指导书\n"
                "开界面选择 红字 即可开具。")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, HF)
    assert dropped == 5, f"5 行页眉应剔除, got {dropped}"
    assert len(out) == 1
    assert out[0].text == "开界面选择 红字 即可开具。"


def test_digit_normalization_matches():
    """FL-CW-SW-003 / 2022年 等数字变体按 # 归一命中词集。"""
    ocr = [_blk("文件编号： FL-CW-SW-099\n正文：按需培训，坚持以适用教育为目的。")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, HF)
    assert dropped == 1
    assert "按需培训" in out[0].text
    assert "FL-CW-SW" not in out[0].text


def test_empty_hf_set_noop():
    """纯扫描件：Pass-1 无词集 → 完全 no-op（fail-open ①）。"""
    ocr = [_blk("富岭科技股份有限公司\n正文内容")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, set())
    assert dropped == 0
    assert out[0].text == "富岭科技股份有限公司\n正文内容"


def test_mini_page_header_dominant_still_filtered():
    """迷你页（页眉占多数）是主修复场景——不得被任何比例阀拦截。"""
    ocr = [_blk("富岭科技股份有限公司\n作业指导书\n文件编号： FL-CW-SW-001\n仅此一行正文")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, HF)
    assert dropped == 3
    assert out[0].text == "仅此一行正文"


def test_set_size_valve():
    """词集规模阀（fail-open ②）：>60 项=检测器失灵 → 整体放弃过滤。"""
    big = {f"词{i}条目" for i in range(61)} | {"富岭科技股份有限公司"}
    ocr = [_blk("富岭科技股份有限公司\n正文")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, big)
    assert dropped == 0
    assert "富岭科技股份有限公司" in out[0].text


def test_all_header_block_removed():
    """整块皆页眉 → 块移除；同页正文块不受影响（fail-open ③）。"""
    ocr = [_blk("富岭科技股份有限公司\n作业指导书", page=2),
           _blk("这一页是正文，应完整保留。", page=2)]
    out, dropped = _strip_hf_lines_from_ocr(ocr, HF)
    assert dropped == 2
    assert len(out) == 1
    assert out[0].text == "这一页是正文，应完整保留。"


def test_body_line_with_unknown_token_kept():
    """行内任一多字符 token 不在词集 → 整行保留（正文提及公司名不误伤）。"""
    ocr = [_blk("富岭科技股份有限公司 是一家餐具制造企业")]
    out, dropped = _strip_hf_lines_from_ocr(ocr, HF)
    assert dropped == 0
    assert "餐具制造企业" in out[0].text
