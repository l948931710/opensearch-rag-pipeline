# -*- coding: utf-8 -*-
"""stress_harness — 生产上线压测工具箱（agent 平台 / ontology-p0）。

零成本本地档：真 uvicorn 服务进程 + 可控 mock DashScope（chat/embedding）+ mock
OpenSearch（本地回退检索路径）+ 真 MySQL 持久化——被测的是**真实的**
FastAPI/executor/loop/ModelGateway/tool 栈与 SSE 线协议，唯外部付费依赖被替身。

场景矩阵 S0–S8 与 DRAFT 门 G1–G9 见 docs/agent_stress_test_launch_plan_2026-07-12.md。
入口：python -m stress_harness.runner --scenario matrix --tier local --scale smoke
"""
