-- ════════════════════════════════════════════════════════════════════════════
-- 010_kb_contribution.sql — 员工知识贡献（众包问答 → 部门管理员采纳 → 走管线入库）
--
-- ADDITIVE / IDEMPOTENT-on-apply（与 003/004/006/008 同纪律）：新建一张表，不改既有列/行、
-- 不动检索/上传路径、不重建 HA3 → 对现有问答/上传/小程序用户零影响。CREATE TABLE IF NOT EXISTS 原生幂等。
--
-- 语义：普通员工在「知识贡献」页看到「缺失知识」（答不出的提问，来自 qa_session_log
--   NO_RESULT/REFUSAL），提交【问题 + 答案文本】；由【目标部门管理员】（category_dept ∈ 其
--   managed_owner_depts）或 kb_admin 采纳；采纳后后端合成一篇 .md 文档→走现有 DataWorks 管线入库。
--
-- ⚠️ 两条【彻底解耦】的生命周期，绝不用单一 status 同时表达（开工前审查收紧）：
--    · review_status     管理员决策：pending → accepted | rejected
--    · ingestion_status  物化进度（仅 accepted 后有意义）：none → registering → registered → searchable | failed
--    缺口【真正关闭】= 存在同 question_hash 且 ingestion_status='searchable' 的贡献（accepted 但未
--    searchable 时缺口仍在，前端标「已有贡献·等待入库」；failed 标「入库失败」并可重试）。
--
-- ⚠️ 采纳=幂等可恢复状态机（不假设 OSS+MySQL 跨系统原子）：首次采纳【一次性固定】并落库
--    doc_id/upload_id/raw_key；OSS 写入与 document_meta/version 登记全部用固定键幂等执行；
--    失败记 ingestion_error，重复调用按固定键续跑、绝不生成第二篇文档（doc_id UNIQUE 兜底）。
--
-- ⚠️ DB：fuling_operation（与 qa_session_log/user_feedback/escalation_ticket 同库——用户生成的
--    运营记录；缺口去重 JOIN qa_session_log 为同库；采纳后 reconcile JOIN fuling_knowledge.document_version
--    为跨库，同实例可跑）。COLLATE 必须显式 utf8mb4_unicode_ci（与全库一致；doc_id 跨库 JOIN
--    document_version、question_hash 比对都依赖它，否则报 1267 Illegal mix of collations）。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_operation
CREATE TABLE IF NOT EXISTS kb_contribution (
    id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    contribution_id      VARCHAR(100) NOT NULL COMMENT '贡献 ID = CONTRIB_<ULID>（时间可排序、无碰撞）',
    question             VARCHAR(512) NOT NULL COMMENT '员工填的问题（= 合成文档标题/正文标题）',
    content              MEDIUMTEXT   NOT NULL COMMENT '员工填的答案/知识内容（= 合成文档正文）',
    normalized_question  VARCHAR(512) DEFAULT NULL COMMENT 'NFKC+去空白标点+小写后的问题（去重/匹配用）',
    question_hash        CHAR(64)     DEFAULT NULL COMMENT 'sha256(normalized_question)，与缺口列表按 hash 对齐去重',

    category_dept        VARCHAR(64)  NOT NULL COMMENT '目标 owner_dept（写白名单校验；员工可选/管理员采纳前可改）',
    suggested_dept       VARCHAR(64)  DEFAULT NULL COMMENT '系统建议归属（来源提问部门，仅建议，不代表真 owner_dept）',
    author_id            VARCHAR(128) NOT NULL COMMENT '提交人钉钉 staffId',
    author_name          VARCHAR(128) DEFAULT NULL COMMENT '提交人显示名（仅审计/展示/英雄榜，绝不写进可检索正文）',

    review_status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending | accepted | rejected',
    ingestion_status     VARCHAR(16)  NOT NULL DEFAULT 'none'    COMMENT 'none | registering | registered | searchable | failed',
    reviewed_by          VARCHAR(128) DEFAULT NULL COMMENT '采纳/驳回操作人 staffId',
    reviewed_at          DATETIME     DEFAULT NULL,
    review_note          VARCHAR(512) DEFAULT NULL COMMENT '采纳/驳回备注',

    doc_id               VARCHAR(128) DEFAULT NULL COMMENT '采纳时一次性固定的合成文档 ID（fuling_knowledge.document_meta.doc_id）',
    upload_id            VARCHAR(64)  DEFAULT NULL COMMENT '固定 upload_id（raw_key 第 4 段），幂等续跑用',
    raw_key              VARCHAR(512) DEFAULT NULL COMMENT '固定 raw_key（OSS 对象键），幂等 put/register 用',
    ingestion_error      VARCHAR(512) DEFAULT NULL COMMENT '物化/登记失败原因（供重试与排障）',
    registered_at        DATETIME     DEFAULT NULL COMMENT 'document_version 登记完成时刻',
    searchable_at        DATETIME     DEFAULT NULL COMMENT 'DAG 索引成功、reconcile 观测到 SUCCESS 的时刻',

    source_message_id    VARCHAR(128) DEFAULT NULL COMMENT '若从某条缺口「回答」而来，记录来源提问 message_id',
    gap_query            VARCHAR(512) DEFAULT NULL COMMENT '来源缺口问题原文（去重/溯源）',
    -- F-35 修复（2026-07-01）：生产表自始有此列（scratch/apply_migration_010.py 建表含它、
    -- 提交端点 INSERT 依赖它），本 DDL 此前漏写 → 按 schema/ 重建的环境提交贡献必 1054。
    normalized_gap_query VARCHAR(512) DEFAULT NULL COMMENT 'normalize_question(gap_query)（NFKC+去空白标点+小写），缺口归并展示/比对用',
    gap_query_hash       CHAR(64)     DEFAULT NULL COMMENT 'sha256(normalize(gap_query))，与缺口列表对齐',

    created_at           DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_contribution_id (contribution_id),
    UNIQUE KEY uk_doc_id (doc_id),                          -- 一条贡献至多一篇文档；防幂等续跑/竞态重复出文档
    INDEX idx_dept_review (category_dept, review_status),   -- 部门管理员审核队列
    INDEX idx_author (author_id),                           -- 我的贡献 / 英雄榜
    INDEX idx_qhash (question_hash),                        -- 缺口去重对齐
    INDEX idx_ingest (ingestion_status)                     -- reconcile 扫 registered→searchable
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='员工知识贡献（众包问答→部门管理员采纳→走管线入库）；review/ingestion 双生命周期解耦';
