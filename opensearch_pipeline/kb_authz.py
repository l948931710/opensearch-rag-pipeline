# -*- coding: utf-8 -*-
"""
kb_authz.py — 知识库管理员【写授权】层（上传 / 升版 / 共享 / 退役）

⚠️ 设计铁律（与读路径解耦，H1 三分原则）：
  本模块**只**负责"谁能写哪个 owner_dept、谁能公开、谁能跨组共享"。它与
  retriever 的【读取】扩展（_expand_groups_to_owners / _DEPT_OWNER_EXPANSION）
  **结构性隔离**——【绝不】import 读扩展函数。原因：读映射是宽松的（marketing
  可读 production 家族），若用它推导写权限，marketing 管理员就能改 production
  文档 —— 越权。写授权必须独立、保守、显式。

三个互不相等的范围（每个 dept_admin）：
  - read_groups → owner_depts   : 能【检索】到哪些 dept_internal（在 retriever，本模块不碰）
  - managed_owner_depts          : 能【上传/升版/退役】的 owner_dept（本模块，来自显式 seed）
  - grantable_owner_depts        : 能【直接共享】给哪些 owner_dept（跨组一律转 kb_admin 审批）

锁定决策（2026-06-23）：
  - 生产事业部"伞组共管"：管理范围统一为 owner_dept='production'（无子线写隔离）。
  - 多叶子→单组"共管同一组池"：marketing/quality/rd 等同组多部门共管同一 owner_dept。
  - 跨组共享 / 公开（permission_level=public）一律需 kb_admin 审批。
  - 管理员身份与其 managed_owner_depts 由【显式名单】seed（user_role.role +
    dept_admin_grant），本模块只做校验与裁决，不从读组推导。

合法 owner_dept 写白名单 = retriever._VALID_ACL_GROUPS（单一来源）。MVP 下新建文档的
owner_dept 取【伞组/组名】粒度（production 而非 production_mold 等历史子线），故写白名单
恰为 10 个组代码。校验一律 fail-closed：未知值丢弃，绝不放行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set, Union

logger = logging.getLogger(__name__)

# ── 角色词表 ──────────────────────────────────────────────────────
ROLE_EMPLOYEE = "employee"
ROLE_DEPT_ADMIN = "dept_admin"
ROLE_KB_ADMIN = "kb_admin"

_VALID_ROLES = frozenset({ROLE_EMPLOYEE, ROLE_DEPT_ADMIN, ROLE_KB_ADMIN})
_ADMIN_ROLES = frozenset({ROLE_DEPT_ADMIN, ROLE_KB_ADMIN})

# ── 合法 permission_level（写侧；与 pipeline_nodes.resolve_permission_level 对齐）──
PERM_PUBLIC = "public"
PERM_DEPT_INTERNAL = "dept_internal"
PERM_RESTRICTED = "restricted"
_VALID_PERMISSION_LEVELS = frozenset({PERM_PUBLIC, PERM_DEPT_INTERNAL, PERM_RESTRICTED})
# 历史别名（与 pipeline_nodes._PERMISSION_ALIAS 一致）
_PERMISSION_ALIAS = {"internal": PERM_DEPT_INTERNAL, "private": PERM_DEPT_INTERNAL}

# owner_dept / 组代码净化白名单字符集（与 retriever._sanitize_ha3_filter_value 同策略：
# 只留字母数字 + 下划线 + 连字符 + 中文，剥离其余以防注入）。
_SANITIZE_RE = re.compile(r"[^\w\-一-鿿]")


def _valid_owner_depts() -> frozenset:
    """写侧合法 owner_dept 白名单（单一来源：retriever._VALID_ACL_GROUPS）。

    惰性 import 避免任何 import 环（retriever 不依赖本模块）。MVP 写粒度为组代码/伞组，
    恰等于 10 个 ACL 组；历史子线 owner（production_mold 等）是【读】可见但【非】新建写目标。
    """
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS

    return _VALID_ACL_GROUPS


def normalize_role(raw: Optional[str]) -> str:
    """把任意 role 入参归一为合法角色；未知/空 → employee（fail-closed，最小权限）。"""
    if not raw or not isinstance(raw, str):
        return ROLE_EMPLOYEE
    r = raw.strip().lower()
    return r if r in _VALID_ROLES else ROLE_EMPLOYEE


def normalize_permission_level(raw: Optional[str]) -> str:
    """归一 permission_level；别名 internal/private → dept_internal；未知/空 → **restricted**（fail-closed）。

    未知/空归一到【最严】级别（restricted = 仅归档、不进检索），让任何误用 fail-closed：即便未来有调用方
    直接存其输出，也绝不会把垃圾/空值静默放成 public 而过度暴露（G8）。真正的写放行仍由 authorize_upload
    独立裁决（dept_admin 设 public 需 kb_admin 审批），不依赖本函数的归一结果。
    """
    if not raw or not isinstance(raw, str):
        return PERM_RESTRICTED
    v = raw.strip().lower()
    v = _PERMISSION_ALIAS.get(v, v)
    return v if v in _VALID_PERMISSION_LEVELS else PERM_RESTRICTED


def sanitize_owner_depts(values: Union[str, Iterable[str], None]) -> List[str]:
    """把任意形态的 owner_dept 入参净化为【白名单内、去重、有序】列表（fail-closed）。

    每项先剥离注入字符，再过写白名单（_valid_owner_depts）。空/None/全非法 → []。
    接受：单字符串、逗号分隔字符串、可迭代（元素本身也可含逗号）。
    """
    if not values:
        return []
    raw: List[str] = []
    if isinstance(values, str):
        raw = values.split(",")
    else:
        for item in values:
            if item is None:
                continue
            raw.extend(str(item).split(","))
    whitelist = _valid_owner_depts()
    out: List[str] = []
    seen: Set[str] = set()
    for d in raw:
        code = _SANITIZE_RE.sub("", (d or "").strip())
        if code and code in whitelist and code not in seen:
            seen.add(code)
            out.append(code)
    return sorted(out)


# ── 身份 ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KbIdentity:
    """知识库写授权身份（由 token claim + DB seed 构造；本模块只消费，不查库）。

    - user_id / name           : 钉钉 staffId / 显示名（审计用）。
    - role                     : employee / dept_admin / kb_admin（已归一）。
    - acl_groups               : 用户【读】权限组列表（仅审计/展示参考；写授权【不】用它推导）。
    - granted_owner_depts      : 【显式 seed】的可管理 owner_dept 列表（dept_admin_grant）。
                                 kb_admin 忽略此字段（拥有全部 owner_dept）。
    """

    user_id: str = ""
    name: str = ""
    role: str = ROLE_EMPLOYEE
    acl_groups: tuple = ()
    granted_owner_depts: tuple = ()

    @staticmethod
    def build(
        user_id: str = "",
        name: str = "",
        role: Optional[str] = None,
        acl_groups: Union[str, Iterable[str], None] = None,
        granted_owner_depts: Union[str, Iterable[str], None] = None,
    ) -> "KbIdentity":
        """归一化构造：role fail-closed，granted_owner_depts 过写白名单。"""
        if isinstance(acl_groups, str):
            groups = tuple(g.strip() for g in acl_groups.split(",") if g.strip())
        elif acl_groups:
            groups = tuple(str(g).strip() for g in acl_groups if str(g).strip())
        else:
            groups = ()
        return KbIdentity(
            user_id=user_id or "",
            name=name or "",
            role=normalize_role(role),
            acl_groups=groups,
            granted_owner_depts=tuple(sanitize_owner_depts(granted_owner_depts)),
        )


def is_admin(identity: KbIdentity) -> bool:
    return identity.role in _ADMIN_ROLES


def can_access_console(identity: KbIdentity) -> bool:
    """是否渲染「知识库管理」入口。仅便利——真正边界永远在每个写接口的服务端校验。"""
    return identity.role in _ADMIN_ROLES


def managed_owner_depts(identity: KbIdentity) -> List[str]:
    """该身份可【上传/升版/退役】的 owner_dept 集合（有序、白名单内）。

    - kb_admin   → 全部合法 owner_dept（跨部门）。
    - dept_admin → 其显式 seed 的 granted_owner_depts（已过白名单）。
    - employee   → []（无写权）。
    """
    if identity.role == ROLE_KB_ADMIN:
        return sorted(_valid_owner_depts())
    if identity.role == ROLE_DEPT_ADMIN:
        return list(identity.granted_owner_depts)
    return []


def grantable_owner_depts(identity: KbIdentity) -> List[str]:
    """该身份可【直接共享】（无需 kb_admin 审批）的 owner_dept 目标集合。

    锁定决策：跨组共享一律 kb_admin 审批 → dept_admin 的"免审批共享面"= 其 managed 集合本身
    （即只能在自己管理的 owner_dept 之间共享；向任何其他组共享都转审批）。kb_admin = 全部。
    """
    return managed_owner_depts(identity)


# ── 裁决结果 ──────────────────────────────────────────────────────
@dataclass
class AuthzDecision:
    """写授权裁决。

    - allowed                  : 请求是否被【允许继续】（False = 硬拒绝，如越权/非法值）。
    - requires_kb_admin_approval: 允许，但需进 kb_admin 审批队列后才上线（如公开 / 跨组共享）。
    - reason                   : 机器可读原因码（审计 / 前端提示）。
    """

    allowed: bool
    requires_kb_admin_approval: bool = False
    reason: str = "ok"


def authorize_upload(
    identity: KbIdentity,
    owner_dept: str,
    permission_level: str,
    share_owner_depts: Union[str, Iterable[str], None] = None,
) -> AuthzDecision:
    """裁决一次上传/升版请求。纯函数，无副作用——服务端在写库前调用，并独立再校验。

    硬拒绝（allowed=False）：
      - 非管理员（not_admin）
      - owner_dept 非法/不在写白名单（invalid_owner_dept）
      - dept_admin 且 owner_dept 不在其 managed 集合（owner_dept_not_managed）
      - permission_level 非法（invalid_permission_level）
    需审批（allowed=True, requires_kb_admin_approval=True）：
      - permission_level=public（防误公开；kb_admin 自身上传除外——其即审批人）
      - 任一 share 目标不在自己 managed（跨组共享）
    其余 → 直接允许。
    """
    if not is_admin(identity):
        return AuthzDecision(False, False, "not_admin")

    owner = _SANITIZE_RE.sub("", (owner_dept or "").strip())
    if not owner or owner not in _valid_owner_depts():
        return AuthzDecision(False, False, "invalid_owner_dept")

    level = (permission_level or "").strip().lower()
    level = _PERMISSION_ALIAS.get(level, level)
    if level not in _VALID_PERMISSION_LEVELS:
        return AuthzDecision(False, False, "invalid_permission_level")

    is_kb_admin = identity.role == ROLE_KB_ADMIN
    managed = set(managed_owner_depts(identity))

    # 写目标必须在管理范围内（kb_admin 拥有全部，已含于 managed）。
    if owner not in managed:
        return AuthzDecision(False, False, "owner_dept_not_managed")

    # 跨组共享：任一 share 目标不在 managed → 需审批（净化后比对，非法目标也算越界）。
    needs_approval = False
    reason = "ok"
    shares = sanitize_owner_depts(share_owner_depts)
    raw_share_count = 0
    if share_owner_depts:
        raw_share_count = (
            len(share_owner_depts.split(","))
            if isinstance(share_owner_depts, str)
            else len(list(share_owner_depts))
        )
    # 净化后数量 < 原始数量 ⇒ 含非法/越界目标；或存在不在 managed 的目标 → 转审批
    if (shares and not set(shares).issubset(managed)) or (raw_share_count and len(shares) < raw_share_count):
        needs_approval = True
        reason = "cross_group_share_requires_kb_admin"

    # 公开：dept_admin 设 public 需 kb_admin 审批；kb_admin 自身即审批人，免审批。
    if level == PERM_PUBLIC and not is_kb_admin:
        needs_approval = True
        reason = "public_requires_kb_admin" if reason == "ok" else reason

    return AuthzDecision(True, needs_approval, reason)


# 退役/恢复【无】独立 authorize_* 纯函数：退役授权 = managed_owner_depts 作用域
# （_kb_can_manage）+「公开文档需 kb_admin」的端点内不对称规则，刻意内联于
# api.py::kb_retire（dept_admin 可退役本部门非 public 文档；public 影响全公司故需
# kb_admin）。不在此提供便利函数，以免与端点产生双口径（曾有 born-dead 的 kb_admin-only
# authorize_retire，语义与端点冲突，已删）。


def audit_managed_grants(granted_owner_depts: Iterable[str]) -> List[str]:
    """暴露 seed 进来但【不在】写白名单的可疑 owner_dept（只读告警，绝不自动放行）。

    类比 retriever.audit_production_owner_taxonomy：净化后被白名单丢弃的项浮出水面，
    供 kb_admin 复核 dept_admin_grant 是否拼写错误 / 引用了未批准的 owner_dept。
    """
    accepted = set(sanitize_owner_depts(granted_owner_depts))
    # accepted = 净化+白名单后保留项；凡【净化后】不在 accepted 的原始项即可疑（拼写错/未批准 owner_dept）。
    # （此前还有一行「accepted 中不在 whitelist 的」二次去重——accepted 恒为 whitelist 子集，故恒空、死代码，已删 B10。）
    suspicious = sorted({
        str(o).strip() for o in (granted_owner_depts or [])
        if str(o).strip() and _SANITIZE_RE.sub("", str(o).strip()) not in accepted
    })
    if suspicious:
        logger.warning(
            "dept_admin_grant 含不在写白名单的 owner_dept（fail-closed：已丢弃，不授予写权）: %s",
            suspicious,
        )
    return sorted(set(suspicious))
