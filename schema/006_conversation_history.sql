-- 006_conversation_history.sql  (rev2：会话级状态与审计日志分离)
-- 控制台服务端会话历史（Phase 2/3）。两件事：
--   1) qa_session_log 仅新增 conversation_id —— 标识每条问答属于哪个逻辑会话（事实属性、append-only）。
--      【不】在审计表上加 conversation_hidden：隐藏是会话级状态，放日志行会导致删除时批量 UPDATE 多行、
--      并发写入后状态分裂、并破坏 qa_session_log 的 append-only 审计语义。
--   2) 新增 qa_conversation 会话元数据表 —— 管标题/最近活动/隐藏状态，与审计日志解耦。
-- 幂等、可重复执行。位于 fuling_operation 库。
--
-- ⚠️ 上线顺序：先应用本迁移 → 验证旧服务正常 → 部署兼容新旧 schema 的新代码 → flag 仍 false 冒烟 →
--    再置 RAG_CONVERSATION_HISTORY=true → 最后开放控制台会话历史 UI。
--    正常写入走原子主 INSERT（缺列时降级 legacy 并 warning），核心审计行恒落库、绝不因增强字段丢失。
-- ⚠️ ALGORITHM=INSTANT 需 MySQL 8.0.12+（且仅在表末尾追加列时可 INSTANT）。低于此版本：去掉该子句，
--    评估 metadata lock 与表大小后再执行（conversation_id 追加在表末尾，不用 AFTER，降低大表 DDL 风险）。

USE fuling_operation;

-- 1) qa_session_log.conversation_id（追加到表末尾；幂等）
DELIMITER $$
DROP PROCEDURE IF EXISTS _ch_add_conversation_id$$
CREATE PROCEDURE _ch_add_conversation_id()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'qa_session_log'
          AND COLUMN_NAME = 'conversation_id'
    ) THEN
        ALTER TABLE qa_session_log
            ADD COLUMN conversation_id VARCHAR(128) DEFAULT NULL
                COMMENT '客户端逻辑会话 ID（同一会话多轮共享）',
            ALGORITHM=INSTANT;
    END IF;
END$$
DELIMITER ;
CALL _ch_add_conversation_id();
DROP PROCEDURE IF EXISTS _ch_add_conversation_id;

-- 2) 历史读取索引 idx_user_conversation_time（user+conversation 取流，时间正序）。
--    幂等校验【列序签名】而非仅索引名：存在同名但列定义不符的索引时报错、不静默跳过。
DELIMITER $$
DROP PROCEDURE IF EXISTS _ch_add_idx$$
CREATE PROCEDURE _ch_add_idx()
BEGIN
    DECLARE sig VARCHAR(255) DEFAULT NULL;
    SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) INTO sig
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'qa_session_log'
      AND INDEX_NAME = 'idx_user_conversation_time';
    IF sig IS NULL THEN
        ALTER TABLE qa_session_log
            ADD INDEX idx_user_conversation_time (user_id, conversation_id, created_at);
    ELSEIF sig <> 'user_id,conversation_id,created_at' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT =
            'idx_user_conversation_time 同名但列定义不符，请人工核对后处理';
    END IF;
END$$
DELIMITER ;
CALL _ch_add_idx();
DROP PROCEDURE IF EXISTS _ch_add_idx;

-- 3) 会话元数据表（标题/最近活动/隐藏状态；与审计日志解耦）
CREATE TABLE IF NOT EXISTS qa_conversation (
    user_id          VARCHAR(128) NOT NULL,
    conversation_id  VARCHAR(128) NOT NULL,
    title            VARCHAR(255) DEFAULT NULL,
    created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    last_message_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    hidden_at        DATETIME(3) DEFAULT NULL,
    PRIMARY KEY (user_id, conversation_id),
    INDEX idx_user_visible_recent (user_id, hidden_at, last_message_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='控制台会话元数据；隐藏状态与审计日志分离';
