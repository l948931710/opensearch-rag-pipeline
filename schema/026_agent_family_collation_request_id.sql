-- schema/026_agent_family_collation_request_id.sql
-- 库：fuling_operation
-- 深度审查 2026-07-09 schema 组收尾：
--
-- ① agent 表族 COLLATE 显式统一（9 表 → utf8mb4_unicode_ci，README 铁律 4 的库规范值）。
--    022 五表 + 025 两表建表未显式 COLLATE、吃库默认——在规范环境（ci_load_schema 显式建库
--    unicode_ci）恰好继承对；但在库级漂移的环境（本地 docker 实测 0900_ai_ci；staging 曾因
--    同类漂移出跨库 JOIN 1267）会与显式 unicode_ci 的 023/024 族内混排：agent_run.run_id ↔
--    llm_call_log.run_id / agent_audit_log.run_id 的 JOIN 即 1267（retention findings 作业
--    在 kb 库踩过的同一坑）。本迁移把 9 表全部显式钉到 unicode_ci——表级显式后不再受库默认
--    摆布，规范环境是 no-op 重建（幂等可重跑）。agent_checkpoint 的 MEDIUMBLOB 是二进制列，
--    不受字符集转换影响。
--    ⚠️ 已知残留：漂移环境里同库 qa 族仍是库默认（0900_ai_ci）——agent↔qa 无 SQL JOIN
--    （thread_id/session_id 关联在 Python 侧），不在本迁移波及面；qa 族统一另立迁移评审。
--
-- ② request_id 加宽 VARCHAR(32) → VARCHAR(64)：带连字符 UUID 是 36 字符，原宽度装不下，
--    代码侧靠 [:32] 截断保命——损失溯源精度。加宽后代码钳制同步放宽到 [:64]
--    （routes/agent.py；钳制保留是防未 apply 本迁移的环境 1406）。

ALTER TABLE tool_registry     CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE agent_run         CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE agent_step        CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE agent_checkpoint  CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE tool_invocation   CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE approval_request  CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE approval_decision CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE llm_call_log      CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE agent_audit_log   CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

ALTER TABLE llm_call_log    MODIFY request_id VARCHAR(64) NULL COMMENT 'X-Request-Id';
ALTER TABLE agent_audit_log MODIFY request_id VARCHAR(64) NULL COMMENT 'X-Request-Id';
