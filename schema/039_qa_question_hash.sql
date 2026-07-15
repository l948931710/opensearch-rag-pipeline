-- schema/039_qa_question_hash.sql
-- 库：fuling_operation
-- 缺口语义去重 Layer-1（pmc 远期立项落地，2026-07-15；与「待回答 Top30」ε-4 同源）：
--   qa_session_log 落归一化问题哈希列。此前不同问法/同问法的缺口归并全靠读侧把
--   NO_RESULT/REFUSAL 候选行拉回 Python 逐条 normalize+sha256（365 天窗 × 2000 cap），
--   同一问题的历史热度被窗口和 cap 截断，「出窗未答计数」不可行（空态免责副题的根因）。
-- 修法：写侧落列（qa_logger 对【脱敏后落库文本】计算 contribution.question_hash 同款
--   口径——与 kb_contribution.question_hash / kb_gaps Python 归并 / asks 平查完全同源，
--   绝不能对脱敏前原文计算，否则与读侧复算对不上）；存量行由
--   scratch/backfill_qa_question_hash_20260715.py（dry-run 默认，--commit 分批 UPDATE）回填。
-- 部署顺序：**代码可先行**（qa_logger 走 1054 负缓存降级，列缺失自动回退旧列集，
--   审计行绝不丢——gen_meta_json/schema 018 同款纪律）；apply 后新行自动带 hash，
--   再跑 backfill 补存量。索引供 asks 平查（hash IN）与后续 SQL 侧 GROUP BY。

ALTER TABLE qa_session_log
  ADD COLUMN question_hash CHAR(64) DEFAULT NULL
    COMMENT '归一化问题哈希（contribution.question_hash 口径，对脱敏后 query_text 计算）；039 起写侧落列，存量走 backfill 脚本'
    AFTER query_text,
  ADD KEY idx_qa_question_hash (question_hash);
