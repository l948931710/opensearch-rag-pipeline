# -*- coding: utf-8 -*-
"""Generate the committed binary extraction fixtures (run once; the binaries are committed to git).

These are small, deterministic, NON-sensitive files used to regression-test the real extractors in CI
without depending on out-of-repo prod data or synthesizing at test time. The docx carries merged table
cells (gridSpan + vMerge) as a permanent guard for the DC-3 dedup fix.

Regenerate:  python tests/fixtures/make_fixtures.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def make_docx():
    import docx
    d = docx.Document()
    d.add_heading("检验规范", level=1)
    d.add_paragraph("正文段落示例。")
    t = d.add_table(rows=2, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "合并表头"   # horizontal gridSpan (merged across cols 0-1)
    t.cell(0, 2).text = "列C"
    t.cell(1, 0).text = "甲"
    t.cell(1, 1).text = "乙"
    t.cell(1, 2).text = "丙"
    t2 = d.add_table(rows=2, cols=2)
    t2.cell(0, 0).merge(t2.cell(1, 0)).text = "纵向合并"  # vertical vMerge (merged across rows 0-1)
    t2.cell(0, 1).text = "P"
    t2.cell(1, 1).text = "Q"
    d.save(os.path.join(HERE, "merged_cells.docx"))


def make_xlsx():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "检验表"
    ws["A1"] = "项目"
    ws["B1"] = "值"
    ws["A2"] = "温度"
    ws["B2"] = "25"
    ws.merge_cells("A3:B3")
    ws["A3"] = "合并行说明"
    wb.save(os.path.join(HERE, "merged_cells.xlsx"))


def make_pdf():
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    # ASCII only — fitz's default font has no CJK glyphs; the PDF test only checks text extraction.
    page.insert_text((72, 72), "PDF fixture line one.", fontsize=12)
    page.insert_text((72, 96), "PDF fixture line two.", fontsize=12)
    doc.save(os.path.join(HERE, "sample.pdf"))
    doc.close()


if __name__ == "__main__":
    make_docx()
    make_xlsx()
    make_pdf()
    print("fixtures written to", HERE)
