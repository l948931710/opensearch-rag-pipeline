-- ════════════════════════════════════════════════════════════════════════════
-- 019 — chunk_meta 索引重试计数 + DEAD 死信终态（工业级审计 G9，2026-07-06）
-- ════════════════════════════════════════════════════════════════════════════
-- 在 fuling_knowledge 数据库上执行。幂等，可重复跑。
--
-- 背景（审计 G9 / pipeline_nodes node_verify_and_repush 已知遗留）：Stage-3 loader
-- 仅按 chunk_meta.index_status IN ('NOT_INDEXED','FAILED') 重选、created_at ASC
-- LIMIT 1000——被 HA3 永久拒收的"毒 chunk"每轮被重新装载→推送→校验失败→raise，
-- 且恒在队头：一条坏记录中断整轮 drain，使后续所有文档停摆。
--
-- 本迁移补 per-chunk 重试预算（镜像 document_version.retry_count<3 的 stage-2 模式）：
--   * chunk_meta.index_retry_count：embedding/push-parity 失败时 +1；
--   * 达上限（默认 3，RAG_STAGE3_CHUNK_MAX_RETRIES）→ index_status='DEAD'（死信终态，
--     不在 loader 重选集内，不再阻塞 drain），所属 document_version.chunk_status
--     置 NEEDS_REVIEW 供人工处理（reindex_states.ChunkIndexStatus.DEAD）。
--
-- 代码侧 fail-open：本迁移未 apply 时（1054 未知列），失败回写自动回退旧 SQL
-- （无重试计数，行为与迁移前逐字节一致）。
-- ════════════════════════════════════════════════════════════════════════════

USE fuling_knowledge;

SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chunk_meta'
      AND COLUMN_NAME = 'index_retry_count'
);
SET @ddl = IF(@col_exists = 0,
    'ALTER TABLE chunk_meta ADD COLUMN index_retry_count INT NOT NULL DEFAULT 0 COMMENT ''stage-3 索引失败重试次数（embedding/parity 失败 +1；达上限转 DEAD 死信，G9）'' AFTER index_status',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 台账登记（011 建立的 schema_migrations；不存在时本段失败可忽略）
INSERT IGNORE INTO schema_migrations (filename, version, notes)
VALUES ('019_chunk_meta_index_retry.sql', '019',
        'chunk_meta.index_retry_count + DEAD 死信终态（G9 毒 chunk 队头阻塞修复）');
