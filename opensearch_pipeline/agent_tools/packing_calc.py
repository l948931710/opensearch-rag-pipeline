# -*- coding: utf-8 -*-
"""
packing_calc.py — 箱规/柜容计算工具（本体 P0 PR11；外评 S8）。

**READ_ONLY 且纯读**：sem.lookup_specs 取已登记箱规（ACL 行过滤 fail-closed、未消解回落
原值）→ packing_math 纯内核按 calc_rule 版本化参数计算柜容/需求柜数——**本工具不落库**
（计算出的 spec 若要登记，走 steward 工作台/受治理 Action，恒 draft——S8/S1 四路径纪律）。

参数纪律（S8）：柜型内尺寸/装载系数/折边余量/生效期全部来自 object_type='calc_rule' 的
版本化对象；**无适用规则拒算**（绝不用硬编码"行业值"顶数），输出恒引用规则版本。
多条适用规则并存 → 拒算并列出候选（让模型/用户显式选 rule_ref，不静默挑一条）。
规则对象自身过 can_read_object（全员可用的公司级规则应登记为 public）。

箱规数据契约（sem_packing 投影，schema/030）：per_box=每箱件数；outer_dim=外箱尺寸
字符串 "长x宽x高"（mm，容 x/X/×/* 分隔与 mm 后缀）；单箱毛重（可选）读 spec_json 的
gross_kg / gross_weight_kg。解析不动即报错引原文，不猜单位。

⚠️ 本工具**不默认进 build_default_registry**——接线集中在 PR13 的
RAG_ONTOLOGY_TOOLS_ENABLE（默认 off；守护单测锁双向语义）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

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
        "target": {
            "type": "string", "minLength": 1,
            "description": "SKU 身份：U8 货号等原始编号（配 namespace）/ 企业展示号 FLP-S-… / object_id",
        },
        "namespace": {
            "type": "string", "minLength": 1,
            "description": "原始编号所属命名空间（如 u8）；传展示号/object_id 时可省略",
        },
        "container": {
            "type": "string", "minLength": 1,
            "description": "可选：只算指定柜型（如 40HQ）；缺省算规则登记的全部柜型",
        },
        "order_qty_pcs": {
            "type": "integer", "minimum": 1,
            "description": "可选：订单件数——给出则同时折算需求箱数与柜数",
        },
        "rule_ref": {
            "type": "string", "minLength": 1,
            "description": "可选：指定 calc_rule 展示号（FLP-CR-…）；多规则并存时必须指定",
        },
    },
    "required": ["target"],
    "additionalProperties": False,
}

_DIM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*[xX×*]"
                     r"\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*$")
_GROSS_KEYS = ("gross_kg", "gross_weight_kg")


class PackingCalcTool:
    """EnterpriseTool：SKU 编号 → 箱规档案 + 柜容/需求计划（引用 calc_rule 版本，纯读）。"""

    spec = ToolSpec(
        name="packing_calc",
        version="1.0.0",
        description="按 SKU 编号查已登记箱规并计算装柜：每柜可装箱数/件数（体积装载模型，"
                    "有毛重与柜限重时取双约束较小者）、给定订单件数时的需求箱数与柜数。"
                    "计算参数（柜型内尺寸/装载系数/折边余量）来自版本化的 calc_rule 对象，"
                    "结果引用规则版本。本工具只查询与计算、不写入。",
        input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object"},
        risk_level=RiskLevel.READ_ONLY,
        permission_scope="ontology.packing.calc",
        data_classification="internal",
        owner_team="platform",
    )

    def __init__(self, store=None):
        self._store = store                # 测试注入；缺省惰性建 RDS store

    def _get_store(self):
        if self._store is None:
            from opensearch_pipeline.ontology.store import RDSOntologyStore
            self._store = RDSOntologyStore()
        return self._store

    def run(self, ctx: "ExecutionContext", args: Dict[str, Any],
            idempotency_key: Optional[str] = None) -> ToolResult:
        self.spec.validate_args(args)
        try:
            return self._run(ctx, args)
        except Exception:   # noqa: BLE001 — 异常原文不回模型（PR-I 纪律），详情进日志
            import logging
            logging.getLogger(__name__).exception("箱规计算失败（详情见日志，不回模型）")
            return ToolResult.fail("箱规计算失败（内部异常，已记录日志；请稍后重试）")

    def _run(self, ctx: "ExecutionContext", args: Dict[str, Any]) -> ToolResult:
        from opensearch_pipeline.ontology.packing_math import PackingRuleError, plan_shipment
        from opensearch_pipeline.ontology.sem import lookup_specs
        store = self._get_store()
        acl = set(ctx.acl_groups or ())

        ans = lookup_specs(store, args["target"], acl_groups=acl,
                           namespace=args.get("namespace"), domains=("packing",))
        if not ans.resolved or not ans.sku:
            return ToolResult.ok(
                content=[ContentBlock.of_text(
                    f"编号 {args['target']!r} 未消解为企业对象：" + "；".join(ans.notes))],
                receipt={"status": "unresolved", "notes": ans.notes})
        if ans.sku.get("object_type") != "sku" or ans.specs.get("packing") is None:
            # 消解成功但非 SKU / 无可见箱规——如实回话（无记录与无权限刻意不可区分）
            return ToolResult.ok(
                content=[ContentBlock.of_text(
                    f"{ans.sku.get('title') or ans.sku['canonical_ref']} "
                    f"[{ans.sku['canonical_ref']}]：" + "；".join(ans.notes))],
                receipt={"status": "no_visible_spec", "sku": ans.sku, "notes": ans.notes})

        spec_row = ans.specs["packing"]
        try:
            ppc, dims_mm, gross = _parse_spec_row(spec_row)
            rule = _select_rule(store, acl, rule_ref=args.get("rule_ref"))
            plan = plan_shipment(carton_mm=dims_mm, rule=rule, pcs_per_carton=ppc,
                                 carton_gross_kg=gross,
                                 order_qty_pcs=args.get("order_qty_pcs"),
                                 container_name=args.get("container"))
        except PackingRuleError as e:
            return ToolResult.ok(
                content=[ContentBlock.of_text(f"无法计算：{e}")],
                receipt={"status": "rule_error", "sku": ans.sku, "error": str(e),
                         "notes": ans.notes})

        text = _format_plan(ans, spec_row, plan)
        receipt = {
            "status": "ok", "sku": ans.sku,
            "spec": {"spec_ref": spec_row.get("spec_ref"),
                     "spec_state": spec_row.get("spec_state"),
                     "box_type": spec_row.get("box_type"),
                     "per_box": ppc, "outer_dim_mm": list(dims_mm),
                     "gross_kg": gross, "source": spec_row.get("source"),
                     "as_of": spec_row.get("as_of"), "version": spec_row.get("version")},
            "citation": plan.citation,
            "plans": [vars(p) for p in plan.plans],
            "order": ({"qty_pcs": plan.order_qty_pcs, "cartons": plan.order_cartons}
                      if plan.order_qty_pcs is not None else None),
            "notes": ans.notes + plan.notes,
        }
        return ToolResult.ok(content=[ContentBlock.of_text(text)], receipt=receipt)


def _parse_spec_row(row: Dict[str, Any]) -> Tuple[int, Tuple[float, float, float],
                                                  Optional[float]]:
    """sem_packing 行 → (每箱件数, 外箱 mm 三元组, 单箱毛重 kg 或 None)。解析不动即报错引原文。"""
    from opensearch_pipeline.ontology.packing_math import PackingRuleError
    raw_per_box = row.get("per_box")
    try:
        ppc = int(float(str(raw_per_box)))
        if ppc <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise PackingRuleError(f"箱规 per_box 无法解析为正整数: {raw_per_box!r}")
    raw_dim = str(row.get("outer_dim") or "")
    m = _DIM_RE.match(raw_dim)
    if not m:
        raise PackingRuleError(
            f"箱规 outer_dim 无法解析（期望 '长x宽x高' mm）: {raw_dim!r}")
    dims = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    gross: Optional[float] = None
    spec_json = row.get("spec_json")
    if spec_json:
        import json as _json
        try:
            golden = _json.loads(spec_json) if isinstance(spec_json, str) else spec_json
            for k in _GROSS_KEYS:
                if golden.get(k) is not None:
                    gross = float(golden[k])
                    break
        except Exception:   # noqa: BLE001 — 毛重是可选增强，坏值当缺失（内核会注记）
            gross = None
    return ppc, dims, gross


def _select_rule(store, acl: set, *, rule_ref: Optional[str] = None):
    """calc_rule 选取：显式 ref 优先；否则唯一适用规则；0 条/多条并存一律显式报错。
    规则对象过 can_read_object（不可读=不存在，防存在性泄露）。"""
    from opensearch_pipeline.ontology.authz import can_read_object
    from opensearch_pipeline.ontology.packing_math import CalcRule, PackingRuleError
    if rule_ref:
        obj = store.get_object_by_ref(rule_ref.strip().upper())
        if (obj is None or obj.get("status") != "active"
                or obj.get("object_type") != "calc_rule"
                or not can_read_object(obj, acl=acl)):
            raise PackingRuleError(f"calc_rule {rule_ref!r} 不存在或不可用")
        rule = CalcRule.from_object(obj)
        rule.assert_effective()
        return rule
    candidates: List[Any] = []
    for obj in store.find_objects("calc_rule", status="active", limit=200):
        if not can_read_object(obj, acl=acl):
            continue
        full = store.get_object(obj["object_id"]) or obj   # find_objects 不带 golden_json
        try:
            rule = CalcRule.from_object(full)
            rule.assert_effective()
        except PackingRuleError:
            continue                                       # 未生效/参数坏的规则不入选
        if "sku" in rule.applicable_object_types:
            candidates.append(rule)
    if not candidates:
        raise PackingRuleError(
            "无适用的 calc_rule（版本化装柜参数）——请 PMC steward 在工作台登记后再算")
    if len(candidates) > 1:
        refs = ", ".join(sorted(r.rule_ref for r in candidates))
        raise PackingRuleError(f"存在多条适用 calc_rule（{refs}）——请以 rule_ref 显式指定")
    return candidates[0]


def _format_plan(ans, spec_row: Dict[str, Any], plan) -> str:
    lines: List[str] = []
    sku = ans.sku
    lines.append(f"SKU：{sku.get('title') or sku['canonical_ref']} [{sku['canonical_ref']}]")
    state = spec_row.get("spec_state")
    dim_s = "×".join(f"{d:g}" for d in plan.carton_mm)
    lines.append(f"箱规：{spec_row.get('box_type') or '—'}，每箱 {plan.pcs_per_carton} 件，"
                 f"外箱 {dim_s}mm（{state or '?'}"
                 + (f"，来源 {'/'.join(spec_row['source'])}" if spec_row.get("source") else "")
                 + (f"，截至 {spec_row['as_of']}" if spec_row.get("as_of") else "") + "）")
    for p in plan.plans:
        seg = (f"{p.container}：每柜约 {p.cartons_capacity} 箱"
               + (f"（{p.pcs_capacity} 件）" if p.pcs_capacity is not None else "")
               + f"，约束方={'体积' if p.binding_constraint == 'volume' else '限重'}")
        if p.containers_needed is not None:
            seg += f"；本单需 {p.containers_needed} 柜（尾柜 {p.last_container_cartons} 箱）"
        lines.append(seg)
    if plan.order_qty_pcs is not None:
        lines.append(f"订单折算：{plan.order_qty_pcs} 件 = {plan.order_cartons} 箱"
                     f"（每箱 {plan.pcs_per_carton} 件，向上取整）")
    c = plan.citation
    lines.append(f"计算依据：calc_rule {c['rule_ref']}"
                 + (f" v{c['rule_version']}" if c.get("rule_version") is not None else "")
                 + f"（{c['model']}，装载系数 {c['fill_rate']:g}，"
                   f"折边余量 {c['hem_allowance_mm']:g}mm"
                 + (f"，来源 {c['source']}" if c.get("source") else "") + "）")
    for n in ans.notes + plan.notes:
        lines.append(f"⚠️ {n}")
    return "\n".join(lines)
