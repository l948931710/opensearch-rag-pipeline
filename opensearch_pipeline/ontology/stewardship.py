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


def ensure_seeds(store, *, prune: bool = True) -> dict:
    """desired-state 对账（PR-G，外审 P1「stewardship 不是安全的 desired state」）：
    代码内声明是**唯一权威**——upsert 声明行之外，把库里**未声明**的 scope 显式撤销
    （旧行为只 upsert，从代码里删掉的授权在库里永久残留=幽灵授权）。

    scope 的增删改一律走代码 PR（评审即双人批准）；运行期没有任何合法写入口
    （工作台不写本表），故 prune=True 是安全默认。prune=False 仅供只读 diff 预览。
    返回 diff：{"declared": n, "removed": [(scope_type, scope_key, steward_dept), ...]}。
    """
    import logging
    declared = {(r["scope_type"], r["scope_key"]) for r in SEED_SCOPES}
    try:
        existing = store.list_stewardship()
    except Exception:   # noqa: BLE001 — 表未建（首灌前）：只播种，不对账
        existing = []
    store.upsert_stewardship(SEED_SCOPES)
    removed = []
    for row in existing:
        key = (row["scope_type"], row["scope_key"])
        if key in declared:
            continue
        if prune:
            store.delete_stewardship(*key)
        removed.append((row["scope_type"], row["scope_key"], row.get("steward_dept")))
    if removed:
        logging.getLogger(__name__).warning(
            "stewardship desired-state 对账：%s %d 条未声明 scope %s——授权变更须走代码 PR",
            "已撤销" if prune else "发现（prune=False 未动）", len(removed), removed)
    return {"declared": len(SEED_SCOPES), "removed": removed}


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


def effective_steward_depts(scope_row: Optional[dict]) -> List[str]:
    """scope 命中行 → **有效 steward 部门列表**（P1-11：backup_dept 是代理管家，
    授权语义与 steward_dept 等同——此前 seeds 存了 backup_dept 但授权侧从不认，
    代理机制实际不可用）。

    顺序固定 [steward_dept, backup_dept]（去重、去空）；未命中/None → []。
    审批路由把该列表 join 成 CSV 写 approver_scope（如 "pmc,rd"），
    工作台授权把它与 dept_admin 的 managed 集求交。
    """
    if not scope_row:
        return []
    out: List[str] = []
    for key in ("steward_dept", "backup_dept"):
        dept = scope_row.get(key)
        if dept and dept not in out:
            out.append(dept)
    return out
