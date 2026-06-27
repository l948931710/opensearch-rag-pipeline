# -*- coding: utf-8 -*-
"""access_grants.py — Phase D 跨部门检索授权的【唯一聚合注入点】。

事实来源（authority）= `fuling_knowledge.kb_access_request`（status='approved'）。
`chunk_meta.allowed_depts`（RDS）与 HA3 `allowed_depts` 字段都只是其【物化投影】——
任何写/重建路径（普通 ingestion / 升版 / re-chunk / HA3 rebuild / 回填脚本）都必须经本模块
解析 allowed_depts 再写，否则文档一旦重建就丢授权（Phase D 约束 2：单一注入点）。

授权值 = 被授权检索本文档的【用户组码】集合（= 申请人 managed_owner_depts，组代码粒度），
经写组码白名单（kb_authz._valid_owner_depts = retriever._VALID_ACL_GROUPS）校验 + 去重 + 稳定
排序；未知/非白名单码 **fail-closed 丢弃** 并显式 warning（约束 6：不静默吞）。授权按逻辑 doc_id
生效、自动跟随 current version（约束 3：调用方按 current-version 关系定位 chunk，本模块只按 doc_id 聚合）。
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List

logger = logging.getLogger(__name__)


def resolve_allowed_depts(doc_ids: Iterable[str], cursor) -> Dict[str, List[str]]:
    """聚合给定 doc_id 集的 approved 跨部门检索授权 → {doc_id: [组码...]}。

    - 只返回【有】approved 授权的 doc（无授权的 doc 不在返回里 → 调用方按空 [] 处理）。
    - 组码经白名单 + 去重 + 稳定排序；非白名单码丢弃并 per-doc warning。
    - cursor：调用方提供的 `fuling_knowledge` 库游标（连接/事务由调用方掌控，本模块不建池）。
    """
    ids = [d for d in dict.fromkeys(doc_ids) if d]   # 去重保序
    if not ids:
        return {}
    from opensearch_pipeline.kb_authz import sanitize_owner_depts, _valid_owner_depts
    whitelist = _valid_owner_depts()
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(
        "SELECT doc_id, requester_depts FROM fuling_knowledge.kb_access_request "
        f"WHERE status='approved' AND doc_id IN ({placeholders})",
        tuple(ids),
    )
    raw: Dict[str, List[str]] = {}
    for row in cursor.fetchall():
        doc_id, rdepts = row[0], row[1]
        if not doc_id:
            continue
        raw.setdefault(doc_id, []).extend((rdepts or "").split(","))

    out: Dict[str, List[str]] = {}
    for doc_id, codes in raw.items():
        clean = sanitize_owner_depts(codes)                 # 白名单内、去重、有序（单一净化口径）
        dropped = sorted({
            c.strip() for c in codes
            if c.strip() and c.strip() not in whitelist
        })
        if dropped:
            logger.warning(
                "kb_access_request doc=%s 含非白名单授权组码（fail-closed 丢弃、不放行）: %s",
                doc_id, dropped,
            )
        if clean:
            out[doc_id] = clean
    return out


def resolve_allowed_depts_one(doc_id: str, cursor) -> List[str]:
    """单文档便利：该 doc 的授权组码列表（无授权 → []）。"""
    if not doc_id:
        return []
    return resolve_allowed_depts([doc_id], cursor).get(doc_id, [])
