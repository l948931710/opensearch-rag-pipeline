-- 054_agent_run_request_identity.sql — ask 业务域幂等 + 活跃执行耗时(Majors α3/γ1,2026-07-21)
-- 库：fuling_operation
--
-- 背景(07-21 评审 M4/M9,codex 共识方案 docs/majors_remediation_plan_2026-07-21_DRAFT.md)：
--   · M4：/api/agent/ask 无客户端幂等键——响应丢失后客户端重试=第二个 run 第二次副作用。
--     client_request_id(客户端 uuid)+ uk_run_client_req 把幂等域从 run 内提升到业务请求域；
--     question_digest(原始 UTF-8 question 的 SHA-256,不 trim 不规范化)供「同键异题」409 裁决。
--   · M9：agent QA 行 latency_ms 恒 0。active_latency_ms 累计各执行腿(ask/resume/redrive)
--     的活跃执行毫秒——挂起与全部终态 CAS 同事务累加,不含审批等待(墙钟 TTR 另算)。
--
-- 语义：
--   · client_request_id NULL=未传键(旧客户端/flag off),唯一索引对 NULL 不约束——存量零影响；
--   · uk_run_client_req(user_id, client_request_id)：同用户同键至多一 run,并发双 POST 由
--     1062 裁决单赢家(输家回读赢家回放,dispatch 命令 done 收口)；
--   · 代码可先行：写侧对 1054 容错(跳过累计/回退本腿延迟,warn-once)；
--   · RAG_AGENT_ASK_IDEM_ENABLE 开启前**必须先 apply**(readiness schema 形态检查钉住)。
--
-- 非幂等 DDL：重复执行由 scripts/apply_migration.py 的 information_schema 预检跳过
-- (单动作语句族——ADD COLUMN/CREATE INDEX 均在预检覆盖面内,刻意不写多动作 ALTER)。

ALTER TABLE agent_run
  ADD COLUMN client_request_id VARCHAR(64) NULL
    COMMENT '客户端业务请求幂等键(054/M4)：console 每次提交生成 uuid,网络重试复用;NULL=未传';

ALTER TABLE agent_run
  ADD COLUMN question_digest CHAR(64) NULL
    COMMENT '原始 UTF-8 question 的 SHA-256(054/M4)：同 client_request_id 异题 → 409';

ALTER TABLE agent_run
  ADD COLUMN active_latency_ms BIGINT NULL
    COMMENT '活跃执行耗时累计毫秒(054/M9)：各腿在挂起/终态 CAS 同事务累加,不含审批等待';

CREATE UNIQUE INDEX uk_run_client_req ON agent_run (user_id, client_request_id);
