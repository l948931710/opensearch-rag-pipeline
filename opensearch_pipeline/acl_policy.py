# -*- coding: utf-8 -*-
"""acl_policy.py — node-ACL 的【单一判定点 + 单一投影点】(纯函数,无 DB / 无 HTTP / 无 config)。

设计稿:`docs/permission_node_acl_design_2026-07-27_DRAFT.md`(codex-review 共识 2026-07-28)。

为什么必须先有这个模块(§2.2a 前置重构):ACL 判定当前散落在**至少 5 处**独立实现——
主检索过滤(`retriever._build_permission_filter`)、撤销复核(`retriever._deny_revoked_cross_dept`)、
邻居/step(`retriever._same_permission`)、图片 cosurface、图片签名(`api.py`)。盲区审计已记载
"4+ 份手工同步副本跨 2 存储"(`architecture_blindspot_audit_2026-07-05.md:410`)。**在新增第 6 种
语义之前必须先收敛**,否则 node 语义会再复制 5 份。

两个核心不变量:

1. **投影模式互斥**(`project_doc_acl`):legacy ⇒ (真实 owner, 组码集);node ⇒ (哨兵, 仅 d:<id>)。
   node 文档若同时投影组码,`retriever` 的组码 OR 分支照样放行 —— 哨兵就白设了。
   ⚠️ chunk 的 owner 继承自 `document_meta.owner_dept` 并原样写 chunk_meta,而 stage-2 / materialize
   都只重算 allowed_depts ⇒ **一篇 node 文档升版/re-chunk 就会把真实 owner 写回检索投影轴、
   legacy owner 分支静默复活 = 权限重开**。故所有写/重建路径(ingestion / 升版 / re-chunk /
   materialize / stage-3 reload / reconcile / 全量导出)**必须**经本函数产出投影值。

2. **读判定真值表**(`can_read_doc`):"按当前权威复核"推不出"全部拒绝"——用户若仍被节点权威
   授权,复核会放行。故 `GRANT=false` 对 node 非 public 是**无条件 DENY**,不是"再复核一次"。

⚠️ 值域:节点值写成 `d:<正整数>`。现有组码全部**不含冒号**,两个值域不相交 ⇒ 可复用现有
   `allowed_depts` MULTI_STRING 字段(2026-07-28 实测:现网 24 字段、该字段确为 MULTI_STRING)
   ⇒ **HA3 零 schema 变更、零重建表**。
⚠️ 节点值**绝不可流经** `retriever._sanitize_ha3_filter_value` —— 该净化器会删掉冒号,
   `d:123` 会被打成 `d123`。本模块自行做严格数字解析后拼接。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────
#
# 检索投影轴的 node-模式哨兵。取长保留值而非短的 `_node`:RDS 该列无 CHECK 约束
# (`schema/001:204`),且 owner 可从任意 `raw/<dept>/…` 路径直接提取、无白名单
# (`pipeline_nodes.py:1536-1547`)⇒ 短值有被真实部门名撞上的可能。
# 2026-07-28 现网核验:chunk_meta.owner_dept 值域仅 13 个(全为组码,无下划线前缀),无碰撞。
NODE_OWNER_SENTINEL = "__acl_node_mode_v1__"

# 两种节点值(值域都含冒号 ⇒ 与组码不相交):
#   d:<id>  = 该节点 + 整棵子树   → 匹配用户【祖先链】
#   dx:<id> = 仅直挂本节点的人     → 匹配用户【直属部门】
# 为什么需要后者(2026-07-29 Sam 提出 + 实测):**26 个节点有子部门却仍有直挂人员,
# 合计 172 人(全员 14.6%)** —— 获胜生产中心 38 / 国际贸易部 28 / 国内营销部 20 …
# 纯子树语义下,"要部分子部门 + 直挂人员、但不要其他子部门"无法表达:选父节点太宽
# (带上不想要的子部门),选子部门又漏掉直挂的人。
NODE_VALUE_PREFIX = "d:"
NODE_EXACT_PREFIX = "dx:"

ACL_MODE_LEGACY = "legacy"
ACL_MODE_NODE = "node"
_VALID_MODES = frozenset({ACL_MODE_LEGACY, ACL_MODE_NODE})

PERM_PUBLIC = "public"
PERM_DEPT_INTERNAL = "dept_internal"

# ── 规模上限(2026-07-28 T8 实测校准,非拍脑袋)────────────────────────────────
# 实测现网:119 部门 / 树深最大 5 / 单用户直属部门数最大 5 /
#          祖先链并集(= 过滤器节点 OR 项数)最大 8 · P99 5 · 平均 2.9。
# T1 实测:HA3 过滤表达式 OR 项 ≥4096 / 127KB 仍通过,未见上限 ⇒ 下列值有 ~128× 余量。
MAX_DIRECT_DEPTS = 8        # 单用户直属部门数(实测 5)
MAX_CHAIN_DEPTH = 15        # 单链深度(实测 5;沿用 dept_ancestry._MAX_HOPS_DEFAULT 防环)
MAX_NODE_OR_TERMS = 32      # 过滤器节点 OR 项总数(实测 8)
MAX_DOC_NODES = 32          # 单文档授权节点数(一级部门仅 17 个)


# ── 值域编解码(严格,不走净化器)──────────────────────────────────────────────
def format_node_value(dept_id: int, *, exact: bool = False) -> str:
    """dept_id → `d:<id>`(子树)或 `dx:<id>`(仅本节点)。

    **严格**:非正整数一律 ValueError,绝不静默产出坏值。
    """
    n = int(dept_id)
    if n <= 0:
        raise ValueError(f"非法 dept_id: {dept_id!r}")
    return f"{NODE_EXACT_PREFIX if exact else NODE_VALUE_PREFIX}{n}"


def _parse_with_prefix(value: object, prefix: str) -> Optional[int]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s.startswith(prefix):
        return None
    tail = s[len(prefix):]
    if not tail.isdigit():          # isdigit ⇒ 排除负号/小数点/空白/全角
        return None
    n = int(tail)
    return n if n > 0 else None


def parse_node_value(value: object) -> Optional[int]:
    """`d:<id>` → dept_id(**子树语义**);其余一律 None(含 `dx:` —— 那是另一种语义)。

    **不做任何"净化后接受"** —— 与组码路径的 `_sanitize_ha3_filter_value`(删非法字符后
    继续用)是刻意相反的策略:节点值是服务端自产的结构化标识,任何偏差都该丢弃 + 告警,
    而不是修补。
    """
    if isinstance(value, str) and value.strip().startswith(NODE_EXACT_PREFIX):
        return None                 # `dx:` 归 parse_exact_node_value,绝不混淆两种语义
    return _parse_with_prefix(value, NODE_VALUE_PREFIX)


def parse_exact_node_value(value: object) -> Optional[int]:
    """`dx:<id>` → dept_id(**仅直挂本节点**语义);其余一律 None。"""
    return _parse_with_prefix(value, NODE_EXACT_PREFIX)


def normalize_node_ids(raw: Iterable[object], *, limit: int = MAX_DOC_NODES) -> Tuple[List[int], bool]:
    """任意入参 → (去重保序的正整数节点列表, overflow)。非法项静默丢弃并计数告警。

    overflow=True 表示**超过 limit**;调用方按场景处置:
      · 写侧(管理员提交)⇒ 返回 422 **拒绝**,绝不静默截断后让 UI 以为已完整保存;
      · 读侧(祖先链)⇒ **丢弃整个节点通道**并告警,且**绝不回退 legacy 口径**
        (否则超限反而换到一条更宽的通道)。
    """
    out: List[int] = []
    seen: Set[int] = set()
    dropped = 0
    for item in raw or []:
        n: Optional[int] = None
        if isinstance(item, int) and not isinstance(item, bool):
            n = item if item > 0 else None
        elif isinstance(item, str):
            n = parse_node_value(item)
            if n is None and item.strip().isdigit():
                n = int(item.strip()) or None
        if n is None:
            dropped += 1
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    if dropped:
        logger.warning("normalize_node_ids: 丢弃 %d 个非法节点值(fail-closed,不修补)", dropped)
    return out, len(out) > limit


# ── 数据结构 ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AclContext:
    """一次请求的读身份。由 API / 钉钉机器人两条入口统一构造(取代裸传组码列表)。

    ancestor_dept_ids:用户全部直属部门的**物理祖先链并集**(含自身,不含虚拟根)。
      ⚠️ 由 `dept_ancestry.resolve_ancestor_chains` 产出 —— 那是**纯物理链**,
      锚点表的显式 `[]`(组码解析停止语义)**不得**截断它,否则授权根节点将覆盖不到
      该部门,直接违反纯子树语义。
    node_channel_ok:False ⇒ 祖先链不可信(组织快照过期/父链查询失败/超限)⇒ 节点通道
      整体不可用,fail-closed 仅 public。
    org_wide_reader:全库读角色(如总经办的 `*` 哨兵)。⚠️ 它**不豁免** GRANT 闸门。
    """
    groups: Tuple[str, ...] = ()
    ancestor_dept_ids: Tuple[int, ...] = ()
    direct_dept_ids: Tuple[int, ...] = ()      # 直属部门(不含祖先)——匹配 `dx:` 仅本节点授权
    node_channel_ok: bool = True
    org_wide_reader: bool = False


@dataclass(frozen=True)
class DocAcl:
    """一篇文档的 ACL 权威视图(由 access_grants 从两个权威源分别解析后组装)。

    ⚠️ **两个权威源分别解析、各自校验**,只在 RDS/HA3 投影边界才编码成混合集合 ——
    绝不能先把组码与 `d:` 值混进一个列表再送进组码白名单(那样 `d:` 会被静默丢弃)。
    """
    mode: str = ACL_MODE_LEGACY
    permission_level: str = ""
    owner_dept: str = ""                       # legacy 权威;node 模式忽略
    groups: Tuple[str, ...] = ()               # legacy:approved 跨部门组码
    node_ids: Tuple[int, ...] = ()             # node:授权节点(**子树**语义,配 `d:`)
    exact_node_ids: Tuple[int, ...] = ()       # node:授权节点(**仅本节点**语义,配 `dx:`)


class InvalidFlagPosture(RuntimeError):
    """`GRANT=true && ENFORCE=false` —— 非法运行姿态,必须启动即失败。"""


def assert_flag_posture(*, grant_enabled: bool, enforce_enabled: bool) -> None:
    """启动期校验。ENFORCE 是"node 文档非 public 命中一律按当前权威复核"的常开闸门,
    **不得随正向开关一起关**:投影未收敛时 HA3 里可能仍是旧真实 owner、legacy approved
    组码也可能还在,只关正向分支反而让这些旧通道复活。"""
    if grant_enabled and not enforce_enabled:
        raise InvalidFlagPosture(
            "RAG_NODE_ACL_GRANT=true 时 RAG_NODE_ACL_ENFORCE 必须为 true"
            "(否则 node 文档的旧 owner/旧组码通道会在投影未收敛期复活)")


# ── 投影(唯一注入点)──────────────────────────────────────────────────────────
def project_doc_acl(
    mode: str,
    real_owner: str,
    groups: Sequence[str] = (),
    node_ids: Sequence[int] = (),
    exact_node_ids: Sequence[int] = (),
) -> Tuple[str, List[str]]:
    """(mode, 真实 owner, 组码, 子树节点, 仅本节点) → (检索投影轴 owner_dept, allowed_depts)。

    **模式互斥**:
      legacy → (真实 owner, [组码...])
      node   → (哨兵,       [d:<id>... , dx:<id>...])   ← 绝不含组码

    未知 mode ⇒ 按 legacy 处理并告警(fail-safe:退回现状语义,绝不误升级为 node)。
    """
    m = (mode or ACL_MODE_LEGACY).strip().lower()
    if m not in _VALID_MODES:
        logger.warning("project_doc_acl: 未知 acl_mode=%r,按 legacy 投影", mode)
        m = ACL_MODE_LEGACY
    if m == ACL_MODE_NODE:
        sub, _ov1 = normalize_node_ids(node_ids)
        exact, _ov2 = normalize_node_ids(exact_node_ids)
        return NODE_OWNER_SENTINEL, (
            [format_node_value(i) for i in sub]
            + [format_node_value(i, exact=True) for i in exact]
        )
    clean = [g for g in (str(x).strip() for x in groups or ()) if g]
    seen: Set[str] = set()
    ordered = [g for g in clean if not (g in seen or seen.add(g))]
    return (real_owner or ""), ordered


# ── 读判定(唯一判定点)────────────────────────────────────────────────────────
def _legacy_readable(ctx: AclContext, doc: DocAcl) -> bool:
    """legacy 语义:owner 分支(含 production 伞形/marketing 非对称展开)或组码授权分支。"""
    if not ctx.groups:
        return False
    try:                                    # 惰性导入:owner 展开 taxonomy 的单一来源
        from opensearch_pipeline.retriever import _expand_groups_to_owners
    except Exception:                       # noqa: BLE001 — 取不到 ACL 模型 ⇒ fail-closed
        logger.warning("_legacy_readable: 取不到 owner 展开表,fail-closed")
        return False
    owners = set(_expand_groups_to_owners(list(ctx.groups)))
    if doc.owner_dept and doc.owner_dept in owners:
        return True
    return bool(set(ctx.groups) & set(doc.groups))


def can_read_doc(
    ctx: AclContext,
    doc: DocAcl,
    *,
    grant_enabled: bool,
    enforce_enabled: bool,
) -> bool:
    """统一读判定。主命中复核 / 撤销复核 / 邻居 / step / cosurface / 图片(重)签名 /
    历史回放 / 本地 OpenSearch 回退 —— **全部走这一个函数**。

    真值表(node 模式 + 非 public):
        ENFORCE=false → InvalidFlagPosture(启动期即拦,此处兜底再抛)
        GRANT=false   → DENY(**无条件**,不调权威判定;org_wide_reader 亦不例外)
        GRANT=true    → 祖先链 ∩ 授权节点 ≠ ∅,或 org_wide_reader
    其余:
        public                → 放行(与模式无关)
        restricted / 未知级别 → 拒绝(fail-closed)
        legacy + dept_internal→ 组码语义(owner 展开 + 组码授权)
        节点通道不可信        → 仅 public
    """
    perm = (doc.permission_level or "").strip().lower()
    if perm == PERM_PUBLIC:
        return True
    if perm != PERM_DEPT_INTERNAL:
        return False                        # restricted / 空 / 未知 ⇒ fail-closed

    mode = (doc.mode or ACL_MODE_LEGACY).strip().lower()
    if mode not in _VALID_MODES:
        logger.warning("can_read_doc: 未知 acl_mode=%r ⇒ 仅 public", doc.mode)
        return False                        # 读 acl_mode 失败 ⇒ 只留 public

    if mode == ACL_MODE_LEGACY:
        return _legacy_readable(ctx, doc)

    # ── node 模式 ──
    if not enforce_enabled:
        raise InvalidFlagPosture(
            "node 模式文档在 ENFORCE=false 下被判定 —— 非法运行姿态(应在启动期拦下)")
    if not grant_enabled:
        return False                        # 无条件 DENY:回滚期的真 public-only
    if not ctx.node_channel_ok:
        return False                        # 祖先链不可信 ⇒ fail-closed
    if ctx.org_wide_reader:
        return True                         # 全库读角色(总经办 `*`),仅在 GRANT 开时
    # 子树授权配【祖先链】;仅本节点授权配【直属部门】—— 两条并集
    if set(ctx.ancestor_dept_ids) & set(doc.node_ids):
        return True
    return bool(set(ctx.direct_dept_ids) & set(doc.exact_node_ids))


# ── 过滤器项(供 retriever 拼 HA3 表达式)──────────────────────────────────────
def node_filter_terms(ctx: AclContext) -> List[str]:
    """祖先链 → HA3 过滤器的节点值列表(已去重、已限流)。

    超限 ⇒ **返回空并告警**(丢弃整个节点通道),**不截断取前 N 项**(顺序不稳定),
    **也绝不回退 legacy 口径**。判定式是纯正向的"交集非空",通道被丢只会**少命中**,
    不可能多命中 —— 前提正是"不回退到另一条更宽的通道"。
    """
    if not ctx.node_channel_ok:
        return []
    anc, ov1 = normalize_node_ids(ctx.ancestor_dept_ids, limit=MAX_NODE_OR_TERMS)
    direct, ov2 = normalize_node_ids(ctx.direct_dept_ids, limit=MAX_NODE_OR_TERMS)
    terms = ([format_node_value(i) for i in anc]
             + [format_node_value(i, exact=True) for i in direct])
    if ov1 or ov2 or len(terms) > MAX_NODE_OR_TERMS:
        logger.warning("node_filter_terms: 节点项 %d 超上限 %d ⇒ 丢弃整个节点通道(fail-closed)",
                       len(terms), MAX_NODE_OR_TERMS)
        return []
    return terms
