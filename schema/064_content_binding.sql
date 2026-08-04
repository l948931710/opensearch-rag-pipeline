-- 064_content_binding.sql — C8：审批内容绑定（OSS version-id 固化）
-- 库：fuling_knowledge（document_version 的归属库，见 schema/README.md 矩阵）
-- 拍板：docs/ops/c8_approval_content_binding_signoff_2026-08-03.md（Sam 2026-08-04）
--
-- ── 缺陷 ─────────────────────────────────────────────────────────────────────
-- 签名 PUT URL 与 upload token 共用 30 分钟 TTL，且**预签名 URL 服务端无法逐个撤销**
-- （OSS AK 签发，按时间过期）。于是：上传合规文件 A → register/审批放行 →
-- 30 分钟内用**同一 put_url** 重 PUT 任意字节 B → DAG 摄取 B。
-- ⚠️ 裸 ETag 复核**不够**：register 时对象=B 登记 ETag(B)，审批前重 PUT A（审批人看到 A），
--    审批后重 PUT B（stage-1 的 If-Match ETag(B) 成功）⇒ 审批人批的是 A、入库的是 B。
--    必须**同时**固定「审批预览读的」与「最终摄取读的」两处身份 —— 这正是 version-id 的作用：
--    重 PUT 产生**新 version**，不影响已固化的那个。
--
-- ── 两列的语义 ───────────────────────────────────────────────────────────────
-- `raw_version_id`：register 时 HEAD 到的 OSS 版本号。doc-preview 按它签 GET、
--   stage-1 按它取件 ⇒ 预览与入库是同一份字节。
--
-- `content_binding_mode`：**三态显式契约，不可只靠 `raw_version_id IS NULL` 推断**
--   （codex BLOCKER：那样分不出「存量」与「新写失败」，实施者会把 NULL 当 legacy 放行、
--   使绑定静默退化回未绑定状态）：
--     · LEGACY_UNBOUND —— 存量行 / 绑定未启用时写入的行。允许兼容读（不带 versionId）。
--     · VERSION_ID     —— 已绑定。**`raw_version_id` 必须非空**；缺失或返回身份不符 ⇒
--                         register / 预览 / 审批 / 摄取**全部 fail-closed**，
--                         **不得回退 ETag、不得回退 legacy**。
--     · FROZEN_KEY     —— 预留给方案 F（不可变 final object）。本批不产出该值。
--
-- ⚠️ 默认值刻意是 LEGACY_UNBOUND：apply 之后、flag 打开之前，所有新行仍是存量语义
--    ⇒ 逐字节不改变现有行为（先部署后 apply 安全，与 048/049/050/062/063 同款纪律）。
ALTER TABLE document_version
    ADD COLUMN raw_version_id VARCHAR(128) DEFAULT NULL
        COMMENT 'C8：register 时 HEAD 到的 OSS 对象版本号；预览与摄取均按它取件（重 PUT 产生新版本，不影响本值）',
    ADD COLUMN content_binding_mode VARCHAR(24) NOT NULL DEFAULT 'LEGACY_UNBOUND'
        COMMENT 'C8 三态契约：LEGACY_UNBOUND=存量/未绑定(允许兼容读) | VERSION_ID=已绑定(raw_version_id 必须非空，不符即 fail-closed) | FROZEN_KEY=预留(方案F)';

-- ── 台账（F-35 纪律：schema 文件 + schema_migrations 一行，缺一即事故预备役）──
-- ⚠️ **不要手写 INSERT**（与 062 同款纪律）：`schema_migrations` 的 PK 是 `filename`、
--    并无 `description` 列，且 checksum 由 apply_migration.py 计算写入
--    （032 起的「同名异 checksum」漂移检测依赖它）。一律走：
--        python scripts/apply_migration.py schema/064_content_binding.sql

