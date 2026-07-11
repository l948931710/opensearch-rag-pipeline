# -*- coding: utf-8 -*-
"""
ontology_resolve.py — 身份解析工具（本体 P0；docs/ontology_p0_plan_2026-07-10.md PR4）。

**READ_ONLY 且纯读**（外评 S1）：包装 `OntologyResolver.resolve()`——精确命中返回 canonical
对象，未命中返回候选+置信；**永不写库**（铸别名/确认走受治理 Action `ontology.identity.resolve`，
PR8）。`intent` 不是模型可设参数：本工具恒 `read`（`additionalProperties:false` 拒伪造）——
模型没有任何通道把"写意图"塞进解析来诱导 auto 放行。

对象级 ACL 在**本层**按 ctx 做（服务层 resolver 不感知身份），规则收敛于
ontology.authz（PR-B，P0-02 验收④）：解析目标对调用者不可读（internal/confidential
且 owner_dept ∉ acl_groups）→ 整体降级为"无法解析"，**不返回 object_id/
canonical_ref/title/type**（ID/ref 本身即存在性泄露，"无害代理号"论已被外审否决）；
候选列表同样先过 can_read_object 再出参。

⚠️ 本工具**刻意不进 build_default_registry**——接线与系统提示词/L7 基线重冻集中在 PR13
一次完成（tests/test_ontology_resolve.py 有未接线守护）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock,
    RiskLevel,
    ToolResult,
    ToolSpec,
)

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext

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
            "description": "要解析的原始编号或名称（U8 货号、客户货号、供应商料号、样品名等）",
        },
        "object_type_hint": {
            "type": "string", "enum": ["product", "sku", "mold", "material"],
            "description": "可选：期望的对象类型（缩小候选范围）",
        },
    },
    "required": ["namespace", "value"],
    "additionalProperties": False,   # intent/身份参数无通道：恒 read、ACL 由 ctx 注入
}


class OntologyResolveTool:
    """EnterpriseTool：源系统编号/名称 → canonical 对象（未确认返回候选，绝不写库）。"""

    spec = ToolSpec(
        name="ontology_resolve",
        version="1.0.0",
        description="把任一源系统编号或名称（U8 货号、客户货号、供应商料号、实验样品名）解析为"
                    "企业统一的产品/SKU/模具/物料对象；未确认时返回候选与置信度，需 steward "
                    "确认后才会成为正式映射。本工具只查询、不写入。",
        input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object"},
        risk_level=RiskLevel.READ_ONLY,
        permission_scope="ontology.resolve",
        data_classification="internal",
        owner_team="platform",
    )

    def __init__(self, resolver=None):
        self._resolver = resolver          # 测试注入；缺省惰性建（RDS store + 默认 embedder）

    def _get_resolver(self):
        if self._resolver is None:
            from opensearch_pipeline.ontology.resolve import OntologyResolver
            from opensearch_pipeline.ontology.store import RDSOntologyStore
            self._resolver = OntologyResolver(RDSOntologyStore())
        return self._resolver

    def run(self, ctx: "ExecutionContext", args: Dict[str, Any],
            idempotency_key: Optional[str] = None) -> ToolResult:
        self.spec.validate_args(args)
        namespace = args["namespace"]
        value = args["value"]
        hint = args.get("object_type_hint")
        try:
            # 🔒 intent 服务端硬编码 read（S1）：写确认只能走受治理 Action，模型无通道触达
            res = self._get_resolver().resolve(namespace, value, intent="read",
                                               object_type_hint=hint)
        except ValueError as e:
            return ToolResult.fail(f"解析参数非法: {e}")
        except Exception:   # noqa: BLE001 — 解析失败以 ToolResult 表达；
            # PR-I（P2）：异常原文不回模型（可能携 SQL/主机名/驱动细节），详情进日志
            import logging
            logging.getLogger(__name__).exception("身份解析失败（详情见日志，不回模型）")
            return ToolResult.fail("身份解析失败（内部异常，已记录日志；请稍后重试）")

        acl = set(ctx.acl_groups or ())
        content, receipt = _format_result(res, acl)
        return ToolResult.ok(content=content, receipt=receipt)


def _format_result(res, acl: set):
    from opensearch_pipeline.ontology.authz import can_read_object, visible_title

    lines: List[str] = []
    resolved_readable = res.status == "resolved" and can_read_object(
        {"data_classification": res.data_classification, "owner_dept": res.owner_dept},
        acl=acl)
    # PR-B（P0-02 验收④）：解析目标不可读 → 与"无法解析"同答，零对象字段出参
    if res.status == "resolved" and not resolved_readable:
        lines.append(f"编号 {res.raw_value!r} 无法解析：无正式映射、无可用候选。"
                     "请确认输入是否正确，或提交 steward 登记。")
        receipt = {"status": "unresolved", "namespace": res.namespace,
                   "norm_value": res.norm_value, "object_id": None, "canonical_ref": None,
                   "target_revision": None, "confidence": 0.0, "method": None,
                   "requires_hitl": True, "candidates": []}
        return [ContentBlock.of_text("\n".join(lines))], receipt

    if res.status == "resolved":
        title = visible_title(res.title, res.data_classification, res.owner_dept, acl=acl)
        rev = f"（版本 {res.target_revision}）" if res.target_revision else ""
        lines.append(f"已解析：{title or res.canonical_ref} [{res.canonical_ref}]{rev}，"
                     f"类型 {res.object_type}，置信 1.00（正式映射）。")
    elif res.status == "candidate":
        lines.append(f"编号 {res.raw_value!r} 尚无正式映射，以下候选**未经确认**"
                     "（需 steward 确认后方可作为依据）：")
    else:
        lines.append(f"编号 {res.raw_value!r} 无法解析：无正式映射、无可用候选。"
                     "请确认输入是否正确，或提交 steward 登记。")

    shown: List[Dict[str, Any]] = []
    for c in res.candidates[:3]:
        # 候选同样先过对象级 ACL——不可读候选整条不出参（含 id/ref）
        if not can_read_object({"data_classification": c.data_classification,
                                "owner_dept": c.owner_dept}, acl=acl):
            continue
        title = visible_title(c.title, c.data_classification, c.owner_dept, acl=acl)
        if res.status != "resolved":
            lines.append(f"- {title or c.canonical_ref} [{c.canonical_ref}] "
                         f"置信 {c.confidence:.2f}（来源 {c.method}）")
        shown.append({"object_id": c.target_object_id, "canonical_ref": c.canonical_ref,
                      "title": title, "object_type": c.object_type,
                      "method": c.method, "confidence": c.confidence})

    receipt = {
        "status": res.status, "namespace": res.namespace, "norm_value": res.norm_value,
        "object_id": res.object_id, "canonical_ref": res.canonical_ref,
        "target_revision": res.target_revision, "confidence": res.confidence,
        "method": res.method, "requires_hitl": res.requires_hitl, "candidates": shown,
    }
    return [ContentBlock.of_text("\n".join(lines))], receipt
