-- schema/050_qa_rewritten_query.sql
-- 库：fuling_operation
-- 多轮追问检索前改写（RAG_FOLLOWUP_REWRITE，默认关；2026-07-18）：
--   「那第二步呢」这类指代性追问此前检索/落库都只有无上下文短文本——检索空手 →
--   缺口队列沉淀的也是无法认领的短问题。改写发生在 serving 检索前（query_rewriter），
--   本列存改写后的独立问题（**脱敏后**文本，_redact_for_log 同口径），原始 query_text
--   原样保留（审计保真）。
-- hash 语义（039 约束延续）：改写实际发生的行，question_hash = hash(脱敏后 rewritten)
--   ——该行「实际所问的问题」就是改写后的独立形式，缺口归并按真实意图；未改写行为
--   逐字节不变。绝不对脱敏前原文计算。
-- 部署顺序：**代码可先行**（qa_logger 对本列走 1054 **TTL** 负缓存降级——与
--   question_hash 的永久负缓存不同，TTL 到期自动重试，schema apply 后无须重启即恢复；
--   读侧 kb_gaps 对本列 1054 自动回退无列 SQL）。无存量 backfill（历史行无改写可补）。

ALTER TABLE qa_session_log
  ADD COLUMN rewritten_query TEXT DEFAULT NULL
    COMMENT '追问改写后的独立问题（脱敏后文本；RAG_FOLLOWUP_REWRITE 开且改写发生才非空）'
    AFTER question_hash;
