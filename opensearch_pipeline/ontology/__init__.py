# -*- coding: utf-8 -*-
"""
ontology — 本体控制面（身份、语义关系与来源治理）；表族 schema/027–029。

定位：夹在原始系统（SoR 留原地）与 v2 Agent Runtime 之间的语义控制面——管对象身份
（canonical + 别名脊柱）、关系、每属性权威来源与受治理动作路由；**非万物 SoR**。
设计与勘误见 docs/ontology_p0_plan_2026-07-10.md。

包导入保持轻量：ids/normalize 纯 stdlib；store（pymysql）等重依赖由使用方按需
import（沿 routes/agent.py 惰性 import 惯例，flag-off 时零加载零回归）。
"""
