-- ════════════════════════════════════════════════════════════════════════════
-- 008_kb_access_request.sql — 跨部门文档检索授权申请（Phase C 记录层）
--
-- ADDITIVE / IDEMPOTENT-on-apply（与 003/004/006 同纪律）：新建一张表，不改既有列/行、
-- 不动检索路径、不重建 HA3 → 对现有问答/上传用户零影响。CREATE TABLE IF NOT EXISTS 原生幂等。
--
-- 语义：部门管理员在「全部门」只读浏览里，对【其他部门】的 dept_internal 文档发起检索授权申请；
--   由【文档所属部门管理员】（owner_dept ∈ 其 managed）或 kb_admin 审批（与三分原则一致：
--   owner 对自己文档的读暴露做主，不从读组推导写权）。
--
-- ⚠️ 本表只是【决策/审计记录层】。审批通过【不】立即放行检索 —— 真正让申请部门能检索到该文档，
--    需要把授予的部门写进文档 allowed_depts 并接入 retriever 的 HA3 ACL 过滤（= Phase D，
--    gated，不可逆 HA3 改动，单独授权）。在 Phase D 落地前，approved 行仅表示「已批准、待放行」。
--
-- ⚠️ DB：fuling_knowledge（与 document_meta / dept_admin_grant 同库，doc_id 指向 document_meta）。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge
CREATE TABLE IF NOT EXISTS kb_access_request (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id          VARCHAR(128) NOT NULL COMMENT '申请访问的文档（fuling_knowledge.document_meta.doc_id）',
    owner_dept      VARCHAR(64)  NOT NULL COMMENT '文档归属 = 审批方（其部门管理员或 kb_admin 审批）',
    requester_id    VARCHAR(128) NOT NULL COMMENT '申请人钉钉 staffId',
    requester_name  VARCHAR(128) DEFAULT NULL COMMENT '申请人显示名（审计/展示）',
    requester_depts VARCHAR(255) NOT NULL COMMENT '申请获得检索权的部门（逗号分隔，= 申请人 managed_owner_depts）',
    reason          VARCHAR(512) DEFAULT NULL COMMENT '申请理由',
    status          VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending / approved / rejected / revoked（revoked=已批准后撤销；只 approved 计入放行，列宽足够无需 ALTER）',
    decided_by      VARCHAR(128) DEFAULT NULL COMMENT '审批 / 撤销操作人 staffId',
    decided_at      DATETIME     DEFAULT NULL COMMENT '审批 / 撤销时间',
    decision_note   VARCHAR(512) DEFAULT NULL COMMENT '审批备注 / 驳回 / 撤销原因',
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_owner_status (owner_dept, status),     -- 审批方列待审批队列
    INDEX idx_requester (requester_id, status),       -- 申请人查自己的申请 / 去重 pending
    INDEX idx_doc (doc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='跨部门文档检索授权申请（Phase C 记录层；真正放行检索 = Phase D allowed_depts）';
