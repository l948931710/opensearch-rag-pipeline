-- schema/027_ontology_core.sql
-- 库：fuling_ontology（PR-B P0-02 独立库隔离：fuling_ro 持 fuling_operation.* 库级 SELECT，
--     ontology 表族留在运营库时服务层 ACL 可被直连绕过——独立库不授 fuling_ro，机器可强制）
-- 本体控制面核心（《本体层设计 v1.1》§B.3 + docs/ontology_p0_plan_2026-07-10.md）。
-- 取号勘误：设计文档所写 024（core）已被 agent v2 占用，实际取 027；详见 schema/README.md 铁律 3。
-- 定位：身份/关系/来源治理的控制面，非万物 SoR——业务事实（库存/订单/金额/完工）仍以各源系统为准，
-- 本体只登记「该属性听谁的、多新鲜、谁负责」（ontology_attribute_source / ontology_stewardship）。
-- P0 收口修订（PR-A，P0-04，2026-07-10 外审）：分支未发布、仅 apply 过一次性本地库，直接修订原文件——
--   ① canonical_ref 升为全局 UNIQUE（原 uk 只在 (object_type, canonical_ref)，两类型误配同 type_code 时
--      get_object_by_ref 的单列查会歧义；全局唯一后 LIMIT 1 从「约定」变「DB 强制」）；
--   ② ontology_ref_seq.type_code 加 UNIQUE（原来只有 PK(object_type)，同码双类型 DB 不拦）。
-- P1 收口修订（PR-G，「DDL 约束不足」，2026-07-10 外审）：
--   ③ lifecycle_state CHECK（draft|verified——自由文本会让挑行规则 verified 优先静默失效）；
--   ④ object_type 加 FK → ontology_ref_seq（登记类型码=治理动作，未登记类型 DB 层拦死）；
--   ⑤ link/identifier/candidate 的 confidence CHECK 0..1、link_type 格式 CHECK 见 028/029。

-- ── 对象主表：canonical 身份由本体铸造（ULID），永不等于任何源系统 ID ────────────────
CREATE TABLE IF NOT EXISTS ontology_object (
  object_id           CHAR(26)     NOT NULL,          -- ULID（本体铸造，对外只出 canonical_ref）
  object_type         VARCHAR(32)  NOT NULL,          -- product|sku|mold|material|calc_rule|…（P0 先物四类）
  canonical_ref       VARCHAR(32)  NOT NULL,          -- 人读展示号 FLP-<类型码>-<序号>（ontology_ref_seq 发号）
  title               VARCHAR(255) NOT NULL,          -- 人读名称（商务品名/牌号等）
  golden_json         JSON         NOT NULL,          -- 黄金记录属性值；每属性权威来源单独查 attribute_source，不双写
  lifecycle_state     VARCHAR(16)  NOT NULL DEFAULT 'draft',   -- draft|verified（CHECK 钉死；新状态=治理动作走迁移）
  owner_dept          VARCHAR(64)  NOT NULL,          -- 身份/生命周期唯一 steward 部门（单值；属性级归属见 stewardship）
  data_classification ENUM('public','internal','confidential') NOT NULL DEFAULT 'internal',
  source_of_record    VARCHAR(32)  NOT NULL DEFAULT 'ontology',   -- u8|ontology|max|ha3（对象主档 SoR）
  version             INT          NOT NULL DEFAULT 1,            -- 乐观锁：UPDATE … WHERE version=? 冲突重取
  status              ENUM('active','merged','retired') NOT NULL DEFAULT 'active',
  merged_into         CHAR(26)     NULL,              -- S3 mark_duplicate 仅作标记（全量 merge 传播=P2）
  created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (object_id),
  UNIQUE KEY uk_ref (canonical_ref),                  -- 全局唯一（P0-04：类型码内嵌于展示号，DB 强制而非约定）
  KEY idx_type_status (object_type, status),
  KEY idx_type_title (object_type, title(64)),
  CONSTRAINT ck_object_lifecycle CHECK (lifecycle_state IN ('draft','verified'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── canonical_ref 发号器：每类型一行计数，UPDATE …=LAST_INSERT_ID(next_no+1) 原子取号 ──
CREATE TABLE IF NOT EXISTS ontology_ref_seq (
  object_type VARCHAR(32) NOT NULL,
  type_code   VARCHAR(8)  NOT NULL,                   -- 展示号里的类型码（P/S/M/MT/…）
  next_no     BIGINT      NOT NULL DEFAULT 0,
  PRIMARY KEY (object_type),
  UNIQUE KEY uk_type_code (type_code)                 -- 全局唯一（P0-04：同码双类型 DB 拦截）
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

-- ── 源快照 population：coverage 的真实分母（PR-F，P1「覆盖率虚高」修复）──────────────
-- seeding/backfill 真跑（非 dry）时按 namespace upsert 本轮源记录数；coverage 有快照
-- 用快照做分母并标 denominator='population'，无快照回退 active/(active+open) 近似并
-- 明标 'approx'——不再把"处置率"冒充"覆盖率"。
CREATE TABLE IF NOT EXISTS ontology_source_population (
  namespace    VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,   -- 与 identifier 同 bin 口径
  records      INT          NOT NULL,                      -- 该 namespace 最近一次源快照记录数
  source       VARCHAR(64)  NOT NULL,                      -- seeding|backfill|<CSV 名>
  snapshot_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (namespace)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ── object_type FK → ref_seq（PR-G ④；建表序在 ref_seq 之后，故置文件尾 ALTER；
--    information_schema 守卫幂等——027 支持重复 apply）──────────────────────────────
SET @has_fk := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_object'
                  AND CONSTRAINT_NAME = 'fk_object_type_registered');
SET @ddl := IF(@has_fk = 0,
               'ALTER TABLE ontology_object ADD CONSTRAINT fk_object_type_registered FOREIGN KEY (object_type) REFERENCES ontology_ref_seq (object_type)',
               'SELECT 1');
PREPARE _s FROM @ddl;
EXECUTE _s;
DEALLOCATE PREPARE _s;

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
