# -*- coding: utf-8 -*-
"""image_binding_reconcile.py — post-binding reconciliation for the PDF image→step override.

Gates each override move (geometric source → content/range destination) by comparing OWNERSHIP
STRENGTH, instead of blindly trusting the override OR blindly rejecting any source-zeroing.

Ownership tiers (strongest → weakest), per the agreed rule:
  4 figure   : the image's printed circled number is covered by the step's exact/range figure ref
  3 local    : explicit local semantic ownership (distinctive shared phrase, ≥4 chars)
  2 spatial  : caption/spatial evidence (the geometric anchor has this by construction)
  1 range    : broad procedural range only (e.g. "①-⑥步操作", "图1-3"), image not specifically referenced
  0 none

Decision: APPLY the override move iff the destination owns the image MORE strongly than the source.
  • Reject when the source owns via figure/local and the destination only has a broad-range/none claim
    (the "range-theft" misfire, e.g. doc 22767C step6→step1).
  • Allow when the source has no meaningful ownership and the destination has stronger (local/figure)
    ownership (e.g. 5FFA22 list-view→step1, 328126 《产品标识卡》→step1).
  • Ambiguous / source≥dest: keep the geometric source (do NOT force a new destination) and flag review.
  • source-zeroing is a WARNING/backstop in the detail, NOT the sole rejection criterion.

Pure + deterministic; no I/O. Robust to malformed / OCR-fragment references (fall back to spatial).
"""
import re

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_C2N = {c: i + 1 for i, c in enumerate(CIRCLED)}
_SEP = "[-–—~～至到]"

OWNER_FIGURE, OWNER_LOCAL, OWNER_SPATIAL, OWNER_RANGE, OWNER_NONE = 4, 3, 2, 1, 0
_TIER_NAME = {4: "figure", 3: "local", 2: "spatial", 1: "broad_range", 0: "none"}


def _strip_doc_prefix(t):
    return re.sub(r"【[^】]*】", "", t or "")


def step_circled_set(text):
    """Parse a step's figure references, distinguishing FIGURE ownership from a BROAD PROCEDURAL
    range. Returns (figure_covered:set[int], procedural_broad:bool):
      figure_covered : numbers the step references as FIGURES — 图⑭, 图1-3, 图⑭-⑯ (figure range),
                       and inline enumeration ①设备②班次③ (which label image regions).
      procedural_broad: the step references a broad STEP range "①-⑥步操作" / "1-6步" — this is the
                       WEAKEST ownership tier and must NOT establish figure ownership (its expanded
                       numbers are excluded from figure_covered). This is the 22767C fix: an image's
                       incidental in-image labels (①② axis) must not be "claimed" by a step range.
    Malformed input → (set(), False)."""
    t = text or ""
    figure_covered, procedural_broad = set(), False
    try:
        # 1. PROCEDURAL step ranges "①-⑥步" / "1-6步" (optional 图 prefix) — mask out; NOT figure refs.
        proc_c = f"(?:图\\s*)?([{CIRCLED}])\\s*{_SEP}\\s*([{CIRCLED}])\\s*步"
        proc_a = rf"(?:图\s*)?(\d{{1,2}})\s*{_SEP}\s*(\d{{1,2}})\s*步"
        for pat, conv in ((proc_c, lambda g: (_C2N[g[0]], _C2N[g[1]])),
                          (proc_a, lambda g: (int(g[0]), int(g[1])))):
            for m in re.finditer(pat, t):
                lo, hi = conv(m.groups())
                if 0 < lo <= hi and (hi - lo) >= 2:
                    procedural_broad = True
        masked = re.sub(proc_c, "  ", t)
        masked = re.sub(proc_a, "  ", masked)
        # 2. FIGURE ranges (NOT step ranges): 图⑭-⑯ / 见图1-3, and bare circled ranges ①-③ (enumeration)
        for m in re.finditer(f"([{CIRCLED}])\\s*{_SEP}\\s*([{CIRCLED}])", masked):
            lo, hi = _C2N[m.group(1)], _C2N[m.group(2)]
            if lo <= hi <= 20:
                figure_covered |= set(range(lo, hi + 1))
        for m in re.finditer(rf"(?:如?图|见图)\s*(\d{{1,2}})\s*{_SEP}\s*(\d{{1,2}})", masked):
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi <= 99:
                figure_covered |= set(range(lo, hi + 1))
        masked2 = re.sub(f"[{CIRCLED}]\\s*{_SEP}\\s*[{CIRCLED}]", "  ", masked)
        masked2 = re.sub(rf"(?:如?图|见图)\s*\d{{1,2}}\s*{_SEP}\s*\d{{1,2}}", "  ", masked2)
        # 3. inline circled enumeration (①设备②班次), standalone 图① , arabic 图7
        figure_covered |= {_C2N[c] for c in masked2 if c in _C2N}
        for m in re.finditer(r"(?:如?图|见图)\s*(\d{1,2})", masked2):
            n = int(m.group(1))
            if 1 <= n <= 99:
                figure_covered.add(n)
    except Exception:
        return set(), False
    return figure_covered, procedural_broad


def image_circled_nums(ocr_text):
    """Circled numbers printed ON the image (from its OCR). NOTE: vlm_annotation_map keys are
    in-image region labels, NOT step-reference figures, so they are deliberately excluded."""
    return {_C2N[c] for c in (ocr_text or "") if c in _C2N}


def _longest_common_run(a, b):
    """Longest common contiguous substring length (Chinese chars) — a local-ownership proxy."""
    a, b = _strip_doc_prefix(a), _strip_doc_prefix(b)
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(len(a)):
        cur = [0] * (len(b) + 1)
        ai = a[i]
        for j in range(len(b)):
            if ai == b[j]:
                cur[j + 1] = prev[j] + 1
                if cur[j + 1] > best:
                    best = cur[j + 1]
        prev = cur
    return best


def ownership(step_text, image_circled, image_summary, is_geometric, local_min=4):
    """Return (score, tier_name, lcs_len). lcs_len carries local-match strength for tie-breaking
    (a single doc-common term can give an incidental ≥local_min match, so the raw length matters)."""
    covered, is_broad = step_circled_set(step_text)
    lcs = _longest_common_run(image_summary, step_text)
    if image_circled and (image_circled & covered):
        return OWNER_FIGURE, "figure", lcs
    if lcs >= local_min:
        return OWNER_LOCAL, "local", lcs
    if is_geometric:
        return OWNER_SPATIAL, "spatial", lcs
    if is_broad:
        return OWNER_RANGE, "broad_range", lcs
    return OWNER_NONE, "none", lcs


def reconcile_move(geo_text, dst_text, image_ocr, image_summary, source_zeroed=False,
                   local_margin=2):
    """Decide whether to APPLY an override move geo→dst by comparing ownership strength.

    Returns structured diagnostics: {apply, result, reason_code, decision, src_tier, dst_tier,
    src_score, dst_score, src_lcs, dst_lcs, image_circled, source_zeroed}.
      result      ∈ {accepted, blocked}
      reason_code ∈ {stronger_destination_owner, range_theft_blocked, source_owner_preserved,
                     ambiguous_kept_at_source}
      decision    ∈ {allow, reject_conflict, reject_review}

    Rule: the destination must own the image MORE strongly than the source.
      • figure > local > spatial > broad_range > none.
      • Within the local tier, the destination must EXCEED the source LCS by `local_margin`
        (so an incidental doc-common-term overlap cannot flip an image off a step that
        genuinely describes it — the 22767C "生产订单"-noise case).
      • source-zeroing is reported as a warning, never the sole rejection criterion."""
    circ = image_circled_nums(image_ocr)
    s_src, t_src, lcs_src = ownership(geo_text, circ, image_summary, is_geometric=True)
    s_dst, t_dst, lcs_dst = ownership(dst_text, circ, image_summary, is_geometric=False)
    # figure-strength (circled-overlap count) for tie-breaking within the figure tier
    src_cov, _ = step_circled_set(geo_text)
    dst_cov, _ = step_circled_set(dst_text)
    fig_src, fig_dst = len(circ & src_cov), len(circ & dst_cov)
    if t_src == "figure" and t_dst == "figure":
        apply = fig_dst > fig_src                       # destination shares MORE of the image's figures
    elif t_src == "local" and t_dst == "local":
        apply = lcs_dst >= lcs_src + local_margin       # require destination clearly stronger
    else:
        apply = s_dst > s_src
    # decision + structured reason code
    _, dst_broad = step_circled_set(dst_text)        # did the destination rely on a broad procedural range?
    if apply:
        decision, reason_code = "allow", "stronger_destination_owner"
    elif s_src >= OWNER_LOCAL and dst_broad:
        # source owns via figure/local; destination's claim rests on a broad procedural range →
        # range theft (even if the destination has an incidental doc-common local term).
        decision, reason_code = "reject_conflict", "range_theft_blocked"
    elif s_src >= OWNER_LOCAL:
        # source owns via figure/local; destination is weaker (none / non-broad) → keep the owner
        decision, reason_code = "reject_review", "source_owner_preserved"
    else:
        # neither side owns meaningfully → keep the geometric source, do not force a destination
        decision, reason_code = "reject_review", "ambiguous_kept_at_source"
    return {"apply": apply, "decision": decision, "reason_code": reason_code,
            "result": "accepted" if apply else "blocked",
            "src_tier": t_src, "dst_tier": t_dst, "src_score": s_src, "dst_score": s_dst,
            "src_lcs": lcs_src, "dst_lcs": lcs_dst, "image_circled": sorted(circ),
            "source_zeroed": bool(source_zeroed)}
