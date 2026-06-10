-- ============================================================
-- Migration: 002_feedback_system.sql
-- Database:  fuling_knowledge
-- Purpose:   Extend qa_session_log and user_feedback tables
--            for the RAG feedback system.
-- Idempotent: Safe to run multiple times without errors.
-- ============================================================

-- 与 001 的 fuling_knowledge 同款约定：全新安装时库本身也不存在（生产上已有，no-op）
CREATE DATABASE IF NOT EXISTS fuling_operation;
USE fuling_operation;

-- ------------------------------------------------------------
-- Ensure the feedback tables exist in THIS database (fuling_operation).
-- The serving code writes fully-qualified fuling_operation.user_feedback /
-- fuling_operation.escalation_ticket / fuling_operation.qa_session_log
-- (feedback_handler.py / qa_logger.py). 001 creates these under
-- fuling_knowledge, so a deployment whose RDS hosts the live data in
-- fuling_operation needs them here too — otherwise every 喜欢/不喜欢/转人工
-- write fails ("Unknown table") and the card shows "反馈处理失败"；
-- qa_session_log 的缺失更隐蔽：qa_logger 把写入失败按非致命吞掉，
-- 问答日志整行静默丢失，反馈再也找不到 message_id。
-- CREATE TABLE IF NOT EXISTS is idempotent: no-op if the table already exists.
-- (Definitions mirror schema/001_opensearch_pipeline.sql.)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_feedback (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    feedback_id         VARCHAR(100) NOT NULL,
    session_id          VARCHAR(128) DEFAULT NULL,
    message_id          VARCHAR(128) DEFAULT NULL,
    user_id             VARCHAR(128) DEFAULT NULL,
    user_name           VARCHAR(128) DEFAULT NULL,
    user_dept           VARCHAR(64) DEFAULT NULL,
    query_text          TEXT DEFAULT NULL,
    ai_answer           MEDIUMTEXT DEFAULT NULL,
    cited_doc_ids_json  JSON DEFAULT NULL,
    cited_chunks_json   JSON DEFAULT NULL,
    feedback_type       VARCHAR(32) DEFAULT NULL COMMENT 'upvote / downvote',
    feedback_reason     VARCHAR(128) DEFAULT NULL,
    feedback_comment    TEXT DEFAULT NULL,
    badcase_category    VARCHAR(64) DEFAULT NULL,
    handled_status      VARCHAR(32) DEFAULT 'PENDING',
    handled_by          VARCHAR(128) DEFAULT NULL,
    handled_comment     TEXT DEFAULT NULL,
    handled_at          DATETIME DEFAULT NULL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_feedback_id (feedback_id),
    INDEX idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS escalation_ticket (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ticket_id           VARCHAR(100) NOT NULL,
    session_id          VARCHAR(128) DEFAULT NULL,
    message_id          VARCHAR(128) DEFAULT NULL,
    user_id             VARCHAR(128) DEFAULT NULL,
    user_name           VARCHAR(128) DEFAULT NULL,
    user_dept           VARCHAR(64) DEFAULT NULL,
    query_text          TEXT DEFAULT NULL,
    ai_answer           MEDIUMTEXT DEFAULT NULL,
    trigger_reason      VARCHAR(64) DEFAULT NULL,
    assigned_dept       VARCHAR(64) DEFAULT NULL,
    assigned_user_id    VARCHAR(128) DEFAULT NULL,
    assigned_user_name  VARCHAR(128) DEFAULT NULL,
    ticket_status       VARCHAR(32) DEFAULT 'PENDING',
    expert_answer       MEDIUMTEXT DEFAULT NULL,
    converted_to_faq    TINYINT(1) DEFAULT 0,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    assigned_at         DATETIME DEFAULT NULL,
    answered_at         DATETIME DEFAULT NULL,
    closed_at           DATETIME DEFAULT NULL,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ticket_id (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 全新安装时 fuling_operation 里没有 qa_session_log（001 只在 fuling_knowledge 建过），
-- 下方对它的 CALL/UPDATE/ALTER 会直接报 1146。这里按「001 基础形态 + 本文件追加的全部列」
-- 一次建全（message_id 直接 NOT NULL、索引齐备），已有部署则完全跳过。
CREATE TABLE IF NOT EXISTS qa_session_log (
    id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id           VARCHAR(128) NOT NULL,
    message_id           VARCHAR(128) NOT NULL COMMENT '消息唯一ID',
    user_id              VARCHAR(128) DEFAULT NULL,
    user_name            VARCHAR(128) DEFAULT NULL COMMENT '用户昵称',
    user_dept            VARCHAR(64) DEFAULT NULL,
    query_text           TEXT DEFAULT NULL,
    answer_text          MEDIUMTEXT DEFAULT NULL,
    intent_type          VARCHAR(64) DEFAULT NULL,
    risk_level           VARCHAR(32) DEFAULT NULL,
    risk_blocked         TINYINT(1) DEFAULT 0,
    retrieved_docs_json  JSON DEFAULT NULL,
    cited_docs_json      JSON DEFAULT NULL,
    latency_ms           INT DEFAULT 0,
    retrieval_latency_ms INT DEFAULT NULL COMMENT '检索阶段耗时(ms)',
    llm_latency_ms       INT DEFAULT NULL COMMENT 'LLM生成阶段耗时(ms)',
    answer_status        VARCHAR(32) DEFAULT 'SUCCESS',
    model_name           VARCHAR(64) DEFAULT NULL COMMENT 'LLM模型名称',
    error_message        TEXT DEFAULT NULL COMMENT '失败时的错误信息',
    opensearch_hit_count INT DEFAULT NULL COMMENT '检索命中数',
    top_score            DECIMAL(10,4) DEFAULT NULL COMMENT '最高检索得分',
    conversation_type    VARCHAR(8) DEFAULT NULL COMMENT '1=单聊 2=群聊',
    content_blocks_json  MEDIUMTEXT DEFAULT NULL COMMENT '图文渲染块 JSON 快照',
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session (session_id),
    INDEX idx_user (user_id),
    INDEX idx_message_id (message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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

-- 小程序/卡片图文穿插块快照（qa_logger.py 写入、dingtalk_bot 卡片重建回读）。
-- 选 MEDIUMTEXT 而非 JSON/TEXT：与 answer_text 同级（16MB），小程序 caption 不截断，
-- 超长图文块不会在 TEXT 64KB 边界把整行 INSERT 打挂（整条问答日志静默丢失正是本列要修的事故）；
-- 也没有 JSON 校验失败这一额外丢行模式。读侧已兼容 str/parsed 两种形态。
CALL _add_column_if_not_exists('qa_session_log', 'content_blocks_json',
    'MEDIUMTEXT DEFAULT NULL COMMENT ''图文渲染块 JSON 快照'' AFTER `conversation_type`');

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
