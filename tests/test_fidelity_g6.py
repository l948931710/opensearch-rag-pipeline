# -*- coding: utf-8 -*-
"""tests/test_fidelity_g6.py — G6 抽取保真金集骨架的回归测试。

覆盖：指标方向性（三指标合读定位失败类型的契约）、比对归一口径、
GT 加载 fail-fast、sha256 锚定 SKIP、stub→run 端到端（合成 PDF）、基线门语义。
"""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

from eval_harness.fidelity.metrics import (  # noqa: E402
    cer, char_f1, norm_for_compare, parse_md_table, table_scores)


# ═══════════════ 指标方向性（AUTHORING_GUIDE 的"合读"表就是这些契约） ═══════════════

def test_identical_text_perfect_scores():
    ref = "一、目的：规范注塑车间刀叉产品的外观检验流程。"
    assert cer(ref, ref) == 0.0
    assert char_f1(ref, ref) == 1.0


def test_scrambled_order_high_cer_high_f1():
    """多栏交错型失败：内容都在（f1 高）但语序乱（cer 高）。"""
    ref = "第一段完整的检验说明文字。第二段关于包装的要求。"
    hyp = "第二段关于包装的要求。第一段完整的检验说明文字。"
    assert char_f1(ref, hyp) == 1.0
    assert cer(ref, hyp) > 0.3


def test_lost_content_low_f1():
    """截断型失败：后半丢了 → f1 掉。"""
    ref = "前半部分内容。" * 5 + "后半部分被截断丢失的内容。" * 5
    hyp = "前半部分内容。" * 5
    assert char_f1(ref, hyp) < 0.75
    assert cer(ref, hyp) > 0.4


def test_norm_insensitive_to_width_and_whitespace():
    assert norm_for_compare("ＦＣＡ００７３  等待 ５ 秒") == norm_for_compare("FCA0073等待5秒")
    assert cer("产品编号 FCA0073", "产品编号ＦＣＡ００７３") == 0.0
    # 中文标点保持敏感（指南承诺）
    assert cer("目的：规范流程。", "目的,规范流程.") > 0.0


def test_cer_empty_ref_semantics():
    assert cer("", "") == 0.0
    assert cer("", "噪声") == 1.0


# ═══════════════ 表格指标 ═══════════════

def test_parse_md_table_keeps_empty_cells_skips_separator():
    grid = parse_md_table("| 名称 | 规格 | 数量 |\n| --- | --- | --- |\n| 刀叉 |  | 200 |")
    assert grid == [["名称", "规格", "数量"], ["刀叉", "", "200"]]


def test_table_exact_match():
    ref = [["名称", "规格"], ["刀叉", "A-100"]]
    s = table_scores(ref, "| 名称 | 规格 |\n| 刀叉 | A-100 |")
    assert s["cell_f1"] == 1.0 and s["grid_exact"] is True


def test_table_column_shift_detected_by_grid_not_f1():
    """G1 类失败：空格丢失使列左移——内容 f1 仍高，grid_exact 抓住结构错。"""
    ref = [["名称", "规格", "数量"], ["刀叉", "", "200"]]
    shifted = "| 名称 | 规格 | 数量 |\n| 刀叉 | 200 |"      # 空格被吞、200 顶到规格列
    s = table_scores(ref, shifted)
    assert s["cell_f1"] == 1.0
    assert s["grid_exact"] is False


def test_table_missing_rows_lowers_f1():
    ref = [["A", "B"], ["1", "2"], ["3", "4"]]
    s = table_scores(ref, "| A | B |")
    assert s["cell_f1"] < 0.6 and s["grid_exact"] is False


# ═══════════════ GT 加载与锚定 ═══════════════

def _minimal_gt(tmp_path, sha, local_path, degraded=False):
    gt = {"_meta": {"schema": "fidelity_v1"},
          "doc1": {"_doc_meta": {"local_path": str(local_path), "doc_sha256": sha,
                                 "file_ext": "pdf", "degraded": degraded},
                   "pages": [{"page": 1, "ref_text": "正文"}], "tables": []}}
    p = tmp_path / "gt.json"
    p.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_gt_loader_failfast_on_missing_sha(tmp_path):
    from eval_harness.fidelity.gt_loader import load_gt
    bad = {"_meta": {"schema": "fidelity_v1"},
           "d": {"_doc_meta": {"local_path": "x.pdf", "file_ext": "pdf"},
                 "pages": [{"page": 1, "ref_text": "t"}]}}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="doc_sha256"):
        load_gt(str(p))


def test_gt_sha_mismatch_becomes_skip_not_score(tmp_path):
    """哈希不符 = 环境问题 → SKIP 该 doc，绝不拿错配 GT 打分。"""
    import fitz
    from eval_harness.fidelity.run_fidelity import run
    pdf = tmp_path / "doc.pdf"
    d = fitz.open(); d.new_page().insert_text((72, 72), "body text here")
    d.save(str(pdf)); d.close()
    gt_path = _minimal_gt(tmp_path, "0" * 64, pdf)          # 错误哈希
    report = run(gt_path, real=False)
    assert report["docs"][0]["status"] == "SKIP"
    assert "mismatch" in report["docs"][0]["reason"]
    assert report["hard_skipped"] == ["doc1"]                # hard 缺测被显式记账


# ═══════════════ stub → run 端到端（合成 PDF；抽取 vs 自身底稿 = 满分） ═══════════════

def _synth_pdf(tmp_path):
    import fitz
    pdf = tmp_path / "sop.pdf"
    d = fitz.open()
    pg = d.new_page()
    pg.insert_text((72, 100), "Section 1 purpose of the inspection process")
    pg.insert_text((72, 130), "applies to all FCA0073 models in workshop two")
    d.save(str(pdf)); d.close()
    return pdf


def test_stub_roundtrip_scores_perfect_then_detects_injected_loss(tmp_path):
    from eval_harness.fidelity.run_fidelity import make_stub, run
    pdf = _synth_pdf(tmp_path)
    stub = make_stub(str(pdf), real=False)
    label = next(k for k in stub if not k.startswith("_"))
    assert stub[label]["_doc_meta"]["degraded"] is True      # 校对前必须 degraded
    assert stub[label]["_doc_meta"]["doc_sha256"] == hashlib.sha256(
        pdf.read_bytes()).hexdigest()

    # 模拟"人工校对完成"：degraded → false，直接用底稿当 GT（抽取 vs 自身 = 满分）
    stub[label]["_doc_meta"]["degraded"] = False
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
    report = run(str(gt_path), real=False)
    doc = report["docs"][0]
    assert doc["status"] == "SCORED" and doc["char_f1"] == 1.0 and doc["cer"] == 0.0

    # 人工校对补上抽取器漏掉的一句 → 分数必须掉（金集有牙齿）
    stub[label]["pages"][0]["ref_text"] += "\nmissing sentence the extractor dropped entirely"
    gt_path.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
    report2 = run(str(gt_path), real=False)
    assert report2["docs"][0]["char_f1"] < 1.0
    assert report2["docs"][0]["cer"] > 0.0


def test_requires_real_ocr_skipped_in_simulate(tmp_path):
    from eval_harness.fidelity.run_fidelity import run
    pdf = _synth_pdf(tmp_path)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    gt = {"_meta": {"schema": "fidelity_v1"},
          "scan1": {"_doc_meta": {"local_path": str(pdf), "doc_sha256": sha,
                                  "file_ext": "pdf", "requires_real_ocr": True},
                    "pages": [{"page": 1, "ref_text": "x"}]}}
    p = tmp_path / "gt.json"
    p.write_text(json.dumps(gt, ensure_ascii=False), encoding="utf-8")
    report = run(str(p), real=False)
    assert report["docs"][0]["status"] == "SKIP"
    assert "requires_real_ocr" in report["docs"][0]["reason"]


# ═══════════════ 基线门语义 ═══════════════

def _report(labels, **corpus):
    base = {"char_f1": 0.95, "cer": 0.05, "table_cell_f1": 0.9, "grid_exact_rate": 0.8}
    base.update(corpus)
    return {"hard_doc_labels": sorted(labels), "hard_skipped": [], "corpus": base}


def test_baseline_pass_regression_incomparable():
    from eval_harness.fidelity.run_fidelity import compare_to_baseline
    baseline = _report(["a", "b"])
    assert compare_to_baseline(_report(["a", "b"]), baseline, 0.02)["verdict"] == "PASS"
    reg = compare_to_baseline(_report(["a", "b"], char_f1=0.90), baseline, 0.02)
    assert reg["verdict"] == "REGRESSION" and any("char_f1" in x for x in reg["fails"])
    cer_reg = compare_to_baseline(_report(["a", "b"], cer=0.10), baseline, 0.02)
    assert cer_reg["verdict"] == "REGRESSION"                # cer 方向相反也抓
    assert compare_to_baseline(_report(["a"]), baseline, 0.02)["verdict"] == "INCOMPARABLE"


def test_baseline_hard_skip_never_passes():
    from eval_harness.fidelity.run_fidelity import compare_to_baseline
    baseline = _report(["a", "b"])
    cur = _report(["a", "b"])
    cur["hard_skipped"] = ["b"]
    out = compare_to_baseline(cur, baseline, 0.02)
    assert out["verdict"] == "REGRESSION" and any("缺测" in x for x in out["fails"])


def test_committed_template_is_valid_schema(tmp_path):
    """仓库里的模板必须永远能过 loader 校验（防模板与 schema 漂移）。"""
    from eval_harness.fidelity.gt_loader import load_gt
    docs = load_gt(os.path.join("eval_harness", "fidelity", "gt_template.json"))
    assert docs[0].degraded is True                          # 模板必须示范 degraded 姿态
