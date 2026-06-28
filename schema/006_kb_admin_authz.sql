-- ════════════════════════════════════════════════════════════════════════════
-- 006_kb_admin_authz.sql — 知识库管理员【写授权】数据层（H5 自助上传 Phase 0）
--
-- ADDITIVE / NULL-SAFE / IDEMPOTENT-on-apply（与 003/004 同纪律）：
--   * 复用既有 user_role.role（001 已建，VARCHAR(64)）承载角色：employee / dept_admin / kb_admin。
--     本迁移仅【加索引】方便按角色枚举管理员，不改列、不改既有行（默认仍 'employee'）。
--   * 新建 dept_admin_grant 表：把"某管理员可管理哪些 owner_dept"【显式落库】——写授权绝不
--     从读组（retriever._expand_groups_to_owners）推导。一行 = 一个 (user_id, managed_owner_dept)。
--   * 不动检索路径、不动 owner_dept taxonomy、不重建 HA3 → 对现有问答用户零影响。
--
-- ⚠️ DB：user_role 在 fuling_knowledge（见 dingtalk_identity 的 fuling_knowledge.user_role），
--    故 dept_admin_grant 同库 fuling_knowledge。
-- ⚠️ MySQL 8.0 无 ADD INDEX IF NOT EXISTS：apply 脚本须 information_schema 守卫该 ALTER；
--    CREATE TABLE IF NOT EXISTS 原生幂等。
--
-- 角色词表（权威定义，单一来源）：
--   employee   — 普通员工，只问答（读 public + 本组 dept_internal）；无上传入口。
--   dept_admin — 部门管理员，可上传/升版/退役其 dept_admin_grant 授予的 owner_dept；
--                公开 / 跨组共享 需 kb_admin 审批。
--   kb_admin   — 知识库管理员，跨部门；审批公开/跨组共享、退役/恢复、维护 owner_dept 授权。
-- 角色与可管理 owner_dept 均由【显式名单】seed（user_role.role + dept_admin_grant），
-- seeded 行优先于自动部门映射（见 dingtalk_identity._resolve_user_dept 的 H3 语义）。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge
-- 按角色枚举管理员（kb_admin 维护名单 / 入口可见性查询）。
ALTER TABLE user_role
    ADD INDEX idx_role (role);

-- @DB fuling_knowledge
-- 部门管理员 → 可管理 owner_dept 的显式授权（写授权单一事实来源）。
-- managed_owner_dept 取【组代码 / 伞组】粒度（如 marketing / production / finance），
-- 与 retriever._VALID_ACL_GROUPS 写白名单一致；非法值由 kb_authz.sanitize_owner_depts fail-closed。
CREATE TABLE IF NOT EXISTS dept_admin_grant (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id             VARCHAR(128) NOT NULL COMMENT '钉钉 staffId（与 user_role.user_id 对应）',
    managed_owner_dept  VARCHAR(64)  NOT NULL COMMENT '可管理的 owner_dept（组代码/伞组粒度）',
    granted_by          VARCHAR(128) DEFAULT NULL COMMENT '授予者 staffId（通常为 kb_admin）',
    note                VARCHAR(255) DEFAULT NULL COMMENT '备注（如所属部门、授权依据）',
    is_active           TINYINT(1)   DEFAULT 1   COMMENT '软删除：撤销授权置 0',
    created_at          DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_owner (user_id, managed_owner_dept),
    INDEX idx_user_active (user_id, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='部门管理员可管理的 owner_dept 显式授权（写授权，绝不从读组推导）';
