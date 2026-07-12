-- schema/036_agent_run_message_id.sql
-- 库：fuling_operation
-- U1/U2（重审计 2026-07-11 §5 UX）：agent_run 增 message_id——run 与 qa_session_log
--   答案行的锚定键，一次修复两个断点：
--   ① 答案读回：审批续跑/断线后发起人此前经 /api/agent/runs/{id} 只能恢复状态不能
--      恢复答案文本（agent_run 无任何指向答案的键）——现在 run 详情经 message_id 从
--      qa_session_log 取回 answer_text（owner/kb_admin 门禁不变）；
--   ② 反馈锚定：审批续跑的 _remember 此前用**新生成** message_id 落库，而前端反馈
--      投票回填的是原 session 帧的旧 id → 👍/👎 挂在 qa_session_log 里不存在的
--      message_id 上；现在 submit 时回填本列、resume 复用同一 id。
--
-- ⚠️ 部署顺序：先 apply 后部署（run_store.get_run SELECT 引用本列；未迁移库上新代码
--   get_run 会整体报错）。历史行 message_id=NULL → 读回退化 None、续跑回退新 id
--   （代码侧显式兜底）。ADD COLUMN 幂等语义由 apply_migration.py 的
--   information_schema 预检 + 重复执行 no-op 保障。

ALTER TABLE agent_run
  ADD COLUMN message_id VARCHAR(64) NULL DEFAULT NULL
    COMMENT 'qa_session_log 答案行锚定键（submit 回填；resume 复用；036）'
    AFTER conversation_id;
