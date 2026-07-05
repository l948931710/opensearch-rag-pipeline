-- ════════════════════════════════════════════════════════════════════════════
-- 018 — qa_session_log.gen_meta_json 生成元数据列 + rag_runtime_contract 运行时 KV
--        （盲区审计 P2-20/21/22 可复现性 + P2-8 摄取↔服务嵌入契约 + P2-14 ops 心跳）
-- ════════════════════════════════════════════════════════════════════════════
-- 双库混排文件（同 004）：ci_load_schema.sh 按 `-- @DB <db>` 分段灌库；生产 apply 用
-- scratch/apply_migration_018.py 分库执行。幂等，可重复跑。
--
-- 背景（docs/architecture_blindspot_audit_2026-07-05.md）：
--   * P2-20/21/22：system prompt 是可变模块常量 + _build_messages 按运行时 flag 条件
--     追加规则（注入守卫/图片子索引/低置信度/纯文本），temperature（缺省 0.1）/top_p
--     （从不发送）与检索制度（fusion/knn_weight/阈值/rerank/top_k/embedding 模型）全部
--     env 可变且均不落库 —— qa_session_log 只有裸 top_score，配置一变历史行不可解释、
--     一条回答的确切 prompt 无法重建。修复：加一个 gen_meta_json 紧凑 JSON 列
--     （prompt_sha/prompt_flags/temperature/top_p/model/retrieval 制度快照），
--     写侧 = llm_generator.build_gen_meta → answer_flow.build_qa_log_kwargs(gen_meta=)。
--   * P2-8：摄取与服务两平面各自从 env 解析 embedding 模型；HA3 文档无模型戳（HA3
--     schema 加字段须整表重建，user-gated 决策），v4→v5 同维升级会静默返回垃圾相似度。
--     代码侧最大程度 = RDS 契约行：stage-3 嵌入节点每次真实运行成功后 UPSERT
--     embedding_model / embedding_dimension（fuling_operation 侧，runtime_contract.py），
--     serving 启动时与 get_config().embedding 比对，失配 → logger.critical +
--     /api/ready 降级 503（刻意不 fail-closed 拒绝启动，联调/本地开发不误伤）。
--   * rag_runtime_contract 是通用运行时 KV：fuling_knowledge 侧同建一份供 ops 心跳
--     （queue_monitor.write_heartbeat，P2-14）—— 两平面各存各的运行时事实。
--
-- 代码侧 fail-open：本迁移未 apply 时，qa_logger 写 gen_meta_json 遇 1054 自动回退
-- 旧列集重试（审计行绝不丢，进程内负缓存防逐行重试刷噪音）；契约读写遇表缺失（1146）
-- 静默跳过 —— 未迁移环境零影响。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_operation

-- qa_session_log 加列（MySQL 8.0 无 ADD COLUMN IF NOT EXISTS → information_schema 幂等守卫）。
-- 选 TEXT 而非 JSON：载荷紧凑（~0.5KB）且与 content_blocks_json 同理由——JSON 类型的
-- 校验失败是一种额外的静默丢行模式，审计行宁可存串不丢行。
DROP PROCEDURE IF EXISTS _qsl_add_gen_meta_col;
DELIMITER $$
CREATE PROCEDURE _qsl_add_gen_meta_col()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'qa_session_log'
          AND COLUMN_NAME  = 'gen_meta_json'
    ) THEN
        ALTER TABLE `qa_session_log`
            ADD COLUMN `gen_meta_json` TEXT DEFAULT NULL
                COMMENT '生成元数据快照 JSON：prompt_sha/prompt_flags/temperature/top_p/model/retrieval 制度（P2-20/21/22；NULL=018 前历史行或未跑 LLM 的 NO_RESULT/错误行）';
    END IF;
END$$
DELIMITER ;

CALL _qsl_add_gen_meta_col();
DROP PROCEDURE IF EXISTS _qsl_add_gen_meta_col;

-- 运营平面运行时契约 KV（写侧 = stage-3 摄取嵌入节点；读侧 = serving 启动比对）
CREATE TABLE IF NOT EXISTS rag_runtime_contract (
    contract_key   VARCHAR(64)  NOT NULL COMMENT '契约键：embedding_model / embedding_dimension（摄取↔服务比对，P2-8）',
    contract_value VARCHAR(255) NOT NULL,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (contract_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='运行时契约/事实 KV（运营平面：嵌入模型契约行）';

-- @DB fuling_knowledge

-- 知识库平面同名 KV：ops_monitor 心跳行（contract_key=ops_monitor_heartbeat，
-- queue_monitor.write_heartbeat / read_heartbeat_age_hours，P2-14）。
CREATE TABLE IF NOT EXISTS rag_runtime_contract (
    contract_key   VARCHAR(64)  NOT NULL COMMENT '契约键：ops_monitor_heartbeat（P2-14 监控链路存活证明）等',
    contract_value VARCHAR(255) NOT NULL,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (contract_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='运行时契约/事实 KV（知识库平面：ops 心跳等）';

-- 回滚：
-- ALTER TABLE fuling_operation.qa_session_log DROP COLUMN gen_meta_json;
-- DROP TABLE fuling_operation.rag_runtime_contract;
-- DROP TABLE fuling_knowledge.rag_runtime_contract;
