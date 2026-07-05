-- ════════════════════════════════════════════════════════════════════════════
-- 017 — 限流拒绝聚合表 + qa_daily_metrics 拒绝列（盲区审计 P1-1，2026-07-05）
-- ════════════════════════════════════════════════════════════════════════════
-- 在 fuling_operation 数据库上执行。幂等，可重复跑。
--
-- 背景（docs/architecture_blindspot_audit_2026-07-05.md P1-1）：全局日熔断
-- （RAG_GLOBAL_DAILY_LLM_CAP）触顶后所有 /api/ask 在 log_qa_session 之前就被
-- 503 拒绝——被拒请求从不落 qa_session_log，qa_rollup 的分母只剩触顶前的成功量，
-- 全站宕机日在 SLO 看板上反而全绿。本迁移给拒绝量一个持久化落点：
--   * qa_admission_reject：rate_limiter 进程内按（北京日, 原因）聚合、~60s 批量
--     UPSERT（累加计数），单写者=serving 进程；读者=qa_rollup（夜间汇总）。
--     写放大有界：每 60 秒至多一批、每批至多原因数（~8）行——拒绝风暴不打 RDS。
--   * qa_daily_metrics 加 rejected_count / rejected_global_cap 两列：rollup 把
--     当日拒绝量随 SLO 结果一起物化；global_cap 拒绝 > 0 判 SLO breach
--     （slo_breaches_json 里出现 global_cap_rejected 项）。
--
-- 代码侧 fail-open：本迁移未 apply 时，rate_limiter 的 UPSERT 失败仅告警日志；
-- qa_rollup 读不到表按无拒绝处理；qa_daily_metrics 写新列遇 1054 自动回退旧列集。
-- ════════════════════════════════════════════════════════════════════════════

USE fuling_operation;

CREATE TABLE IF NOT EXISTS qa_admission_reject (
    stat_date    DATE        NOT NULL COMMENT '北京业务日（rate_limiter 日界同源）',
    reason       VARCHAR(32) NOT NULL COMMENT '拒绝原因机器码：global_cap/per_min/per_day/thinking_quota/thinking_anon/thinking_off/aux_per_min；__ 前缀=非拒绝行（__admitted__=当日准入量持久化，P2-11 重启回种）',
    reject_count INT UNSIGNED NOT NULL DEFAULT 0,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (stat_date, reason)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='限流拒绝聚合（offered vs admitted 的缺失半边；写侧 rate_limiter、读侧 qa_rollup）';

-- qa_daily_metrics 加列（MySQL 8.0 无 ADD COLUMN IF NOT EXISTS → information_schema 幂等守卫）
DROP PROCEDURE IF EXISTS _qdm_add_rejected_cols;
DELIMITER $$
CREATE PROCEDURE _qdm_add_rejected_cols()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'qa_daily_metrics'
          AND COLUMN_NAME  = 'rejected_count'
    ) THEN
        ALTER TABLE `qa_daily_metrics`
            ADD COLUMN `rejected_count` INT UNSIGNED DEFAULT NULL
                COMMENT '当日限流拒绝总量（全部原因；NULL=017 前的历史行/无数据）',
            ADD COLUMN `rejected_global_cap` INT UNSIGNED DEFAULT NULL
                COMMENT '当日全局熔断拒绝量（>0 即 SLO breach global_cap_rejected）';
    END IF;
END$$
DELIMITER ;

CALL _qdm_add_rejected_cols();
DROP PROCEDURE IF EXISTS _qdm_add_rejected_cols;

-- 回滚：
-- ALTER TABLE qa_daily_metrics DROP COLUMN rejected_count, DROP COLUMN rejected_global_cap;
-- DROP TABLE qa_admission_reject;
