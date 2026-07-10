-- schema/028_ontology_identity.sql
-- 库：fuling_operation
-- 身份脊柱：别名映射（ontology_identifier）+ 候选承载层（resolution_case/candidate，外评 S2）。
-- 取号勘误：设计文档所写 025（identifier）已被 agent v2 占用，实际取 028；见 schema/README.md 铁律 3。
--
-- 分层（S1/S2 读写分离的落库形态）：
--   · 候选阶段一律在 resolution_case/candidate —— 一个未解析编号可挂 N 个候选目标（uk_ns_norm 装不下）；
--   · ontology_identifier 只存「已确认」映射及其历史 —— 至多一行 active（S4 生成列唯一键）；
--   · 在线 resolve() 纯读两层，永不写入；落库只发生在播种/回填/工作台决策/受治理 Action 四条路径。

-- ── 别名脊柱：所有源系统编号（含 U8 货号）都是指向 canonical 对象的别名 ────────────────
CREATE TABLE IF NOT EXISTS ontology_identifier (
  identifier_id     CHAR(26)     NOT NULL,            -- ULID
  namespace         VARCHAR(64)  NOT NULL,            -- u8|internal|customer:<客户>|supplier:<名>|material_grade|lab_sample|lot_code|…
  raw_value         VARCHAR(255) NOT NULL,            -- 原始观测值（保真）
  norm_value        VARCHAR(255) NOT NULL,            -- normalize() 归一值；⚠️ 绝不剥改模后缀（ABC123 ≠ ABC123-M）
  target_object_id  CHAR(26)     NOT NULL,
  target_revision   VARCHAR(16)  NULL,                -- 改模变体指到 Product@rN 的版本轴（如 'r2'）
  relation          ENUM('canonical','alias','variant','equivalent') NOT NULL DEFAULT 'alias',
  resolution_method ENUM('seed','rule','kie','embedding','manual') NOT NULL,
  confidence        DECIMAL(4,3) NOT NULL DEFAULT 1.000,
  status            ENUM('active','superseded','rejected') NOT NULL DEFAULT 'active',
  -- S4：至多一行 active——active→1、其余→NULL（MySQL 唯一键中 NULL 不冲突），历史行可共存
  active_key        TINYINT AS (CASE WHEN status = 'active' THEN 1 ELSE NULL END) STORED,
  superseded_by     CHAR(26)     NULL,                -- S3 repoint 链：旧行 → 新行
  source_case_id    CHAR(26)     NULL,                -- 由哪个 resolution_case 确认而来（证据回链）
  confirmed_by      VARCHAR(64)  NULL,                -- steward user_id；auto 通道记 'auto'（入抽检队列）
  approval_request_id CHAR(32)   NULL,                -- 门 A（会话内受治理 Action）经 v2 审批时回链 025
  first_seen_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  confirmed_at      DATETIME(3)  NULL,
  updated_at        DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (identifier_id),
  UNIQUE KEY uk_ns_norm_active (namespace, norm_value, active_key),
  KEY idx_ns_norm (namespace, norm_value),            -- 历史/全状态回查（非唯一）
  KEY idx_target (target_object_id, status),
  KEY idx_method_status (resolution_method, status)   -- auto 抽检队列（method+active 扫描）
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── 消解 case：未解析编号的观测聚合 + 证据快照 + 积压统计（S2）──────────────────────
-- 同一 (namespace, norm) 至多一个 open case（open_key 生成列，模式同上）；resolved/dismissed 留史。
CREATE TABLE IF NOT EXISTS ontology_resolution_case (
  case_id            CHAR(26)     NOT NULL,           -- ULID
  namespace          VARCHAR(64)  NOT NULL,
  raw_value          VARCHAR(255) NOT NULL,
  norm_value         VARCHAR(255) NOT NULL,
  object_type_hint   VARCHAR(32)  NULL,               -- 期望对象类型（product/sku/…），供工作台分栏
  status             ENUM('open','resolved','dismissed') NOT NULL DEFAULT 'open',
  open_key           TINYINT AS (CASE WHEN status = 'open' THEN 1 ELSE NULL END) STORED,
  evidence_json      JSON         NULL,               -- 证据快照：来源观测（U8 行/KIE 抽取/查询上下文）、特征
  seen_count         INT          NOT NULL DEFAULT 1, -- 观测次数（积压排序：高频未解析优先处置）
  first_seen_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_seen_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  resolved_identifier_id CHAR(26) NULL,               -- 处置产物：确认后落 ontology_identifier 的行
  resolved_by        VARCHAR(64)  NULL,
  resolved_at        DATETIME(3)  NULL,
  resolution_note    VARCHAR(255) NULL,               -- 处置理由（驳回/终止必填，代码侧约束）
  PRIMARY KEY (case_id),
  UNIQUE KEY uk_ns_norm_open (namespace, norm_value, open_key),
  KEY idx_status_seen (status, last_seen_at),
  KEY idx_status_freq (status, seen_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── 消解候选：case × 候选目标 ×方法（同一目标可被多方法独立提出，各带置信与依据）────────
CREATE TABLE IF NOT EXISTS ontology_resolution_candidate (
  candidate_id     CHAR(26)     NOT NULL,             -- ULID
  case_id          CHAR(26)     NOT NULL,
  target_object_id CHAR(26)     NOT NULL,
  target_revision  VARCHAR(16)  NULL,
  method           ENUM('rule','kie','embedding') NOT NULL,   -- S6：embedding 候选构造性无 auto 通路
  confidence       DECIMAL(4,3) NOT NULL,
  features_json    JSON         NULL,                 -- 匹配依据（命中属性/相似度/剥后缀提示等）
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (candidate_id),
  UNIQUE KEY uk_case_target_method (case_id, target_object_id, method),
  KEY idx_case (case_id, confidence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
