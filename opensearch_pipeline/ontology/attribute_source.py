# -*- coding: utf-8 -*-
"""
attribute_source.py — 属性溯源目录的代码内声明（种子）。

本体只对「每个 对象类型.属性 的权威来源是谁」负责（控制面定位）——本目录是答案溯源
（"数据从哪来、多新鲜"）与工具路由（文档→knowledge_search / 结构化→sem_ / 可复算→
packing_calc）的权威源。**授权（谁能确认/处置）不在这里**，见 stewardship.py（外评 S5）。

声明即代码（沿 tool_registry sync_specs 模式）：改来源治理走本文件评审，ensure_seeds
upsert 入库，人工直改 DB 会在下次 upsert 被代码声明覆盖。
"""
from __future__ import annotations

from typing import List

# 首批种子（P0-P1 落地细化 §2.3，steward 列已拆去 stewardship）
SEED_ROWS: List[dict] = [
    {"object_type": "product", "attribute": "name_zh", "sor_system": "ontology",
     "sync_mode": "manual", "freshness": None, "notes": "商务品名（营销口径）"},
    {"object_type": "product", "attribute": "spec_base", "sor_system": "ontology(SpecSheet)",
     "sync_mode": "derived", "freshness": "on-demand",
     "notes": "材质/克重/几何——PMC-3 基础属性 SoR；缺失时 packing_calc 输出标'估算·量产前须打样'"},
    {"object_type": "sku", "attribute": "箱规", "sor_system": "ontology(PackingSpec)",
     "sync_mode": "derived", "freshness": "on-demand", "notes": "packing_calc 推导（PMC-1）"},
    {"object_type": "sku", "attribute": "香规", "sor_system": "ontology(StackingProfile)",
     "sync_mode": "derived", "freshness": "on-demand", "notes": "packing_calc 推导（PMC-1）"},
    {"object_type": "mold", "attribute": "mold_profile", "sor_system": "ontology",
     "sync_mode": "manual", "freshness": None,
     "notes": "腔数/出模数/资产归属——interim 登记；Max 上线切换=独立工作包，非改一行"},
    {"object_type": "material", "attribute": "采购价", "sor_system": "ontology(PurchasePrice)",
     "sync_mode": "manual", "freshness": "on-demand",
     "notes": "confidential；P2 拆 PurchasePrice/QuotePrice 前的占位口径"},
]


def ensure_seeds(store) -> int:
    """把代码内声明 upsert 进 ontology_attribute_source（幂等；代码即权威）。"""
    return store.upsert_attribute_sources(SEED_ROWS)
