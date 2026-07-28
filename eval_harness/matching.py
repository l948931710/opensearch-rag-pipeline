"""Gold <-> retrieved matching + deterministic answer-quality detectors.

The retrieval gold in the xlsx is expressed as document *names* (e.g. 《工作服管理规定》).
The live index returns chunks carrying a `title` and `doc_id`. Matching here is title-based
(name normalization + substring / token overlap) with an optional resolved doc_id fast-path.

Deterministic answer detectors (refusal / source-leak / image markers) are ported from
~/Downloads/opensearch-rag-data/eval_samples/scripts/text_quality_eval.py so the numbers are
comparable to the prior text-quality A/B work.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

# 标题归一口径的语义版本。**改这里就是改尺子** —— L1 的 recall@k 全部由 gold_doc_rank 决定，
# 而它只看归一后的标题。与 l4_ingestion_evaluator_version / l6_evaluator_version 同款用途：
# 进 regime，使"尺子变了"与"管线变好了"在差量网里可区分。
#   2.0.0 (2026-07-27)：剥文件扩展名 + 括号只去符号保留内容（两条见下）
MATCHER_VERSION = "2.0.0"

# 《...》 document-name spans
_BOOKNAME = re.compile(r"《([^《》]+)》")
# 文件扩展名。语料标题带扩展名（HA3 的 `title` 就是文件名），金集写的是纯文档名 ——
# 不剥的话包含判定被尾巴挡死，只能退到 bigram Jaccard。实测 QA-109：检索把
# 《A37-8化学品泄露.docx》放在 rank 1/3/4，金集写《A37-8化学品泄露应急响应》，
# 归一成 'a378化学品泄露docx' vs 'a378化学品泄露应急响应' → 互不包含 → 0.5 < 0.6 →
# **判不匹配，记成 gold_rank=13**。这类错只会低报召回，不会高报。
_EXT = re.compile(r"\.(docx?|xlsx?|pptx?|pdf|txt|csv|html?|md)$", re.I)
# 括号：**只去符号，保留内容**。此前是把括号连内容一起删（`[（(【\[].*?[)）】\]]`），
# 会把标题塌缩成通用词根：《通知（进出厂规定）》→ '通知'，而 '通知' ⊂ 任何含"通知"的标题
# ⇒ 相似度 1.0。实测该规则今天就能让《关于饭卡充值的通知》《关于2021年秋季上下班时间调整
# 通知》三份**完全无关**的文档冒充金档（本轮实跑里恰好没开火，是潜伏的高报召回源）。
# 保留内容既杀掉这条假阳性，又不损失任何真匹配（21746 对全量交叉验证：翻上 2 组皆应匹配、
# 翻下 3 组皆为该假匹配家族、0 组真匹配被破坏）。
_BRACKETS = re.compile(r"[（()）【\[\]】]")
_PUNCT = re.compile(r"[\s　_\-—·、,，。.／/]+")


def parse_expected_docs(cell) -> List[str]:
    """Extract acceptable target document names from an xlsx '预期来源文档' cell.

    Handles single names, slash/or-separated alternatives, and concatenated
    《A》《B》《C》 multi-doc cells. Returns [] for '—' / blank (negatives)."""
    if not cell:
        return []
    s = str(cell).strip()
    if s in {"—", "-", "无", ""}:
        return []
    names = _BOOKNAME.findall(s)
    if names:
        return [n.strip() for n in names if n.strip()]
    # no 《》 — treat the whole cell (split on common separators) as a name
    parts = re.split(r"[/／、，,]| 或 |或", s)
    return [p.strip() for p in parts if p.strip()]


def normalize_title(s: Optional[str]) -> str:
    if not s:
        return ""
    s = _BOOKNAME.sub(r"\1", str(s))           # drop 《》 wrappers
    s = _EXT.sub("", s)                        # drop file extension (titles carry it, gold doesn't)
    s = _BRACKETS.sub("", s)                   # drop bracket CHARS, keep their content
    s = _PUNCT.sub("", s)                      # drop punctuation/space
    return s.lower()


def _tok(s: str) -> set:
    # crude char-bigram set for Chinese token overlap
    s = normalize_title(s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def title_similarity(expected: str, candidate: str) -> float:
    """0..1 similarity: 1.0 if one normalized name contains the other, else bigram Jaccard."""
    e, c = normalize_title(expected), normalize_title(candidate)
    if not e or not c:
        return 0.0
    if e in c or c in e:
        return 1.0
    te, tc = _tok(e), _tok(c)
    if not te or not tc:
        return 0.0
    return len(te & tc) / len(te | tc)


def chunk_matches_expected(chunk: Dict, expected_names: Sequence[str],
                           expected_doc_ids: Sequence[str], threshold: float = 0.6) -> bool:
    """Does this retrieved chunk belong to any acceptable gold document?"""
    did = str(chunk.get("doc_id") or "")
    if expected_doc_ids and did and did in set(expected_doc_ids):
        return True
    title = chunk.get("title") or chunk.get("section_title") or ""
    for name in expected_names:
        if title_similarity(name, title) >= threshold:
            return True
    return False


def gold_doc_rank(retrieved: List[Dict], expected_names: Sequence[str],
                  expected_doc_ids: Sequence[str], threshold: float = 0.6) -> Optional[int]:
    """1-based rank of the first retrieved chunk belonging to a gold document; None if absent."""
    for i, ch in enumerate(retrieved):
        if chunk_matches_expected(ch, expected_names, expected_doc_ids, threshold):
            return i + 1
    return None


def relevance_vector(retrieved: List[Dict], expected_names: Sequence[str],
                     expected_doc_ids: Sequence[str], threshold: float = 0.6) -> List[float]:
    """Per-result relevance (1.0/0.0) aligned to ranked order, for nDCG."""
    return [
        1.0 if chunk_matches_expected(ch, expected_names, expected_doc_ids, threshold) else 0.0
        for ch in retrieved
    ]


# ── keyword coverage (for answer/retrieval content checks) ──────────────

def keyword_coverage(text: Optional[str], keywords: Sequence[str]) -> float:
    if not keywords:
        return float("nan")
    if not text:
        return 0.0
    t = str(text)
    hit = sum(1 for kw in keywords if kw and str(kw) in t)
    return hit / len(keywords)


# ── deterministic answer-quality detectors (ported from text_quality_eval.py) ──

_REFUSAL = re.compile(r"(抱歉|无法回答|没有找到|未找到|未能找到|无相关|不包含相关|未提及|没有相关)")
# A STRONG refusal phrase (the assistant declining), used for hard-refusal detection.
_REFUSAL_STRONG = re.compile(
    r"(抱歉[，,。]?\s*(当前|知识库|未|没有)|知识库中(未|没有)|未找到相关信息|"
    r"无法回答|没有找到相关|未能找到相关|未提供相关信息)"
)
# A genuine SOURCE-LIST leak (rule 8 forbids the model emitting its own source list).
# NOTE: must NOT match the legitimate grounding phrase "根据参考文档/根据提供的参考文档".
_SRC_LEAK = re.compile(
    r"(参考来源|资料来源|引用来源|信息来源|参考资料[:：]|来源[:：]\s*《|出处[:：]\s*《)"
)
_IMG_MARKER = re.compile(r"<<IMG:\s*\d+\s*>>")
_NUM_STEP = re.compile(r"(?m)^\s*(?:\d+[\.\、)]|第[一二三四五六七八九十]+步|步骤\s*\d+)")


def refusal_detected(answer: Optional[str]) -> bool:
    """Soft signal: any decline language present (may fire on partial answers)."""
    return bool(answer) and bool(_REFUSAL.search(answer))


def hard_refusal(answer: Optional[str], max_chars: int = 110) -> bool:
    """True only when the answer is DOMINATED by a refusal, so a comprehensive answer
    that merely notes one missing sub-point is not counted. Two paths:
      - anchored: the strong decline opens the answer (first ~30 chars) -> hard refusal
        regardless of length (verbose refusals like '抱歉，…未找到。不过…' still decline);
      - unanchored: strong decline elsewhere counts only when the whole answer is short."""
    if not answer:
        return False
    a = answer.strip()
    m = _REFUSAL_STRONG.search(a)
    if not m:
        return False
    return m.start() <= 30 or len(a) <= max_chars


def source_leak_detected(answer: Optional[str]) -> bool:
    return bool(answer) and bool(_SRC_LEAK.search(answer))


def img_marker_count(answer: Optional[str]) -> int:
    return len(_IMG_MARKER.findall(answer or ""))


def numbered_step_count(answer: Optional[str]) -> int:
    return len(_NUM_STEP.findall(answer or ""))
