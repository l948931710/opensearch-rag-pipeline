-- schema/023_llm_call_log.sql
-- 库：fuling_operation
-- LLM 调用账本（v2 报告 §7 模块 F；WS1 收尾②）。ModelGateway 每次 provider.chat 记一行——
-- 成本按 user/dept 归集（补 Repo「token 解析后不落库」缺口，E5 成本可见的数据底座）。
-- ADDITIVE / IDEMPOTENT：仅新建一张表。取号 023（README 台账续现值）。
-- 注：cost_estimate 暂留 NULL（不编造模型单价）；token 为真实值，价表配置后回算成本。

CREATE TABLE IF NOT EXISTS llm_call_log (
  call_id           CHAR(32)     NOT NULL,
  run_id            CHAR(32)     NULL,             -- 归属 agent_run（同步问答可空）
  request_id        VARCHAR(32)  NULL,             -- X-Request-Id
  provider          VARCHAR(32)  NOT NULL,         -- dashscope / selfhosted_vllm ...
  model             VARCHAR(64)  NOT NULL,
  category          VARCHAR(32)  NULL,             -- deep/default/quick/sql
  prompt_version    VARCHAR(32)  NULL,
  tokens_prompt     INT          NULL,
  tokens_completion INT          NULL,
  cost_estimate     DECIMAL(10,4) NULL,            -- 价表配置后回算；暂 NULL
  latency_ms        INT          NULL,
  status            VARCHAR(16)  NULL,             -- ok / error
  user_id           VARCHAR(64)  NULL,
  dept_group        VARCHAR(32)  NULL,             -- 成本按部门归集
  created_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (call_id),
  KEY idx_run (run_id),
  KEY idx_cost (dept_group, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
