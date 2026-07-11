# -*- coding: utf-8 -*-
"""
authz.py — 本体对象级 ACL 的**单一实现**（PR-B，P0-01/02 收口）。

外审 P0-01 的根因是授权判定散落三处（sem 行过滤 / resolve 工具标题掩码 / 工作台
只有 steward scope 无对象 ACL）且工作台读侧完全没有对象级过滤。本模块收敛为一套
规则，workbench routes、agent tools、sem 三方共用；语义与检索侧 allowed_depts
同一世界观（组码 = kb ACL 组码，caller 传**已展开的有效组**，本层不做伞形展开）。

规则（fail-closed）：
- ``can_read_object``：bypass（kb_admin 等特权，调用方显式声明）恒可；
  data_classification='public' 恒可；internal/confidential 须 owner_dept ∈ acl；
  行缺 owner_dept/classification 列 → 不可见（宁可少给）。
- ``visible_title``：confidential 且无归属部门权限 → 掩码（可见性弱于 read 的
  展示层缓冲——用于「行可见但标题须遮」的投影场景）。
- **拒绝 = 当作不存在**：调用方对不可读对象一律返回 404/None/未消解，
  不区分「无记录」与「无权限」（防存在性泄露）。
- ``can_mutate_identity``：写侧闸 = 来源 scope steward 授权（调用方已过）之上，
  再要求 ①目标对象可读 ②目标 active ③类型一致（期望类型给定时）。任一不满足
  即拒——confirm/repoint/merge 的"只校一侧"漏洞由此闭合。
"""
from __future__ import annotations

from typing import Any, Collection, Dict, List, Optional, Tuple

__all__ = [
    "MASKED_TITLE",
    "acl_read_predicate_sql",
    "can_mutate_identity",
    "can_read_object",
    "visible_title",
]

MASKED_TITLE = "[受限对象]"   # 全系统唯一掩码文案（sem / resolve 工具 / 工作台同源）


def can_read_object(obj: Optional[Dict[str, Any]], *, acl: Collection[str],
                    bypass_acl: bool = False) -> bool:
    """对象/投影行是否对调用方可见。obj=None → False（不可读与不存在同答）。"""
    if obj is None:
        return False
    if bypass_acl:
        return True
    if obj.get("data_classification") == "public":
        return True
    dept = obj.get("owner_dept")
    return bool(dept) and dept in set(acl or ())


def acl_read_predicate_sql(*, acl: Collection[str], bypass_acl: bool = False,
                           column_prefix: str = "") -> Tuple[str, List[Any]]:
    """``can_read_object`` 的 **SQL 谓词形态**（P0-A②③：搜索/计数把 ACL 下推到查询层，
    LIMIT/COUNT 只作用在授权集合上——绝不"先 limit 再逐行 filter"）。

    与 ``can_read_object`` 同模块互相印照（单一来源，防语义漂移——两者必须同步演进）：
    - bypass → ``1=1``（kb_admin 等特权，调用方显式声明）；
    - 其余 → ``data_classification='public' OR owner_dept IN (acl)``；acl 为空只剩 public。
    - NULL 语义与行级判定一致 fail-closed：``NULL='public'`` 为 UNKNOWN（不可见）、
      ``NULL IN (...)`` 为 UNKNOWN（不可见）——行缺列宁可少给。

    返回 ``(where_fragment, params)``；fragment 自带括号，可直接 ``AND`` 拼接。
    column_prefix 供带表别名的查询（如 ``o.``）。
    """
    if bypass_acl:
        return "1=1", []
    pub = f"{column_prefix}data_classification = 'public'"
    depts = sorted({str(d) for d in (acl or ()) if d})   # 去空去重排序：SQL 确定性
    if not depts:
        return f"({pub})", []
    marks = ",".join(["%s"] * len(depts))
    return f"({pub} OR {column_prefix}owner_dept IN ({marks}))", list(depts)


def visible_title(title: Optional[str], data_classification: Optional[str],
                  owner_dept: Optional[str], *, acl: Collection[str],
                  bypass_acl: bool = False) -> Optional[str]:
    """标题掩码：confidential 且无归属部门权限 → MASKED_TITLE（其余原样）。"""
    if title is None or bypass_acl:
        return title
    if data_classification == "confidential" and owner_dept \
            and owner_dept not in set(acl or ()):
        return MASKED_TITLE
    return title


def can_mutate_identity(target_obj: Optional[Dict[str, Any]], *, acl: Collection[str],
                        bypass_acl: bool = False,
                        expected_object_type: Optional[str] = None) -> Optional[str]:
    """身份写动作（confirm/repoint/merge）对目标对象的闸。

    返回 None=放行；否则返回拒绝原因（调用方转 4xx，不泄露超出必要的细节）。
    来源侧 steward scope 授权由调用方先行（_authorize_steward）；本函数补齐
    "目标侧"三闸：可读 / active / 类型一致。
    """
    if not can_read_object(target_obj, acl=acl, bypass_acl=bypass_acl):
        return "目标对象不存在或不可见"
    if target_obj.get("status") != "active":
        return f"目标对象非 active（{target_obj.get('status')}）"
    if expected_object_type and target_obj.get("object_type") != expected_object_type:
        return (f"对象类型不一致（期望 {expected_object_type}，"
                f"目标 {target_obj.get('object_type')}）")
    return None
