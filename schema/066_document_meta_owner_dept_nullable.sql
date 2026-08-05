-- ════════════════════════════════════════════════════════════════════════════
-- 066_document_meta_owner_dept_nullable.sql — 现网漂移纠正：document_meta.owner_dept 放开 NULL
-- 库：fuling_knowledge（document_meta 的归属库，见 schema/README.md 矩阵）
--
-- ✅ **2026-08-04 23:26:10 已 apply 生产**（Sam 当日授权 PROD-RW:2026-08-04，经
--    scripts/apply_migration.py，checksum=6f5ad8c8…）。apply 后核实：owner_dept
--    IS_NULLABLE=YES、存量 1938 行 0 NULL 0 空串 ⇒ **既有行逐字节不变**。
-- ✅ **2026-08-04 23:28:24 staging 亦记台账**（fuling_knowledge_stg）——但 **ALTER 未执行**：
--    幂等守卫判定该列已可空（COLUMN_COMMENT 仍为空 = 从未被本迁移改写）。
--    ⇒ **staging 当年按仓库权威 DDL 建表，没有此漂移；偏的只有生产那一张表**。
--    这条实测是本次漂移「生产独有」结论的直接证据，别删。
--
-- ⚠️ 这不是新特性，是**把生产表拉回权威 DDL**。schema/001_opensearch_pipeline.sql:69
--    自第一个提交（4e4a65c）起就写的是 `owner_dept VARCHAR(64) DEFAULT NULL`，而生产
--    实际是 `varchar(64) NOT NULL`（2026-08-04 实测 information_schema）。生产表当年
--    由另一份更"富"的 DDL 建出（还带 COLUMN_COMMENT，001 里没有），这条分歧从建库起
--    就在，只是**在今天之前每个写入方都会给 owner_dept 赋值**，所以没人踩到。
--
-- ── 事故（2026-08-04，全员上传中断）────────────────────────────────────────────
-- node 归属登记（routes/kb_console.py::kb_register 的 `node_owner_id is not None` 分支）
-- 是史上第一个往该列写 NULL 的调用方——按 schema/060 的设计，node 模式下归属由
-- `owner_dept_id` 表达，`owner_dept` 置 NULL 正是为了**不让 node 文档的组码残值命中
-- legacy 管理员的作用域 SQL**（060 设计稿 §3.5 的越权洞）。
--
--   INSERT INTO document_meta (..., owner_dept, ...) VALUES (%s,%s,%s,NULL,...)
--     → 生产 sql_mode 含 STRICT_TRANS_TABLES（不静默转空串）
--     → ERROR 1048 Column 'owner_dept' cannot be null
--     → kb_console.py 外层 except → HTTP 500「登记失败 (trace: …)」
--     → 前端 uploadErrText 无 500 分支 → 兜底文案「上传失败，请稍后重试」
--
-- 实测佐证：OSS `raw/node-34265162/` 下 13 个孤儿对象（upload-url 成功 + 直传成功 +
-- 登记事务全数回滚）；`document_meta` acl_mode 分布 legacy 1938 / node **0**；
-- `kb_doc_node_grant` **0 行**（事务死在第 3 条语句，走不到授权那步）。
--
-- ⚠️ 为什么桩库测试抓不到：tests/test_kb_register.py 的 _FakeCur 不校验 NOT NULL。
--    这类"现网约束比权威 DDL 更严"的缺陷只有真 MySQL 能抓 ⇒ node 上传的端到端用例
--    必须落在 tests/test_kb_db_integration.py（CI db-integration 门），不是桩库套件。
--
-- ── 影响面（放松约束，方向安全）──────────────────────────────────────────────
-- ADDITIVE-EQUIVALENT：只放开可空性，不改类型/长度/字符集/索引，不动任何存量行
-- （实测 1938 行：0 个 NULL、0 个空串 —— 放开后存量逐字节不变）。
-- 读侧：_kb_doc_owner_scope_sql 的 legacy 腿是 `acl_mode='legacy' AND owner_dept IN (…)`，
--   NULL 永不命中 ⇒ node 文档只能经 node 腿可达（正是 060 要的语义）。
--   idx_owner_dept 保留（MySQL 的 B-tree 索引 NULL 可入）。
-- ⚠️ 待验证的下游（本迁移不负责，见 apply 后核查清单）：看板 kb_insights / qa_facts
--   的 owner_dept 归属聚合遇 NULL 的表现（2026-08-03 组织树重设计已改 kind-aware 解析，
--   需实测确认 node 文档不落进"未知"桶而是走 owner_dept_id）。
--
-- ── 顺带记录的另一处漂移（本次**不动**）──────────────────────────────────────
-- `document_meta.title` 生产为 NOT NULL、001 权威写 `DEFAULT NULL`。所有现存写入方
-- 都必给 title，无实际风险，故不在本次事故修复范围内扩大改动面；单列入 schema 漂移
-- 待办，与 owner_dept 一并在下次全表 parity 核对时处理。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge

-- 幂等守卫（与 060/062/064 同款内联 PREPARE 形态）：MySQL 8 的 MODIFY COLUMN 没有
-- IF 条件式，裸重跑虽不报错但会白白重建一次表定义并覆盖 COMMENT——按 information_schema
-- 现状判定，已可空即跳过。
-- ⚠️ 不用 CALL _add_column_if_not_exists 这类 helper：它们建在 fuling_operation，
--    knowledge 库里 CALL 会 1305 PROCEDURE does not exist（schema/060 头注同款教训）。
SET @c = (SELECT COUNT(*) FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'document_meta'
            AND COLUMN_NAME = 'owner_dept'
            AND IS_NULLABLE = 'NO');

SET @ddl = IF(@c = 1,
    "ALTER TABLE document_meta MODIFY COLUMN owner_dept VARCHAR(64) NULL DEFAULT NULL COMMENT '归属部门组码（acl_mode=legacy 时权威，如 hr/finance/admin/it/production/quality/rd/marketing/pmc）；acl_mode=node 时为 NULL——归属改由 owner_dept_id 表达，置 NULL 是为了让组码残值不命中 legacy 管理员的作用域 SQL，见 schema/060'",
    "SELECT 'document_meta.owner_dept 已可空，跳过' AS _skip");

PREPARE _s FROM @ddl;
EXECUTE _s;
DEALLOCATE PREPARE _s;

-- ── 回滚 ────────────────────────────────────────────────────────────────────
-- 收紧回 NOT NULL 只有在**库内没有任何 owner_dept IS NULL 的行**时才可能，否则 1138。
-- 即：一旦有 node 文档登记成功，本迁移事实上不可逆（需先把那些文档退役/迁回 legacy）。
-- 应用前请确认 node 上传确实是要开的路径。
--   SELECT COUNT(*) FROM document_meta WHERE owner_dept IS NULL;   -- 必须为 0 才能收紧
--   ALTER TABLE document_meta MODIFY COLUMN owner_dept VARCHAR(64) NOT NULL COMMENT '归属部门，如 hr/finance/admin/it/production/quality/rd/marketing/pmc';
