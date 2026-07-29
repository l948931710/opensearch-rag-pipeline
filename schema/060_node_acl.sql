-- 060_node_acl.sql — node-ACL 阶段 A（设计稿 docs/permission_node_acl_design_2026-07-27_DRAFT.md，
--   codex-review 达成共识 2026-07-28）：文档可见度从"手工维护的粗粒度组码"改为
--   "管理员在钉钉组织树上勾选节点"。
--
-- 判定式：可见 ⟺ 用户祖先链 ∩ 文档授权节点集 ≠ ∅（读侧展开：文档只存被勾节点，
--   查询时展开用户祖先链）⇒ 新建叶子部门自动继承、部门改名免疫、**零文档重推**。
--
-- ADDITIVE / IDEMPOTENT-on-apply：只加列与新表，不改既有列/行、不动检索路径、不重建 HA3。
--   apply 后全部存量文档 acl_mode='legacy'，行为逐字节不变（双 flag 亦默认不放行）。
--
-- ⚠️ 两根 owner 轴是【不同的列】，本迁移只加"归属轴"的节点列，绝不动检索投影轴：
--     · document_meta.owner_dept / owner_dept_id = 归属/管理轴（看板 kb_insights、
--       dept_admin 作用域读它）—— node 模式下**保持真实属主**；
--     · chunk_meta.owner_dept + HA3 owner_dept = 检索投影轴 —— node 模式下写哨兵
--       `__acl_node_mode_v1__`（由 acl_policy.project_doc_acl 单点产出，不在本 DDL）。
--   哨兵靠"没有任何用户组会展开出它"使 owner 分支对 node 文档永不命中 = 真替换语义。
--
-- ⚠️ dept_admin 管理轴必须与归属轴同步迁移（否则归属一变节点，部门管理员立刻管不到
--   自己的文档）。dept_admin_grant.managed_owner_dept 是 NOT NULL + UNIQUE(user_id,
--   managed_owner_dept)（schema/006:34-37）⇒ 加 nullable 列既表达不了 node-only 授权、
--   也挡不住一行同授两轴 ⇒ 本迁移建**独立表** dept_admin_node_grant。
--
-- ⚠️ DB：fuling_knowledge（与 document_meta / chunk_meta / kb_access_request 同库）。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge

-- ── 1. 文档 ACL 模式 + 归属节点 + 并发替换的 CAS 版本号 ──────────────────────
CALL _add_column_if_not_exists('document_meta', 'acl_mode',
    "VARCHAR(16) NOT NULL DEFAULT 'legacy' COMMENT 'legacy=组码语义(现状) | node=组织树节点语义'");
CALL _add_column_if_not_exists('document_meta', 'owner_dept_id',
    "BIGINT UNSIGNED DEFAULT NULL COMMENT '归属组织节点(钉钉 dept_id)；node 模式权威，legacy 模式为 NULL'");
CALL _add_column_if_not_exists('document_meta', 'acl_revision',
    "INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '可见范围整体替换的 CAS 版本；并发保存靠它串行化'");

-- dept_admin 作用域 SQL 会按 (acl_mode, owner_dept_id) 过滤 node 文档
CALL _add_index_if_not_exists('document_meta', 'idx_acl_mode_owner_node',
    '(acl_mode, owner_dept_id)');


-- ── 2. 文档 → 授权节点（可见度权威，与 kb_access_request 并列为两个权威源）────
-- 语义：一行 = 一篇文档授权给一个组织节点【及其整棵子树】。撤销走 soft-revoke
--   （revoked_at 置时刻），再授权复活同一行 ⇒ (doc_id, dept_id) 唯一。
-- ⚠️ 投影【模式互斥】：node 文档的 allowed_depts 只含 d:<dept_id>，**绝不含组码**
--   （否则 retriever 的组码 OR 分支照样放行，哨兵就白设了）。由 acl_policy
--   .project_doc_acl 单点保证，本表只存权威。
CREATE TABLE IF NOT EXISTS kb_doc_node_grant (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id        VARCHAR(128) NOT NULL COMMENT '被授权的文档（document_meta.doc_id）',
    dept_id       BIGINT UNSIGNED NOT NULL COMMENT '被授权的钉钉组织节点；子树语义',
    granted_by    VARCHAR(128) DEFAULT NULL COMMENT '操作人 staffId',
    granted_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    revoked_at    DATETIME     DEFAULT NULL COMMENT 'NULL=生效中；非空=已撤销（软撤销，保留审计）',
    revoked_by    VARCHAR(128) DEFAULT NULL,
    note          VARCHAR(255) DEFAULT NULL,
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_doc_dept (doc_id, dept_id),
    INDEX idx_doc_active (doc_id, revoked_at),
    INDEX idx_dept (dept_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='文档→组织节点授权（可见度权威）；子树语义、软撤销、(doc_id,dept_id) 唯一';


-- ── 3. dept_admin 管辖子树（管理轴，与读组正交；三分授权原则不变）────────────
-- ⚠️ 读 ≠ 管理 ≠ 授权：本表只表达"能管这些文档"（上传/升版/退役），**不代表能读全部**，
--   也不代表能授权给别人。读组仍由用户自己的祖先链决定。
-- source：auto=按管理员自己所在部门自动派生（Sam 2026-07-28 拍板）；manual=人工指定。
--   ⚠️ manual 存在时 auto 失效（**替换而非取并集**）；无部门/多直属部门不得自动取并集
--   ⇒ fail-closed 转人工；自动根变更 / 根是中心级 / 后代规模超阈值须告警 + 人工确认
--   （实测:1175 人里 97 人 8% 直接挂一级中心，自动派生会一次给出整个中心的管辖权）。
CREATE TABLE IF NOT EXISTS dept_admin_node_grant (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id          VARCHAR(128) NOT NULL COMMENT '钉钉 staffId（与 user_role.user_id 对应）',
    managed_dept_id  BIGINT UNSIGNED NOT NULL COMMENT '管辖子树的【根】；覆盖该节点及全部后代',
    source           VARCHAR(16)  NOT NULL DEFAULT 'manual' COMMENT 'auto=按所在部门派生 | manual=人工指定（manual 存在时 auto 失效）',
    granted_by       VARCHAR(128) DEFAULT NULL,
    is_active        TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '软撤销',
    note             VARCHAR(255) DEFAULT NULL,
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_user_dept_source (user_id, managed_dept_id, source),
    INDEX idx_user_active (user_id, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='dept_admin 管辖子树根（管理轴）；auto 派生 + manual 覆盖，manual 优先';


-- ── 4. 组织快照（dept_dim / staff_dim）——本设计的硬前置 ─────────────────────
-- ⚠️ 为什么必须落 RDS：现有 /api/kb/org-tree 的快照读 scratch/dingtalk_org_tree.json，
--   而 scratch/ 同时被 .gitignore 与 .dockerignore 排除、Dockerfile 只 COPY
--   opensearch_pipeline/ ⇒ **2026-07-23 起的镜像应用里该文件根本不存在，生产恒返回 null**。
-- 同步 job 每日一次；快照 >48h 视为过期 ⇒ 祖先链解析 fail-closed 仅 public（Sam 2026-07-28）。
-- snapshot_rev：同步批次号，进 DocAcl/后代集缓存键 —— 组织变动后缓存自动失效，
--   避免已移出子树的部门继续被旧管理员管辖。
CREATE TABLE IF NOT EXISTS dept_dim (
    dept_id       BIGINT UNSIGNED NOT NULL PRIMARY KEY COMMENT '钉钉 dept_id',
    parent_id     BIGINT UNSIGNED NOT NULL COMMENT '父节点；顶层的父为钉钉根 1',
    name          VARCHAR(255) NOT NULL DEFAULT '',
    depth         INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '祖先链长度（不含虚拟根）',
    is_active     TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '同步中未再出现 ⇒ 置 0（不硬删，孤儿授权靠它检出）',
    snapshot_rev  BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '同步批次号（缓存键）',
    synced_at     DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_parent (parent_id),
    INDEX idx_active_rev (is_active, snapshot_rev)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='钉钉组织树快照（node-ACL 祖先链解析 + 管理台组织选择器 + 奖励统计分母共用）';

CREATE TABLE IF NOT EXISTS staff_dim (
    staff_id      VARCHAR(128) NOT NULL PRIMARY KEY COMMENT '钉钉 staffId',
    dept_ids      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '直属部门 id CSV（一人可多部门）',
    primary_dept  BIGINT UNSIGNED DEFAULT NULL COMMENT '主部门（管辖根自动派生用）',
    is_active     TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '同步中未再出现 ⇒ 置 0（离职/移出）',
    snapshot_rev  BIGINT UNSIGNED NOT NULL DEFAULT 0,
    synced_at     DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_primary (primary_dept),
    INDEX idx_active_rev (is_active, snapshot_rev)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='钉钉员工→部门快照；⚠️ 只存 id 与部门归属，不存姓名（仓库为 public，姓名不进代码面）';
