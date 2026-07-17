-- ════════════════════════════════════════════════════════════════════════════
-- 043_agent_dispatch_outbox.sql — PR-3 Stage A：durable dispatch 命令表（outbox+lease）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机（PR-3 立项，docs/pr3_durable_worker_design_2026-07-17_DRAFT.md）：
--   「谁在执行这个 run」此前只活在 Web 进程内存（线程池 + _live 字典）——API 受理后
--   进程崩溃/滚动发布，命令即丢（用户以为在答，什么都不会发生）。本表是唯一的
--   pre-dispatch durable 真相：**命令行先落库，agent_run 行在 worker 认领时才创建**
--   （command-as-truth，D1 拍板——agent_run 状态机与 037 生成列零改动）。
--
-- 语义：
--   · status：queued（待认领）→ claimed（持有 lease 执行中）→ done / failed / cancelled；
--   · lease_holder/lease_expires_at：认领租约——过期未续=持有者已死，恢复扫描重驱；
--   · run_id：认领后创建 run 即回填（bind）——**绑定前 at-least-once（可重驱），
--     绑定后 at-most-once（绝不重执行，终态收口）**（D4）；
--   · attempts：重驱封顶（默认 3）→ failed（last_error 留因）；
--   · payload_json：重建执行所需最小载荷（question/thinking/conversation_id/message_id/
--     thread_id/request_id）——**含用户问题原文，敏感级与 qa_session_log 同级**；
--     ACL/身份绝不入 payload（恢复时以 user_id 现解，铁律 5）。retention 随 Stage B
--     纳入 F-36 作业。
--
-- 认领模式：SELECT ... FOR UPDATE SKIP LOCKED（MySQL 8）——单副本零竞争，多副本
--   天然安全（谁抢到谁跑），dispatcher 拆独立部署时本表语义不变（D2）。
--
-- ADDITIVE / 幂等：仅新建表。flag（RAG_AGENT_DURABLE_DISPATCH，默认 off）关闭时
--   代码不读写本表——可先 apply 后部署。
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_dispatch_command (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  command_id       CHAR(32)     NOT NULL COMMENT 'uuid hex（幂等/关联键）',
  kind             ENUM('submit') NOT NULL DEFAULT 'submit' COMMENT 'Stage B 扩 resume',
  status           ENUM('queued','claimed','done','failed','cancelled')
                   NOT NULL DEFAULT 'queued',
  thread_id        VARCHAR(160) NOT NULL COMMENT '会话键（与 agent_run.thread_id 同域）',
  user_id          VARCHAR(64)  NOT NULL COMMENT '发起人（恢复时以此现解 ACL，铁律 5）',
  channel          VARCHAR(16)  NOT NULL DEFAULT 'console',
  payload_json     MEDIUMTEXT   NOT NULL COMMENT '执行载荷（含问题原文；敏感级=qa_session_log）',
  run_id           CHAR(32)     DEFAULT NULL COMMENT '认领后创建 run 即回填（绑定后绝不重执行）',
  attempts         INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '认领次数（重驱封顶判据）',
  lease_holder     VARCHAR(64)  DEFAULT NULL COMMENT 'worker 实例 id（进程级）',
  lease_expires_at DATETIME(3)  DEFAULT NULL COMMENT '租约到期（过期=持有者已死，可重驱）',
  last_error       VARCHAR(500) DEFAULT NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                   ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  UNIQUE KEY uk_command (command_id),
  KEY idx_claim (status, lease_expires_at),
  KEY idx_run (run_id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='PR-3 durable dispatch 命令（outbox+lease；command-as-truth）';
