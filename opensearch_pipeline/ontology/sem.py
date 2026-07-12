# -*- coding: utf-8 -*-
"""
sem.py — PMC-1 语义投影的**唯一读取口**（P0 PR10；S7 授权收紧）。

S7 铁律：sem_packing/sem_stacking 不授通用 fuling_ro——所有读取必须经本模块
（复用 fuling_admin 读路径/店内连接池），**acl_groups 行级过滤在此层 fail-closed**；
未来 readonly_sql 若要碰 sem_*，必须走本层的部门 WHERE 注入，禁止直查视图。

契约：
- **纯读**（S1：本模块永不落库、永不 auto-activate、miss 不入 case——观测入队
  归回填 worker 显式调用）；
- **未消解回落原值标「未确认」**（落地细化 §5.2 降级铁律）：输入编号解析不到
  active 对象 → resolved=False + 原值回显，绝不假装精确；模糊消解（rule/embedding
  候选）不在本层——那是 resolve.py 的在线路径与 PR12 工具编排的事，本层只认
  object_id / canonical_ref / (namespace, raw) 精确别名三种输入；
- **行过滤规则**：spec 行可见 ⟺ bypass_acl（kb_admin 等特权调用方显式声明）
  或 data_classification='public' 或 spec.owner_dept ∈ acl_groups。acl_groups
  为 None/空 → 只见 public（fail-closed）。**不区分「无记录」与「无权限」**
  （防存在性泄露）——两者同样返回 spec=None；
- caller 传**已展开的有效组**（production 伞形→production_* 的展开归上游 ACL 注入，
  与检索侧同源），本层不重复实现展开；
- verified 优先：同 SKU 多 spec 时取 verified 最新，无 verified 取 draft 最新并
  标「估算/未验证——量产前须打样确认」（packing_calc 的落库恒 draft，S8）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, List, Optional, Tuple

from opensearch_pipeline.ontology.authz import can_read_object, visible_title
from opensearch_pipeline.ontology.store import SEM_PROJECTIONS

logger = logging.getLogger(__name__)

__all__ = ["SemAnswer", "lookup_specs"]

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_REF_RE = re.compile(r"^FLP-[A-Z]{1,8}-\d{6,}$")
UNRESOLVED_NOTE = "未确认（编号未消解，回落原值）——请经 steward 工作台确认后再作依据"
DRAFT_NOTE = "为 draft（估算/未验证）——量产前须车间打样确认"


@dataclass
class SemAnswer:
    """一次投影查询的完整回答。specs[domain] 为选中行（ACL 过滤后）或 None。"""

    resolved: bool
    raw_input: str
    namespace: Optional[str] = None
    sku: Optional[Dict[str, Any]] = None          # object_id/canonical_ref/title/object_type
    specs: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)
    spec_counts: Dict[str, int] = field(default_factory=dict)   # 可见行数（含未选中）
    notes: List[str] = field(default_factory=list)


# 行过滤/标题掩码收敛到 authz.py 单一实现（PR-B）；保留薄别名维持本模块内调用点语义
def _row_visible(row: Dict[str, Any], acl: set, bypass_acl: bool) -> bool:
    return can_read_object(row, acl=acl, bypass_acl=bypass_acl)


_PRODUCT_FIELDS = ("product_id", "product_ref", "product_name",
                   "product_owner_dept", "product_classification")


def _mask_product_fields(row: Dict[str, Any], acl: set, bypass_acl: bool) -> Dict[str, Any]:
    """P0-03：product 三字段按 product **自身** owner_dept/data_classification 独立裁决。

    行过滤键（owner_dept/data_classification）绑的是 spec——公开 SKU + 公开 spec +
    confidential product 时 product_id/ref/name 会整行透出给未授权方。此处用视图新投的
    product_owner_dept/product_classification（schema/034）走同一 can_read_object：
    不可读 → product_* 全部置 None（拒绝=当作不存在，ID/ref 本身即泄露）。
    旧视图行（034 前无这两列 → None/None）天然 fail-closed：非 bypass 一律遮蔽，
    升级窗口宁可少给。无关联 product（LEFT JOIN 空）不动。"""
    if not any(row.get(k) is not None for k in ("product_id", "product_ref", "product_name")):
        return row
    prod = {"owner_dept": row.get("product_owner_dept"),
            "data_classification": row.get("product_classification")}
    if can_read_object(prod, acl=acl, bypass_acl=bypass_acl):
        return row
    out = dict(row)
    for k in _PRODUCT_FIELDS:
        out[k] = None
    return out


def _visible_title(title: Optional[str], data_classification: Optional[str],
                   owner_dept: Optional[str], acl: set, bypass_acl: bool) -> Optional[str]:
    return visible_title(title, data_classification, owner_dept,
                         acl=acl, bypass_acl=bypass_acl)


def _pick_best(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """verified 优先，同态取最新（updated_at 缺失时按 spec_ref 铸序兜底，双后端同序）。"""
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda r: (r.get("spec_state") == "verified",
                       str(r.get("spec_updated_at") or ""),
                       str(r.get("spec_ref") or "")),
        reverse=True)[0]


def _spec_provenance_fields(store, row: Dict[str, Any]) -> Dict[str, Any]:
    """P1-10 读出口：spec 行透出 source/version/as_of——「数据从哪来、多新鲜」随答案走。

    source = 值级溯源（golden_provenance_json）的 sor_system 去重列表；
    as_of  = 溯源戳最新时点（无溯源回退对象 updated_at；再无 → None，绝不编时间）；
    version = 对象乐观锁版本（同一 spec 的答案可与后续变更精确对账）。
    富化 fail-open：溯源读不出不拦答案，三字段置 None（graceful degradation 惯例）。"""
    out: Dict[str, Any] = {"source": None, "version": None, "as_of": None}
    try:
        obj = store.get_object(row.get("spec_id")) or {}
        out["version"] = obj.get("version")
        raw = obj.get("golden_provenance_json")
        prov = json.loads(raw) if isinstance(raw, str) else (raw or {})
        stamps = [v for v in prov.values() if isinstance(v, dict)]
        sources = sorted({str(v["sor_system"]) for v in stamps if v.get("sor_system")})
        as_ofs = sorted(str(v["as_of"]) for v in stamps if v.get("as_of"))
        out["source"] = sources or None
        out["as_of"] = (as_ofs[-1] if as_ofs
                        else (str(obj["updated_at"]) if obj.get("updated_at") else None))
    except Exception:   # noqa: BLE001 — 溯源富化绝不拦答案
        logger.warning("spec 溯源富化失败（fail-open，source/as_of 置 None）", exc_info=True)
    return out


def _resolve_target(store, target: str, namespace: Optional[str]) \
        -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """精确消解三通道：ULID → 展示号 → (namespace, raw) active 别名。返回 (对象, 失败注)。"""
    t = (target or "").strip()
    if not t:
        return None, "输入为空"
    if _ULID_RE.match(t):
        obj = store.get_object(t)
        if obj is not None or not namespace:   # 26 位 Crockford 撞形的业务码：带 ns 继续走别名通道
            return obj, None
    if _REF_RE.match(t.upper()):
        return store.get_object_by_ref(t.upper()), None
    if not namespace:
        return None, "原始编号消解需提供 namespace（或先经 ontology_resolve 解析）"
    from opensearch_pipeline.ontology.normalize import normalize
    try:
        norm = normalize(namespace, t)
    except ValueError as e:
        return None, f"参数非法: {e}"
    ident = store.get_active_identifier(namespace, norm)
    if ident is None:
        return None, None
    return store.get_object(ident["target_object_id"]), None


def lookup_specs(store, target: str, *, acl_groups: Optional[Collection[str]],
                 namespace: Optional[str] = None,
                 domains: Collection[str] = ("packing", "stacking"),
                 bypass_acl: bool = False) -> SemAnswer:
    """按已消解身份查箱规/香规投影。target=object_id | canonical_ref | 原始编号(配 namespace)。

    纯读；未消解回落原值；行级 ACL fail-closed（规则见模块 docstring）。
    """
    unknown = [d for d in domains if d not in SEM_PROJECTIONS]
    if unknown:
        raise ValueError(f"未知投影域 {unknown}（可用：{sorted(SEM_PROJECTIONS)}）")
    acl = set(acl_groups or ())
    obj, err = _resolve_target(store, target, namespace)
    ans = SemAnswer(resolved=False, raw_input=target, namespace=namespace)
    if err:
        ans.notes.append(err)
        return ans
    if obj is None or obj.get("status") != "active":
        ans.notes.append(UNRESOLVED_NOTE)
        return ans
    # PR-B（P0-02 验收④）：对象字段出参前先做对象级 ACL——不可读对象与"未消解"同答，
    # 不返回 object_id/canonical_ref/title/type（掩码标题也不给：ID/ref 本身即泄露）。
    if not can_read_object(obj, acl=acl, bypass_acl=bypass_acl):
        ans.notes.append(UNRESOLVED_NOTE)
        return ans

    ans.resolved = True
    ans.sku = {
        "object_id": obj["object_id"], "canonical_ref": obj["canonical_ref"],
        "object_type": obj["object_type"],
        "title": _visible_title(obj.get("title"), obj.get("data_classification"),
                                obj.get("owner_dept"), acl, bypass_acl),
    }
    if obj["object_type"] != "sku":
        ans.notes.append(
            f"该编号解析为 {obj['object_type']}（非 SKU）——箱规/香规挂在 SKU 上，"
            "请先定位具体 SKU（规格）")
        return ans

    for domain in domains:
        rows = store.sem_spec_rows(obj["object_id"], domain)
        visible = [r for r in rows if _row_visible(r, acl, bypass_acl)]
        ans.spec_counts[domain] = len(visible)
        best = _pick_best(visible)
        if best is not None:   # P1-10：选中行透出 source/version/as_of（浅拷贝不改源行）
            best = dict(best)
            best.update(_spec_provenance_fields(store, best))
            # P0-03：product 字段按 product 自身 ACL 独立遮蔽（spec 行可见 ≠ product 可见）
            best = _mask_product_fields(best, acl, bypass_acl)
        ans.specs[domain] = best
        label = "箱规" if domain == "packing" else "香规/堆叠"
        if best is None:
            # 刻意不区分「无记录」与「无权限」（防存在性泄露）
            ans.notes.append(f"无可见的已登记{label}（未登记或不可见）")
        elif best.get("spec_state") != "verified":
            ans.notes.append(f"{label}{DRAFT_NOTE}")
    return ans
