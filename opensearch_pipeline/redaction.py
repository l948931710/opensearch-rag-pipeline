# -*- coding: utf-8 -*-
"""Auto-redaction — generate a sanitized *derived* version of a PII-bearing doc.

Upgrades the DAG-2 PII gate from `PII → whole-doc QUARANTINE` to an optional
`PII → sanitized derivative → re-scan → publish-if-clean (else quarantine)` path,
without ever loosening safety. **Produces a NEW derivative; never mutates the
original.** A derivative is accepted only if a fresh re-scan finds **zero
high-severity PII** in both the sanitized text AND the re-OCR of every redacted
image.

v2 (2026-06-20) — closes the gaps a Claude judge panel + grep found in v1, where
the rescan keyed on full digit-runs only and missed:
  - partial-masked IDs (4digit+****+4digit) — leak first/last segments,
  - names anchored to ATTRIBUTES (DOB/gender/dept/address) not just a full-ID,
  - residential addresses (no pattern existed),
  - boundary leaks (bank/USCC placeholder + trailing check char).
So v2 adds: masked-id + address patterns; broadened name detection
(label / roster-row / id-placeholder adjacency + an optional LLM name sweep for
rosters); a non-alnum-bounded bank_card + a USCC pattern; a broadened rescan gate
(structural + regex-name + optional LLM-name); and image redaction is triggered
by the same broadened residual (so name/address/partial-id images get masked too).

SCOPE / KNOWN LIMIT — field-level redaction is for documents with SCATTERED PII
(a handful of IDs/names). For a dense per-person ROSTER (a quasi-identifier table:
name+ID+DOB+age+gender+dept per row), prefer structural **EXCISION** (drop the whole
table + its images): field redaction leaves re-identifiable attribute tuples even
when names/IDs are masked, and OCR yields endless identifier variants (17-char IDs,
25-digit account numbers) that patterns chase forever. A 2026-06-20 three-round
Claude judge panel confirmed a quarantined ~50-person roster doc is NOT safely
field-redactable — it stays quarantined pending an excision-based derivative.

Invariants:
  1. Irreversible placeholders — NO partial reveal (no 前六后四).
  2. Text AND image redacted; images use solid pixel fill (never blur/mosaic),
     then re-OCR + re-scan. Originals never reach serving/embedding/VLM.
  3. Re-scan gate: accept (REDACTED_CLEAN) only when high-severity residual == 0.
  4. Audit stores hashes + counts only — never raw names/IDs/OCR/addresses.
  5. Human spot-check stays mandatory before publishing (review_required=True);
     this module never publishes.

States: PII_DETECTED → REDACTION_GENERATED → REDACTION_RESCAN_PASSED
        → REDACTED_CLEAN | REDACTION_FAILED_QUARANTINED
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from opensearch_pipeline.pipeline_nodes import ENTITY_PATTERNS

REDACTION_VERSION = "redact-v2"

# ── structural PII patterns ─────────────────────────────────────
# Full national ID (18 consecutive digits, date-structured) — from the canonical source.
_FULL_ID = ENTITY_PATTERNS["cn_id_card"]
# Partial-masked identifier: digits + 2+ mask glyphs + digits (masked ID / card / phone).
_PARTIAL_ID = r"(?<![\dA-Za-z])\d{3,4}[\*＊✱•·∗●]{2,}[\dXx]{3,4}(?![\dA-Za-z])"
_MOBILE = ENTITY_PATTERNS["cn_mobile"]
_EMAIL = ENTITY_PATTERNS["email"]
_ACCESS_KEY = ENTITY_PATTERNS["access_key"]
_SECRET = ENTITY_PATTERNS["secret_like"]
# Bank card: 16–19 digit run NOT embedded in a longer alnum token (so a USCC that
# ends in a check-letter is never half-matched, leaking the trailing char — v1 bug).
_BANK_CARD = r"(?<![\dA-Za-z])\d{16,19}(?![\dA-Za-z])"
# Unified Social Credit Code (company id): 18 upper-alnum, digit-led. Institutional,
# not personal PII, but redacted as a whole token so the trailing check char can't
# leak (the v1 bug where bank_card matched only the digit run and left a stray letter).
_USCC = r"(?<![\dA-Za-z])\d[0-9A-HJ-NP-Z]{17}(?![\dA-Za-z])"
# Residential/mailing address: city/district + a road component + a number + 号.
_ADDRESS = (r"[一-龥]{2,8}(?:市|区|县|自治州|地区)"
            r"[一-龥0-9（）()]{0,22}?"
            r"(?:路|街|大道|大街|巷|弄|村|镇|乡|新村|小区|段)"
            r"[一-龥0-9（）()]{0,15}?\d{1,5}号")

# Order matters: most-specific first; USCC before bank_card; full-id before partial.
_REDACT_ORDER: List[Tuple[str, str]] = [
    ("cn_id_card", _FULL_ID),
    ("masked_id", _PARTIAL_ID),
    ("access_key", _ACCESS_KEY),
    ("secret_like", _SECRET),
    ("cn_mobile", _MOBILE),
    ("email", _EMAIL),
    ("bank_card", _BANK_CARD),
    ("uscc", _USCC),
    ("address", _ADDRESS),
]

PLACEHOLDERS = {
    "cn_id_card": "[身份证号已脱敏]",
    "masked_id": "[标识已脱敏]",
    "cn_mobile": "[手机号已脱敏]",
    "bank_card": "[银行卡号已脱敏]",
    "email": "[邮箱已脱敏]",
    "access_key": "[密钥已脱敏]",
    "secret_like": "[密钥已脱敏]",
    "uscc": "[统一社会信用代码已脱敏]",
    "address": "[地址已脱敏]",
    "name": "[姓名已脱敏]",
}

# High-severity types gate publish (zero residual required). uscc/email/mobile are
# redacted but NOT publish-blocking (institutional / lower-severity).
HIGH_TYPES = {"cn_id_card", "masked_id", "access_key", "secret_like",
              "bank_card", "address", "name"}

# ── name detection (regex layer) ────────────────────────────────
_ID_PHS = "|".join(re.escape(PLACEHOLDERS[k]) for k in ("cn_id_card", "masked_id"))
# CJK personal name (2–4 Han) by: a label; immediately before gender(+attr); or
# adjacent to an ID placeholder.
# (?<!\[) so a label that is itself part of a placeholder (e.g. "[姓名已脱敏]")
# is never re-parsed as "姓名" + a residual name during rescan.
# Distinctive labels only. "参保人"/"申报人" were dropped — they are prefixes of
# business terms (参保人员 / 申报…) and grabbed the following chars as a "name"
# (regression: 参保人员减员申报 → 参保人[姓名已脱敏]报). Real names are still caught by
# the gender / ID-adjacency rules + the LLM sweep.
_NAME_LABELED = re.compile(r"(?<!\[)(姓\s*名|名\s*字|联系人|员工姓名|户主|被保险人|职工姓名)"
                           r"([:：\s]{0,3})([一-龥]{2,4})")
_NAME_BEFORE_GENDER = re.compile(r"(?<![一-龥])([一-龥]{2,4})"
                                 r"(?=\s*[男女](?:\s|,|，|\d|汉|[一-龥]{1,2}族|$))")
# name immediately BEFORE an ID placeholder (the common roster layout "张三 110...").
# Deliberately no AFTER-id rule: it grabbed whatever token followed (labels etc.) →
# the LLM sweep covers the rare "ID then name" case without that over-redaction.
_NAME_BEFORE_IDPH = re.compile(r"(?<![一-龥])([一-龥]{2,4})(\s*(?:" + _ID_PHS + "))")

_ORG_SUFFIX = ("部", "中心", "公司", "科", "室", "组", "处", "课", "局", "站", "厂",
               "院", "所", "队", "团", "会", "集团", "系统", "平台", "门户", "工厂")

# words that look like 2–4 Han + 男/女 etc. but are NOT names → don't mask (guard
# against over-redaction failing the judge's over_redaction gate).
_NAME_STOPLIST = {
    "本人", "员工", "职工", "男女", "性别", "联系", "对接", "负责", "经办", "申报",
    "参保", "减员", "增员", "人员", "信息", "部门", "中心", "公司", "单位", "审核",
    "退保", "工伤", "离职", "在职", "确认", "提交", "登记", "管理", "操作", "说明",
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _redact_names_regex(text: str) -> Tuple[str, int]:
    """Redact CJK person names via label / pre-gender / ID-adjacency rules."""
    n = 0
    PH = PLACEHOLDERS["name"]

    def _lab(m):
        nonlocal n
        if m.group(3) in _NAME_STOPLIST:
            return m.group(0)
        n += 1
        return m.group(1) + m.group(2) + PH
    text = _NAME_LABELED.sub(_lab, text)

    def _gen(m):
        nonlocal n
        if m.group(1) in _NAME_STOPLIST:
            return m.group(0)
        n += 1
        return PH
    text = _NAME_BEFORE_GENDER.sub(_gen, text)

    def _bid(m):
        nonlocal n
        if m.group(1) in _NAME_STOPLIST:
            return m.group(0)
        n += 1
        return PH + m.group(2)
    text = _NAME_BEFORE_IDPH.sub(_bid, text)
    return text, n


def _looks_like_name(s: str) -> bool:
    """A 2–4 Han token that isn't a stoplist word or an org/dept-suffixed term."""
    return (2 <= len(s) <= 4 and re.fullmatch(r"[一-龥]{2,4}", s) is not None
            and s not in _NAME_STOPLIST and not s.endswith(_ORG_SUFFIX))


def redact_names_llm(text: str, llm_fn: Callable[[str], str]) -> Tuple[str, int]:
    """Optional LLM sweep: ask the model for residual person-name spans, redact each.

    llm_fn(prompt)->str returns the model's raw reply (expected: a JSON array of
    name strings). Robust to junk: parses the first JSON array; ignores non-2–4-Han
    tokens and stoplist words. Returns (text, redaction_count). No-op on any error.
    """
    if not text or llm_fn is None:
        return text, 0
    prompt = ("从下面文本中找出所有【中国自然人姓名】（2-4个汉字的真实人名）。"
              "只返回一个 JSON 数组，元素是去重后的姓名字符串；不要公司名、地名、部门名、"
              "岗位、业务术语或占位符。若没有人名返回 []。\n\n文本：\n" + text[:8000])
    try:
        reply = llm_fn(prompt) or ""
        m = re.search(r"\[.*\]", reply, re.DOTALL)
        names = json.loads(m.group(0)) if m else []
    except Exception:
        return text, 0
    n = 0
    PH = PLACEHOLDERS["name"]
    for nm in sorted({str(x).strip() for x in names if x}, key=len, reverse=True):
        if not _looks_like_name(nm):
            continue
        # Boundary-aware: only redact STANDALONE occurrences (not a substring glued
        # inside a longer Han phrase). A raw str.replace let an LLM-returned garbage
        # token gut business terms (e.g. "参保人员减员申报" → "参保人[姓名已脱敏]报").
        new, c = re.subn(r"(?<![一-龥])" + re.escape(nm) + r"(?![一-龥])", PH, text)
        if c:
            n += c
            text = new
    return text, n


def redact_text(text: str, *, name_llm_fn: Optional[Callable[[str], str]] = None) -> Tuple[str, Dict[str, int]]:
    """Replace every PII span with an irreversible placeholder. Returns (text, counts)."""
    if not text:
        return text or "", {}
    counts: Dict[str, int] = {}
    out = text
    for name, pattern in _REDACT_ORDER:
        out, c = re.subn(pattern, PLACEHOLDERS[name], out)
        if c:
            counts[name] = counts.get(name, 0) + c
    out, nreg = _redact_names_regex(out)
    if nreg:
        counts["name"] = counts.get("name", 0) + nreg
    if name_llm_fn is not None:
        out, nllm = redact_names_llm(out, name_llm_fn)
        if nllm:
            counts["name"] = counts.get("name", 0) + nllm
    return out, counts


def rescan(text: str, *, name_llm_fn: Optional[Callable[[str], str]] = None) -> Dict[str, int]:
    """Detector over (sanitized) text → {entity_type: count} for ALL residual hits.

    Structural patterns + regex name rules; optionally an independent LLM name
    re-detect (counted as 'name') for high-assurance rosters.
    """
    found: Dict[str, int] = {}
    if not text:
        return found
    for name, pattern in _REDACT_ORDER:
        c = len(re.findall(pattern, text))
        if c:
            found[name] = c
    # residual names by regex (post-redaction these should be 0)
    _, nreg = _redact_names_regex(text)
    if nreg:
        found["name"] = found.get("name", 0) + nreg
    if name_llm_fn is not None:
        _, nllm = redact_names_llm(text, name_llm_fn)
        if nllm:
            found["name"] = found.get("name", 0) + nllm
    return found


def high_residual(found: Dict[str, int]) -> Dict[str, int]:
    """Subset of findings that are high-severity (these block publish)."""
    return {k: v for k, v in found.items() if k in HIGH_TYPES}


# ── image redaction ─────────────────────────────────────────────
def redact_image_solid(image_bytes: bytes,
                       boxes: Optional[List[Tuple[int, int, int, int]]] = None) -> bytes:
    """Solid (irreversible) pixel redaction → JPEG bytes. boxes=None → full fill."""
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(image_bytes))
    if im.mode != "RGB":
        im = im.convert("RGB")
    draw = ImageDraw.Draw(im)
    if not boxes:
        draw.rectangle([0, 0, im.size[0], im.size[1]], fill=(0, 0, 0))
    else:
        for (x0, y0, x1, y1) in boxes:
            draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def redact_image(image_bytes: bytes, ocr_text: str, *,
                 ocr_fn: Optional[Callable[[bytes], str]] = None,
                 name_llm_fn: Optional[Callable[[str], str]] = None,
                 boxes: Optional[List[Tuple[int, int, int, int]]] = None) -> Dict[str, Any]:
    """Redact an image iff its OCR carries high-severity PII (broadened detector),
    then re-OCR + re-scan. KEEP only when genuinely clean."""
    src_h = _sha(image_bytes)
    pre_high = high_residual(rescan(ocr_text or "", name_llm_fn=name_llm_fn))
    if not pre_high:
        return {"action": "KEPT", "mode": "none", "sanitized_bytes": None,
                "src_hash": src_h, "sanitized_hash": src_h, "residual_high": {}}
    sanitized = redact_image_solid(image_bytes, boxes=boxes)
    mode = "boxes_solid" if boxes else "full_solid"
    reocr = ocr_fn(sanitized) if ocr_fn else ""
    resid = high_residual(rescan(reocr or "", name_llm_fn=name_llm_fn))
    if resid and boxes:  # box redaction missed → escalate to full fill
        sanitized = redact_image_solid(image_bytes, boxes=None)
        mode = "full_solid_escalated"
        reocr = ocr_fn(sanitized) if ocr_fn else ""
        resid = high_residual(rescan(reocr or "", name_llm_fn=name_llm_fn))
    return {"action": "REDACTED" if not resid else "FAILED", "mode": mode,
            "sanitized_bytes": sanitized, "src_hash": src_h,
            "sanitized_hash": _sha(sanitized), "residual_high": resid}


# ── document orchestration ──────────────────────────────────────
def sanitize_document(*, text: str,
                      images: Optional[List[Dict[str, Any]]] = None,
                      ocr_fn: Optional[Callable[[bytes], str]] = None,
                      name_llm_fn: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """Produce a sanitized derivative + audit + final state. Never mutates inputs."""
    images = images or []
    states = ["PII_DETECTED"]

    sanitized_text, text_counts = redact_text(text, name_llm_fn=name_llm_fn)
    image_results = []
    for img in images:
        r = redact_image(img.get("image_bytes", b""), img.get("ocr_text", ""),
                         ocr_fn=ocr_fn, name_llm_fn=name_llm_fn, boxes=img.get("boxes"))
        r["ref"] = img.get("ref")
        image_results.append(r)
    states.append("REDACTION_GENERATED")

    text_high = high_residual(rescan(sanitized_text, name_llm_fn=name_llm_fn))
    img_high: Dict[str, int] = {}
    for r in image_results:
        for k, v in r.get("residual_high", {}).items():
            img_high[k] = img_high.get(k, 0) + v
    img_failed = [r for r in image_results if r["action"] == "FAILED"]

    total_high = dict(text_high)
    for k, v in img_high.items():
        total_high[k] = total_high.get(k, 0) + v

    if not total_high and not img_failed:
        states.append("REDACTION_RESCAN_PASSED")
        state = "REDACTED_CLEAN"
    else:
        state = "REDACTION_FAILED_QUARANTINED"

    redaction_count = sum(text_counts.values()) + sum(1 for r in image_results if r["action"] == "REDACTED")
    audit = {
        "redaction_version": REDACTION_VERSION,
        "pii_types": sorted(text_counts.keys()),
        "text_finding_counts": text_counts,            # counts only, never raw values
        "image_count": len(image_results),
        "images_redacted": sum(1 for r in image_results if r["action"] == "REDACTED"),
        "images_kept": sum(1 for r in image_results if r["action"] == "KEPT"),
        "images_failed": len(img_failed),
        "source_text_hash": _sha((text or "").encode("utf-8")),
        "sanitized_text_hash": _sha((sanitized_text or "").encode("utf-8")),
        "image_hashes": [{"ref": r.get("ref"), "src": r["src_hash"][:16],
                          "sanitized": r["sanitized_hash"][:16], "mode": r["mode"]}
                         for r in image_results],
        "redaction_count": redaction_count,
        "redaction_status": state,
        "rescan_high_residual": total_high,
        "review_required": True,
    }
    return {"state": state, "states": states, "sanitized_text": sanitized_text,
            "redaction_counts": text_counts, "image_results": image_results,
            "audit": audit, "review_required": True}


# OCR'd layout/bounding-box coordinate dumps: runs of >=4 comma-separated small ints
# (e.g. "39,22,23,146,90"). Not PII, but ingestion noise — strip from kept text.
_BBOX_NOISE = re.compile(r"(?:\d{1,4}\s*[,，]\s*){3,}\d{1,4}")


def _strip_layout_noise(text: str) -> str:
    out = _BBOX_NOISE.sub(" ", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out)


def sanitize_by_excision(*, blocks: List[Dict[str, Any]],
                         images: Optional[List[Dict[str, Any]]] = None,
                         name_llm_fn: Optional[Callable[[str], str]] = None,
                         drop_threshold: int = 2) -> Dict[str, Any]:
    """Excision-based sanitize for docs with a DENSE embedded PII roster.

    The robust answer when field-level redaction can't win (a quasi-identifier
    table where attribute tuples re-identify even with names/IDs masked, and OCR
    yields endless identifier variants): **drop whole content units that carry
    dense PII**, keep only clean ones.

      - a block with >= drop_threshold high-severity PII findings → EXCISED (dropped);
      - a block with 1 scattered finding → field-redacted (kept);
      - a clean block → kept as-is;
      - an image whose OCR carries ANY high-severity PII → DROPPED (not retained,
        not even solid-masked); a clean image → kept (referenced as-is).

    Returns a derivative built only from clean/clean-after-redaction units, plus a
    final re-scan. Never mutates inputs; audit carries counts/hashes only.
    """
    images = images or []
    kept_parts: List[str] = []
    bstat = {"kept": 0, "field_redacted": 0, "excised": 0}
    for blk in blocks:
        t = (blk.get("text") or blk.get("ocr_text") or "") if isinstance(blk, dict) else ""
        if not t.strip():
            continue
        total = sum(high_residual(rescan(t, name_llm_fn=name_llm_fn)).values())
        if total >= drop_threshold:                       # dense high-PII unit → excise
            bstat["excised"] += 1
            continue
        rt, _ = redact_text(t, name_llm_fn=name_llm_fn)    # else field-redact (incl medium: mobile/email)
        kept_parts.append(rt)
        bstat["field_redacted" if rt != t else "kept"] += 1
    kept_text = _strip_layout_noise("\n".join(kept_parts))

    kept_images: List[str] = []
    istat = {"kept": 0, "excised": 0}
    for img in images:
        if high_residual(rescan(img.get("ocr_text", ""), name_llm_fn=name_llm_fn)):
            istat["excised"] += 1
        else:
            kept_images.append(img.get("ref"))
            istat["kept"] += 1

    final_high = high_residual(rescan(kept_text, name_llm_fn=name_llm_fn))
    state = "EXCISED_CLEAN" if not final_high else "EXCISION_FAILED"
    audit = {
        "redaction_version": REDACTION_VERSION + "-excision",
        "block_stats": bstat,
        "image_stats": istat,
        "kept_image_refs": kept_images,
        "kept_text_hash": _sha(kept_text.encode("utf-8")),
        "kept_text_chars": len(kept_text),
        "final_high_residual": final_high,
        "redaction_status": state,
        "review_required": True,
    }
    return {"state": state, "kept_text": kept_text, "kept_images": kept_images,
            "block_stats": bstat, "image_stats": istat, "audit": audit,
            "review_required": True}
