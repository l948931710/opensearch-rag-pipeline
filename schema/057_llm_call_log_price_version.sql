-- 057_llm_call_log_price_version.sql — LLM 成本回算价表版本列(Majors γ4/M15,2026-07-21)
-- 库：fuling_operation
--
-- 背景(07-21 评审 M15,codex 共识 docs/majors_remediation_plan_2026-07-21_DRAFT.md)：
--   agent LLM 台账 cost_estimate 恒 NULL——记录在案的拍板「不编造模型单价,价表配置后
--   回算」(schema/023:6)。本列为回算落值配套：cost_estimate 由 RAG_MODEL_PRICE_TABLE_JSON
--   的 {version, effective_from, models:{...}} 计算时,price_table_version 同行落值——
--   使成本可审计(哪版价表算的)、价表换版可追溯。价表未配(默认)→ 两列均维持 NULL
--   (拍板不破)。同步写与折叠写两条路径共同落值(codex major)。
--
-- ADDITIVE：单列 NULL 默认,存量零回填；写侧对 1054 容错(**代码可先行**)。
-- 非幂等 DDL：重复执行由 scripts/apply_migration.py 的 information_schema 预检跳过。

ALTER TABLE llm_call_log
  ADD COLUMN price_table_version VARCHAR(32) NULL
    COMMENT '回算 cost_estimate 所用价表版本(057/M15)：价表未配时与 cost_estimate 同为 NULL';
