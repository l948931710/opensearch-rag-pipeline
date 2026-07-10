# -*- coding: utf-8 -*-
"""
stewardship.py — 消解/纠错授权的 scope 声明与裁决（外评 S5：授权独立于 attribute_source）。

三种 scope 粒度 + 裁决优先级（specific 优先）：
    attribute（'sku.箱规'） > namespace 全名（'customer:KFC'） > namespace 前缀（'customer'）
    > object_type（'product'）
未命中任何 scope → 返回 None，调用方 fail-closed 到仅 kb_admin 可处置（与 routes/agent.py
审批授权同纪律）。

steward_dept 一律复用既有 ACL 部门组码（retriever._VALID_ACL_GROUPS；新组码=受治理 ACL
变更，须走 user_role→白名单→灰度 的独立 PR，绝不在此处私造）。
"""
from __future__ import annotations

from typing import List, Optional

# 首批种子：PMC-1 试点期的默认授权（复用既有组码：pmc/supply/rd/marketing）
SEED_SCOPES: List[dict] = [
    {"scope_type": "object_type", "scope_key": "product", "steward_dept": "pmc",
     "backup_dept": "rd", "notes": "试点期产品身份确认归 PMC（产品主数据管家岗设立后迁移）"},
    {"scope_type": "object_type", "scope_key": "sku", "steward_dept": "pmc",
     "backup_dept": None, "notes": "包装变体/箱规香规载体"},
    {"scope_type": "object_type", "scope_key": "mold", "steward_dept": "pmc",
     "backup_dept": None, "notes": "模具 interim 登记（Max 切换前）"},
    {"scope_type": "object_type", "scope_key": "material", "steward_dept": "supply",
     "backup_dept": None, "notes": "物料/牌号；采购价 confidential"},
    {"scope_type": "namespace", "scope_key": "lab_sample", "steward_dept": "rd",
     "backup_dept": None, "notes": "样品名判重归研发"},
    {"scope_type": "namespace", "scope_key": "customer", "steward_dept": "marketing",
     "backup_dept": None, "notes": "客户货号别名归营销（前缀 scope，覆盖全部 customer:*）"},
]


def ensure_seeds(store) -> int:
    """把代码内声明 upsert 进 ontology_stewardship（幂等；代码即权威）。"""
    return store.upsert_stewardship(SEED_SCOPES)


def resolve_steward(rows: List[dict], *, object_type: Optional[str] = None,
                    namespace: Optional[str] = None,
                    attribute: Optional[str] = None) -> Optional[dict]:
    """按优先级裁决 steward scope（纯函数，rows=store.list_stewardship()）。

    attribute 形如 'sku.箱规'（object_type.attribute 全键）；namespace 先试全名再试
    冒号前缀。返回命中的 stewardship 行（含 steward_dept/backup_dept），未命中 None。
    """
    index = {(r["scope_type"], r["scope_key"]): r for r in rows}
    if attribute:
        hit = index.get(("attribute", attribute))
        if hit:
            return hit
    if namespace:
        hit = index.get(("namespace", namespace))
        if hit:
            return hit
        prefix = namespace.split(":", 1)[0]
        hit = index.get(("namespace", prefix))
        if hit:
            return hit
    if object_type:
        hit = index.get(("object_type", object_type))
        if hit:
            return hit
    return None
