-- schema/027_ontology_core.sql
-- 库：fuling_operation
-- 本体控制面核心（《本体层设计 v1.1》§B.3 + docs/ontology_p0_plan_2026-07-10.md）。
-- 取号勘误：设计文档所写 024（core）已被 agent v2 占用，实际取 027；详见 schema/README.md 铁律 3。
-- 定位：身份/关系/来源治理的控制面，非万物 SoR——业务事实（库存/订单/金额/完工）仍以各源系统为准，
-- 本体只登记「该属性听谁的、多新鲜、谁负责」（ontology_attribute_source / ontology_stewardship）。

-- ── 对象主表：canonical 身份由本体铸造（ULID），永不等于任何源系统 ID ────────────────
CREATE TABLE IF NOT EXISTS ontology_object (
  object_id           CHAR(26)     NOT NULL,          -- ULID（本体铸造，对外只出 canonical_ref）
  object_type         VARCHAR(32)  NOT NULL,          -- product|sku|mold|material|calc_rule|…（P0 先物四类）
  canonical_ref       VARCHAR(32)  NOT NULL,          -- 人读展示号 FLP-<类型码>-<序号>（ontology_ref_seq 发号）
  title               VARCHAR(255) NOT NULL,          -- 人读名称（商务品名/牌号等）
  golden_json         JSON         NOT NULL,          -- 黄金记录属性值；每属性权威来源单独查 attribute_source，不双写
  lifecycle_state     VARCHAR(16)  NOT NULL DEFAULT 'draft',   -- draft|verified|…（如 PackingSpec 已验证）
  owner_dept          VARCHAR(64)  NOT NULL,          -- 身份/生命周期唯一 steward 部门（单值；属性级归属见 stewardship）
  data_classification ENUM('public','internal','confidential') NOT NULL DEFAULT 'internal',
  source_of_record    VARCHAR(32)  NOT NULL DEFAULT 'ontology',   -- u8|ontology|max|ha3（对象主档 SoR）
  version             INT          NOT NULL DEFAULT 1,            -- 乐观锁：UPDATE … WHERE version=? 冲突重取
  status              ENUM('active','merged','retired') NOT NULL DEFAULT 'active',
  merged_into         CHAR(26)     NULL,              -- S3 mark_duplicate 仅作标记（全量 merge 传播=P2）
  created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (object_id),
  UNIQUE KEY uk_type_ref (object_type, canonical_ref),
  KEY idx_type_status (object_type, status),
  KEY idx_type_title (object_type, title(64))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── canonical_ref 发号器：每类型一行计数，UPDATE …=LAST_INSERT_ID(next_no+1) 原子取号 ──
CREATE TABLE IF NOT EXISTS ontology_ref_seq (
  object_type VARCHAR(32) NOT NULL,
  type_code   VARCHAR(8)  NOT NULL,                   -- 展示号里的类型码（P/S/M/MT/…）
  next_no     BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (object_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 首批「物」四类 + P3 参数对象；INSERT IGNORE 幂等（重复执行 no-op）
INSERT IGNORE INTO ontology_ref_seq (object_type, type_code, next_no) VALUES
  ('product','P',0), ('sku','S',0), ('mold','M',0), ('material','MT',0), ('calc_rule','CR',0);

-- ── 属性溯源目录：本体只对「每个 对象类型.属性 的权威来源是谁」负责（纯来源治理，S5）────
-- 授权（谁能确认/处置）不在本表——见 ontology_stewardship；种子行由代码侧 ensure_seeds 维护
-- （沿 tool_registry sync_specs 模式：代码内声明 → upsert 入库，DDL 只建结构）。
CREATE TABLE IF NOT EXISTS ontology_attribute_source (
  object_type VARCHAR(32)  NOT NULL,
  attribute   VARCHAR(64)  NOT NULL,
  sor_system  VARCHAR(64)  NOT NULL,                  -- u8 | ontology(SpecSheet) | ontology(PackingSpec) | max…
  sync_mode   VARCHAR(32)  NOT NULL,                  -- manual | derived | daily_ro | api | event-derived
  freshness   VARCHAR(32)  NULL,                      -- T-1 | realtime | on-demand（时效口径，答案溯源随带）
  notes       VARCHAR(255) NULL,
  updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (object_type, attribute)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── stewardship：审批/工作台授权的权威源（S5 独立化，最小形态）─────────────────────
-- scope 三种粒度：object_type（身份确认归谁）/ namespace（某客户别名归谁）/ attribute（属性级）。
-- 裁决顺序（代码侧）：attribute > namespace > object_type，未命中 → 仅 kb_admin 可处置（fail-closed）。
CREATE TABLE IF NOT EXISTS ontology_stewardship (
  scope_type   ENUM('object_type','namespace','attribute') NOT NULL,
  scope_key    VARCHAR(96) NOT NULL,                  -- 如 'product' / 'customer:KFC' / 'sku.箱规'
  steward_dept VARCHAR(64) NOT NULL,                  -- 对齐 kb_authz 部门组码（新组码须走 ACL 白名单灰度）
  backup_dept  VARCHAR(64) NULL,                      -- 代理人机制（go/no-go ② steward SLA）
  notes        VARCHAR(255) NULL,
  updated_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (scope_type, scope_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
