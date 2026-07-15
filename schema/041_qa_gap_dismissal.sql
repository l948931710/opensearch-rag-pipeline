-- schema/041_qa_gap_dismissal.sql
-- 库：fuling_operation
-- 「忽略此缺口」驳回机制（ε-4 遗留，2026-07-15 用户拍板：交给 dept_admin）：
--   「待回答」高频排行里的噪音问题（闲聊误入/一次性问题/不适合入库）此前无处置手段，
--   永远占坑。本表按 question_hash 记录管理员忽略动作，kb_gaps 读侧排除 active 行。
-- 权限：dept_admin/kb_admin（console 管理员，_require_kb_console）；员工不可。
-- 可撤销 + 留痕：revoked_at IS NULL = 生效中；撤销置 revoked_at/revoked_by（行不删，
--   谁忽略/谁恢复都可追溯）；重新忽略 = ON DUPLICATE KEY 复活同一行（last action wins）。
-- 语义组联动（RAG_QA_GAP_SEMANTIC 开时）：dismiss/restore 端点按 qa_gap_semantic_group
--   现值扩展到同组全部成员逐 hash 落行——否则归并卡片摘头后其余成员下轮换头复现
--   （打地鼠）。新问法（新 hash）不受既往忽略牵连——语义连坐永久静默是有意不做的。
-- 读侧失败 fail-open（忽略行复现，不 500）——宁可噪音回来，不弄丢真缺口。

CREATE TABLE IF NOT EXISTS qa_gap_dismissal (
    question_hash     CHAR(64) NOT NULL PRIMARY KEY
        COMMENT '被忽略问法 hash（contribution.question_hash 口径）',
    question_preview  VARCHAR(512) DEFAULT NULL
        COMMENT '问法快照（脱敏后，供审计知道忽略了什么）',
    reason            VARCHAR(255) DEFAULT NULL COMMENT '忽略原因（可选）',
    dismissed_by      VARCHAR(128) NOT NULL COMMENT '操作人 user_id',
    dismissed_by_name VARCHAR(128) DEFAULT NULL,
    revoked_at        TIMESTAMP NULL DEFAULT NULL COMMENT 'NULL=忽略生效中；非空=已恢复',
    revoked_by        VARCHAR(128) DEFAULT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_dismissal_active (revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='「待回答」缺口忽略台账（dept_admin 治理动作，可撤销留痕；schema/041）';
