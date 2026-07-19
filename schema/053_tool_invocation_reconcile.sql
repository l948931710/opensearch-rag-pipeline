-- 053_tool_invocation_reconcile.sql — uncertain 对账退避/隔离（R3 P1-RT-09，2026-07-18）
-- 库：fuling_operation
--
-- 背景（ARR 外审 P1-RT-09）：对账扫描固定「最老优先 LIMIT 50」且 unknown/unsupported
-- 行不推进任何状态——最老 50 条若永远查不清（缺 checker/外部系统故障），其后可恢复
-- 记录永远排不到（毒头阻塞，uncertain 越积越多）。
--
-- 修复：tool_invocation 增两列——
--   · reconcile_attempts：对账尝试数；达上限（RAG_AGENT_RECONCILE_MAX_ATTEMPTS，默认 20）
--     即隔离（不再入扫描面，quarantined 计数上浮，人工通道兜底）；
--   · next_reconcile_at：指数退避的下次可扫时间（unknown 每次 ×2，30s..1h 封顶）——
--     查不清的行让位给新到期行，毒头不再霸占批次。
-- 代码可先行：lister/更新对 1054 容错回退旧「最老 LIMIT 50」语义（过渡窗）。

ALTER TABLE tool_invocation
  ADD COLUMN reconcile_attempts INT NOT NULL DEFAULT 0
    COMMENT '对账尝试数（053）：达上限即隔离出扫描面，人工通道兜底',
  ADD COLUMN next_reconcile_at DATETIME(3) DEFAULT NULL
    COMMENT '下次可对账时间（053）：unknown 指数退避，NULL=立即可扫',
  ADD KEY idx_invocation_reconcile (status, next_reconcile_at);
