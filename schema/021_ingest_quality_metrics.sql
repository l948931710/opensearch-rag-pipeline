-- ════════════════════════════════════════════════════════════════════════════
-- 021 — 摄取批次内容质量指标表（工业级审计 G22，2026-07-06）
-- ════════════════════════════════════════════════════════════════════════════
-- 在 fuling_knowledge 数据库上执行。幂等，可重复跑。
--
-- 背景（审计 G22）：生产摄取只有 count/状态探针（queue_monitor 全是 COUNT(*)），
-- 内容质量（切块中断率/垃圾块/图片过绑）只在发布门的离线 L6 全量评测里测——
-- 坏 PDF 批次要等下次发布门才被发现。本表由 node_validate_chunks 每批 INSERT
-- 一行（轻量启发式指标，纯 Python 零 API），配 send_ops_alert 阈值告警，
-- 使质量退化当天可见。写侧 fail-open：表未建/写失败仅日志，绝不阻断摄取。
-- ════════════════════════════════════════════════════════════════════════════

USE fuling_knowledge;

CREATE TABLE IF NOT EXISTS ingest_quality_metrics (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stat_date      DATE         NOT NULL COMMENT '北京业务日',
    doc_count      INT          NOT NULL DEFAULT 0 COMMENT '本批文档数',
    chunk_total    INT          NOT NULL DEFAULT 0 COMMENT '本批 chunk 总数（valid+invalid）',
    chunk_invalid  INT          NOT NULL DEFAULT 0 COMMENT '验证器丢弃数（空/长度/gibberish 等）',
    gibberish_cnt  INT          NOT NULL DEFAULT 0 COMMENT 'gibberish_text 判定数（G25）',
    midcut_rate    DECIMAL(6,4) NOT NULL DEFAULT 0 COMMENT '疑似句中截断率（文本类 chunk 尾部非句末标点占比）',
    p50_tokens     INT          NOT NULL DEFAULT 0,
    p95_tokens     INT          NOT NULL DEFAULT 0,
    type_dist_json VARCHAR(1024) DEFAULT NULL COMMENT 'chunk_type -> count 分布',
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_stat_date (stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='摄取批次内容质量指标（G22 per-batch；离线 L6 全量评测的日常轻量哨兵）';

INSERT IGNORE INTO schema_migrations (filename, version, notes)
VALUES ('021_ingest_quality_metrics.sql', '021',
        'ingest_quality_metrics 批次质量指标表（G22 per-batch 质量哨兵）');
