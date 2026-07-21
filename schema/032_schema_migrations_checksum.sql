-- schema/032_schema_migrations_checksum.sql
-- 库：三库各一份（fuling_knowledge / fuling_operation / fuling_ontology——与 011 台账表同分布）
-- PR-D（P0-09，2026-07-10 外审）：schema_migrations 加 checksum 列——apply_migration.py
-- 记录迁移文件 SHA-256，同名不同 checksum 即中止（"同版本内容被改过"从此机器可检；
-- 修订已发布文件须走 NNNa 修订号，README 铁律 2）。
-- 011 已发布生产，不改原文件（铁律 2），新列走本迁移；information_schema 守卫幂等
-- （MySQL 8 无 ADD COLUMN IF NOT EXISTS，用 PREPARE 动态跳过）。
-- 取号：031 已被本体事件（P2）预订，本文件取 032；下一可用号 = 033。

SET @has_tbl := (SELECT COUNT(*) FROM information_schema.TABLES
                 WHERE TABLE_SCHEMA = DATABASE()
                   AND TABLE_NAME = 'schema_migrations');
SET @has := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE()
               AND TABLE_NAME = 'schema_migrations'
               AND COLUMN_NAME = 'checksum');
SET @ddl := IF(@has_tbl = 1 AND @has = 0,
               'ALTER TABLE schema_migrations ADD COLUMN checksum CHAR(64) NULL COMMENT ''迁移文件 SHA-256（apply_migration.py 记录，防同版本内容漂移）''',
               'SELECT 1');
PREPARE _s FROM @ddl;
EXECUTE _s;
DEALLOCATE PREPARE _s;
