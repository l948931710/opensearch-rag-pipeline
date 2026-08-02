-- ════════════════════════════════════════════════════════════════════════════
-- 061_node_owner_axis.sql — node-ACL 阶段 B（归属轴）：meta 投影 outbox + 管辖根候选表
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 阶段 B 设计=docs/permission_node_acl_design_2026-07-27_DRAFT.md §3.5/§5.2a，
-- codex-review 2026-08-01 四轮 APPROVE。060 已备齐 acl_mode/owner_dept_id/acl_revision/
-- dept_admin_node_grant/dept_dim/staff_dim；本迁移只补两张新表 + 一条注释扩展。
--
-- ── 1. kb_doc_meta_projection_outbox ────────────────────────────────────────
-- 为何需要（codex 阶段 B 评审 BLOCKER，2026-08-01）：doc-meta 端点改 title/category 后
--   把 chunk 标 NOT_INDEXED 是不够的——stage-3 的 loader 在 dag.run()（锁获取）**之前**
--   就把 title/category/chunk_text 读进内存（dataworks_orchestrator.py:454-605 → :610），
--   推完又按 chunk_id 无条件回写 INDEXED（pipeline_nodes.py:8271-8288）⇒ 窗口内落的
--   脏标记被本轮吞掉，HA3 永久保留旧元数据。修法与 009+049 同型：**持久投影意图 +
--   generation CAS**——端点同事务 enqueue；stage-3 在 loader 之前 pre-drain（同步
--   chunk_meta.category + 标 NOT_INDEXED 并记住 generation）；**仅在本轮 DAG 成功推送后**
--   按 (id, generation) CAS 标 processed——窗口中途复活的行 generation 已 +1、CAS 落空，
--   留到下轮重标重推（title 走 loader 的 document_meta JOIN 现读，重推幂等）。
--
-- 与 kb_acl_projection_outbox（009）分表而非复用：ACL outbox 的 drain 动作 =
--   materialize_doc_allowed_depts（改 allowed_depts/owner 投影），本表的 drain 动作 =
--   category 同步 + 重推标记，两者时机（pre-drain vs materialize）与失败语义不同，
--   混在一张表要靠 reason 分派、任何一处漏判都串味。generation 语义与 049 逐字一致。
--
-- ── 2. dept_admin_node_candidate ────────────────────────────────────────────
-- 为何独立候选表（codex 阶段 B 评审 major，2026-08-01）：dept_admin_node_grant 的全部
--   消费者天然把 is_active=1 理解为【有效权威】——把"待确认"行放进权威表，等于要求每个
--   授权查询永远记得追加 confirmed 条件，漏一处即静默提权。候选与权威分离后，权威表
--   语义不变（is_active=1 即有效），确认动作 = 事务内重校验（当前 role / 直属部门 /
--   快照 freshness 对比 derived_snapshot_rev / 规模 / manual override）后写权威表。
--
-- 自动派生规则（Sam 2026-07-28 裁决 T5 + 2026-08-01 确认全量实施，锁死五条）：
--   仅 active dept_admin 派生；恰 1 个直属部门才生成候选（0/多 ⇒ fail-closed 转人工）；
--   安全首派（非中心级 + 后代规模在阈内 + 无 manual override + 根未变）直接 upsert
--   权威表 source='auto'；根变更/中心级/超规模 ⇒ 撤旧 auto + 只写候选待 kb_admin 确认；
--   确认时 derived_snapshot_rev 与当前不符 ⇒ 409 + 按当前快照重派生返回最新候选（TOCTOU 关死）。
--
-- ── 3. acl_revision 注释扩展 ────────────────────────────────────────────────
-- doc-meta 端点每次实际变更（含 title/category-only）都推进 acl_revision——否则两个持同
--   revision 的并发元数据编辑第二个仍会通过，CAS 对元数据并发无效（codex major）。列注释
--   同步改为「文档管理面编辑 CAS」，类型/默认值不动（MODIFY 仅换 COMMENT，幂等安全）。
--
-- ADDITIVE：两张 CREATE TABLE IF NOT EXISTS 原生幂等；MODIFY COLUMN 重复执行无副作用。
-- ⚠️ 先部署后 apply 安全：代码侧对本迁移未 apply 走 1054 负缓存回退（与 049/050 同型）。
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS kb_doc_meta_projection_outbox (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id      VARCHAR(128) NOT NULL COMMENT '待重投影元数据的文档（document_meta.doc_id）',
    reason      VARCHAR(64)  DEFAULT NULL COMMENT '入队原因（title_changed/category_changed/…，审计）',
    generation  BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '代次：每次 enqueue/复活 +1；processed 标记按 (id, generation) CAS——mid-run 复活的编辑不被本轮 done 擦掉（语义与 049 逐字一致）',
    attempts    INT          NOT NULL DEFAULT 0 COMMENT 'pre-drain 重试次数（失败/CAS 落空累加）',
    last_error  VARCHAR(512) DEFAULT NULL COMMENT '最近一次失败原因（成功置空）',
    enqueued_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    processed_at DATETIME    DEFAULT NULL COMMENT '重投影落实时间（NULL=待处理；仅 stage-3 本轮 DAG 成功推送后 CAS 写入）',
    UNIQUE KEY uniq_doc (doc_id),          -- 一行一 doc；重复 enqueue 走 ON DUPLICATE 复活 + generation+1
    INDEX idx_pending (processed_at)       -- pre-drain 扫 processed_at IS NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  -- ⚠️ COLLATE 显式 = utf8mb4_unicode_ci，与全库一致（doc_id 与 document_meta JOIN）。
  COMMENT='阶段 B doc-meta 投影 outbox：改标题/分类同事务入队 + stage-3 pre-drain（generation CAS 防 loader 窗口丢更新）';

CREATE TABLE IF NOT EXISTS dept_admin_node_candidate (
    id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id              VARCHAR(128) NOT NULL COMMENT '钉钉 staffId（dept_admin 本人）',
    proposed_dept_id     BIGINT UNSIGNED NOT NULL COMMENT '派生出的候选管辖根',
    derived_snapshot_rev BIGINT UNSIGNED NOT NULL COMMENT '派生时的组织快照 rev；确认时不符 ⇒ 409 重派生（TOCTOU 关死）',
    risk_reason          VARCHAR(64)  NOT NULL COMMENT '为何需人工确认：center_level|root_changed|oversized',
    status               VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending|confirmed|rejected|superseded',
    confirmed_by         VARCHAR(128) DEFAULT NULL COMMENT '确认/驳回的 kb_admin',
    confirmed_at         DATETIME     DEFAULT NULL,
    created_at           DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_dept (user_id, proposed_dept_id),   -- 同根候选去重（复活走 ON DUPLICATE 回 pending + 更新 rev）
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='dept_admin 管辖根自动派生候选（待 kb_admin 确认）；权威表 dept_admin_node_grant 语义不变（is_active=1 即有效）';

-- acl_revision 注释扩展（类型/默认值与 060 完全一致，仅换 COMMENT，幂等）
ALTER TABLE document_meta
  MODIFY COLUMN acl_revision INT UNSIGNED NOT NULL DEFAULT 0
    COMMENT '文档管理面编辑 CAS（可见集整体替换 + doc-meta 元数据编辑共用）；每次实际变更 +1，并发保存靠它串行化';
