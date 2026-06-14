# -*- coding: utf-8 -*-
"""sampling.py — chunker A/B 框架的抽样 + bootstrap 工具(v3.1)

提供:
  1. stratified_sampling(goldset, target, seed) — 按文档类型分层抽样
     (v3.1 用户要求 — 各文档类型样本数分布均匀,主战场加权)
  2. compute_doc_pool_stats(dirs) — 文档池类型分布盘点
  3. doc_clustered_bootstrap_ci(per_case, doc_id_of) — doc-clustered CI(v3 #11)
     (同一 PDF 多题不独立, query-level 独立采样会低估 CI 带宽)
  4. fabrication_severity_test(off_cases, on_cases) — asymmetric + severity(v3 #10, v3.1 #5)
"""
from __future__ import annotations

import os
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ── 文档类型从 goldset 题目推断 ──

def _infer_type_from_case(case: Dict[str, Any]) -> str:
    """从 goldset case 推断文档类型(pdf/docx/xlsx/pptx/?)."""
    kind = case.get("kind", "positive")
    if kind == "negative":
        return "negative"
    for r in (case.get("resolution") or []):
        title = (r.get("title") or "").lower()
        if ".pdf" in title:
            return "pdf"
        if ".docx" in title or title.endswith(".doc"):
            return "docx"
        if ".xlsx" in title or title.endswith(".xls"):
            return "xlsx"
        if ".pptx" in title or title.endswith(".ppt"):
            return "pptx"
    return "?"


def stratified_sampling(
    cases: Sequence[Dict[str, Any]],
    target: Dict[str, int],
    *,
    seed: int = 20260614,
    allow_partial: bool = False,
) -> List[Dict[str, Any]]:
    """按文档类型分层抽样.

    Args:
        cases: goldset 题目列表(每项 含 qid/kind/resolution)
        target: {'pdf': 15, 'docx': 10, 'xlsx': 5, 'pptx': 0, 'negative': 8}
                'pptx': 0 表示该类型不抽,'?' 不出现在 target 即不抽
        seed: random seed(可复现)
        allow_partial: 若某类型可用数 < target 是否允许部分抽样(默认 raise)

    Returns:
        抽样后 cases 列表(顺序按 type 分组,组内 random)

    Raises:
        ValueError: 任一类型 target > 实际可用数 且 allow_partial=False
    """
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for c in cases:
        by_type[_infer_type_from_case(c)].append(c)

    rng = random.Random(seed)
    sampled: List[Dict[str, Any]] = []
    breakdown: Dict[str, Tuple[int, int]] = {}  # type: (target, sampled)

    for t, n in target.items():
        if n == 0:
            breakdown[t] = (0, 0)
            continue
        pool = by_type.get(t, [])
        if n > len(pool):
            if not allow_partial:
                raise ValueError(
                    f"stratified_sampling: type={t!r} target={n} but only {len(pool)} available. "
                    f"Use allow_partial=True or expand goldset.")
            actual = len(pool)
        else:
            actual = n
        chosen = rng.sample(pool, actual)
        sampled.extend(chosen)
        breakdown[t] = (n, actual)

    print(f"[stratified_sampling] target -> sampled (seed={seed}):")
    for t, (tgt, got) in breakdown.items():
        marker = "✓" if tgt == got else ("⚠️" if got < tgt else "?")
        print(f"  {marker} {t:<10} target={tgt:<4} sampled={got}")
    print(f"  total: {len(sampled)} cases")
    return sampled


# ── 文档池类型盘点 ──

def compute_doc_pool_stats(dirs: Sequence[Path]) -> Counter:
    """盘点文档池各类型 doc 数(供预检调用).

    Args:
        dirs: 文档目录列表(每个目录递归扫描)

    Returns:
        Counter({'pdf': 5, 'docx': 51, 'xlsx': 6, 'pptx': 1})
    """
    counter: Counter = Counter()
    suffix_map = {
        ".pdf": "pdf", ".docx": "docx", ".doc": "docx",
        ".xlsx": "xlsx", ".xls": "xlsx",
        ".pptx": "pptx", ".ppt": "pptx",
    }
    for d in dirs:
        d = Path(d).expanduser()
        if not d.exists():
            print(f"[compute_doc_pool_stats] skip non-existent dir: {d}")
            continue
        for p in d.rglob("*"):
            if p.is_file():
                t = suffix_map.get(p.suffix.lower())
                if t:
                    counter[t] += 1
    return counter


# ── doc-clustered bootstrap CI(v3 #11)──

def doc_clustered_bootstrap_ci(
    per_case: Sequence[Dict[str, Any]],
    *,
    value_key: str,
    doc_id_key: str = "doc_id",
    n_resamples: int = 10000,
    ci: float = 0.95,
    seed: int = 20260614,
) -> Dict[str, Any]:
    """按 doc_id 聚类的 bootstrap CI.

    Why doc-clustered:
        同一 PDF 多题彼此相关(共享 chunker 行为),query-level 独立采样会低估
        CI 带宽. 文档级聚类 bootstrap 每次重采样以 doc 为单位整体进入/出 ——
        这样 effective N 是 unique doc 数而非 query 数,CI 反映真实方差.

    Args:
        per_case: List of {doc_id_key: ..., value_key: float}
        value_key: 取值字段名(如 'jaccard' / 'faithfulness_delta')
        doc_id_key: 文档分组字段(默认 'doc_id')
        n_resamples: bootstrap 重采样次数
        ci: 置信水平(0.95 = 95% CI)
        seed: random seed

    Returns:
        {
          'query_level_mean': ...,  # naive mean
          'unique_docs': ...,
          'doc_clustered_mean': ...,
          'doc_clustered_ci_lower': ...,
          'doc_clustered_ci_upper': ...,
        }
    """
    if not per_case:
        return {
            'query_level_mean': float('nan'),
            'unique_docs': 0,
            'doc_clustered_mean': float('nan'),
            'doc_clustered_ci_lower': float('nan'),
            'doc_clustered_ci_upper': float('nan'),
        }

    # group by doc
    by_doc: Dict[str, List[float]] = defaultdict(list)
    for r in per_case:
        doc = r.get(doc_id_key, "?")
        v = r.get(value_key)
        if v is None:
            continue
        by_doc[doc].append(float(v))

    docs = sorted(by_doc.keys())
    n_docs = len(docs)

    # query-level (naive) mean
    all_vals = [v for vs in by_doc.values() for v in vs]
    q_mean = sum(all_vals) / len(all_vals) if all_vals else float('nan')

    if n_docs == 0:
        return {
            'query_level_mean': q_mean,
            'unique_docs': 0,
            'doc_clustered_mean': float('nan'),
            'doc_clustered_ci_lower': float('nan'),
            'doc_clustered_ci_upper': float('nan'),
        }

    # 每 doc 的 mean(再做 doc-level bootstrap)
    doc_means = [sum(by_doc[d]) / len(by_doc[d]) for d in docs]

    rng = random.Random(seed)
    boot_means: List[float] = []
    for _ in range(n_resamples):
        sample = [rng.choice(doc_means) for _ in range(n_docs)]
        boot_means.append(sum(sample) / n_docs)

    boot_means.sort()
    alpha = (1 - ci) / 2
    lo = boot_means[int(alpha * n_resamples)]
    hi = boot_means[int((1 - alpha) * n_resamples)]
    dc_mean = sum(doc_means) / n_docs

    return {
        'query_level_mean': q_mean,
        'unique_docs': n_docs,
        'doc_clustered_mean': dc_mean,
        'doc_clustered_ci_lower': lo,
        'doc_clustered_ci_upper': hi,
    }


# ── fabrication asymmetric + severity(v3 #10, v3.1 #5)──

def fabrication_severity_test(
    off_per_case: Sequence[Dict[str, Any]],
    on_per_case: Sequence[Dict[str, Any]],
    *,
    fab_key: str = "fabrication",
    severity_key: str = "fabrication_severity",
) -> Dict[str, Any]:
    """fabrication asymmetric + severity 分级测试.

    Args:
        off_per_case / on_per_case: per qid 评测结果列表(同序对应)
        fab_key: fabrication bool 字段
        severity_key: severity 字段(HIGH/MEDIUM/LOW)

    Returns:
        {
          'on_only_high': int,    # ON 新增 HIGH 严重 fabrication 数
          'off_only_high': int,
          'on_only_medium_plus': int,  # MEDIUM + HIGH
          'off_only_medium_plus': int,
          'high_severity_block': bool,    # ON-only HIGH > 0 → 阻断(v3.1 #5)
          'asymmetric_violation': bool,   # ON-only MEDIUM+ > OFF-only(v3 #10)
        }
    """
    assert len(off_per_case) == len(on_per_case), "OFF/ON 序列长度必须一致(paired)"

    on_only_high = 0
    off_only_high = 0
    on_only_mp = 0
    off_only_mp = 0

    for off, on in zip(off_per_case, on_per_case):
        off_fab = bool(off.get(fab_key))
        on_fab = bool(on.get(fab_key))
        off_sev = (off.get(severity_key) or "").upper()
        on_sev = (on.get(severity_key) or "").upper()

        if on_fab and not off_fab:
            if on_sev == "HIGH":
                on_only_high += 1
            if on_sev in ("HIGH", "MEDIUM"):
                on_only_mp += 1
        if off_fab and not on_fab:
            if off_sev == "HIGH":
                off_only_high += 1
            if off_sev in ("HIGH", "MEDIUM"):
                off_only_mp += 1

    return {
        'on_only_high': on_only_high,
        'off_only_high': off_only_high,
        'on_only_medium_plus': on_only_mp,
        'off_only_medium_plus': off_only_mp,
        'high_severity_block': on_only_high > 0,           # v3.1 #5 一票阻断
        'asymmetric_violation': on_only_mp > off_only_mp,  # v3 #10
    }
