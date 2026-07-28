# -*- coding: utf-8 -*-
"""PDF-D3（2026-07-25）：画在截图上的**纯数字标注号**不得被判成 heading。

实证（《U8弃审》作业指导书，图内标注号是纯阿拉伯数字 1–24 而非圈号）：这些行以标题字号
渲染 → PDF 的字号启发判成 heading，后果有两层：
  ① `section_path` 被污染 —— 真步骤段的章节变成 '7' / '13  16'，该值会进 chunk 文本的
     「章节：」前缀（→ embedding）、RDS chunk_meta、HA3 字段、serving 展示与 QA 血缘；
  ② chunker 按 heading 建步骤组 —— 孵出正文只有 "18"/"21"/"22" 的 step_card，**把图全
     抢走**，真正的 step_no=4/5/6 卡 image_refs 为空。

范围刻意收窄在 PDF：`is_pseudo_heading` 是 PDF/DOCX 共享接口，DOCX 的 heading 来自样式
（作者意图），不可由 PDF 版面证据外推 —— 故新增独立判据只接进 pdf_extractor 的
`looks_callout`（它同时 veto 字号与加粗两路；第三路中文正则 fallback 不受影响，纯数字
本就不命中它）。本文件同时钉死"DOCX 未受影响"。

页面用 stub 驱动 `_pass2_extract_page`：真实渲染件在这里不划算 —— pdfplumber 的
text-策略表格检测会把规整的合成文字网格整页吞成 table，反复调坐标只会让夹具变脆。
真实 PDF 路径的覆盖由 49 篇本地语料普查 + 目标文档的 L4 硬闸承担（见提交说明）。
"""
import pytest

from opensearch_pipeline.extraction.pdf_extractor import _LayoutAnalysis, _pass2_extract_page
from opensearch_pipeline.extraction.schema import is_bare_numeric_callout, is_pseudo_heading

BODY, HEAD = 10.0, 16.0


# ── 判据本身 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["18", "13  16", "  7 ", "1", "24", "１８", "9　9"])
def test_bare_numeric_callout_positives(text):
    assert is_bare_numeric_callout(text) is True


@pytest.mark.parametrize("text", [
    "4.1", "4.1 Check Mold", "3.2.4正常单据记账",   # 合法点分节标题，绝不能 veto
    "1目的", "一、盘点车间原辅料", "1、进入U8系统", "步骤3", "第五步 安装硬盘",
    "18 步骤", "Step 3", "", "  ",
])
def test_bare_numeric_callout_negatives(text):
    assert is_bare_numeric_callout(text) is False


def test_fullwidth_digits_are_intentionally_covered():
    """`\\d` 是 Unicode 十进制数字而非 [0-9] —— 全角 "１８" 同属标注号形态，这是有意的。"""
    assert is_bare_numeric_callout("１８") is True


def test_shared_pseudo_heading_contract_unchanged():
    """DOCX 共享接口语义**未变**：它仍只 veto 圈号，纯数字对它是 False。"""
    assert is_pseudo_heading("⑤双击图标") is True
    assert is_pseudo_heading("18") is False
    assert is_pseudo_heading("13  16") is False
    assert is_pseudo_heading("4.1 检查模具") is False


def test_docx_style_heading_still_wins_for_bare_number():
    """DOCX 样式命中的 "18" 仍是 heading —— 本次改动没碰 DOCX 路径。"""
    from opensearch_pipeline.extraction.docx_extractor import _detect_heading_level
    assert _detect_heading_level("Heading 1", "18") == 1


# ── PDF 版面：字号/加粗两路都要被 veto ───────────────────────────────────────

def _word(text, top, size, fontname="ABCDEF+SimSun", x0=42.5):
    return {"text": text, "top": top, "bottom": top + size + 2.0,
            "x0": x0, "x1": x0 + 8.0 * len(text),
            "size": size, "fontname": fontname, "upright": True}


class _StubPage:
    """`_pass2_extract_page` 只用到 width/height/crop/find_tables/extract_words 五个口。"""

    def __init__(self, words, width=595.276, height=841.89):
        self._words = words
        self.width = width
        self.height = height

    def crop(self, _bbox):
        return self

    def find_tables(self, table_settings=None):
        return []

    def extract_words(self, **_kw):
        return list(self._words)


def _blocks(words, heading_sizes=None):
    analysis = _LayoutAnalysis()
    analysis.body_size = BODY
    analysis.heading_size_to_level = heading_sizes if heading_sizes is not None else {HEAD: 1}
    section = [None]
    blocks, _warn = _pass2_extract_page(_StubPage(words), 1, analysis, section)
    return blocks


def _kinds(blocks):
    return [(b.block_type, (b.text or "").strip(), b.section_path) for b in blocks]


def test_heading_sized_bare_number_is_not_a_heading():
    blocks = _blocks([
        _word("正文说明这一段是操作前提条件", 120, BODY),
        _word("18", 160, HEAD, x0=250.0),
        _word("操作员核对打印出来的标签", 200, BODY),
    ])
    headings = [t for k, t, _ in _kinds(blocks) if k == "heading"]
    assert "18" not in headings, "标题字号的纯数字标注号仍被判成 heading（PDF-D3 回归）"


def test_multi_token_bare_number_is_not_a_heading():
    blocks = _blocks([
        _word("正文说明这一段是操作前提条件", 120, BODY),
        _word("13  16", 160, HEAD, x0=250.0),
        _word("操作员核对打印出来的标签", 200, BODY),
    ])
    assert "13  16" not in [t for k, t, _ in _kinds(blocks) if k == "heading"]


def test_bold_body_sized_bare_number_is_not_a_heading():
    """`looks_callout` 同时 veto 字号与**加粗**两路 —— 加粗+正文字号的数字同样不该成标题。"""
    blocks = _blocks([
        _word("正文说明这一段是操作前提条件", 120, BODY),
        _word("42", 160, BODY, fontname="ABCDEF+SimHei-Bold", x0=250.0),
        _word("操作员核对打印出来的标签", 200, BODY),
    ])
    assert "42" not in [t for k, t, _ in _kinds(blocks) if k == "heading"]


def test_dotted_section_heading_is_preserved():
    """反向钉子：合法点分节标题必须**仍是** heading（它驱动切块边界）。"""
    blocks = _blocks([
        _word("正文说明这一段是操作前提条件", 120, BODY),
        _word("4.1 检查模具", 160, HEAD),
        _word("操作员核对打印出来的标签", 200, BODY),
    ])
    assert "4.1 检查模具" in [t for k, t, _ in _kinds(blocks) if k == "heading"]


def test_section_path_not_polluted_by_bare_number():
    """核心危害之二：数字不得写进 current_section，污染后续正文的章节。"""
    blocks = _blocks([
        _word("4.1 检查模具", 120, HEAD),
        _word("18", 160, HEAD, x0=250.0),
        _word("操作员核对打印出来的标签", 200, BODY),
    ])
    tail = [sp for k, t, sp in _kinds(blocks) if k == "paragraph" and "核对" in t]
    assert tail, "正文段落缺失"
    assert tail[0] == "4.1 检查模具", f"章节被数字污染: {tail[0]!r}"


def test_bare_number_stays_in_text_as_paragraph():
    """降级不是删除 —— 数字仍以段落形式留在正文里，零字符丢失。"""
    blocks = _blocks([
        _word("正文说明这一段是操作前提条件", 120, BODY),
        _word("18", 160, HEAD, x0=250.0),
    ])
    assert any("18" in (b.text or "") for b in blocks), "数字被丢弃（应只降级不删除）"
