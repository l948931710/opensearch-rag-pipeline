# ═══════════════════════════════════════════════════════════════
# OpenSearch RAG Pipeline — RDS Schema (Production Aligned)
# ═══════════════════════════════════════════════════════════════
# 在 fuling_knowledge 数据库上执行
# ═══════════════════════════════════════════════════════════════

CREATE DATABASE IF NOT EXISTS fuling_knowledge;
USE fuling_knowledge;

SET FOREIGN_KEY_CHECKS = 0;

-- ──────────────────────────────────────────────────────────────
-- 1. User & Access Control Tables
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_role (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id     VARCHAR(128) NOT NULL,
    user_name   VARCHAR(128) DEFAULT NULL,
    dept_code   VARCHAR(64) DEFAULT NULL,
    role        VARCHAR(64) DEFAULT NULL,
    is_active   TINYINT(1) DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    -- user_id 必须唯一：dept_code 驱动 HA3 dept_internal 权限过滤，重复行会导致部门解析不确定；
    -- 代码里的 INSERT ... ON DUPLICATE KEY UPDATE 也依赖此键去重（存量库见 003_user_role_unique.sql）
    UNIQUE KEY uk_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS document_acl_rule (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id         VARCHAR(100) NOT NULL,
    principal_type VARCHAR(32) DEFAULT NULL COMMENT 'user / dept / role',
    principal_id   VARCHAR(128) DEFAULT NULL,
    permission     VARCHAR(32) DEFAULT NULL COMMENT 'read / write / admin',
    effect         VARCHAR(16) DEFAULT 'allow' COMMENT 'allow / deny',
    created_by     VARCHAR(128) DEFAULT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_doc_id (doc_id),
    INDEX idx_principal (principal_type, principal_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ──────────────────────────────────────────────────────────────
-- 2. Metadata & Classification Tables
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tag_taxonomy (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    tag_key     VARCHAR(64) NOT NULL,
    tag_value   VARCHAR(128) NOT NULL,
    tag_label   VARCHAR(128) DEFAULT NULL,
    description VARCHAR(500) DEFAULT NULL,
    is_active   TINYINT(1) DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tag_key_val (tag_key, tag_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS document_meta (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id                VARCHAR(100) NOT NULL,
    title                 VARCHAR(255) DEFAULT NULL,
    original_filename     VARCHAR(255) DEFAULT NULL,
    owner_dept            VARCHAR(64) DEFAULT NULL,
    owner_user_id         VARCHAR(128) DEFAULT NULL,
    owner_name            VARCHAR(128) DEFAULT NULL,
    category_l1           VARCHAR(64) DEFAULT NULL,
    category_l2           VARCHAR(64) DEFAULT NULL,
    original_category     VARCHAR(255) DEFAULT NULL,
    doc_type              VARCHAR(64) DEFAULT NULL,
    permission_level      VARCHAR(64) DEFAULT 'public',
    kb_type               VARCHAR(64) DEFAULT 'public',
    status                VARCHAR(32) DEFAULT 'active',
    current_version_no    INT DEFAULT 1,
    effective_date        DATE DEFAULT NULL,
    expiry_date           DATE DEFAULT NULL,
    summary               TEXT DEFAULT NULL,
    tags_json             JSON DEFAULT NULL,
    extra_json            JSON DEFAULT NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_doc_id (doc_id),
    INDEX idx_category (category_l1, category_l2),
    INDEX idx_permission (permission_level),
    INDEX idx_owner_dept (owner_dept)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS document_tag (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id      VARCHAR(100) NOT NULL,
    tag_key     VARCHAR(64) NOT NULL,
    tag_value   VARCHAR(128) NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_doc_tag (doc_id, tag_key, tag_value),
    INDEX idx_tag (tag_key, tag_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ──────────────────────────────────────────────────────────────
-- 3. Document Version & RAG State Table
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS document_version (
    id                         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id                     VARCHAR(100) NOT NULL,
    version_no                 INT NOT NULL,
    bucket_name                VARCHAR(128) DEFAULT NULL,
    raw_key                    VARCHAR(1024) DEFAULT NULL,
    raw_key_hash               CHAR(64) DEFAULT NULL,
    quarantine_key             VARCHAR(1024) DEFAULT NULL,
    review_key                 VARCHAR(1024) DEFAULT NULL,
    rag_ready_key              VARCHAR(1024) DEFAULT NULL,
    system_manifest_key        VARCHAR(1024) DEFAULT NULL,
    checksum_sha256            CHAR(64) DEFAULT NULL,
    etag                       VARCHAR(128) DEFAULT NULL,
    file_ext                   VARCHAR(32) DEFAULT NULL,
    mime_type                  VARCHAR(128) DEFAULT NULL,
    file_size_bytes            BIGINT DEFAULT NULL,
    validation_status          VARCHAR(32) DEFAULT 'PENDING',
    approval_status            VARCHAR(32) DEFAULT 'PENDING',
    -- publish_status 枚举值（VARCHAR 不强制，文档用）：
    --   NOT_PUBLISHED / PUBLISHED / QUARANTINED / SKIPPED_EMPTY
    --   SKIPPED_EMPTY 由 node_publish_to_rag_ready 在 md_data 为空时使用（RD 61D861 修复）。
    --   若未来切 ENUM 类型，需把 'SKIPPED_EMPTY' 加入枚举定义。
    publish_status             VARCHAR(32) DEFAULT 'NOT_PUBLISHED',
    classification_method      VARCHAR(32) DEFAULT NULL,
    classification_confidence  DECIMAL(5,4) DEFAULT NULL,
    suggested_tags_json        JSON DEFAULT NULL,
    risk_level                 VARCHAR(32) DEFAULT 'low',
    risk_hits_json             JSON DEFAULT NULL,
    bailian_workspace_id       VARCHAR(128) DEFAULT NULL,
    bailian_kb_id              VARCHAR(128) DEFAULT NULL,
    bailian_file_id            VARCHAR(128) DEFAULT NULL,
    bailian_import_status      VARCHAR(32) DEFAULT NULL,
    error_code                 VARCHAR(64) DEFAULT NULL,
    error_message              TEXT DEFAULT NULL,
    received_at                DATETIME DEFAULT NULL,
    processed_at               DATETIME DEFAULT NULL,
    published_at               DATETIME DEFAULT NULL,
    activated_at               DATETIME DEFAULT NULL,
    created_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at                 DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    -- Processing & RAG Status fields
    gate_status                VARCHAR(32) DEFAULT 'pending_clean',
    content_process_status     VARCHAR(32) DEFAULT 'NOT_STARTED',
    content_process_error      TEXT DEFAULT NULL,
    classification_status      VARCHAR(32) DEFAULT 'NOT_STARTED',

    -- RAG Pipeline Increment fields
    canonical_json_key         VARCHAR(1024) DEFAULT NULL,
    canonical_md_key           VARCHAR(1024) DEFAULT NULL,
    redacted_key               VARCHAR(1024) DEFAULT NULL,
    redaction_report_key       VARCHAR(1024) DEFAULT NULL,
    extraction_status          VARCHAR(32) DEFAULT 'NOT_STARTED',
    ocr_status                 VARCHAR(32) DEFAULT 'NOT_REQUIRED',
    chunk_status               VARCHAR(32) DEFAULT 'NOT_STARTED',
    index_status               VARCHAR(32) DEFAULT 'NOT_INDEXED',
    page_count                 INT DEFAULT NULL,
    text_length                INT DEFAULT NULL,
    extract_method             VARCHAR(64) DEFAULT NULL,
    faq_eligible               BOOLEAN DEFAULT FALSE,
    retry_count                INT DEFAULT 0,
    chunk_count                INT DEFAULT NULL,
    
    -- Backward compatibility fields
    status                     VARCHAR(32) DEFAULT 'active',
    registered_at              DATETIME DEFAULT NULL,

    UNIQUE KEY uk_doc_version (doc_id, version_no),
    INDEX idx_extraction (extraction_status),
    INDEX idx_chunk (chunk_status),
    INDEX idx_index (index_status),
    INDEX idx_content_process (content_process_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ──────────────────────────────────────────────────────────────
-- 4. Chunk Metadata Management Table
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chunk_meta (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    chunk_id              VARCHAR(128) NOT NULL,
    doc_id                VARCHAR(100) NOT NULL,
    version_no            INT NOT NULL,
    chunk_index           INT NOT NULL,
    page_num              INT DEFAULT NULL,
    section_title         VARCHAR(255) DEFAULT NULL,
    chunk_text_preview    TEXT DEFAULT NULL,
    source_url            VARCHAR(1024) DEFAULT NULL,
    extra_json            JSON DEFAULT NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- RAG Specific Increment fields (necessary for vectorization/indexing)
    chunk_type            VARCHAR(64) DEFAULT 'text_chunk',
    chunk_text            LONGTEXT NOT NULL,
    token_count           INT DEFAULT NULL,
    source                VARCHAR(32) DEFAULT 'native',
    rag_ready_key         VARCHAR(1024) DEFAULT NULL,
    permission_level      VARCHAR(64) DEFAULT 'public',
    owner_dept            VARCHAR(64) DEFAULT NULL,
    allowed_depts         JSON DEFAULT NULL,
    allowed_roles         JSON DEFAULT NULL,
    category_l1           VARCHAR(64) DEFAULT NULL,
    category_l2           VARCHAR(64) DEFAULT NULL,
    sensitive_redacted    BOOLEAN DEFAULT TRUE,
    is_active             BOOLEAN DEFAULT TRUE,

    -- Embedding Status
    embedding_status      VARCHAR(32) DEFAULT 'NOT_STARTED',
    embedding_model       VARCHAR(128) DEFAULT NULL,
    embedding_dimension   INT DEFAULT NULL,
    embedding_version     VARCHAR(64) DEFAULT NULL,
    embedding_error_code  VARCHAR(128) DEFAULT NULL,
    embedding_error_message TEXT DEFAULT NULL,
    embedded_at           DATETIME DEFAULT NULL,

    -- Indexing Status
    index_status          VARCHAR(32) DEFAULT 'NOT_INDEXED',
    index_name            VARCHAR(128) DEFAULT NULL,
    opensearch_doc_id     VARCHAR(128) DEFAULT NULL,
    opensearch_bulk_job_id VARCHAR(128) DEFAULT NULL,
    index_error_code      VARCHAR(128) DEFAULT NULL,
    index_error_message   TEXT DEFAULT NULL,
    indexed_at            DATETIME DEFAULT NULL,
    updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_chunk_id (chunk_id),
    INDEX idx_doc_version (doc_id, version_no),
    INDEX idx_doc_active (doc_id, is_active),
    INDEX idx_embedding_status (embedding_status),
    INDEX idx_index_status (index_status),
    INDEX idx_is_active (is_active),
    INDEX idx_permission_dept (permission_level, owner_dept)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ──────────────────────────────────────────────────────────────
-- 5. Operational, Audit & Queue Tables
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS kb_audit_log (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    trace_id       VARCHAR(100) DEFAULT NULL,
    doc_id         VARCHAR(100) DEFAULT NULL,
    version_no     INT DEFAULT NULL,
    action_type    VARCHAR(64) DEFAULT NULL,
    action_result  VARCHAR(32) DEFAULT NULL,
    operator_type  VARCHAR(32) DEFAULT NULL,
    operator_id    VARCHAR(128) DEFAULT NULL,
    bucket_name    VARCHAR(128) DEFAULT NULL,
    oss_key        VARCHAR(1024) DEFAULT NULL,
    message        TEXT DEFAULT NULL,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_doc_id (doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS kb_import_job (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id                VARCHAR(100) NOT NULL,
    target_scope          VARCHAR(64) DEFAULT NULL,
    target_dept           VARCHAR(64) DEFAULT NULL,
    target_category_l1    VARCHAR(64) DEFAULT NULL,
    target_prefix         VARCHAR(1024) DEFAULT NULL,
    bailian_workspace_id  VARCHAR(128) DEFAULT NULL,
    bailian_kb_id         VARCHAR(128) DEFAULT NULL,
    bailian_category_id   VARCHAR(128) DEFAULT NULL,
    job_status            VARCHAR(32) DEFAULT 'PENDING',
    import_log_key        VARCHAR(1024) DEFAULT NULL,
    total_files           INT DEFAULT 0,
    success_files         INT DEFAULT 0,
    failed_files          INT DEFAULT 0,
    error_code            VARCHAR(64) DEFAULT NULL,
    error_message         TEXT DEFAULT NULL,
    started_at            DATETIME DEFAULT NULL,
    finished_at           DATETIME DEFAULT NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_job_id (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS review_task (
    id                         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    task_id                    VARCHAR(100) NOT NULL,
    doc_id                     VARCHAR(100) NOT NULL,
    version_no                 INT NOT NULL,
    review_key                 VARCHAR(1024) DEFAULT NULL,
    review_type                VARCHAR(64) DEFAULT NULL,
    review_reason              TEXT DEFAULT NULL,
    review_status              VARCHAR(32) DEFAULT 'PENDING',
    owner_dept                 VARCHAR(64) DEFAULT NULL,
    suggested_category_l1      VARCHAR(64) DEFAULT NULL,
    suggested_category_l2      VARCHAR(64) DEFAULT NULL,
    suggested_permission_level VARCHAR(64) DEFAULT NULL,
    confidence_score           DECIMAL(5,4) DEFAULT NULL,
    reviewer_user_id           VARCHAR(128) DEFAULT NULL,
    reviewer_name              VARCHAR(128) DEFAULT NULL,
    reviewer_comment           TEXT DEFAULT NULL,
    reviewed_at                DATETIME DEFAULT NULL,
    created_at                 DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at                 DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_task_id (task_id),
    INDEX idx_doc_version (doc_id, version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS faq_review_queue (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    faq_id                VARCHAR(100) NOT NULL,
    question              TEXT DEFAULT NULL,
    answer                MEDIUMTEXT DEFAULT NULL,
    source_type           VARCHAR(32) DEFAULT NULL,
    source_doc_id         VARCHAR(100) DEFAULT NULL,
    source_version_no     INT DEFAULT NULL,
    source_ticket_id      VARCHAR(100) DEFAULT NULL,
    owner_dept            VARCHAR(64) DEFAULT NULL,
    category_l1           VARCHAR(64) DEFAULT NULL,
    category_l2           VARCHAR(64) DEFAULT NULL,
    permission_level      VARCHAR(64) DEFAULT 'public',
    review_status         VARCHAR(32) DEFAULT 'PENDING',
    reviewer_user_id      VARCHAR(128) DEFAULT NULL,
    reviewer_name         VARCHAR(128) DEFAULT NULL,
    reviewer_comment      TEXT DEFAULT NULL,
    published_rag_key     VARCHAR(1024) DEFAULT NULL,
    bailian_file_id       VARCHAR(128) DEFAULT NULL,
    created_by            VARCHAR(128) DEFAULT NULL,
    reviewed_at           DATETIME DEFAULT NULL,
    published_at          DATETIME DEFAULT NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_faq_id (faq_id),
    INDEX idx_source_doc (source_doc_id, source_version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS qa_session_log (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    session_id          VARCHAR(128) NOT NULL,
    message_id          VARCHAR(128) DEFAULT NULL,
    user_id             VARCHAR(128) DEFAULT NULL,
    user_dept           VARCHAR(64) DEFAULT NULL,
    query_text          TEXT DEFAULT NULL,
    answer_text         MEDIUMTEXT DEFAULT NULL,
    intent_type         VARCHAR(64) DEFAULT NULL,
    risk_level          VARCHAR(32) DEFAULT NULL,
    risk_blocked        TINYINT(1) DEFAULT 0,
    retrieved_docs_json JSON DEFAULT NULL,
    cited_docs_json     JSON DEFAULT NULL,
    latency_ms          INT DEFAULT 0,
    answer_status       VARCHAR(32) DEFAULT 'SUCCESS',
    content_blocks_json MEDIUMTEXT DEFAULT NULL COMMENT '图文渲染块 JSON 快照',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session (session_id),
    INDEX idx_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

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
    feedback_type       VARCHAR(32) DEFAULT NULL COMMENT 'thumb_up / thumb_down',
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

-- ──────────────────────────────────────────────────────────────
-- 6. Batch LLM Processing Tables
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS batch_llm_job (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id              VARCHAR(100) NOT NULL,
    batch_id            VARCHAR(128) DEFAULT NULL,
    request_file_id     VARCHAR(128) DEFAULT NULL,
    request_oss_key     VARCHAR(1024) DEFAULT NULL,
    output_file_id      VARCHAR(128) DEFAULT NULL,
    job_status          VARCHAR(32) DEFAULT 'PENDING',
    total_items         INT DEFAULT 0,
    success_items       INT DEFAULT 0,
    failed_items        INT DEFAULT 0,
    error_message       TEXT DEFAULT NULL,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_job_id (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS batch_llm_item (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id        VARCHAR(100) NOT NULL,
    custom_id     VARCHAR(128) DEFAULT NULL,
    doc_id        VARCHAR(100) DEFAULT NULL,
    version_no    INT DEFAULT NULL,
    raw_key       VARCHAR(1024) DEFAULT NULL,
    item_status   VARCHAR(32) DEFAULT 'PENDING',
    result_json   JSON DEFAULT NULL,
    error_message TEXT DEFAULT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_job_custom (job_id, custom_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ──────────────────────────────────────────────────────────────
-- 7. Downstream RAG-Specific Pipeline Support Tables
-- ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS opensearch_bulk_job (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id                VARCHAR(128) NOT NULL,
    index_name            VARCHAR(128) NOT NULL,
    total_chunks          INT DEFAULT 0,
    success_count         INT DEFAULT 0,
    fail_count            INT DEFAULT 0,
    status                VARCHAR(32) DEFAULT 'PENDING'
      COMMENT 'PENDING / RUNNING / COMPLETED / PARTIAL_FAIL / FAILED',
    payload_oss_key       VARCHAR(1024) DEFAULT NULL,
    payload_size_bytes    INT DEFAULT NULL,
    error_message         TEXT DEFAULT NULL,
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at          DATETIME DEFAULT NULL,
    UNIQUE KEY uk_job_id (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='OpenSearch bulk 索引任务跟踪';

CREATE TABLE IF NOT EXISTS document_sensitive_finding (
    id                    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id                VARCHAR(100) NOT NULL,
    version_no            INT NOT NULL,
    finding_type          VARCHAR(64) NOT NULL
      COMMENT 'cn_mobile / cn_id_card / email / bank_card / credential ...',
    severity              VARCHAR(32) NOT NULL
      COMMENT 'high / medium / low',
    page_num              INT DEFAULT NULL,
    block_index           INT DEFAULT NULL,
    matched_text_hash     VARCHAR(128) DEFAULT NULL
      COMMENT 'SHA-256 of matched text (never store raw PII)',
    matched_text_preview  VARCHAR(255) DEFAULT NULL
      COMMENT 'Masked preview',
    action                VARCHAR(32) NOT NULL
      COMMENT 'REDACTED / QUARANTINED / IGNORED',
    created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_doc_version (doc_id, version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='敏感信息检测结果';

SET FOREIGN_KEY_CHECKS = 1;
