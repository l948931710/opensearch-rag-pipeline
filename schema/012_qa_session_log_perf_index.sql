-- ════════════════════════════════════════════════════════════════════════════
-- 012_qa_session_log_perf_index.sql — qa_session_log 复合索引（性能第一梯队 #1，2026-07-01）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机：5+ 看板/rollup/热问端点按 (answer_status[, created_at 窗口]) 过滤——
--   · kb_insights / kb_governance：answer_status 分桶 + 近 30 天窗口
--   · qa_rollup：按北京日全量扫 created_at 窗口
--   · hot-questions / gaps：answer_status IN ('NO_RESULT','REFUSAL') + 窗口
-- 该表逐问答追加、（留存策略落地前）无清理，是唯一随上线时间单调变慢的服务侧
-- 查询族；现有索引仅 idx_session/idx_user/idx_message_id，上述查询全表扫。
--
-- (answer_status, created_at) 顺序：等值/短 IN 在前、范围在后；纯 created_at
-- 窗口查询（无 status 谓词）可走 index skip scan（MySQL 8.0.13+）或由优化器
-- 选全扫——主要收益目标是带 status 谓词的看板族。
--
-- ADDITIVE / 幂等：仅加索引。⚠️ GATED PROD DDL——建索引期间 Online DDL（INPLACE）
-- 不锁写但耗 IO，选低峰执行。apply 后向本库 schema_migrations 记 '012'。
-- ════════════════════════════════════════════════════════════════════════════

-- 幂等守卫写在 apply 脚本里（information_schema.statistics 查重）；裸 SQL 版本：
CREATE INDEX idx_status_created ON qa_session_log (answer_status, created_at);

-- 回滚：
-- DROP INDEX idx_status_created ON qa_session_log;
