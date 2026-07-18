# -*- coding: utf-8 -*-
"""qa_gap_groups.py — 缺口相似问法语义归组（schema/039+040，pmc 远期立项落地 2026-07-15）。

三块职责，全部围绕「展示层归并、绝不自动关闭」的预注册边界（见 schema/040 头注）：
  1. greedy_group：纯函数贪心归组（脚本生成 qa_gap_semantic_group 用；可离线单测）；
  2. load_group_map：kb_gaps 读侧一次 IN 查询取成员→组映射（fail-open，表缺失即空映射）；
  3. merge_semantic_gaps：把 kb_gaps 已聚合的 open_gaps 按组归并（纯函数，可单测）。

口径纪律：question_hash 一律 contribution.question_hash（对脱敏后落库文本）；
缺口关闭仍走 exact hash（kb_contribution 覆盖判定零改动）——组归并后卡片携带的
question_hash 必须是【真实成员 hash】（优先组代表，代表不在开放成员中则取热度最高成员），
贡献提交只关闭该成员，其余成员下轮照常展示。
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.90   # 保守起步：宁可漏并，绝不错并（错并=把不同问题揉成一张卡）


def semantic_groups_on() -> bool:
    """读侧归并总闸 RAG_QA_GAP_SEMANTIC（默认关——表未生成/未 backfill 前零行为变化）。"""
    return os.environ.get("RAG_QA_GAP_SEMANTIC", "").strip().lower() in ("1", "true", "yes", "on")


def semantic_threshold() -> float:
    try:
        v = float(os.environ.get("RAG_QA_GAP_SEMANTIC_THRESHOLD", str(DEFAULT_THRESHOLD)))
    except ValueError:
        return DEFAULT_THRESHOLD
    return v if 0.0 < v <= 1.0 else DEFAULT_THRESHOLD


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度；零向量/长度不一致 → 0.0（视为不相似，绝不归并）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def greedy_group(members: List[Dict[str, Any]], threshold: float) -> List[Dict[str, Any]]:
    """贪心归组：按 weight（提问次数）降序遍历，与既有组代表余弦 ≥ threshold 即入组，
    否则自立为新组代表。确定性：weight 相同按 hash 字典序（跑两次结果一致，可复算）。

    members: [{"hash": str, "text": str, "vector": List[float], "weight": int}]
    返回表行: [{"question_hash", "group_id", "representative_text", "similarity"}]
    组代表自身也占一行（group_id=自身，similarity=1.0）——读侧 IN 查询单次取全。
    """
    ordered = sorted(members, key=lambda m: (-int(m.get("weight") or 0), str(m.get("hash") or "")))
    reps: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for m in ordered:
        h = m.get("hash") or ""
        if not h:
            continue
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for r in reps:
            sim = cosine(m.get("vector") or [], r.get("vector") or [])
            if best is None or sim > best[0]:
                best = (sim, r)
        if best is not None and best[0] >= threshold:
            rows.append({"question_hash": h, "group_id": best[1]["hash"],
                         "representative_text": best[1].get("text") or "",
                         "similarity": round(best[0], 4)})
        else:
            reps.append(m)
            rows.append({"question_hash": h, "group_id": h,
                         "representative_text": m.get("text") or "", "similarity": 1.0})
    return rows


def load_group_map(cur, op_db: str, hashes: List[str]) -> Dict[str, str]:
    """成员 hash → group_id 映射（一次 IN 查询）。任何异常上抛给调用方按 fail-open 处理。
    只返回【真属于多成员组或有映射行】的 hash——无行=无组，读侧按原 hash 独立展示。"""
    ids = [h for h in (hashes or []) if h]
    if not ids:
        return {}
    ph = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT question_hash, group_id FROM {op_db}.qa_gap_semantic_group"
        f" WHERE question_hash IN ({ph})", tuple(ids))
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r[0] and r[1]}


def merge_semantic_gaps(open_gaps: List[Dict[str, Any]],
                        group_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """按语义组归并 kb_gaps 的开放缺口（纯函数；输入元素形状=kb_gaps 组装的 dict）。

    归并语义：asks 求和（message_id 全局唯一，跨 hash 无重复）、days 取 min、
    kind refusal 优先、pending 任一成员即真、dept 取首个非空；卡片主 hash/msg/raw =
    组代表（代表不在开放成员中则取 asks 最高成员）——保证 question_hash 恒为真实成员，
    贡献关闭语义不变。phrasings=成员数（>1 说明卡片背后有多种问法）。
    """
    if not group_map:
        return open_gaps
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for g in open_gaps:
        key = group_map.get(g.get("hash") or "", g.get("hash") or "")
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(g)
    merged: List[Dict[str, Any]] = []
    for key in order:
        members = by_key[key]
        if len(members) == 1:
            m = dict(members[0])
            m.setdefault("phrasings", 1)
            merged.append(m)
            continue
        # 主成员：组代表若在开放成员中优先；否则 asks 最高（并列取 days 最小）
        head = next((m for m in members if m.get("hash") == key), None)
        if head is None:
            head = sorted(members, key=lambda m: (-int(m.get("asks") or 0),
                                                  int(m.get("days") or 0)))[0]
        merged.append({
            "hash": head["hash"], "raw": head["raw"], "msg": head.get("msg", ""),
            "asks": sum(int(m.get("asks") or 0) for m in members),
            "days": min(int(m.get("days") or 0) for m in members),
            "kind": ("refusal" if any(m.get("kind") == "refusal" for m in members)
                     else head.get("kind", "")),
            "dept": next((m["dept"] for m in members if m.get("dept")), ""),
            "pending": any(m.get("pending") for m in members),
            "phrasings": len(members),
            # 上下文随主成员走（卡片 msg=head 的 message_id，「查看上下文」查的正是它）
            "has_context": bool(head.get("has_context")),
        })
    return merged
