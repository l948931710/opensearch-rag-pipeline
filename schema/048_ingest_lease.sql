-- ════════════════════════════════════════════════════════════════════════════
-- 048_ingest_lease.sql — PR-4 W1：摄取台账租约/栅栏列（lease + fencing epoch）
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 动机（PR-4 立项，docs/ingest_lease_fencing_scope_2026-07-17.md；源头=效率评审核查
--   docs/audits/perf_efficiency_review_verification_2026-07-17.md G5）：
--   摄取两本台账（content_process_status＝stage-1/2、index_status＝stage-3）的互斥
--   此前只有一次性 CAS 认领 + 2h 年龄式失效接管——没有运行中续租（>2h 的存活运行被
--   当尸体接管）、没有持有者身份、没有写回栅栏（被接管的僵尸继续写回：撕裂
--   write_chunk_meta 的 DELETE→INSERT、终态互相覆盖，最坏基于陈旧视图跑
--   node_deactivate_old_chunks 的不可逆 HA3 删除）。本迁移落租约三列，协议在
--   opensearch_pipeline/ingest_lease.py。
--
-- 语义：
--   · 认领单位=（doc_id, version_no）行本身；同一 dv 行同一时刻只处于一个 stage，
--     故一套租约列同时服务两本台账（chunk_meta 不加列，dv 级租约覆盖其下所有写）；
--   · lease_holder/lease_expires_at：语义同 agent_dispatch_command（schema/043）——
--     到期未续=持有者已死，接管谓词按此放行；NULL=旧包/未启用租约的认领，
--     清扫回退 2h updated_at 年龄兜底（新旧混跑无缝）；
--   · lease_epoch：fencing token——每次认领/接管 +1；终态写与破坏性写带
--     `AND lease_holder=? AND lease_epoch=?` 谓词（多语句写回改同事务 FOR UPDATE
--     验租），丢锁僵尸的写回原子性失败（rowcount=0 → LeaseLost，弃单文档继续批）；
--   · 所有租约时间算术在 SQL 侧（NOW(3)），Python 永不比较墙钟——无时钟偏斜问题。
--
-- ADDITIVE：三列 ALTER（NULL/0 默认，存量行零回填）+ 一个辅助索引。
--   flag（RAG_INGEST_LEASE_ENABLE，默认 off）关闭时代码不读写本组列——可先 apply
--   后部署。非幂等 DDL：重复执行由 scripts/apply_migration.py 的 information_schema
--   预检跳过（同 031 约定）。
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE document_version
  ADD COLUMN lease_holder     VARCHAR(64)  DEFAULT NULL
    COMMENT 'PR-4 认领方实例 id（host-pid-uuid8）；NULL=未启用租约的认领（走 2h 年龄兜底）'
    AFTER error_message,
  ADD COLUMN lease_expires_at DATETIME(3)  DEFAULT NULL
    COMMENT 'PR-4 租约到期（SQL 侧 NOW(3) 算术）；过期=持有者已死，可接管'
    AFTER lease_holder,
  ADD COLUMN lease_epoch      INT UNSIGNED NOT NULL DEFAULT 0
    COMMENT 'PR-4 fencing token：每次认领/接管 +1；写回带 holder+epoch 谓词'
    AFTER lease_expires_at;

-- 接管/观测扫描辅助（清扫谓词仍以 status 列打头，选择性来自既有
-- idx_content_process / idx_index_status；本索引服务 lease_expires_at 范围过滤与巡检）。
ALTER TABLE document_version
  ADD KEY idx_lease_expiry (lease_expires_at);
