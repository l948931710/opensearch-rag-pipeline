"""Layer 6 — Chunk-artifact content quality (full-corpus, read-only).

L0-L5 evaluate the index + retrieval + answer behaviour. L6 evaluates the *chunks
themselves* — the ingestion product — across the whole active corpus. It is the
post-rebuild acceptance gate for chunk quality and (once locked) the permanent
chunk-content baseline every future rebuild is compared against.

Design (see docs/audits + the L6 plan):
  - One read-only pass over fuling_knowledge.chunk_meta (is_active=1) joined to
    document_meta. All metrics compute in Python over that one result set.
  - Pure metric functions (family_*) take plain dict chunks and return plain dicts —
    no I/O — so the math is unit-tested offline (envboot forces simulate OFF, so the
    harness can't be run in sim mode; tests are the offline validation path).

Metric families:
  A   structural integrity  — consume the seven-dim D1-D7 JSON (do NOT re-query)
  A2  generation->accept    — round-1 via the D4/D7 read-only proxy (orphans=0 ∧
                              missing-parent=0); durable ingest manifest = future direct
  B   boundary/segmentation — size dist per type, [5,2000] tail readback, mid-sentence
                              cut rate, orphaned-heading rate, stored-vs-recomputed token
  C   self-containedness    — dangling-anaphor heuristic (full) + judge bundle (sampled)
  D   type-routing          — metadata expected-family vs observed + D3 under-chunk (soft)
  E   redundancy/dedup      — exact normalized-hash + length-blocked near-dup (template
                              vs twin), doc-clustered CI
  F   image-binding         — corpus-wide per-chunk image-ref duplication (over-attach)
  G   retrievability        — sampled dense self-query (reuses L0 machinery; informational)
  H   RDS<->HA3 id-set       — all-type missing_in_ha3/extra_in_ha3/Jaccard (extra ⇒
                              incomplete purge)。铁律(2026-07-21 终局)：query 枚举有随
                              build/merge 漂移的确定性盲区，**存在性判定唯官方 fetch 为准**
                              ——query 判缺经 fetch 复核，只有 fetch 也无(missing_confirmed)
                              或无法定性(unclassified/fetch 失败)才影响门；纯 query_invisible
                              (盲区伪影，数据在场)不 NO_GO。extra 是 query 实返行，恒为缺陷。

Three-state gate verdict (no fail-open on missing data):
  GO                      — every hard-gate input measured AND passes
  NO_GO_DEFECT            — a hard gate measured and failed
  NO_GO_INCOMPLETE_EVIDENCE — a hard-gate input unmeasured (D1-D7 JSON absent, HA3 enum
                              truncated, ...); the layer runs fail-open but the gate never
                              returns GO on unmeasured evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Dict, List, Optional

from .. import envboot  # noqa: F401  side-effecting: forces public/live read-only env
from ..metrics import percentiles, score_distribution
from ..sampling import doc_clustered_bootstrap_ci

# Reuse the production token estimator + structural-header set so L6 measures exactly
# what the chunker / node_validate_chunks enforce at build time.
from opensearch_pipeline.chunker import _STEP_SECTION_HEADERS, _estimate_tokens

# ── constants ────────────────────────────────────────────────────────────
RUBRIC_VERSION = "chunk_rubric_v1"

# node_validate_chunks drop band (pipeline_nodes.py:3640-3643) — readback target.
TOKEN_MIN, TOKEN_MAX = 5, 2000

# Structural-type chunks that node_validate_chunks logs loudly when dropped.
STRUCTURAL_TYPES = {"procedure_parent", "step_card", "table_chunk", "visual_knowledge"}
# Prose types that should end on a sentence boundary (others are legitimately fragmentary).
PROSE_TYPES = {"text_chunk", "clause_chunk", "faq_chunk", "section_chunk"}

# L6 判定口径的语义版本（2026-07-27 新增）。与 L4 的 `l4_ingestion_evaluator_version`
# 同款用途：改**尺子**会移动 l6.* 指标，而 `code_commit` 不在 _REGIME_KEYS 里 —— 不进
# regime 的话，"改了判据"与"管线变好了"在差量网里完全无法区分。改口径必须手工 bump。
L6_EVALUATOR_VERSION = "2.0.0"   # 2.0.0: mid-sentence 判据加形态豁免（见 _is_prose_like）

# CJK + ASCII sentence terminators (CJK closing quotes/brackets count as terminal too).
_TERMINAL_CHARS = set("。！？!?．…⋯；;”’）)】」』》〉")

# ── mid-sentence 判据的形态豁免（2026-07-27）──────────────────────────────
# 旧判据 = "最后一个字符不是终止标点"，对**散文**成立，但 `text_chunk` 实际是"其它"类：
# 表格行、清单、图片 OCR 合成块、幻灯标题、目录点线大量落在里面，它们本来就不以句号结尾。
#
# 实测（220 条散文类 chunk，生产同款路由，`scratch/midsentence_diagnose_20260727.py`）：
# 命中 91 条中 —— 表格(制表符) 59、清单/表(多行短行) 17、标签 OCR 3、图片 OCR 合成块 4、
# 幻灯标题 3、目录点线 1、尾部孤立标题 2，**真·句中切断只有 2 条**。
# 即旧口径 0.4136 里有 **0.4045 是误判**，真值 0.0091（门槛 0.05，本就轻松通过）。
#
# ⚠️ 这些是**形态**判据，不是"把不及格的排除掉"：每一条都对应一种"合法地不以句号结尾"
# 的内容形态。灵敏度不降 —— 上述那 2 条真切断在新口径下仍然命中（有回归测试钉住）。
_TABULAR_MARKERS = "◆☆■□▲△●○※"
_HEADING_TAIL_RE = re.compile(
    r"(?:^|\n)[ \t]*("
    r"\d+[、.．][^\n]{0,20}"                   # "3、定期保养"
    r"|\d+\.\d+[^\n]{0,20}"                   # "4.3驾驶规定"
    r"|[一二三四五六七八九十]+[、.．][^\n]{0,20}"   # "三、职责"
    r"|\d+\s*[一-龥]{1,12}"                   # "3定义"（编号后无标点，实测存在）
    r"|[(（][一二三四五六七八九十\d]+[)）][^\n]{0,20}"   # "(五)火场休克的抢救"
    r")$")
# 抽取器给每篇加的文档头（`【文档:甘蔗渣产品培训】`）。它是**产出方生成**的标记，
# 比"文本看起来短"可靠得多，故用它锚定标题块豁免。
_DOC_TITLE_MARKER_RE = re.compile(r"^\s*【文档[:：][^】]*】\s*")


def _is_prose_like(text: str) -> bool:
    """这段文本是否**真的是散文**（值得用"句末标点"判断边界）。

    返回 False 的都是"合法地不以句号结尾"的形态，不纳入 mid-sentence 统计。
    """
    t = text.rstrip()
    if not t:
        return False
    if "\t" in t:                                    # 表格：制表符分隔
        return False
    # 空格对齐的表单/签署栏（"编制：〇〇〇    审核：    批准：    日期：…"）——
    # （示例里的人名以〇替代：本仓为 public repo，真实语料的签署人姓名不入库）
    # 与制表符版同型，只是用空格对齐，故制表符判据抓不到。判据=末行含 ≥3 个「字段：」。
    _last_line = (t.split("\n")[-1] if "\n" in t else t)
    if len(re.findall(r"[一-龥A-Za-z]{2,6}[：:]", _last_line)) >= 3:
        return False
    if t[-1] in _TABULAR_MARKERS:                    # 表格：符号标记结尾
        return False
    if re.search(r"„{3,}|\.{5,}|…{3,}", t):           # 目录页点线 leader
        return False
    if "[图片OCR]" in t or "[图片]" in t:              # 图片 OCR/占位合成块
        return False
    lines = [ln for ln in t.split("\n") if ln.strip()]
    if len(lines) >= 3 and sum(len(ln) < 26 for ln in lines) >= len(lines) * 0.6:
        return False                                  # 清单/表：多数为短行
    # 幻灯/文档标题块：整块 = 抽取器加的 `【文档:…】` 头 + 一行短标题。
    # ⚠️ 这一条**刻意锚在抽取器自己生成的标记上**，而不是"短即标题"。先前写成
    # `len(t) <= 40 and 不含句内标点` —— 既有测试 test_boundary_midsentence_doc_clustered
    # 当场证伪：`"这句话没有结束符号被切断"`（12 字、无顿号）会被一并豁免，那是把灵敏度
    # 也削掉了，不是收窄误判。实测语料里依赖此条的 3 条**全部**带 `【文档:…】` 头
    # （docs/evidence/midsentence_diagnose_20260727），故按该形态收紧，不留长度猜测。
    if (_DOC_TITLE_MARKER_RE.match(t) and len(lines) <= 2 and len(t) <= 60
            and not re.search(r"[、，,；;]", _DOC_TITLE_MARKER_RE.sub("", t))):
        return False
    return True


# Dangling-reference openers: a chunk starting with one of these, with no section_title to
# resolve the antecedent, is likely not self-contained.
_DANGLING_PREFIXES = (
    "它", "他", "她", "该", "此", "其", "这", "那",
    "上述", "前述", "如前", "如上", "见上", "见下", "见图", "如图", "综上",
    "因此", "所以", "于是", "据此", "为此", "由此",
)

# Expected observed chunk-type *family* per routing mode (Family D).
_MODE_EXPECTED_FAMILY = {
    "faq": {"faq_chunk"},
    "clause": {"clause_chunk"},
    "step": {"procedure_parent", "step_card"},
    "text": {"text_chunk", "table_chunk", "section_chunk"},
    "slide": {"text_chunk", "section_chunk"},
}


# ── small reused helpers ──────────────────────────────────────────────────

def _norm(t: Optional[str]) -> str:
    """Whitespace-stripped text (matches l0_index_health._norm for dup detection)."""
    return "".join((t or "").split())


def _shingles(text: str, k: int = 8) -> set:
    """Character k-gram set after whitespace strip.

    Byte-identical to scripts/corpus_cleanup.py::_shingles — inlined to avoid importing
    a CLI module with heavy top-level side effects.
    """
    s = re.sub(r"\s+", "", text or "")
    return {s[i:i + k] for i in range(len(s) - k + 1)}


def _ext_of(filename: Optional[str]) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


# ── Family B — boundary / segmentation ────────────────────────────────────

def family_boundary(chunks: List[Dict]) -> Dict:
    """Size distribution per type, [5,2000] tail readback, mid-sentence-cut rate,
    orphaned-heading rate, stored-vs-recomputed token drift. All deterministic."""
    by_type_tokens: Dict[str, List[int]] = defaultdict(list)
    oversize: List[Dict] = []
    undersize: List[Dict] = []
    token_drift: List[Dict] = []
    midsentence_cases: List[Dict] = []   # for doc-clustered CI (prose types only)
    trailing_heading_cases: List[Dict] = []   # 尾部孤立标题（2026-07-27 单列）
    orphan_heading_cases: List[Dict] = []
    n_prose = 0

    for c in chunks:
        text = c.get("chunk_text") or ""
        ctype = c.get("chunk_type") or "?"
        recomputed = _estimate_tokens(text)
        by_type_tokens[ctype].append(recomputed)

        # [5,2000] readback — any survivor outside the band leaked past the build gate.
        if recomputed > TOKEN_MAX:
            oversize.append({"chunk_id": c.get("chunk_id"), "doc_id": c.get("doc_id"),
                             "chunk_type": ctype, "tokens": recomputed,
                             "structural": ctype in STRUCTURAL_TYPES})
        elif recomputed < TOKEN_MIN:
            undersize.append({"chunk_id": c.get("chunk_id"), "doc_id": c.get("doc_id"),
                              "chunk_type": ctype, "tokens": recomputed})

        # stored token_count vs recomputed (integrity of the persisted field)
        stored = c.get("token_count")
        if stored is not None and abs(int(stored) - recomputed) > max(5, int(recomputed * 0.2)):
            token_drift.append({"chunk_id": c.get("chunk_id"), "stored": int(stored),
                                "recomputed": recomputed})

        # mid-sentence cut — prose types only，且必须**形态上真是散文**（见 _is_prose_like）
        if ctype in PROSE_TYPES:
            n_prose += 1
            stripped = text.rstrip()
            last = stripped[-1] if stripped else ""
            if _is_prose_like(text):
                # 尾部孤立标题（"…\n3、定期保养"）不是"句中切断"，但**是**真边界缺陷 ——
                # 单列一项，绝不因为收窄口径而把它悄悄丢掉。
                if _HEADING_TAIL_RE.search(stripped):
                    trailing_heading_cases.append({"doc_id": c.get("doc_id"), "v": 1.0,
                                                   "chunk_id": c.get("chunk_id")})
                else:
                    trailing_heading_cases.append({"doc_id": c.get("doc_id"), "v": 0.0})
                    mid = bool(stripped) and last not in _TERMINAL_CHARS
                    midsentence_cases.append({"doc_id": c.get("doc_id"),
                                              "v": 1.0 if mid else 0.0,
                                              "chunk_id": c.get("chunk_id"),
                                              "tail": stripped[-60:]})
            # orphaned heading: body is only a (structural) heading line with little following body
            body = _norm(text)
            first_line = (text.strip().splitlines() or [""])[0].strip()
            heading_only = (first_line in _STEP_SECTION_HEADERS and len(body) <= len(_norm(first_line)) + 8)
            orphan_heading_cases.append({"doc_id": c.get("doc_id"),
                                         "v": 1.0 if heading_only else 0.0})

    dist = {t: score_distribution(toks) for t, toks in sorted(by_type_tokens.items())}
    mid_ci = doc_clustered_bootstrap_ci(midsentence_cases, value_key="v") if midsentence_cases else {}
    th_ci = doc_clustered_bootstrap_ci(trailing_heading_cases, value_key="v") if trailing_heading_cases else {}
    oh_ci = doc_clustered_bootstrap_ci(orphan_heading_cases, value_key="v") if orphan_heading_cases else {}

    return {
        "n_chunks": len(chunks),
        "size_distribution_by_type": dist,
        "oversize_count": len(oversize),
        "oversize_structural_count": sum(1 for o in oversize if o["structural"]),
        "oversize_sample": oversize[:10],
        "undersize_count": len(undersize),
        "undersize_sample": undersize[:10],
        "token_drift_count": len(token_drift),
        "token_drift_sample": token_drift[:10],
        "n_prose": n_prose,
        "midsentence_cut_rate": _round_nan(mid_ci.get("doc_clustered_mean")),
        "midsentence_cut_ci": [_round_nan(mid_ci.get("doc_clustered_ci_lower")),
                               _round_nan(mid_ci.get("doc_clustered_ci_upper"))],
        "midsentence_unique_docs": mid_ci.get("unique_docs"),
        "midsentence_n_scored": len(midsentence_cases),
        # 诊断样本：旧实现只给一个率、不给样本，0.5 的读数无从归因（这次就卡在这儿）
        "midsentence_sample": [m for m in midsentence_cases if m.get("v")][:10],
        "trailing_heading_rate": _round_nan(th_ci.get("doc_clustered_mean")),
        "trailing_heading_ci_upper": _round_nan(th_ci.get("doc_clustered_ci_upper")),
        "orphan_heading_rate": _round_nan(oh_ci.get("doc_clustered_mean")),
        "orphan_heading_ci_upper": _round_nan(oh_ci.get("doc_clustered_ci_upper")),
    }


def _round_nan(v, nd: int = 4):
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


# ── Family C — self-containedness (deterministic heuristic; LLM bundle separate) ──

def family_self_containedness_heuristic(chunks: List[Dict]) -> Dict:
    """Fraction of chunks opening with a dangling anaphor/connective and lacking a
    resolvable section_title. Full-corpus, no LLM. The LLM judge confirms on a sample."""
    cases: List[Dict] = []
    sample: List[Dict] = []
    for c in chunks:
        if (c.get("chunk_type") or "") not in PROSE_TYPES:
            continue
        text = (c.get("chunk_text") or "").lstrip()
        # skip a context_prefix style 【...】 marker if present
        body = re.sub(r"^【[^】]*】", "", text).lstrip()
        dangling = body.startswith(_DANGLING_PREFIXES) and not (c.get("section_title") or "").strip()
        cases.append({"doc_id": c.get("doc_id"), "v": 1.0 if dangling else 0.0})
        if dangling and len(sample) < 20:
            sample.append({"chunk_id": c.get("chunk_id"), "doc_id": c.get("doc_id"),
                           "preview": body[:60]})
    ci = doc_clustered_bootstrap_ci(cases, value_key="v") if cases else {}
    return {
        "n_prose": len(cases),
        "dangling_anaphor_rate": _round_nan(ci.get("doc_clustered_mean")),
        "dangling_anaphor_ci_upper": _round_nan(ci.get("doc_clustered_ci_upper")),
        "sample": sample,
    }


# ── Family E — redundancy / dedup ─────────────────────────────────────────

def family_dedup(chunks: List[Dict], *, near_threshold: float = 0.9,
                 min_len: int = 40, max_block: int = 400) -> Dict:
    """Exact normalized-hash dedup (full) + length-blocked near-dup Jaccard.

    Distinguishes legit template-repeat (same doc) from twin contamination (cross-doc).
    Blocking by normalized-length bucket bounds the comparison from naive O(n^2).
    """
    # exact dups
    by_hash: Dict[str, List[Dict]] = defaultdict(list)
    for c in chunks:
        nt = _norm(c.get("chunk_text"))
        if len(nt) < min_len:
            continue
        h = hashlib.md5(nt.encode("utf-8")).hexdigest()
        by_hash[h].append(c)
    exact_groups = [g for g in by_hash.values() if len(g) > 1]
    exact_same_doc = sum(1 for g in exact_groups if len({x.get("doc_id") for x in g}) == 1)
    exact_cross_doc = len(exact_groups) - exact_same_doc

    # near-dup via length-bucket blocking (skip exact-dup members already grouped)
    buckets: Dict[int, List[Dict]] = defaultdict(list)
    for c in chunks:
        nt = _norm(c.get("chunk_text"))
        if len(nt) < min_len:
            continue
        buckets[len(nt) // 50].append(c)

    near_pairs_same: List[Dict] = []
    near_pairs_cross: List[Dict] = []
    truncated_blocks = 0
    seen_pairs = set()
    bucket_keys = sorted(buckets)
    for bk in bucket_keys:
        # compare within bucket and to the next bucket (length ±50 chars)
        cand = buckets[bk] + buckets.get(bk + 1, [])
        if len(cand) > max_block:
            truncated_blocks += 1
            cand = cand[:max_block]
        shs = [(_shingles(c.get("chunk_text") or ""), c) for c in cand]
        for i in range(len(shs)):
            si, ci = shs[i]
            if not si:
                continue
            for j in range(i + 1, len(shs)):
                sj, cj = shs[j]
                if not sj:
                    continue
                key = tuple(sorted((ci.get("chunk_id") or str(id(ci)),
                                    cj.get("chunk_id") or str(id(cj)))))
                if key in seen_pairs:
                    continue
                inter = len(si & sj)
                if inter == 0:
                    continue
                jac = inter / len(si | sj)
                if jac >= near_threshold:
                    seen_pairs.add(key)
                    rec = {"a": ci.get("chunk_id"), "b": cj.get("chunk_id"),
                           "jaccard": round(jac, 3), "a_doc": ci.get("doc_id"),
                           "b_doc": cj.get("doc_id")}
                    if ci.get("doc_id") == cj.get("doc_id"):
                        near_pairs_same.append(rec)
                    else:
                        near_pairs_cross.append(rec)

    # near-dup factor: chunks involved in any near-dup pair / total eligible
    eligible = sum(1 for c in chunks if len(_norm(c.get("chunk_text"))) >= min_len)
    near_chunks = len({p["a"] for p in near_pairs_cross} | {p["b"] for p in near_pairs_cross})
    near_dup_factor = round(1.0 + (near_chunks / eligible), 4) if eligible else 1.0

    return {
        "eligible_chunks": eligible,
        "exact_dup_groups": len(exact_groups),
        "exact_dup_same_doc": exact_same_doc,     # legit template repetition
        "exact_dup_cross_doc": exact_cross_doc,   # suspicious (twin contamination)
        "exact_dup_sample": [[x.get("chunk_id") for x in g[:3]] for g in exact_groups[:5]],
        "near_dup_pairs_same_doc": len(near_pairs_same),
        "near_dup_pairs_cross_doc": len(near_pairs_cross),
        "near_dup_cross_factor": near_dup_factor,
        "near_dup_cross_sample": near_pairs_cross[:10],
        "blocking_truncated_blocks": truncated_blocks,
    }


# ── Family F — image-binding over-attach guardrail ────────────────────────

def family_image_binding(chunks: List[Dict]) -> Dict:
    """Per-chunk image-ref duplication factor from image_refs_json (no GT needed).

    dup = n_refs / n_unique_identities (identity = image_index, else oss_key). p95/max
    over chunks-with-images per format catches the over-attach regression (>1.5 bug)."""
    by_fmt_dup: Dict[str, List[float]] = defaultdict(list)
    malformed = 0
    overattach: List[Dict] = []
    n_with_images = 0
    for c in chunks:
        raw = c.get("image_refs_json")
        if not raw:
            continue
        try:
            refs = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(refs, list) or not refs:
            continue
        n_with_images += 1
        identities = set()
        for r in refs:
            if not isinstance(r, dict):
                continue
            ident = r.get("image_index")
            if ident is None:
                ident = r.get("oss_key") or r.get("source_image")
            identities.add(ident)
        n_unique = max(1, len(identities))
        dup = len(refs) / n_unique
        fmt = _ext_of(c.get("original_filename")) or "?"
        by_fmt_dup[fmt].append(dup)
        if dup > 1.2:
            overattach.append({"chunk_id": c.get("chunk_id"), "n_refs": len(refs),
                               "n_unique": n_unique, "dup": round(dup, 3)})

    per_fmt = {}
    all_dups: List[float] = []
    for fmt, dups in sorted(by_fmt_dup.items()):
        pcs = percentiles(dups, (95, 99))
        per_fmt[fmt] = {"n": len(dups), "p95": pcs.get("p95"), "max": round(max(dups), 4)}
        all_dups.extend(dups)
    overall = percentiles(all_dups, (95, 99)) if all_dups else {"p95": None, "p99": None}
    return {
        "n_chunks_with_images": n_with_images,
        "malformed_json": malformed,
        "img_dup_factor_p95": overall.get("p95"),
        "img_dup_factor_max": round(max(all_dups), 4) if all_dups else None,
        "per_format": per_fmt,
        "overattach_sample": overattach[:10],
    }


# ── Family D — type-routing correctness (metadata + D3, soft round-1) ──────

def _expected_family_from_meta(doc: Dict) -> Optional[set]:
    """Metadata-derivable expected chunk-type family (faq/clause/manual/sop text).

    Mirrors the non-canonical-dependent branches of node_chunk_documents routing. Returns
    None when the decision needs canonical (step-detect / xlsx-layout), so Family D abstains
    rather than guessing."""
    cat1 = str(doc.get("category_l1") or "").lower()
    cat2 = str(doc.get("category_l2") or "").lower()
    title = str(doc.get("title") or "").lower()
    doc_id = str(doc.get("doc_id") or "").lower()
    ext = _ext_of(doc.get("original_filename"))
    if ext in ("xlsx", "xls", "pptx", "ppt"):
        return None  # layout/slide routing needs canonical blocks
    if "faq" in cat1 or "faq" in cat2 or "faq" in title or "faq" in doc_id:
        return _MODE_EXPECTED_FAMILY["faq"]
    if (any(k in cat1 for k in ("policy", "standard", "regulation"))
            or any(k in cat2 for k in ("policy", "standard", "regulation"))
            or "制度" in title or "规定" in title or "规范" in title):
        return _MODE_EXPECTED_FAMILY["clause"]
    # manual/sop/guide all produce text or step; step needs canonical step-detect, so the
    # safe metadata expectation is "text OR step" — we only flag a hard mismatch (e.g. a
    # policy doc that produced faq_chunk).
    return _MODE_EXPECTED_FAMILY["text"] | _MODE_EXPECTED_FAMILY["step"]


def family_routing(chunks: List[Dict], d3: Optional[Dict]) -> Dict:
    """Soft round-1 routing check: per-doc observed chunk-type family vs the
    metadata-derivable expectation, plus D3's under-chunk candidate count."""
    by_doc_types: Dict[str, set] = defaultdict(set)
    doc_meta: Dict[str, Dict] = {}
    for c in chunks:
        d = c.get("doc_id")
        by_doc_types[d].add(c.get("chunk_type"))
        doc_meta.setdefault(d, c)

    mismatches: List[Dict] = []
    n_checked = 0
    match_cases: List[Dict] = []
    for d, observed in by_doc_types.items():
        exp = _expected_family_from_meta(doc_meta[d])
        if exp is None:
            continue
        n_checked += 1
        ok = bool(observed & exp)
        match_cases.append({"doc_id": d, "v": 1.0 if ok else 0.0})
        if not ok:
            mismatches.append({"doc_id": d, "expected": sorted(exp),
                               "observed": sorted(observed)})

    ci = doc_clustered_bootstrap_ci(match_cases, value_key="v") if match_cases else {}
    out = {
        "n_docs_checked": n_checked,
        "routing_match_rate": _round_nan(ci.get("doc_clustered_mean")),
        "routing_match_ci_lower": _round_nan(ci.get("doc_clustered_ci_lower")),
        "mismatch_count": len(mismatches),
        "mismatch_sample": mismatches[:10],
        "note": "metadata-level (xlsx/pptx + step-detect abstained — need canonical, phase-2)",
    }
    if d3:
        out["d3_under_chunk_candidates"] = (d3.get("routed_total", 0)
                                            - d3.get("routed_with_step", 0))
        out["d3_routed_total"] = d3.get("routed_total")
    return out


# ── fingerprint ───────────────────────────────────────────────────────────

def compute_fingerprint(chunks: List[Dict], d7_payload: Optional[Dict],
                        code_commit: str) -> Dict:
    """Input snapshot fingerprint so two runs are only compared when inputs match
    (else input_drift, not algorithm instability)."""
    chunk_ids = sorted(str(c.get("chunk_id")) for c in chunks)
    cid_hash = hashlib.sha256("\n".join(chunk_ids).encode("utf-8")).hexdigest()[:16]
    d7_hash = None
    if d7_payload is not None:
        d7_hash = hashlib.sha256(
            json.dumps(d7_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
    return {
        "active_chunks": len(chunks),
        "active_docs": len({c.get("doc_id") for c in chunks}),
        "chunk_id_set_hash": cid_hash,
        "d7_json_hash": d7_hash,
        "code_commit": code_commit,
        "rubric_version": RUBRIC_VERSION,
    }


# ── three-state gate verdict ──────────────────────────────────────────────

def build_gates_and_verdict(fam: Dict, d7: Optional[Dict], h: Dict) -> Dict:
    """Assemble hard/soft gates and derive GO / NO_GO_DEFECT / NO_GO_INCOMPLETE_EVIDENCE.

    A hard gate with value None (unmeasured) → INCOMPLETE_EVIDENCE, never GO.
    """
    gates: Dict[str, Dict] = {}

    def hard(name, value, ok, target):
        gates[name] = {"target": target, "value": value, "pass": ok, "hard": True}

    def soft(name, value, ok, target):
        gates[name] = {"target": target, "value": value, "pass": ok, "hard": False}

    b = fam["boundary"]
    # HARD: no chunk outside the [5,2000] build band
    out_band = b["oversize_count"] + b["undersize_count"]
    hard("tokens in [5,2000] (B)", out_band, out_band == 0, "= 0 chunks out of band")
    # HARD: no structural chunk oversize (the A37 silent-drop precursor)
    hard("no oversize structural chunk (B/A2)", b["oversize_structural_count"],
         b["oversize_structural_count"] == 0, "= 0")

    # HARD: A2 via D4/D7 proxy + structural symptoms
    if d7 is not None:
        d4 = (d7.get("D4") or {}).get("orphan_count")
        d7m = (d7.get("D7") or {}).get("missing")
        d7d = (d7.get("D7") or {}).get("duplicate")
        d1 = (d7.get("D1") or {}).get("sym_diff")
        d1_active = (d7.get("D1") or {}).get("rds_active")
        # match the seven-dim audit's 0.5% drift tolerance (not an arbitrary <=1)
        d1_tol = max(1, int((d1_active or 0) * 0.005)) if d1_active else 1
        d6c = (d7.get("D6") or {}).get("chunk_compliant_all")
        d6t = (d7.get("D6") or {}).get("total_chunks")
        d6_rate = (d6c / d6t) if (d6c is not None and d6t) else None
        hard("orphan step_cards = 0 (A/D4)", d4, d4 == 0 if d4 is not None else None, "= 0")
        hard("procedure_parent balance (A/D7)",
             {"missing": d7m, "duplicate": d7d},
             (d7m == 0 and d7d == 0) if (d7m is not None and d7d is not None) else None,
             "missing=0 ∧ duplicate=0")
        hard("RDS↔HA3 step_card drift (A/D1)", d1,
             (d1 <= d1_tol) if d1 is not None else None, f"sym_diff <= {d1_tol} (0.5%)")
        hard("image_refs shape compliance (A/D6)",
             round(d6_rate, 4) if d6_rate is not None else None,
             (d6_rate >= 0.95) if d6_rate is not None else None, ">= 0.95")
    else:
        hard("orphan step_cards = 0 (A/D4)", None, None, "= 0 (D1-D7 JSON missing)")
        hard("procedure_parent balance (A/D7)", None, None, "missing=0 (D1-D7 JSON missing)")
        hard("image_refs shape compliance (A/D6)", None, None, ">= 0.95 (D1-D7 JSON missing)")

    # HARD: image over-attach. No image chunks ⇒ vacuously passes (nothing to over-attach),
    # NOT unmeasured — a text-only corpus must not be blocked as INCOMPLETE_EVIDENCE.
    f = fam["image_binding"]
    p95 = f.get("img_dup_factor_p95")
    if (f.get("n_chunks_with_images") or 0) == 0:
        hard("img_dup_factor p95 (F)", "n/a (no image chunks)", True, "<= 1.20")
    else:
        hard("img_dup_factor p95 (F)", p95, (p95 <= 1.20) if p95 is not None else None,
             "<= 1.20")
    hard("image_refs JSON parseable (F)", f.get("malformed_json"),
         f.get("malformed_json") == 0, "= 0 malformed")

    # HARD: RDS↔HA3 all-type id-set (extra ⇒ incomplete purge). Truncated enum = unmeasured.
    # 终局(2026-07-21)四级判定，顺序固定：
    #   1) extra>0 恒 FAIL——extra 是 query 实际返回行，不受枚举盲区影响，是确凿缺陷；
    #   2) fetch_ok=True → 按 fetch 分类：confirmed>0=FAIL；unclassified>0=unmeasured
    #      (INCOMPLETE)；纯 query_invisible(盲区伪影，数据在场)=PASS；
    #   3) fetch 尝试过但整体失败(fetch_ok=False) → unmeasured——无法定性绝不误判 DEFECT；
    #   4) 无 fetch_ok 字段(miss==0 未触发 fetch，或合成/历史 h dict) → 旧语义。
    # 枚举不完整 = 饱和(truncated) ∨ 任一桶协议不健康(errorCode/coveredPercent<1/越桶/
    # 重复 PK/is_active 契约破裂)。⚠️ 只查 truncated 会漏掉后者 → 破枚举也可能 PASS。
    enum_incomplete = bool(h.get("truncated") or h.get("unhealthy_buckets"))
    if enum_incomplete:
        hard("RDS↔HA3 all-type id-set (H)",
             {"truncated": h.get("truncated"),
              "unhealthy_buckets": dict(list((h.get("unhealthy_buckets") or {}).items())[:5])},
             None, "missing=0 ∧ extra=0（HA3 倒排枚举不完整 — unmeasured）")
    elif "fetch_ok" in h:
        miss, extra = h.get("missing_in_ha3"), h.get("extra_in_ha3")
        mc, mu = h.get("missing_confirmed"), h.get("missing_unclassified")
        ei = h.get("enum_invisible", h.get("query_invisible"))
        value = {"missing_enum_scope": miss, "missing_confirmed": mc,
                 "enum_invisible": ei, "unclassified": mu, "extra": extra}
        if h.get("fetch_error"):
            value["fetch_error"] = h["fetch_error"]   # defect/incomplete 并存时证据都留
        if extra:
            ok_h = False           # extra 是枚举实返行，确凿缺陷，优先于一切
        elif h["fetch_ok"] is not True:
            ok_h = None            # fetch 尝试过但失败 → 存在性无法定性
        elif mc:
            ok_h = False           # fetch 也取不回 → 数据真丢失
        elif mu or ei:
            # ⚠️ 语义变更(2026-07-22)：零向量时代 enum_invisible 是**预期伪影**故放绿；
            # 倒排枚举下它意味着枚举或 is_active 锚点失效 → extra/orphan 方向不可测。
            ok_h = None
        else:
            ok_h = True
        hard("RDS↔HA3 all-type id-set (H)", value, ok_h,
             "fetch-confirmed missing=0 ∧ extra=0（extra/confirmed → DEFECT；"
             "enum_invisible/unclassified/fetch 失败 → INCOMPLETE）")
    else:
        miss, extra = h.get("missing_in_ha3"), h.get("extra_in_ha3")
        hard("RDS↔HA3 all-type id-set (H)", {"missing": miss, "extra": extra},
             (miss == 0 and extra == 0) if (miss is not None and extra is not None) else None,
             "missing=0 ∧ extra=0")

    # SOFT (round-1 trend; gate on CI upper bound where applicable)
    mid = b.get("midsentence_cut_rate")
    mid_hi = (b.get("midsentence_cut_ci") or [None, None])[1]
    soft("mid-sentence cut rate (B)", mid,
         (mid_hi <= 0.05) if mid_hi is not None else None, "CI-upper <= 0.05")
    c = fam["self_containedness"]
    soft("dangling-anaphor rate (C)", c.get("dangling_anaphor_rate"),
         (c.get("dangling_anaphor_ci_upper") <= 0.05)
         if c.get("dangling_anaphor_ci_upper") is not None else None, "CI-upper <= 0.05")
    e = fam["dedup"]
    soft("cross-doc near-dup factor (E)", e.get("near_dup_cross_factor"),
         (e.get("near_dup_cross_factor") <= 1.10)
         if e.get("near_dup_cross_factor") is not None else None, "<= 1.10")
    d = fam["routing"]
    soft("routing-family match rate (D)", d.get("routing_match_rate"),
         (d.get("routing_match_ci_lower") >= 0.95)
         if d.get("routing_match_ci_lower") is not None else None,
         "CI-lower >= 0.95 (soft, metadata-level)")

    # derive verdict from HARD gates only
    hard_gates = {k: v for k, v in gates.items() if v["hard"]}
    any_unmeasured = any(g["pass"] is None for g in hard_gates.values())
    any_failed = any(g["pass"] is False for g in hard_gates.values())
    if any_failed:
        state = "NO_GO_DEFECT"
    elif any_unmeasured:
        state = "NO_GO_INCOMPLETE_EVIDENCE"
    else:
        state = "GO"
    return {"gates": gates, "state": state, "go_no_go": state == "GO"}


# ── judge bundle (LLM self-containedness, sampled; merged in a later step) ──

def _stratify_sample(chunks: List[Dict], n_repr: int, n_rare: int, seed: int) -> List[Dict]:
    """Representative stratified sample by chunk_type, with a floor for rare types."""
    import random
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for c in chunks:
        by_type[c.get("chunk_type") or "?"].append(c)
    rng = random.Random(seed)
    types = sorted(by_type)
    total = len(chunks) or 1
    chosen: List[Dict] = []
    chosen_ids = set()

    def _add(c):
        if c.get("chunk_id") not in chosen_ids:
            chosen.append(c)
            chosen_ids.add(c.get("chunk_id"))

    # rare-type floor first
    rare_types = sorted(types, key=lambda t: len(by_type[t]))
    per_rare = max(1, n_rare // max(1, min(len(rare_types), 5)))
    for t in rare_types[:5]:
        for c in rng.sample(by_type[t], min(per_rare, len(by_type[t]))):
            _add(c)
    # proportional representative
    for t in types:
        pool = [c for c in by_type[t] if c.get("chunk_id") not in chosen_ids]
        k = max(1, round(n_repr * len(by_type[t]) / total))
        for c in rng.sample(pool, min(k, len(pool))):
            _add(c)
    return chosen


def build_chunk_judge_bundle(chunks: List[Dict], risk_ids: set, *,
                             n_repr: int = 130, n_risk: int = 40, n_rare: int = 10,
                             seed: int = 20260615) -> List[Dict]:
    """Blinded chunk judge bundle (kind='chunk'): chunk_text + chunk_type + section_title
    only. Representative / risk-targeted / rare buckets are tagged so the merge step keeps
    their stats SEPARATE (risk-enriched must not pollute the population pass-rate)."""
    import random
    rng = random.Random(seed)
    repr_sample = _stratify_sample(chunks, n_repr, n_rare, seed)
    repr_ids = {c.get("chunk_id") for c in repr_sample}
    risk_pool = [c for c in chunks if c.get("chunk_id") in risk_ids
                 and c.get("chunk_id") not in repr_ids]
    risk_sample = rng.sample(risk_pool, min(n_risk, len(risk_pool)))

    bundle = []
    for bucket, sample in (("representative", repr_sample), ("risk", risk_sample)):
        for c in sample:
            cid = c.get("chunk_id")
            bundle.append({
                "qid": f"chunk::{cid}",
                "item_id": cid,                    # stable id
                "kind": "chunk",
                "bucket": bucket,
                "chunk_type": c.get("chunk_type"),
                "section_title": c.get("section_title"),
                "chunk_text": (c.get("chunk_text") or "")[:4000],
                "rubric_version": RUBRIC_VERSION,
            })
    return bundle


# ── Family H — RDS↔HA3 all-type id-set ────────────────────────────────────

def _ha3_all_active_chunk_ids(bucket: int = None, max_rounds: int = 3) -> Dict:
    """All chunk_ids present in HA3 via **倒排**分桶枚举（2026-07-22 起）。

    ⚠️ 旧实现是零向量 PK-range 扫描 + loop-until-stable。2026-07-22 终局定性推翻该模型：
    零向量与任何向量内积恒为 0 ⇒ 全部得分并列 ⇒ 返回哪个子集由 ANN 遍历/剪枝路径决定，
    是**任意但确定的子集**（重扫恒缺同一批 113 行，换 eps 向量恒缺另一批 220 行，交集仅
    5）——loop-until-stable 对确定性欠返回完全无效，历次 idset 判缺全是该伪影。
    现改用 clients.ha3_enumerate_bucket（纯倒排、单页不翻页、锚点 is_active 双态、
    桶宽 ≤500）。`bucket`/`max_rounds` 保留仅为签名兼容：桶宽恒钳到 HA3_ENUM_BUCKET，
    max_rounds 被忽略。

    Returns {"ids": set, "returned": int, "truncated": bool, "windows": int,
             "unhealthy_buckets": {start: reason}}。
    **`truncated` 或 `unhealthy_buckets` 任一为真 ⇒ 枚举不完整**，H 门必须判 INCOMPLETE
    （见 build_gates_and_verdict）。绝不回退零向量。"""
    from opensearch_pipeline.clients import (
        HA3_ENUM_BUCKET, ha3_enumerate_bucket,
    )
    from .. import ha3live
    # PK ceiling from RDS (HA3 PK = chunk_meta.id)。优先 AUTO_INCREMENT 高水位——最高位
    # RDS 行被硬删而 HA3 行仍在时，孤儿 PK 会高于 MAX(id)（失败方向=漏检）。
    conn = ha3live.rds_conn()
    cur = conn.cursor()
    id_hi = 0
    try:
        cur.execute("SELECT AUTO_INCREMENT FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chunk_meta'")
        row = cur.fetchone()
        if row:
            v = list(row.values())[0] if isinstance(row, dict) else row[0]
            id_hi = int(v or 0)
    except Exception:  # noqa: BLE001 — 拿不到高水位就回退 MAX(id)
        id_hi = 0
    if not id_hi:
        cur.execute("SELECT MAX(id) FROM chunk_meta")
        row = cur.fetchone()
        id_hi = (list(row.values())[0] if isinstance(row, dict) else row[0]) or 0
    cur.close()
    conn.close()
    if not id_hi:
        return {"ids": set(), "returned": 0, "truncated": True, "windows": 0,
                "unhealthy_buckets": {}}
    bk = min(int(bucket or HA3_ENUM_BUCKET), HA3_ENUM_BUCKET)
    cli, tbl = ha3live.client(), ha3live.table()
    ids, windows, unhealthy = set(), 0, {}
    start = 0
    while start <= id_hi:
        end = min(start + bk, id_hi + 1)
        res = ha3_enumerate_bucket(cli, tbl, start, end, ["chunk_id", "id", "is_active"])
        if not res["healthy"]:
            unhealthy[start] = res["reason"]
        for r in res["rows"].values():
            cid = r.get("chunk_id")
            if cid:
                ids.add(cid)
        windows += 1
        start = end
    return {"ids": ids, "returned": len(ids), "truncated": False, "windows": windows,
            "unhealthy_buckets": unhealthy}


def _fld(item: Dict, key: str):
    f = item.get("fields", item)
    return f.get(key) if isinstance(f, dict) else None


def _pk_map_for_chunk_ids(chunk_ids: List[str], batch: int = 500) -> Dict[str, int]:
    """chunk_id → chunk_meta.id(=HA3 主键)映射，批量 IN 查询（只读；chunk_id 有唯一键）。"""
    from ..ha3live import rds_conn
    out: Dict[str, int] = {}
    if not chunk_ids:
        return out
    conn = rds_conn()
    try:
        with conn.cursor() as c:
            for i in range(0, len(chunk_ids), batch):
                sub = chunk_ids[i:i + batch]
                ph = ",".join(["%s"] * len(sub))
                c.execute("SELECT chunk_id, id FROM chunk_meta"
                          f" WHERE is_active=1 AND chunk_id IN ({ph})", tuple(sub))
                for r in c.fetchall():
                    cid = r["chunk_id"] if isinstance(r, dict) else r[0]
                    pk = r["id"] if isinstance(r, dict) else r[1]
                    out[cid] = int(pk)
    finally:
        conn.close()
    return out


def _fetch_reclassify_idset(missing_chunk_ids: List[str]) -> Dict:
    """铁律(2026-07-21)：存在性判定唯官方 /vector-service/fetch 为准——query 枚举判缺的
    chunk_id 经 fetch 复核，只有 fetch 也无才算真缺失。

    复用生产侧 reconcile._fetch_reclassify_missing（同一 fail-open 契约），此处只补
    chunk_id↔pk 双向映射；映射不到 pk 的 chunk_id（corpus 拉取与枚举窗口间的 rechunk race）
    保守计入 unclassified。fail-open：任何异常 → fetch_ok=False（门侧按 INCOMPLETE 处理，
    绝不误判 GO 也不误判 DEFECT）。"""
    try:
        from opensearch_pipeline import reconcile as _rec
        from .. import ha3live
        pk_by_cid = _pk_map_for_chunk_ids(missing_chunk_ids)
        unmapped = [c for c in missing_chunk_ids if c not in pk_by_cid]
        cid_by_pk = {pk: cid for cid, pk in pk_by_cid.items()}
        fr = _rec._fetch_reclassify_missing(
            ha3live.client(), ha3live.table(),
            [{"id": pk} for pk in pk_by_cid.values()])
        if not fr.get("ok"):
            return {"fetch_ok": False, "fetch_error": str(fr.get("error"))[:160],
                    "fetch_errors": (fr.get("fetch_errors") or [])[:10]}
        confirmed = sorted(cid_by_pk[p] for p in fr["missing_confirmed"])
        uncls = sorted([cid_by_pk[p] for p in fr["unclassified"]] + unmapped)
        n_inv = len(fr.get("enum_invisible") or fr.get("query_invisible") or [])
        return {
            "fetch_ok": True,
            "missing_confirmed": len(confirmed),
            # enum_invisible 规范名；query_invisible 兼容别名（同值双写）
            "enum_invisible": n_inv,
            "query_invisible": n_inv,
            "missing_unclassified": len(uncls),
            "missing_confirmed_sample": confirmed[:10],
            "fetch_errors": (fr.get("fetch_errors") or [])[:10],
        }
    except Exception as e:  # noqa: BLE001 — fail-open：定性失败只降级为 INCOMPLETE
        return {"fetch_ok": False, "fetch_error": f"{type(e).__name__}: {e}"[:160]}


def family_idset_reconciliation(rds_chunk_ids: set) -> Dict:
    try:
        ha3 = _ha3_all_active_chunk_ids()
    except Exception as e:  # fail-open layer; gate sees truncated/None
        return {"error": f"{type(e).__name__}: {e}"[:160], "truncated": True}
    ha3_ids = ha3["ids"]
    miss = rds_chunk_ids - ha3_ids       # in RDS active, absent from query enum — 判缺候选
    extra = ha3_ids - rds_chunk_ids      # in HA3, not RDS active — incomplete purge（确凿）
    union = rds_chunk_ids | ha3_ids
    jac = round(len(rds_chunk_ids & ha3_ids) / len(union), 4) if union else None
    out = {
        "rds_active": len(rds_chunk_ids),
        "ha3_returned": ha3["returned"],
        "ha3_unique": len(ha3_ids),
        "truncated": ha3["truncated"],
        "unhealthy_buckets": ha3.get("unhealthy_buckets") or {},
        "missing_in_ha3": len(miss),
        "extra_in_ha3": len(extra),
        # 枚举覆盖率指标，不是存在性 parity——枚举不健康时会被压低。
        "idset_jaccard": jac,
        "missing_sample": sorted(miss)[:10],
        "extra_sample": sorted(extra)[:10],
    }
    # 终局(2026-07-22)：枚举判缺只是候选，经官方 fetch 复核定性；枚举不完整时
    # （truncated 或有不健康桶）候选集本身不可信，维持 unmeasured 语义、**不做 fetch**。
    if miss and not ha3["truncated"] and not out["unhealthy_buckets"]:
        out.update(_fetch_reclassify_idset(sorted(miss)))
    return out


# ── corpus pull + orchestration ───────────────────────────────────────────

_CORPUS_SQL = (
    "SELECT cm.chunk_id, cm.doc_id, cm.chunk_type, cm.section_title, cm.chunk_text, "
    "cm.token_count, cm.chunk_index, cm.parent_chunk_id, cm.step_no, cm.image_refs_json, "
    "cm.owner_dept, dm.title, dm.original_filename, dm.category_l1, dm.category_l2, "
    "dm.permission_level "
    "FROM chunk_meta cm LEFT JOIN document_meta dm ON dm.doc_id = cm.doc_id "
    "WHERE cm.is_active = 1"
)


def _load_corpus() -> List[Dict]:
    from ..ha3live import rds_conn
    conn = rds_conn()
    try:
        with conn.cursor() as c:
            c.execute(_CORPUS_SQL)
            return list(c.fetchall())
    finally:
        conn.close()


def _load_d7(path: Optional[str]) -> Optional[Dict]:
    if not path:
        # default: newest ha3_step_card_coverage_*.json under docs/audits
        adir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "docs", "audits")
        if os.path.isdir(adir):
            cands = sorted([f for f in os.listdir(adir)
                            if f.startswith("ha3_step_card_coverage_") and f.endswith(".json")])
            if cands:
                path = os.path.join(adir, cands[-1])
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return None
    return None


def _git_commit() -> str:
    try:
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return subprocess.check_output(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                       text=True).strip()
    except Exception:
        return "unknown"


def analyze_corpus(chunks: List[Dict], d7_payload: Optional[Dict], h: Dict,
                   code_commit: str = "unknown") -> Dict:
    """Pure: run every deterministic family + verdict over an in-memory corpus.

    Separated from run() so the metric math is unit-tested offline."""
    fam = {
        "boundary": family_boundary(chunks),
        "self_containedness": family_self_containedness_heuristic(chunks),
        "dedup": family_dedup(chunks),
        "image_binding": family_image_binding(chunks),
        "routing": family_routing(chunks, (d7_payload or {}).get("D3")),
    }
    gv = build_gates_and_verdict(fam, d7_payload, h)
    return {
        "fingerprint": compute_fingerprint(chunks, d7_payload, code_commit),
        "families": fam,
        "idset": h,
        "gates": gv["gates"],
        "state": gv["state"],
        "go_no_go": gv["go_no_go"],
    }


def run(d7_json_path: Optional[str] = None, *, judge_bundle: bool = True,
        seed: int = 20260615) -> Dict:
    """Full read-only L6 run against the live corpus."""
    chunks = _load_corpus()
    if not chunks:
        return {"applicable": False, "note": "no active chunks in chunk_meta"}
    d7 = _load_d7(d7_json_path)
    rds_ids = {c.get("chunk_id") for c in chunks}
    h = family_idset_reconciliation(rds_ids)

    out = analyze_corpus(chunks, d7, h, code_commit=_git_commit())
    out["applicable"] = True
    out["d7_source"] = "loaded" if d7 is not None else "MISSING"

    if judge_bundle:
        # risk-targeted ids: mid-sentence + dangling + over-attach + near-dup + oversize
        b = out["families"]["boundary"]
        risk_ids = set()
        for s in (b.get("oversize_sample") or []):
            risk_ids.add(s.get("chunk_id"))
        for s in (out["families"]["self_containedness"].get("sample") or []):
            risk_ids.add(s.get("chunk_id"))
        for s in (out["families"]["image_binding"].get("overattach_sample") or []):
            risk_ids.add(s.get("chunk_id"))
        for p in (out["families"]["dedup"].get("near_dup_cross_sample") or []):
            risk_ids.update({p.get("a"), p.get("b")})
        out["judge_bundle_chunk"] = build_chunk_judge_bundle(
            chunks, {x for x in risk_ids if x}, seed=seed)
    return out
