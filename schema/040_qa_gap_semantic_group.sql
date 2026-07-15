-- schema/040_qa_gap_semantic_group.sql
-- 库：fuling_operation
-- 缺口语义去重 Layer-2（pmc 远期立项落地，2026-07-15）：相似问法语义组映射表。
--   字面归一化（039 的 question_hash）只并得了「如何申请密钥」vs「如何 申请密钥！」；
--   「怎么开发票」vs「发票怎么开」这类相似问法各占一坑——同一需求被拆散，
--   排名进不了 Top30。本表把相似问法映射到同一 group_id，kb_gaps 读侧按组归并展示。
-- ⚠️ 预注册边界：**仅展示层归并，绝不驱动自动关闭**——缺口关闭仍走 exact question_hash
--   （kb_contribution 覆盖判定零改动）。语义相似≠同一问题，自动跨问法关闭有假关风险；
--   回答代表问法后其余成员仍开放，由后续贡献逐个覆盖。
-- 生成：scratch/build_qa_gap_semantic_groups_20260715.py（text-embedding-v4 dense 余弦 +
--   贪心归组，阈值 RAG_QA_GAP_SEMANTIC_THRESHOLD 默认 0.90 保守；dry-run 默认，--commit 写表）。
--   读侧 RAG_QA_GAP_SEMANTIC 默认关；表缺失/查询失败 → 不归并原样展示（fail-open）。
-- group_id 取组内权重（提问次数）最高成员的 question_hash（代表即成员，无独立 ID 体系）。

CREATE TABLE IF NOT EXISTS qa_gap_semantic_group (
    question_hash        CHAR(64) NOT NULL PRIMARY KEY
        COMMENT '成员问法 hash（contribution.question_hash 口径，=qa_session_log.question_hash）',
    group_id             CHAR(64) NOT NULL
        COMMENT '语义组代表 hash（组内提问次数最高成员的 question_hash；代表行 group_id=自身）',
    representative_text  VARCHAR(512) DEFAULT NULL
        COMMENT '组代表问法快照（生成时已脱敏文本，供脚本/排障；读侧展示用成员现值不用它）',
    similarity           DECIMAL(6,4) DEFAULT NULL
        COMMENT '与组代表的余弦相似度（代表自身=1.0000）',
    model                VARCHAR(64) DEFAULT NULL COMMENT '归组 embedding 模型（如 text-embedding-v4）',
    grouped_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_gap_group (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='缺口相似问法语义组映射（展示层归并专用，绝不驱动自动关闭；schema/040）';
