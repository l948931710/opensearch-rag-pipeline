-- ============================================================
-- Migration: 002_feedback_system.sql
-- Database:  fuling_knowledge
-- Purpose:   Extend qa_session_log and user_feedback tables
--            for the RAG feedback system.
-- Idempotent: Safe to run multiple times without errors.
-- ============================================================

USE fuling_operation;

-- ------------------------------------------------------------
-- Helper stored procedures for idempotent DDL operations
-- ------------------------------------------------------------

DROP PROCEDURE IF EXISTS _add_column_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _add_column_if_not_exists(
    IN p_table  VARCHAR(64),
    IN p_column VARCHAR(64),
    IN p_definition TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = p_table
          AND COLUMN_NAME  = p_column
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD COLUMN `', p_column, '` ', p_definition);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS _add_index_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _add_index_if_not_exists(
    IN p_table   VARCHAR(64),
    IN p_index   VARCHAR(64),
    IN p_columns TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = p_table
          AND INDEX_NAME   = p_index
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD INDEX `', p_index, '` (', p_columns, ')');
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

DROP PROCEDURE IF EXISTS _add_unique_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _add_unique_if_not_exists(
    IN p_table   VARCHAR(64),
    IN p_index   VARCHAR(64),
    IN p_columns TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = p_table
          AND INDEX_NAME   = p_index
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', p_table, '` ADD UNIQUE INDEX `', p_index, '` (', p_columns, ')');
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

-- ------------------------------------------------------------
-- qa_session_log: add new columns
-- ------------------------------------------------------------

CALL _add_column_if_not_exists('qa_session_log', 'user_name',
    'VARCHAR(128) DEFAULT NULL COMMENT ''用户昵称'' AFTER `user_id`');

CALL _add_column_if_not_exists('qa_session_log', 'model_name',
    'VARCHAR(64) DEFAULT NULL COMMENT ''LLM模型名称'' AFTER `answer_status`');

CALL _add_column_if_not_exists('qa_session_log', 'error_message',
    'TEXT DEFAULT NULL COMMENT ''失败时的错误信息'' AFTER `model_name`');

CALL _add_column_if_not_exists('qa_session_log', 'opensearch_hit_count',
    'INT DEFAULT NULL COMMENT ''检索命中数'' AFTER `error_message`');

CALL _add_column_if_not_exists('qa_session_log', 'top_score',
    'DECIMAL(10,4) DEFAULT NULL COMMENT ''最高检索得分'' AFTER `opensearch_hit_count`');

CALL _add_column_if_not_exists('qa_session_log', 'conversation_type',
    'VARCHAR(8) DEFAULT NULL COMMENT ''1=单聊 2=群聊'' AFTER `top_score`');

CALL _add_column_if_not_exists('qa_session_log', 'retrieval_latency_ms',
    'INT DEFAULT NULL COMMENT ''检索阶段耗时(ms)'' AFTER `latency_ms`');

CALL _add_column_if_not_exists('qa_session_log', 'llm_latency_ms',
    'INT DEFAULT NULL COMMENT ''LLM生成阶段耗时(ms)'' AFTER `retrieval_latency_ms`');

-- ------------------------------------------------------------
-- qa_session_log: make message_id NOT NULL
-- ------------------------------------------------------------

UPDATE qa_session_log SET message_id = CONCAT('legacy_', id) WHERE message_id IS NULL;

ALTER TABLE qa_session_log MODIFY COLUMN message_id VARCHAR(128) NOT NULL COMMENT '消息唯一ID';

-- ------------------------------------------------------------
-- qa_session_log: add index on message_id
-- ------------------------------------------------------------

CALL _add_index_if_not_exists('qa_session_log', 'idx_message_id', 'message_id');

-- ------------------------------------------------------------
-- user_feedback: make message_id and user_id NOT NULL
-- ------------------------------------------------------------

UPDATE user_feedback SET message_id = CONCAT('legacy_', id) WHERE message_id IS NULL;

ALTER TABLE user_feedback MODIFY COLUMN message_id VARCHAR(128) NOT NULL COMMENT '消息唯一ID';

UPDATE user_feedback SET user_id = CONCAT('unknown_', id) WHERE user_id IS NULL;

ALTER TABLE user_feedback MODIFY COLUMN user_id VARCHAR(128) NOT NULL COMMENT '用户ID';

-- ------------------------------------------------------------
-- user_feedback: add indexes
-- ------------------------------------------------------------

CALL _add_index_if_not_exists('user_feedback', 'idx_message_id', 'message_id');

CALL _add_unique_if_not_exists('user_feedback', 'uk_message_user', 'message_id, user_id');

-- ------------------------------------------------------------
-- user_feedback: update feedback_type comment
-- ------------------------------------------------------------

ALTER TABLE user_feedback MODIFY COLUMN feedback_type VARCHAR(32) DEFAULT NULL COMMENT 'upvote / downvote';

-- ------------------------------------------------------------
-- Clean up helper stored procedures
-- ------------------------------------------------------------

DROP PROCEDURE IF EXISTS _add_column_if_not_exists;
DROP PROCEDURE IF EXISTS _add_index_if_not_exists;
DROP PROCEDURE IF EXISTS _add_unique_if_not_exists;
