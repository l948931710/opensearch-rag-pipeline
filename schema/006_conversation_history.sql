-- 006_conversation_history.sql
-- 服务端会话历史（控制台 Phase 2/3）：给 qa_session_log 增两列
--   conversation_id      —— 客户端会话 ID（同一会话多轮共享）。与 session_id 区分：
--                            session_id 随页面会话/进程重置，conversation_id 由客户端稳定持有，
--                            故可跨刷新/跨设备把多轮归到同一会话。
--   conversation_hidden  —— 软删除标记。用户从会话列表移除时置 1；审计/分析行【原样保留】，
--                            绝不物理删除（qa_session_log 是溯源/分析主表）。
-- 幂等、可重复执行。位于 fuling_operation 库（与 qa_session_log 同库）。
--
-- ⚠️ 上线顺序：写入由 RAG_CONVERSATION_HISTORY 开关 gate。请【先】在 RDS 应用本迁移，
--    【再】把开关置 true。回填走独立小事务，即便误开（列尚不存在）也只记 warning、
--    绝不回滚已落库的审计行（见 qa_logger.log_qa_session）。

USE fuling_operation;

DELIMITER $$
DROP PROCEDURE IF EXISTS _ch_add_col$$
CREATE PROCEDURE _ch_add_col(IN p_table VARCHAR(64), IN p_col VARCHAR(64), IN p_def TEXT)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = p_table AND COLUMN_NAME = p_col
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `', p_col, '` ', p_def);
        PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
    END IF;
END$$
DELIMITER ;

CALL _ch_add_col('qa_session_log', 'conversation_id',
    'VARCHAR(128) DEFAULT NULL COMMENT ''客户端会话 ID（控制台会话历史归属）'' AFTER `session_id`');
CALL _ch_add_col('qa_session_log', 'conversation_hidden',
    'TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''软删除：1=用户已从会话列表移除（审计行保留）'' AFTER `conversation_id`');

DROP PROCEDURE IF EXISTS _ch_add_col;

-- 会话列表/取流查询用复合索引（幂等）。
SET @idx := (SELECT COUNT(*) FROM information_schema.STATISTICS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'qa_session_log'
               AND INDEX_NAME = 'idx_user_conversation');
SET @ddl := IF(@idx = 0,
    'ALTER TABLE qa_session_log ADD INDEX idx_user_conversation (user_id, conversation_id, conversation_hidden)',
    'SELECT 1');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;
