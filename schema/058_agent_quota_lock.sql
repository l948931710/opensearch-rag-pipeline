-- 058_agent_quota_lock.sql — 审批入场配额哨兵锁 + 配额向索引(Majors α5/M6,2026-07-21)
-- 库：fuling_operation
--
-- 背景(07-21 评审 M6,codex 共识方案 docs/majors_remediation_plan_2026-07-21_DRAFT.md)：
--   审批提案无 pending 配额——单人可用正常 ask 额度铺满审批队列(3 天 TTL×200 上限
--   oldest-first),淹没他人合法审批。三层配额(per-requester / per-requester-tool / global,
--   env 默认全 0=off)在 suspend_run_atomic 同一事务内裁决;并发穿透防护不依赖隐式
--   gap-lock(隔离级别/优化器敏感),改为**哨兵行 FOR UPDATE 显式串行化**：
--   insert_request 在任一 cap>0 时先锁 'approval_admission' 哨兵行,再三层 COUNT
--   (走 idx_approval_quota)→ INSERT;锁随 suspend 事务 commit/rollback 释放。
--
-- fail 方向(codex 共识 v4 blocker)：任一 cap>0 而本表/哨兵行不可用(1146/无行)→
--   ApprovalQuotaUnavailable,suspend 整事务回滚——**配置了的护栏绝不静默失效**;
--   readiness 在 cap>0 时校验本表+哨兵行,缺 → ready 红。caps 全 0 → 完全不触本表。
--
-- 全部语句自幂等(IF NOT EXISTS / INSERT IGNORE / CREATE INDEX 走 runner 预检),可重放。

CREATE TABLE IF NOT EXISTS agent_quota_lock (
  lock_name  VARCHAR(64) NOT NULL COMMENT '配额域哨兵(058/M6)：FOR UPDATE 串行化入场裁决',
  PRIMARY KEY (lock_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO agent_quota_lock (lock_name) VALUES ('approval_admission');

CREATE INDEX idx_approval_quota ON approval_request (status, requested_by, tool_name);
