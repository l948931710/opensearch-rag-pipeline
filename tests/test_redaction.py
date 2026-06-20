# -*- coding: utf-8 -*-
"""Unit tests for redaction — text + image sanitization, re-scan gate, audit hygiene.

All PII here is SYNTHETIC (pattern-valid fake IDs / names), never real data.
"""
import io
import json

from opensearch_pipeline import redaction as R

# synthetic, pattern-valid fakes
ID1 = "110101199003078515"
ID2 = "330821199511260033"
MOB = "13800138000"
EMAIL = "zhangsan@example.com"
BANK = "6222021234567890123"  # 19 digits, not date-structured → bank_card, not cn_id_card


def _png(w=300, h=200):
    from PIL import Image
    im = Image.new("RGB", (w, h), (200, 50, 50))
    buf = io.BytesIO(); im.save(buf, format="PNG"); return buf.getvalue()


# ── text ────────────────────────────────────────────────────────
def test_redact_text_irreversible_placeholders():
    text = f"员工 {ID1}，手机 {MOB}，邮箱 {EMAIL}，卡号 {BANK}。"
    out, counts = R.redact_text(text)
    assert ID1 not in out and MOB not in out and EMAIL not in out and BANK not in out
    assert R.PLACEHOLDERS["cn_id_card"] in out
    assert R.PLACEHOLDERS["cn_mobile"] in out
    assert R.PLACEHOLDERS["email"] in out
    assert R.PLACEHOLDERS["bank_card"] in out
    # NO partial reveal (前六后四) — first 6 / last 4 of the id must not survive
    assert ID1[:6] not in out and ID1[-4:] not in out
    assert counts.get("cn_id_card") == 1 and counts.get("bank_card") == 1


def test_name_id_cooccurrence_redacted():
    text = f"张三 {ID1} 男\n姓名：李四"
    out, counts = R.redact_text(text)
    assert "张三" not in out and "李四" not in out
    assert out.count(R.PLACEHOLDERS["name"]) >= 2


def test_id_redacted_before_bankcard_no_double_count():
    # an 18-digit ID must be taken by cn_id_card, never re-matched as a 16-19 bank card
    out, counts = R.redact_text(f"id {ID1}")
    assert counts.get("cn_id_card") == 1 and "bank_card" not in counts


def test_long_caseid_and_uscc_not_personal_pii():
    # 21-digit 办件单号 and 18-char-with-letter USCC are not personal PII patterns
    case_id = "120000000000000000009"   # synthetic 21-digit case-number shape
    out, counts = R.redact_text(f"办件单号{case_id}")
    assert "cn_id_card" not in counts and "bank_card" not in counts


def test_rescan_and_high_residual():
    text = f"员工 {ID1} 手机 {MOB}"
    assert "cn_id_card" in R.high_residual(R.rescan(text))   # raw → high present
    out, _ = R.redact_text(text)
    assert R.high_residual(R.rescan(out)) == {}              # sanitized → no high


# ── image ───────────────────────────────────────────────────────
def test_redact_image_kept_when_no_high_pii():
    r = R.redact_image(_png(), "这是一张普通界面截图，没有身份证", ocr_fn=lambda b: "")
    assert r["action"] == "KEPT" and r["sanitized_bytes"] is None


def test_redact_image_full_solid_when_high_pii():
    # ocr_text carries an ID → must solid-redact; fake re-OCR of the black image is clean
    r = R.redact_image(_png(), f"花名册 {ID1}", ocr_fn=lambda b: "")
    assert r["action"] == "REDACTED" and r["mode"] == "full_solid"
    assert r["sanitized_bytes"] is not None and r["residual_high"] == {}
    # solid fill actually changed the bytes
    assert r["sanitized_hash"] != r["src_hash"]


def test_redact_image_failed_when_residual_remains():
    # pathological: re-OCR still returns the ID (as if fill failed) → FAILED
    r = R.redact_image(_png(), f"花名册 {ID1}", ocr_fn=lambda b: f"still {ID1}")
    assert r["action"] == "FAILED" and "cn_id_card" in r["residual_high"]


# ── document orchestration ──────────────────────────────────────
def test_sanitize_document_clean_path():
    text = f"离职退保流程。花名册：张三 {ID1} 男；王五 {ID2} 女。"
    images = [{"image_bytes": _png(), "ocr_text": f"花名册 {ID1}", "ref": "p1_img0"}]
    res = R.sanitize_document(text=text, images=images, ocr_fn=lambda b: "")
    assert res["state"] == "REDACTED_CLEAN"
    assert "REDACTION_RESCAN_PASSED" in res["states"]
    assert ID1 not in res["sanitized_text"] and ID2 not in res["sanitized_text"]
    assert res["image_results"][0]["action"] == "REDACTED"
    assert res["review_required"] is True


def test_sanitize_document_failed_quarantine_path():
    text = f"花名册 {ID1}"
    images = [{"image_bytes": _png(), "ocr_text": f"{ID1}", "ref": "p1"}]
    # image re-OCR keeps leaking the ID → cannot certify clean
    res = R.sanitize_document(text=text, images=images, ocr_fn=lambda b: ID1)
    assert res["state"] == "REDACTION_FAILED_QUARANTINED"
    assert "REDACTION_RESCAN_PASSED" not in res["states"]


def test_audit_has_no_raw_pii():
    text = f"张三 {ID1} 手机 {MOB} 邮箱 {EMAIL}"
    images = [{"image_bytes": _png(), "ocr_text": f"{ID1}", "ref": "p1"}]
    res = R.sanitize_document(text=text, images=images, ocr_fn=lambda b: "")
    blob = json.dumps(res["audit"], ensure_ascii=False)
    for raw in (ID1, MOB, EMAIL, "张三"):
        assert raw not in blob, f"raw PII leaked into audit: {raw}"
    # audit carries only hashes + counts + status
    assert res["audit"]["redaction_status"] == "REDACTED_CLEAN"
    assert res["audit"]["review_required"] is True
    assert "sanitized_text_hash" in res["audit"] and "text_finding_counts" in res["audit"]


def test_sanitize_does_not_mutate_inputs():
    text = f"张三 {ID1}"
    images = [{"image_bytes": _png(), "ocr_text": f"{ID1}", "ref": "p1"}]
    orig_text = text
    R.sanitize_document(text=text, images=images, ocr_fn=lambda b: "")
    assert text == orig_text                       # caller's string untouched
    assert images[0]["ocr_text"] == f"{ID1}"       # caller's image dict untouched


# ── v2: gaps the judge panel found in v1 (synthetic data only) ──
def test_v2_partial_masked_id_redacted():
    # 4-digit + asterisks + 4-digit — v1 missed this (needs 18 consecutive digits)
    out, counts = R.redact_text("证件号码 1234****5678 已登记")
    assert "1234" not in out and "5678" not in out
    assert R.PLACEHOLDERS["masked_id"] in out and counts.get("masked_id") == 1
    assert "masked_id" in R.high_residual(R.rescan("证件 1234****5678"))


def test_v2_address_redacted():
    out, counts = R.redact_text("常住地址：测试市虚拟大道东段99号 附近")
    assert "测试市虚拟大道东段99号" not in out
    assert R.PLACEHOLDERS["address"] in out and counts.get("address") == 1
    assert "address" in R.high_residual(R.rescan("住址 测试市虚拟大道东段99号"))


def test_v2_uscc_no_trailing_char_leak():
    # 18-char USCC ending in a letter must be redacted whole — no stray trailing char
    out, counts = R.redact_text("统一社会信用代码 91330000123456789X 单位")
    assert "91330000123456789X" not in out
    assert (R.PLACEHOLDERS["uscc"] + "X") not in out  # no leaked check char (v1 bug)
    assert "X 单位" not in out.replace(R.PLACEHOLDERS["uscc"], "")


def test_v2_bank_card_pure_digits_still_caught():
    out, counts = R.redact_text(f"卡号 {BANK} 尾号")
    assert BANK not in out and counts.get("bank_card") == 1


def test_v2_name_roster_row_before_gender():
    out, _ = R.redact_text("赵六 男 1980/1/1 在职")
    assert "赵六" not in out and out.startswith(R.PLACEHOLDERS["name"])


def test_v2_over_redaction_guard_stoplist():
    # common words next to 男/女 must NOT be masked (avoid failing over_redaction)
    out, counts = R.redact_text("本人 男，员工 女，性别 男")
    assert R.PLACEHOLDERS["name"] not in out and "name" not in counts


def test_v2_name_llm_sweep():
    # the LLM sweep catches names regex misses (no gender/label/ID anchor)
    fake_llm = lambda prompt: '["孙八"]'
    out, counts = R.redact_text("审批人 孙八 负责复核流程", name_llm_fn=fake_llm)
    assert "孙八" not in out and counts.get("name", 0) >= 1


def test_v2_name_llm_sweep_ignores_non_names():
    fake_llm = lambda prompt: '["质量部", "综合管理中心"]'  # org/dept terms, not names
    out, counts = R.redact_text("质量部与综合管理中心协同", name_llm_fn=fake_llm)
    assert "质量部" in out and "name" not in counts  # org-suffix guard preserves them


def test_v2_llm_sweep_boundary_aware_no_substring_nuke():
    # regression: an LLM-returned token that is a substring of a business phrase
    # must NOT be raw-replaced inside it (v2 bug: 参保人员减员申报 → 参保人[姓名已脱敏]报)
    fake_llm = lambda p: '["员减员申"]'
    out, counts = R.redact_text("点击参保人员减员申报按钮", name_llm_fn=fake_llm)
    assert "参保人员减员申报" in out and "name" not in counts


def test_v2_high_residual_covers_new_types():
    txt = "证件 1234****5678 住址 测试市虚拟大道东段99号 赵六 男"
    hi = R.high_residual(R.rescan(txt))
    assert "masked_id" in hi and "address" in hi and "name" in hi


# ── excision (dense-roster path) ────────────────────────────────
def test_excision_drops_roster_keeps_procedure():
    blocks = [
        {"text": "第一步：员工离职次月办理退保，避免多缴。"},          # clean procedure → keep
        {"ocr_text": f"花名册 赵六 男 {ID1} 钱七 女 {ID2} 测试市虚拟大道东段99号"},  # dense PII → excise
        {"text": "第二步：每月接收保险明细表并核对。"},               # clean → keep
    ]
    images = [
        {"ocr_text": "系统操作界面：点击提交按钮", "ref": "proc.png"},   # clean → keep
        {"ocr_text": f"参保人员 {ID1}", "ref": "roster.png"},            # PII → excise
    ]
    r = R.sanitize_by_excision(blocks=blocks, images=images)
    assert r["state"] == "EXCISED_CLEAN"
    assert "第一步" in r["kept_text"] and "第二步" in r["kept_text"]
    assert ID1 not in r["kept_text"] and ID2 not in r["kept_text"] and "赵六" not in r["kept_text"]
    assert r["block_stats"]["excised"] == 1 and r["block_stats"]["kept"] == 2
    assert r["kept_images"] == ["proc.png"] and r["image_stats"]["excised"] == 1
    assert R.high_residual(R.rescan(r["kept_text"])) == {}


def test_excision_strips_bbox_coordinate_noise():
    blocks = [{"text": "第一步：办理退保。"},
              {"ocr_text": "39,22,23,146,90 12,34,56,78,90 暂停参保信息"}]
    r = R.sanitize_by_excision(blocks=blocks)
    assert "39,22,23,146,90" not in r["kept_text"] and "12,34,56,78,90" not in r["kept_text"]
    assert "第一步" in r["kept_text"] and "暂停参保信息" in r["kept_text"]


def test_excision_field_redacts_scattered_single_finding():
    # a procedure block with ONE stray id is kept but field-redacted (not dropped)
    blocks = [{"text": f"示例：联系电话 {MOB} 见说明。"}]
    r = R.sanitize_by_excision(blocks=blocks)
    assert r["block_stats"]["field_redacted"] == 1 and r["block_stats"]["excised"] == 0
    assert MOB not in r["kept_text"] and "说明" in r["kept_text"]
