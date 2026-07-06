# 企业级 Agent 底座 · v2 设计与实施资料

> 由证据重建的架构结论集（2026-07-03）。基线：本 Repo `@7c704ce` 全量源码扫描 + 15 个开源仓库源码审查 + v1 报告（PDF）38 条主张逐条裁决。

## 阅读顺序

| # | 文件 | 用途 |
|---|---|---|
| 1 | [`富岭企业级Agent底座架构设计报告-v2.md`](富岭企业级Agent底座架构设计报告-v2.md) | **主报告**：A–N 模块设计、三张架构图、接口签名与 DDL 草案、P0–P4 路线、关键问题直答 |
| 2 | [`implementation-plan.md`](implementation-plan.md) | **实施改造计划**（rev-2026-07-06）：WS0-Pre + WS0–WS5 文件级改动清单 + flag + 测试 + 回滚 + 验收（**WS0-Pre 逐条关闭后才拆 issue**） |
| 3 | [`../reviews/agent-platform-v2-architecture-review-2026-07-06.md`](../reviews/agent-platform-v2-architecture-review-2026-07-06.md) | **实施前架构评审**：go/no-go gate review，判定"有条件 NO-GO"；其 §7 修正批次已纳入 implementation-plan 的 WS0-Pre 与各〔rev〕条目 |
| 4 | [`report-v2-changelog.md`](report-v2-changelog.md) | v1→v2 逐条变更（42 条：确认/推翻/重写/弱化/新增，附双证据） |

## 证据底座（审查过程产物）

| 文件 | 用途 |
|---|---|
| [`repo-architecture-map.md`](repo-architecture-map.md) | 当前系统事实基线：调用链 / 身份 ACL 链 / RAG 链 / 会话链 / 符号索引 / 线上待确认清单 |
| [`report-gap-analysis.md`](report-gap-analysis.md) | 主张裁决表 + A–N 七选一状态判定 + Top 10 缺口 |
| [`open-source-code-review.md`](open-source-code-review.md) | 15 个开源仓库源码审查（commit / LICENSE / 遥测 / 可借鉴 / PDF 裁决） |
| [`borrowing-matrix.md`](borrowing-matrix.md) | 七级采纳度矩阵，每项富岭 Repo + 开源源码双证据 + 供应链检查 |

## 实施起点

第一优先级是 **WS0-Pre（开工前修正批次，rev-2026-07-06 新增）**——HEAD re-baseline、运行时执行模型定稿、SessionMemory owner 归属、可靠调度地基、staging 环境、数据出境闸门、成本 spend 闸，七项 gate 关闭后才进 **WS0（状态外置与多实例化）**。详见 `implementation-plan.md`。

> 证据纪律：文中 ✅ 均区分 `[代码存在]` / `[线上在跑]`；本 Repo 无部署流水线，所有 Repo 结论最高 `[代码存在]`，线上状态见主报告 §13 未确认清单。
