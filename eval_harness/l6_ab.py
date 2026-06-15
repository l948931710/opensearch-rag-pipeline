"""L6 follow-up — offline A/B experiment harness for the two top content-quality findings.

Read-only. NO production writes, NO re-chunk/re-index. Reproduces:
  - the fix TRANSFORMS (also the spec for the eventual chunker fix):
      * Fix A: drop the clause-mode `[上文] …` breadcrumb line
      * Fix B: drop the stale `章节:…` component from the `【…】` prefix (+ blank section_title)
  - the readability A/B sample (stratified, seeded) → shard files for a blinded judge panel
  - the recall A/B (embedding-stability + synthesized-query recall@k/MRR/nDCG + per-type
    self-retrieval), comparing before vs Fix A / Fix B / Fix A+B, with step_card Fix B OFF.

CLI:
  RAG_ENV=prod_ro RAG_READONLY=true RAG_ALLOW_REMOTE_DB=read_only_ack \\
    python -m eval_harness.l6_ab build --out scratch/l6_ab        # readability A/B bundle/shards
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
from collections import defaultdict
from typing import Dict, List, Optional

# Structural section headers a `[上文]` breadcrumb must NOT point at (no semantic context).
_STRUCTURAL_HDR = frozenset({
    "目的", "范围", "适用范围", "适用", "职责", "权责", "职责权限", "定义", "术语",
    "总则", "前言", "引言", "概述", "参考文件", "相关文件", "参考资料", "修订记录",
})


# ── fix transforms (the chunker-fix spec, applied here at text level for the offline A/B) ──

def strip_shangwen(text: str) -> str:
    """Fix A — remove the clause-mode `[上文] …` breadcrumb line (any line, not just first)."""
    return re.sub(r"(?m)^\[上文\][^\n]*\n?", "", text)


def drop_stale_section(text: str) -> str:
    """Fix B — drop the `章节:…` component from a leading `【部门|文档|章节】` prefix, keep the rest."""
    m = re.match(r"^【([^】]*)】\n?(.*)$", text, re.S)
    if not m:
        return text
    parts = [p.strip() for p in m.group(1).split("|") if "章节:" not in p]
    body = m.group(2)
    return f"【{' | '.join(parts)}】\n{body}" if parts else body


def shangwen_title(text: str) -> str:
    m = re.search(r"\[上文\]\s*([^\n]*)", text or "")
    return m.group(1).strip() if m else ""


def is_weak_prev(title: str) -> bool:
    """A `[上文]` breadcrumb is weak when it points at a structural header or a bare number."""
    t = (title or "").strip()
    stripped = re.sub(r"^[\d.、\s（）()A-Za-z]+", "", t).strip("：: 　")
    return (stripped in _STRUCTURAL_HDR) or bool(re.fullmatch(r"[\d.、\s（）()A-Za-z：:]+", t))


def section_is_clause_fragment(section_title: Optional[str]) -> bool:
    """Proxy for a mis-set/stale section label: a numbered-clause fragment, not a real heading."""
    return bool(re.match(r"^\s*\d+(\.\d+){1,}", section_title or ""))


# ── readability A/B sample (stratified, seeded) ──

def select_strata(chunks: List[Dict], *, seed: int = 20260615,
                  stale_min: int = 12) -> Dict[str, List[Dict]]:
    """Deterministic stratified selection of affected chunks for the readability/recall A/B."""
    rng = random.Random(seed)
    shang = [c for c in chunks if "[上文]" in (c.get("chunk_text") or "")]
    s1_weak = [c for c in shang if is_weak_prev(shangwen_title(c.get("chunk_text") or ""))]
    s1_ok = [c for c in shang if not is_weak_prev(shangwen_title(c.get("chunk_text") or ""))]
    have = [c for c in chunks if (c.get("section_title") or "").strip()]
    by = defaultdict(list)
    for c in have:
        by[(c["doc_id"], c["section_title"])].append(c)
    stale_pool = [c for v in by.values() if len(v) >= stale_min for c in v]
    s2_step = [c for c in chunks if c["chunk_type"] == "step_card"
               and section_is_clause_fragment(c.get("section_title"))]

    def take(pool, n):
        pool = [c for c in pool if len(c.get("chunk_text") or "") > 40]
        return rng.sample(pool, min(n, len(pool)))

    return {
        "I1_weak_shangwen": take(s1_weak, 12),
        "I1_ok_shangwen": take(s1_ok, 15),
        "I2_stale_section": take(stale_pool, 18),
        "I2_step_clausefrag": take(s2_step, 15),
    }


_FIX = {"I1_weak_shangwen": ("strip_shangwen", True),
        "I1_ok_shangwen": ("strip_shangwen", True),
        "I2_stale_section": ("drop_sect", False),
        "I2_step_clausefrag": ("drop_sect", False)}


def _apply_fix(text: str, fix: str) -> str:
    return strip_shangwen(text) if fix == "strip_shangwen" else drop_stale_section(text)


def build_readability_ab(out_dir: str, *, seed: int = 20260615, n_shards: int = 6) -> Dict:
    """Build the before/after bundle + blinded shard files. Returns a small summary."""
    from .layers.l6_chunk_quality import _load_corpus  # envboot sets read-only prod env
    chunks = _load_corpus()
    strata = select_strata(chunks, seed=seed)
    bundle, pairs = [], []
    for stratum, sample in strata.items():
        fix, keepsect = _FIX[stratum]
        for c in sample:
            cid = c["chunk_id"]
            before = c.get("chunk_text") or ""
            sect = c.get("section_title")
            after = _apply_fix(before, fix)
            after_sect = sect if keepsect else ""
            if after.strip() == before.strip() and after_sect == sect:
                continue
            for variant, txt, st in (("before", before, sect), ("after", after, after_sect)):
                bundle.append({"qid": f"{cid}::{variant}", "item_id": f"{cid}::{variant}",
                               "kind": "chunk", "stratum": stratum, "variant": variant,
                               "chunk_type": c["chunk_type"], "section_title": st,
                               "chunk_text": txt[:4000], "rubric_version": "chunk_rubric_v1"})
            pairs.append({"chunk_id": cid, "stratum": stratum, "chunk_type": c["chunk_type"],
                          "has_parent": bool(c.get("parent_chunk_id")),
                          "has_step_no": c.get("step_no") is not None,
                          "has_image_refs": bool(c.get("image_refs_json"))})
    os.makedirs(out_dir, exist_ok=True)
    json.dump(bundle, open(os.path.join(out_dir, "ab_bundle.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(pairs, open(os.path.join(out_dir, "ab_pairs.json"), "w"),
              ensure_ascii=False, indent=1)
    # blinded shards (item_id + chunk_type + section_title + chunk_text only)
    view = [{k: b[k] for k in ("item_id", "chunk_type", "section_title", "chunk_text")}
            for b in bundle]
    import math
    per = math.ceil(len(view) / n_shards)
    for i in range(n_shards):
        json.dump(view[i * per:(i + 1) * per],
                  open(os.path.join(out_dir, f"shard_{i}.json"), "w"),
                  ensure_ascii=False, indent=1)
    return {"pairs": len(pairs), "bundle_items": len(bundle),
            "per_stratum": {k: len(v) for k, v in strata.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--out", default="scratch/l6_ab")
    ap.add_argument("--seed", type=int, default=20260615)
    args = ap.parse_args()
    if args.cmd == "build":
        print(json.dumps(build_readability_ab(args.out, seed=args.seed), ensure_ascii=False))


if __name__ == "__main__":
    main()
