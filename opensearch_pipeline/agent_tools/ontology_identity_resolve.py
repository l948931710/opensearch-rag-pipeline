# -*- coding: utf-8 -*-
"""
ontology_identity_resolve.py — 受治理动作「铸/确认身份别名」（门 A；本体 P0 PR8）。

**LOW_WRITE + approval_policy="always"**：Policy 风险基线（policy.py 只减不增）把任何授予
上收为 REQUIRE_APPROVAL——模型提案本工具**必然挂起**走 v2 审批四处置，绝无绕行。
审批路由不走"发起人部门"，走 **per-attr steward**（approval_store 的 approver_scope
解析器 seam：本工具构造时注册，按 stewardship scope 裁决，scope 行带 backup_dept 时
输出 CSV "steward,backup"（P1-11）；scope 未登记/解析失败 → '' = 仅 kb_admin 可审，
fail-closed）。

**发起人对象级 ACL（P0-B）**：propose（_approver_scope 携 ctx 时）与真正落库（run）
都以服务端注入的 ctx.acl_groups 过 **authz.can_mutate_identity**（与工作台
confirm/repoint/merge 同一实现）——目标不可见与不存在**同答**；审批人的职责范围
（approval_scope 重验）只是额外条件，绝不替代发起人自身的对象权限。

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
from opensearch_pipeline.ontology.authz import (
    MASKED_TITLE,
    can_mutate_identity,
    visible_title as _authz_visible_title,
)

# P0-B②：不可见与不存在**同答**的唯一失败文案（can_mutate_identity 对 None 目标与
# 不可读目标返回同一原因，这里再归并成同一串出参——错误消息/结构逐字节一致）
_TARGET_NOT_FOUND_MSG = "目标对象不存在（先用 ontology_resolve 查询候选）"


def _requester_acl(ctx) -> set:
    """requester 对象级 ACL = 服务端注入的 ctx.acl_groups（ExecutionContext 服务端构造、
    请求体不可伪造）。**无 kb_admin bypass**——agent 通道与 ontology_resolve 读工具同纪律，
    特权操作走工作台（routes/ontology.py 才有 _reader_acl 的 bypass 语义）。"""
    return set(getattr(ctx, "acl_groups", ()) or ())


def _gate_target(target, acl: set) -> Optional[str]:
    """P0-B①：发起人对目标对象的写闸——与工作台 confirm/repoint/merge **同一个**
    authz.can_mutate_identity（单一授权实现）。返回 None=放行，否则统一化的失败文案；
    不可见/不存在归并为 _TARGET_NOT_FOUND_MSG（防存在性泄露），其余原因原样带出。
    审批人 scope 校验只是**额外**条件（run 内保留），从不替代发起人自身的对象权限。"""
    reason = can_mutate_identity(target, acl=acl, bypass_acl=False)
    if reason is None:
        return None
    if "不存在" in reason or "不可见" in reason:
        return _TARGET_NOT_FOUND_MSG
    return f"{reason}，不能作为映射目标"


def _display_target(target, acl: set) -> str:
    """成功/幂等消息里的目标展示（P0-B④）：requester ACL 门后仅可读目标可达；
    纵深防御——标题若仍被掩码（语义漂移兜底），**绝不回退 canonical_ref**
    （业务语义标识，掩码即为了藏它的业务身份），用不透明 object_id 占位。"""
    title = _authz_visible_title(target.get("title"), target.get("data_classification"),
                                 target.get("owner_dept"), acl=acl)
    if title == MASKED_TITLE:
        return f"{MASKED_TITLE}（对象 {target.get('object_id')}）"
    name = title or f"对象 {target.get('object_id')}"
    return f"{name} [{target.get('canonical_ref')}]"

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
        "expected_version": {
            "type": "integer", "minimum": 1,
            "description": "可选：提案时目标对象的 version（乐观锁钉住——审批期间对象被改则"
                           "拒绝执行要求重新提案；建议随 ontology_resolve 结果携带）",
        },
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
        risk_level=RiskLevel.HIGH_WRITE,   # PR-C（P0-06）：身份映射改变检索与计算依据——
                                           # 审计必须 fail-closed（executor 现成机制），不按普通低风险写

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
    def _approver_scope(self, ctx: Optional["ExecutionContext"],
                        args: Dict[str, Any]) -> Optional[str]:
        """per-attr steward：目标对象类型 + namespace → stewardship 裁决。

        返回 **CSV**（P1-11）：scope 行带 backup_dept 时为 "steward,backup"
        （如 "pmc,rd"），否则单部门——审批决策侧（routes/agent.py）按 CSV 拆分后
        与 managed 求交。None（scope 未登记）→ seam 收敛为仅 kb_admin；异常由 seam
        fail-closed。

        P0-B（propose 侧同闸）：ctx 非 None（create_request 挂起时点，携发起人身份）
        且发起人过不了 can_mutate_identity → 返回 None（scope 收敛 ''=仅 kb_admin
        可审，域 steward 的队列绝不出现发起人本就无权发起的提案）；审批时点的现算
        （resolve_scope_live）约定传 ctx=None → 跳过该闸，执行侧 run() 的同一
        can_mutate_identity 闸兜底。"""
        from opensearch_pipeline.ontology.stewardship import (
            effective_steward_depts,
            resolve_steward,
        )
        store = self._get_store()
        target = store.get_object(str(args.get("target_object_id") or ""))
        if ctx is not None and _gate_target(target, _requester_acl(ctx)) is not None:
            return None
        hit = resolve_steward(store.list_stewardship(),
                              namespace=str(args.get("namespace") or "") or None,
                              object_type=(target or {}).get("object_type"))
        depts = effective_steward_depts(hit)
        return ",".join(depts) if depts else None

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
        # P0-B①：落库前的发起人对象级 ACL 闸——服务端注入的 ctx.acl_groups 调用与工作台
        # 同一个 authz.can_mutate_identity（可读 / active 一体裁决）。不可见与不存在同答
        # （防存在性泄露）；该闸先于 version/scope 重验——那些错误文案会泄露不可见对象的
        # 存在与版本。批准人的职责范围（下方 scope 校验）只是**额外**条件，不能替代
        # 发起人自身的对象权限（外审动态探针：pmc 发起人 + supply steward 批准 ≠ 放行）。
        denial = _gate_target(target, _requester_acl(ctx))
        if denial is not None:
            return ToolResult.fail(denial)
        # PR-C（P0-06 #4 落库前重验）：
        # ① 提案钉了 expected_version → 审批期间对象任何变更（golden/密级/状态经 version
        #    自增体现）即拒绝执行，要求重新提案；
        # ② 审批放行携带 approval_scope（adjudicator 注入）→ 现算 stewardship 比对，
        #    审批后 steward 变更即拒绝（旧部门的批准不落到新事实上）。
        expected_version = args.get("expected_version")
        if expected_version is not None and int(target.get("version") or 0) != int(expected_version):
            return ToolResult.fail(
                f"目标对象已变更（version {target.get('version')} ≠ 提案时 {expected_version}），"
                "批准失效，请重新发起提案")
        granted_scope = getattr(ctx, "approval_scope", None)
        if granted_scope is not None:
            try:
                live_scope = self._approver_scope(ctx, args) or ""
            except Exception:   # noqa: BLE001 — 现算失败按漂移处理（fail-closed）
                live_scope = None
            if live_scope is None or (granted_scope or "") != live_scope:
                return ToolResult.fail(
                    "stewardship 已变更（审批时 scope 与当前不一致），批准失效，请重新发起提案")

        existing = store.get_active_identifier(namespace, norm)
        if existing is not None:
            return self._idempotent_or_conflict(ctx, existing, target, revision)

        # PR-C（P0-05/06）：铸别名 +（若有）闭 open case **一个事务**；approval_request_id
        # 落事实行（025↔028 双向回链）；confirmed_by 记**真实审批人**（发起人在 receipt）
        approver = getattr(ctx, "approved_by", None)
        try:
            identifier_id, closed_case = store.insert_identifier_closing_case(
                namespace, raw, norm, target_id, method="manual", relation=relation,
                target_revision=revision, confidence=1.0,
                confirmed_by=approver or ctx.user_id,
                approval_request_id=getattr(ctx, "approval_request_id", None),
                note=args.get("note") or "经受治理动作确认")
        except DuplicateActiveIdentifier:
            existing = store.get_active_identifier(namespace, norm)   # 竞态：重查归幂等/冲突
            if existing is None:
                return ToolResult.fail("并发冲突（别名刚被处置），请重试")
            return self._idempotent_or_conflict(ctx, existing, target, revision)
        except Exception:   # noqa: BLE001 — 存储失败以 ToolResult 表达；
            # PR-I（P2）：异常原文不回模型（可能携 SQL/主机名/驱动细节），详情进日志
            logger.exception("身份确认落库失败（详情见日志，不回模型）")
            return ToolResult.fail("身份确认落库失败（存储异常，已记录日志；请稍后重试或联系管理员）")

        rev_txt = f"（版本 {revision}）" if revision else ""
        return ToolResult.ok(
            content=[ContentBlock.of_text(
                f"已确认：{namespace} 编号 {raw!r} → "
                f"{_display_target(target, _requester_acl(ctx))}{rev_txt}，"
                "即刻生效为正式映射。")],
            receipt={"identifier_id": identifier_id, "namespace": namespace,
                     "norm_value": norm, "target_object_id": target_id,
                     "target_revision": revision, "relation": relation,
                     "closed_case_id": closed_case, "idempotent": False,
                     "requested_by": ctx.user_id, "approved_by": approver,
                     "approval_request_id": getattr(ctx, "approval_request_id", None)})

    def _idempotent_or_conflict(self, ctx: "ExecutionContext", existing: Dict[str, Any],
                                target: Dict[str, Any], revision: Optional[str]) -> ToolResult:
        same_target = existing["target_object_id"] == target["object_id"]
        same_rev = (existing.get("target_revision") or None) == (revision or None)
        if same_target and same_rev:
            return ToolResult.ok(
                content=[ContentBlock.of_text(
                    f"该编号已是 {_display_target(target, _requester_acl(ctx))} "
                    "的正式映射（幂等，无需重复确认）。")],
                receipt={"identifier_id": existing["identifier_id"],
                         "target_object_id": target["object_id"],
                         "target_revision": existing.get("target_revision"),
                         "idempotent": True})
        return ToolResult.fail(
            "该编号已有正式映射指向其它对象/版本——受治理动作不覆盖既有事实，"
            "请在 console 工作台（本体消解 tab）走纠错：改指或退役后重确认。")
