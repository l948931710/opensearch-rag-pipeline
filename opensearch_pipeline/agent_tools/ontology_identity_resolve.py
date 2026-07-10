# -*- coding: utf-8 -*-
"""
ontology_identity_resolve.py — 受治理动作「铸/确认身份别名」（门 A；本体 P0 PR8）。

**LOW_WRITE + approval_policy="always"**：Policy 风险基线（policy.py 只减不增）把任何授予
上收为 REQUIRE_APPROVAL——模型提案本工具**必然挂起**走 v2 审批四处置，绝无绕行。
审批路由不走"发起人部门"，走 **per-attr steward**（approval_store 的 approver_scope
解析器 seam：本工具构造时注册，按 stewardship scope 裁决；scope 未登记/解析失败 →
'' = 仅 kb_admin 可审，fail-closed）。

写语义（服务端受控，S1 四路径之一）：
- 同 (namespace, norm) 已有 active 且指向同一目标（含 revision 一致）→ **幂等成功**
  （审批重放/重试不重复副作用；ToolExecutor 层另有 uk_tool_idem 兜底）；
- 已有 active 指向**其它**目标 → 拒绝并导流 console 工作台纠错（改指/退役）——
  受治理动作绝不静默覆盖既有事实；
- 落成后 best-effort 闭环同 (ns,norm) 的 open resolution_case（fail-open）。

⚠️ 刻意不进 build_default_registry——接线+提示词+L7 重冻集中 PR13（有未接线守护单测）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from opensearch_pipeline.agent_tools.ontology_resolve import _visible_title

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

logger = logging.getLogger(__name__)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "namespace": {
            "type": "string", "minLength": 1,
            "description": "编号所属命名空间：u8 / internal / customer:<客户名> / "
                           "supplier:<供应商名> / material_grade / lab_sample / lot_code",
        },
        "value": {
            "type": "string", "minLength": 1,
            "description": "要正式确认的原始编号或名称",
        },
        "target_object_id": {
            "type": "string", "minLength": 1,
            "description": "确认指向的 canonical 对象 ID（先用 ontology_resolve 查询候选）",
        },
        "target_revision": {
            "type": "string",
            "description": "可选：改模版本轴（如 r2——改模变体指向同一 Product 的不同版本）",
        },
        "relation": {
            "type": "string", "enum": ["alias", "variant", "equivalent"],
            "description": "映射关系（缺省 alias）",
        },
        "note": {"type": "string", "description": "确认理由（留档）"},
    },
    "required": ["namespace", "value", "target_object_id"],
    "additionalProperties": False,   # 身份/置信无模型通道：confirmed_by 恒为发起人，ACL 由 ctx
}


class OntologyIdentityResolveTool:
    """EnterpriseTool：把源系统编号正式确认为 canonical 对象的别名（恒需审批）。"""

    spec = ToolSpec(
        name="ontology_identity_resolve",
        version="1.0.0",
        description="把一个源系统编号/名称正式确认（铸）为企业统一对象的别名映射。这是受治理"
                    "写动作：提交后会挂起等相应 steward 审批，批准后才生效为检索与计算的依据。"
                    "仅在用户明确要求建立/确认编号归属时使用；查询请用 ontology_resolve。",
        input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object"},
        risk_level=RiskLevel.LOW_WRITE,
        permission_scope="ontology.identity.resolve",
        data_classification="internal",
        idempotency="key_required",
        side_effects=True,
        approval_policy="always",
        owner_team="platform",
    )

    def __init__(self, store=None):
        self._store = store            # 测试注入；缺省惰性 RDS
        from opensearch_pipeline.agent_runtime.approval_store import (
            register_approver_scope_resolver)
        register_approver_scope_resolver(self.spec.name, self._approver_scope)

    def _get_store(self):
        if self._store is None:
            from opensearch_pipeline.ontology.store import RDSOntologyStore
            self._store = RDSOntologyStore()
        return self._store

    # ── 审批路由（approval_store seam 回调）────────────────────────────────
    def _approver_scope(self, ctx: "ExecutionContext", args: Dict[str, Any]) -> Optional[str]:
        """per-attr steward：目标对象类型 + namespace → stewardship 裁决。
        None（scope 未登记）→ seam 收敛为仅 kb_admin；异常由 seam fail-closed。"""
        from opensearch_pipeline.ontology.stewardship import resolve_steward
        store = self._get_store()
        obj = store.get_object(str(args.get("target_object_id") or "")) or {}
        hit = resolve_steward(store.list_stewardship(),
                              namespace=str(args.get("namespace") or "") or None,
                              object_type=obj.get("object_type"))
        return hit["steward_dept"] if hit else None

    # ── 执行（审批放行后由 ToolExecutor 驱动）──────────────────────────────
    def run(self, ctx: "ExecutionContext", args: Dict[str, Any],
            idempotency_key: Optional[str] = None) -> ToolResult:
        self.spec.validate_args(args)
        namespace = args["namespace"]
        raw = args["value"]
        target_id = args["target_object_id"]
        revision = args.get("target_revision")
        relation = args.get("relation") or "alias"

        from opensearch_pipeline.ontology.normalize import normalize
        from opensearch_pipeline.ontology.store import DuplicateActiveIdentifier
        try:
            norm = normalize(namespace, raw)
        except ValueError as e:
            return ToolResult.fail(f"参数非法: {e}")

        store = self._get_store()
        target = store.get_object(target_id)
        if target is None:
            return ToolResult.fail("目标对象不存在（先用 ontology_resolve 查询候选）")
        if target["status"] != "active":
            return ToolResult.fail(f"目标对象非 active（{target['status']}），不能作为映射目标")

        existing = store.get_active_identifier(namespace, norm)
        if existing is not None:
            return self._idempotent_or_conflict(ctx, existing, target, revision)

        try:
            identifier_id = store.insert_identifier(
                namespace, raw, norm, target_id, method="manual", relation=relation,
                target_revision=revision, confidence=1.0, confirmed_by=ctx.user_id)
        except DuplicateActiveIdentifier:
            existing = store.get_active_identifier(namespace, norm)   # 竞态：重查归幂等/冲突
            if existing is None:
                return ToolResult.fail("并发冲突（别名刚被处置），请重试")
            return self._idempotent_or_conflict(ctx, existing, target, revision)
        except Exception as e:   # noqa: BLE001 — 存储失败以 ToolResult 表达
            return ToolResult.fail(f"身份确认落库失败: {e}")

        closed_case = None
        try:   # best-effort 闭环工作台 open case（fail-open：闭不掉不影响确认本身）
            case = store.get_open_case(namespace, norm)
            if case and store.resolve_case(case["case_id"], identifier_id=identifier_id,
                                           by=ctx.user_id,
                                           note=args.get("note") or "经受治理动作确认"):
                closed_case = case["case_id"]
        except Exception:   # noqa: BLE001
            logger.warning("open case 闭环失败（fail-open）", exc_info=True)

        title = _visible_title(target.get("title"), target.get("data_classification"),
                               target.get("owner_dept"), set(ctx.acl_groups or ()))
        rev_txt = f"（版本 {revision}）" if revision else ""
        return ToolResult.ok(
            content=[ContentBlock.of_text(
                f"已确认：{namespace} 编号 {raw!r} → {title or target.get('canonical_ref')} "
                f"[{target.get('canonical_ref')}]{rev_txt}，即刻生效为正式映射。")],
            receipt={"identifier_id": identifier_id, "namespace": namespace,
                     "norm_value": norm, "target_object_id": target_id,
                     "target_revision": revision, "relation": relation,
                     "closed_case_id": closed_case, "idempotent": False})

    def _idempotent_or_conflict(self, ctx: "ExecutionContext", existing: Dict[str, Any],
                                target: Dict[str, Any], revision: Optional[str]) -> ToolResult:
        same_target = existing["target_object_id"] == target["object_id"]
        same_rev = (existing.get("target_revision") or None) == (revision or None)
        if same_target and same_rev:
            title = _visible_title(target.get("title"), target.get("data_classification"),
                                   target.get("owner_dept"), set(ctx.acl_groups or ()))
            return ToolResult.ok(
                content=[ContentBlock.of_text(
                    f"该编号已是 {title or target.get('canonical_ref')} "
                    f"[{target.get('canonical_ref')}] 的正式映射（幂等，无需重复确认）。")],
                receipt={"identifier_id": existing["identifier_id"],
                         "target_object_id": target["object_id"],
                         "target_revision": existing.get("target_revision"),
                         "idempotent": True})
        return ToolResult.fail(
            "该编号已有正式映射指向其它对象/版本——受治理动作不覆盖既有事实，"
            "请在 console 工作台（本体消解 tab）走纠错：改指或退役后重确认。")
