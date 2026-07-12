# -*- coding: utf-8 -*-
"""
packing_math.py — PMC-1 箱规纯数学内核（P0 PR11；外评 S8）。

铁律：
- **纯函数、零 IO、零硬编码业务数字**——柜型内尺寸/装载系数/折边余量/取整策略/生效期
  全部来自 CalcRule（object_type='calc_rule' 的版本化参数对象，steward=PMC，
  golden_json 承载参数）；无适用规则 → PackingRuleError（fail-closed，绝不用
  "行业常见值"顶数——拒绝编造数字是仓库纪律）。
- **输出恒引用参数版本**：每次计算返回 citation（rule_ref/rule_version/model/
  fill_rate/来源），供工具层随答案透出（S8「输出引用参数版本」）。
- **装载模型显式声明**：P0 用体积装载模型（volume_fill）——柜内容积 × 装载系数 ÷
  单箱体积，非 3D 摆放求解器；有毛重与柜限重时取体积/重量双约束的较小者。模型名
  随 citation 出参，答案侧不得把它当精确摆柜结果。
- 单位纪律：长度 mm（与 sem_packing 投影同单位）、体积 m³、重量 kg。
- 折边余量语义：装柜适配前对纸箱外尺寸的每维膨胀量（mm）——具体业务口径由
  calc_rule 对象的 golden 参数文档化，内核只按数值应用，不自造纸箱下料公式。

calc_rule.golden_json 参数契约（from_object 校验）：
    {
      "containers": {"40HQ": {"inner_l_mm":…, "inner_w_mm":…, "inner_h_mm":…,
                               "max_payload_kg":…(可选)}, …},
      "fill_rate": 0.85,                 # (0,1]，体积装载系数
      "hem_allowance_mm": 5,             # ≥0，每维折边余量
      "applicable_object_types": ["sku"],          # 可选，缺省 ["sku"]
      "applicable_categories": ["纸制品", …],       # 可选，空=不限品类
      "effective_from": "2026-01-01",              # 可选（ISO 日期）
      "effective_to":   null,                      # 可选
      "source": "PMC 装柜作业标准 v3"               # 可选，来源说明
    }
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["CalcRule", "ContainerSpec", "PackingRuleError", "plan_shipment"]

MODEL_NAME = "volume_fill"


class PackingRuleError(ValueError):
    """规则缺失/参数非法/不在生效期/输入不足——一律显式报错，绝不静默给默认值。"""


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    inner_l_mm: float
    inner_w_mm: float
    inner_h_mm: float
    max_payload_kg: Optional[float] = None

    @property
    def volume_m3(self) -> float:
        return self.inner_l_mm * self.inner_w_mm * self.inner_h_mm / 1e9


@dataclass(frozen=True)
class CalcRule:
    """版本化计算参数对象（ontology_object[object_type='calc_rule'] 的解析形态）。"""

    rule_ref: str                          # canonical_ref（FLP-CR-…）
    version: Optional[int]                 # 对象乐观锁版本（答案与后续变更可对账）
    title: str
    containers: Dict[str, ContainerSpec]
    fill_rate: float
    hem_allowance_mm: float
    applicable_object_types: Tuple[str, ...] = ("sku",)
    applicable_categories: Tuple[str, ...] = ()
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    source: Optional[str] = None
    object_id: Optional[str] = None

    @classmethod
    def from_object(cls, obj: Dict[str, Any]) -> "CalcRule":
        """从 ontology_object 行解析（golden_json 已由 store 层反序列化为 dict 或原始 str）。
        任何缺失/越界参数 → PackingRuleError（登记规则是治理动作，坏规则宁可拒算）。"""
        import json as _json
        golden = obj.get("golden_json") or {}
        if isinstance(golden, str):
            try:
                golden = _json.loads(golden)
            except Exception:
                raise PackingRuleError(
                    f"calc_rule {obj.get('canonical_ref')} 的 golden_json 非法 JSON")
        raw_containers = golden.get("containers")
        if not isinstance(raw_containers, dict) or not raw_containers:
            raise PackingRuleError(
                f"calc_rule {obj.get('canonical_ref')} 缺 containers 参数（柜型内尺寸）")
        containers: Dict[str, ContainerSpec] = {}
        for name, c in raw_containers.items():
            try:
                spec = ContainerSpec(
                    name=str(name),
                    inner_l_mm=float(c["inner_l_mm"]), inner_w_mm=float(c["inner_w_mm"]),
                    inner_h_mm=float(c["inner_h_mm"]),
                    max_payload_kg=(float(c["max_payload_kg"])
                                    if c.get("max_payload_kg") is not None else None))
            except (KeyError, TypeError, ValueError) as e:
                raise PackingRuleError(f"柜型 {name!r} 参数非法: {e}")
            if min(spec.inner_l_mm, spec.inner_w_mm, spec.inner_h_mm) <= 0:
                raise PackingRuleError(f"柜型 {name!r} 内尺寸须为正数")
            if spec.max_payload_kg is not None and spec.max_payload_kg <= 0:
                raise PackingRuleError(f"柜型 {name!r} 限重须为正数")
            containers[spec.name] = spec
        try:
            fill_rate = float(golden["fill_rate"])
            hem = float(golden.get("hem_allowance_mm", 0))
        except (KeyError, TypeError, ValueError) as e:
            raise PackingRuleError(f"fill_rate/hem_allowance_mm 参数非法: {e}")
        if not (0 < fill_rate <= 1):
            raise PackingRuleError(f"fill_rate 须在 (0,1]，得到 {fill_rate}")
        if hem < 0:
            raise PackingRuleError(f"hem_allowance_mm 须 ≥0，得到 {hem}")

        def _parse_date(key: str) -> Optional[date]:
            v = golden.get(key)
            if not v:
                return None
            try:
                return date.fromisoformat(str(v))
            except ValueError:
                raise PackingRuleError(f"{key} 非 ISO 日期: {v!r}")

        return cls(
            rule_ref=str(obj.get("canonical_ref") or ""),
            version=obj.get("version"),
            title=str(obj.get("title") or ""),
            containers=containers, fill_rate=fill_rate, hem_allowance_mm=hem,
            applicable_object_types=tuple(golden.get("applicable_object_types") or ("sku",)),
            applicable_categories=tuple(golden.get("applicable_categories") or ()),
            effective_from=_parse_date("effective_from"),
            effective_to=_parse_date("effective_to"),
            source=(str(golden["source"]) if golden.get("source") else None),
            object_id=obj.get("object_id"))

    def assert_effective(self, as_of: Optional[date] = None) -> None:
        d = as_of or date.today()
        if self.effective_from and d < self.effective_from:
            raise PackingRuleError(
                f"calc_rule {self.rule_ref} 自 {self.effective_from} 起生效（今 {d}）")
        if self.effective_to and d > self.effective_to:
            raise PackingRuleError(
                f"calc_rule {self.rule_ref} 已于 {self.effective_to} 失效（今 {d}）")

    def citation(self) -> Dict[str, Any]:
        """S8：输出引用参数版本——随每次计算结果出参。"""
        return {"rule_ref": self.rule_ref, "rule_version": self.version,
                "rule_title": self.title, "model": MODEL_NAME,
                "fill_rate": self.fill_rate, "hem_allowance_mm": self.hem_allowance_mm,
                "source": self.source}


@dataclass
class ContainerPlan:
    container: str
    cartons_by_volume: int
    cartons_by_weight: Optional[int]
    cartons_capacity: int                  # min(体积, 重量) 双约束
    pcs_capacity: Optional[int]            # 有 pcs_per_carton 时
    binding_constraint: str                # volume | weight
    containers_needed: Optional[int] = None      # 有 order_qty 时
    last_container_cartons: Optional[int] = None


@dataclass
class ShipmentPlan:
    carton_mm: Tuple[float, float, float]
    padded_mm: Tuple[float, float, float]
    carton_volume_m3: float
    pcs_per_carton: Optional[int]
    order_qty_pcs: Optional[int]
    order_cartons: Optional[int]
    plans: List[ContainerPlan] = field(default_factory=list)
    citation: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


def _positive(name: str, v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise PackingRuleError(f"{name} 非数值: {v!r}")
    if f <= 0 or not math.isfinite(f):
        raise PackingRuleError(f"{name} 须为正数: {v!r}")
    return f


def plan_shipment(*, carton_mm: Tuple[Any, Any, Any], rule: CalcRule,
                  pcs_per_carton: Optional[Any] = None,
                  carton_gross_kg: Optional[Any] = None,
                  order_qty_pcs: Optional[Any] = None,
                  container_name: Optional[str] = None,
                  as_of: Optional[date] = None) -> ShipmentPlan:
    """箱规 → 柜容/需求计划。所有业务参数出自 rule；输入不足即报错不猜。

    - 柜容（每柜可装箱数）：floor(柜内容积 × fill_rate ÷ 单箱体积)（体积装载模型，
      箱尺寸先加每维折边余量）；有毛重与柜限重 → 再算 floor(限重 ÷ 单箱毛重)，
      取两者较小并标注约束方（binding_constraint）。
    - 需求（order_qty_pcs 给出时）：需求箱数 = ceil(件数 ÷ 每箱件数)；柜数 =
      ceil(箱数 ÷ 每柜箱数)；尾柜箱数一并给出。
    - 取整语义固定：产能向下取整（宁可少报不虚报）、需求向上取整（宁可多备不漏发）。
    """
    rule.assert_effective(as_of)
    l_mm = _positive("箱长(mm)", carton_mm[0])
    w_mm = _positive("箱宽(mm)", carton_mm[1])
    h_mm = _positive("箱高(mm)", carton_mm[2])
    padded = (l_mm + rule.hem_allowance_mm, w_mm + rule.hem_allowance_mm,
              h_mm + rule.hem_allowance_mm)
    carton_m3 = padded[0] * padded[1] * padded[2] / 1e9

    ppc: Optional[int] = None
    if pcs_per_carton is not None:
        ppc = int(_positive("每箱件数", pcs_per_carton))
    gross: Optional[float] = None
    if carton_gross_kg is not None:
        gross = _positive("单箱毛重(kg)", carton_gross_kg)

    qty: Optional[int] = None
    order_cartons: Optional[int] = None
    if order_qty_pcs is not None:
        if ppc is None:
            raise PackingRuleError("给了订单件数但箱规缺每箱件数（pcs_per_carton）——无法折算箱数")
        qty = int(_positive("订单件数", order_qty_pcs))
        order_cartons = math.ceil(qty / ppc)

    if container_name is not None:
        if container_name not in rule.containers:
            raise PackingRuleError(
                f"柜型 {container_name!r} 不在 calc_rule {rule.rule_ref} 登记柜型"
                f"（可用：{sorted(rule.containers)}）")
        targets = [rule.containers[container_name]]
    else:
        targets = [rule.containers[k] for k in sorted(rule.containers)]

    plan = ShipmentPlan(carton_mm=(l_mm, w_mm, h_mm), padded_mm=padded,
                        carton_volume_m3=carton_m3, pcs_per_carton=ppc,
                        order_qty_pcs=qty, order_cartons=order_cartons,
                        citation=rule.citation())
    for cont in targets:
        by_vol = math.floor(cont.volume_m3 * rule.fill_rate / carton_m3)
        by_wt: Optional[int] = None
        if gross is not None and cont.max_payload_kg is not None:
            by_wt = math.floor(cont.max_payload_kg / gross)
        capacity = by_vol if by_wt is None else min(by_vol, by_wt)
        binding = "volume" if (by_wt is None or by_vol <= by_wt) else "weight"
        cp = ContainerPlan(
            container=cont.name, cartons_by_volume=by_vol, cartons_by_weight=by_wt,
            cartons_capacity=capacity, binding_constraint=binding,
            pcs_capacity=(capacity * ppc if ppc is not None else None))
        if order_cartons is not None:
            if capacity <= 0:
                raise PackingRuleError(
                    f"柜型 {cont.name} 单柜装不下一箱（箱 {padded} mm vs 柜容/限重）——请人工核")
            cp.containers_needed = math.ceil(order_cartons / capacity)
            rem = order_cartons % capacity
            cp.last_container_cartons = capacity if rem == 0 else rem
        plan.plans.append(cp)
    if gross is None:
        plan.notes.append("箱规无单箱毛重——柜容仅按体积约束（未校验柜限重）")
    return plan
