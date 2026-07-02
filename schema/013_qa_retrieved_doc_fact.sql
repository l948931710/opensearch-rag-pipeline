-- ════════════════════════════════════════════════════════════════════════════
-- 013_qa_retrieved_doc_fact.sql — 检索/引用文档物化事实表（perf#3，2026-07-01 批次）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机：看板/缺口归属链（retrieved_docs_json→doc_id→document_meta.owner_dept）在
-- 6 处热查询（kb_insights×4 / kb_governance×2 / kb_gaps REFUSAL）里每请求现场
-- JSON_TABLE 展开 + CONVERT(...) COLLATE 跨库 JOIN——JSON_TABLE 不可索引，窗口内
-- 每行都要解析 JSON，成本随 qa_session_log 无界增长；且长期背着 JSON_TABLE 输出串
-- 默认 _0900_ai_ci 的 collation 陷阱（1267 家族）。qa_logger 写日志时本就持有
-- retrieved_docs/cited_docs 列表，顺手物化瘦行后，归属查询变普通索引 JOIN。
--
-- 行语义：每条回答 × 每个去重 doc_id × {retrieved=0 | cited=1} 一行；
--   PK 去 chunk 扇出（top_k=7 中同 doc 多 chunk 只落一行，计数口径仍由查询端
--   COUNT(DISTINCT q.message_id) 决定，两种 JOIN 形态结果一致）。
--
-- ⚠️ collation 纪律（README 铁律 4）：两个 JOIN 键显式声明——
--   message_id ↔ fuling_operation.qa_session_log.message_id（002 = utf8mb4_unicode_ci）
--   doc_id     ↔ fuling_knowledge.document_meta.doc_id     （001 = utf8mb4_unicode_ci）
--   apply 前请核对生产两列实际 collation（information_schema.COLUMNS），若历史漂移为
--   _0900_ai_ci，本表列需改成与其一致（否则索引 JOIN 报 1267 → 代码自动回退 JSON_TABLE）。
--
-- 配套（代码侧，同批次落地）：
--   · 写：qa_logger.log_qa_session → qa_facts.insert_qa_doc_facts（fail-open，1146 熔断）
--   · 读：RAG_QA_FACT_JOIN=true + 表探测通过 → 索引 JOIN；否则 JSON_TABLE 回退（默认）
--   · 留存：retention.py 'qa_facts' 作业（RAG_RETENTION_QA_FACTS_MONTHS，默认 18 月同 qa_rows）
--
-- 部署顺序：apply 本文件（建表+回填）→ 部署新包 → SAE 注入 RAG_QA_FACT_JOIN=true →
--   重跑一次下方回填段（INSERT IGNORE 幂等），补「apply 与新包上线之间」窗口的空档行。
--
-- ADDITIVE / 幂等：CREATE TABLE IF NOT EXISTS + INSERT IGNORE。⚠️ GATED PROD DDL——
-- apply 后向 fuling_operation.schema_migrations 记 '013'。
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS qa_retrieved_doc (
    message_id  VARCHAR(128)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL
                COMMENT '↔ qa_session_log.message_id',
    doc_id      VARCHAR(100)
                CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL
                COMMENT '↔ fuling_knowledge.document_meta.doc_id',
    cited       TINYINT(1) NOT NULL DEFAULT 0 COMMENT '0=retrieved（召回） 1=cited（实际引用）',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, doc_id, cited),
    INDEX idx_doc_created (doc_id, created_at),
    INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='qa 回答 × 文档 归属事实行（perf#3）：看板/缺口归属 JOIN 的物化承载';

-- ── 存量回填（幂等，可重跑）────────────────────────────────────────────────────
-- created_at 取父行时刻，使回填行与增量行在窗口查询里同一口径。
-- jt.doc_id 输出串（_0900_ai_ci）到 _unicode_ci 目标列是 INSERT 赋值转换，无 1267 风险。

INSERT IGNORE INTO qa_retrieved_doc (message_id, doc_id, cited, created_at)
SELECT q.message_id, jt.doc_id, 0, q.created_at
  FROM qa_session_log q
  JOIN JSON_TABLE(q.retrieved_docs_json, '$[*]'
       COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt
 WHERE q.retrieved_docs_json IS NOT NULL
   AND jt.doc_id IS NOT NULL AND jt.doc_id != '';

INSERT IGNORE INTO qa_retrieved_doc (message_id, doc_id, cited, created_at)
SELECT q.message_id, jt.doc_id, 1, q.created_at
  FROM qa_session_log q
  JOIN JSON_TABLE(q.cited_docs_json, '$[*]'
       COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt
 WHERE q.cited_docs_json IS NOT NULL
   AND jt.doc_id IS NOT NULL AND jt.doc_id != '';

-- 回滚：
-- DROP TABLE qa_retrieved_doc;   -- 代码侧探测失败自动回退 JSON_TABLE，无需回滚代码
