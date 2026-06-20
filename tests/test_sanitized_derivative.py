# -*- coding: utf-8 -*-
"""Tests for the controlled sanitized-derivative capability (default no-auto-publish)."""
import io

from opensearch_pipeline import sanitized_derivative as SD

ID = "110101199003078515"  # synthetic, pattern-valid fake


def _png(c=(120, 40, 60)):
    from PIL import Image
    im = Image.new("RGB", (240, 160), c)
    buf = io.BytesIO(); im.save(buf, format="PNG"); return buf.getvalue()


PROC = ("1、第一步：办理退保。\n2、第二步：核对明细。\n"
        "3、第三步：登录政务网，社保暂停。\n4、第四步：医保减员申报。\n5、第五步：双系统完成。")


def test_default_is_no_auto_publish():
    assert SD.DEFAULT_AUTO_PUBLISH is False


def test_build_sanitized_source_docx():
    imgs = [{"image_bytes": _png(), "bind_step": "3"},
            {"image_bytes": _png((10, 200, 30)), "bind_step": "4"}]
    r = SD.build_sanitized_source_docx("《退保手续》（脱敏版）", PROC, imgs)
    assert r["image_count"] == 2 and len(r["docx_bytes"]) > 0
    assert len(r["pkg_sha256"]) == 64 and len(r["procedure_text_sha256"]) == 64
    assert all("source_sha256" in m and "embedded_sha256" in m for m in r["images"])
    # re-open: heading + 5 step paras + 2 images
    import docx
    d = docx.Document(io.BytesIO(r["docx_bytes"]))
    assert sum(1 for x in d.part.rels.values() if "image" in x.reltype) == 2
    assert any("第三步" in p.text for p in d.paragraphs)


def test_validate_clean_package_ok():
    r = SD.build_sanitized_source_docx("《退保手续》（脱敏版）", PROC,
                                       [{"image_bytes": _png(), "bind_step": "3"}])
    v = SD.validate_package_no_residue(r["docx_bytes"])
    assert v["ok"] and v["image_count"] == 1 and not v["case_numbers_present"]


def test_validate_catches_forbidden_doc_id():
    fake_id = "DOC_HR_00000000000000_TESTID"   # synthetic; never a real prod doc_id
    txt = PROC + f"\n参考原始 {fake_id}。"
    r = SD.build_sanitized_source_docx("t", txt, [])
    v = SD.validate_package_no_residue(r["docx_bytes"], forbidden_doc_ids=(fake_id,))
    assert not v["ok"] and v["violations"]["forbidden_doc_id"] == [fake_id]


def test_validate_catches_high_pii_and_placeholder_and_assetpath():
    for inject, key in [(f"员工 {ID}", "high_pii"),
                        ("某字段 [身份证号已脱敏]", "redaction_placeholder"),
                        ("见 processing/assets/hr/X/v1/y.jpg", "asset_path")]:
        r = SD.build_sanitized_source_docx("t", PROC + "\n" + inject, [])
        v = SD.validate_package_no_residue(r["docx_bytes"])
        assert not v["ok"], f"{key} should fail"
        assert v["violations"][key]


def test_case_number_is_info_only_not_violation():
    # synthetic 21-digit case-number shape (not a real 办件单号)
    r = SD.build_sanitized_source_docx("t", PROC + "\n办件单号120000000000000000009", [])
    v = SD.validate_package_no_residue(r["docx_bytes"])
    assert v["ok"] and v["case_numbers_present"]   # institutional case number, not a blocker


def test_publish_authorized_default_deny_and_full_pass():
    ok, missing = SD.publish_authorized({})
    assert not ok and set(missing) == set(SD.PUBLISH_REQUIREMENTS)
    gates = {k: True for k in SD.PUBLISH_REQUIREMENTS}
    assert SD.publish_authorized(gates) == (True, [])
    gates2 = dict(gates); gates2["human_spotcheck"] = False
    ok2, missing2 = SD.publish_authorized(gates2)
    assert not ok2 and missing2 == ["human_spotcheck"]
