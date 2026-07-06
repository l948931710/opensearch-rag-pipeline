-- ════════════════════════════════════════════════════════════════════════════
-- 020 — document_version 内容 simhash 指纹（工业级审计 G19，2026-07-06）
-- ════════════════════════════════════════════════════════════════════════════
-- 在 fuling_knowledge 数据库上执行。幂等，可重复跑。
--
-- 背景（审计 G19）：摄取去重只有字节级精确哈希（canonical_sha256/etag）——同一
-- SOP 重新导出的 PDF 全量二次入索引，同内容文档在检索里分裂打分。本迁移加
-- 64 位 simhash 指纹列：node_build_canonical 计算落库（text_similarity.simhash64，
-- 字符 3-gram），近重复（Hamming ≤ RAG_NEAR_DUP_HAMMING，默认 3）时 WARN-and-process
-- （只告警不拦截；ACL 语义微妙，拦截留给人工——与跨文档精确去重默认一致）。
--
-- 代码侧 fail-open：本迁移未 apply（1054 未知列）时，落库回退旧列集、近重复检测
-- 静默跳过，行为与迁移前一致。存量回填可选（新摄取自然累积覆盖率）。
-- ════════════════════════════════════════════════════════════════════════════

USE fuling_knowledge;

SET @col_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'document_version'
      AND COLUMN_NAME = 'content_simhash'
);
SET @ddl = IF(@col_exists = 0,
    'ALTER TABLE document_version ADD COLUMN content_simhash BIGINT UNSIGNED NULL COMMENT ''canonical 文本 64 位 simhash（G19 近重复检测；NULL=未计算/文本过短）'' AFTER canonical_sha256',
    'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

INSERT IGNORE INTO schema_migrations (filename, version, notes)
VALUES ('020_document_version_simhash.sql', '020',
        'document_version.content_simhash（G19 simhash 近重复 WARN）');
