-- schema/030_sem_views.sql
-- 库：fuling_ontology（PR-B P0-02 独立库隔离：fuling_ro 持 fuling_operation.* 库级 SELECT，
--     ontology 表族留在运营库时服务层 ACL 可被直连绕过——独立库不授 fuling_ro，机器可强制）
-- PMC-1 语义投影（P0 PR10）：sem_packing（箱规）/ sem_stacking（香规/堆叠）。
-- 取号勘误：设计文档所写 028（sem 视图）已被 agent v2 占用，实际取 030；详见 schema/README.md 铁律 3。
--
-- ⚠️ S7 授权铁律（docs/ontology_p0_plan_2026-07-10.md §2）：
--   sem_* 不授通用 fuling_ro（防未来 Text-to-SQL/脚本绕过服务层行过滤）；
--   唯一读取口 = opensearch_pipeline/ontology/sem.py（复用 fuling_admin 读路径，
--   acl_groups 行级过滤在服务层查询处 fail-closed）；未来 readonly_sql 若接 sem_*
--   必须走服务层部门 WHERE 注入，禁止直查视图。
--
-- 对设计草图的显式偏离：草图用 JSON_EXTRACT(golden_json,'$.sku_id') 关联 spec↔SKU——
-- JSON 关联无索引且把关系藏进属性值；实际用 029 ontology_link 承载
-- （link_type='packing_spec_of_sku'/'stacking_spec_of_sku'，src=spec → dst=sku，
-- 与播种期 sku_of_product 同构，uk_src_dst_type 天然防重）。
--
-- 降级铁律（落地细化 §5.2）：视图只投影**已消解**对象；未消解编号由服务层回落原值
-- 标「未确认」，不假装精确。spec 的 verified/draft 经 lifecycle_state 透出，
-- 服务层挑行规则=verified 优先、同态取最新（packing_calc 的估算结果恒 draft 落库）。

-- ── spec 对象类型发号登记（INSERT IGNORE 幂等；mint_object 未登记即拒，治理动作显式化）──
INSERT IGNORE INTO ontology_ref_seq (object_type, type_code, next_no) VALUES
  ('packing_spec','PS',0), ('stacking_spec','SS',0);

-- ── 箱规投影：SKU ← packing_spec（spec 侧 owner_dept/data_classification 是行过滤依据）──
CREATE OR REPLACE VIEW sem_packing AS
SELECT s.object_id          AS sku_id,
       s.canonical_ref      AS sku_ref,
       s.title              AS sku_title,
       s.owner_dept         AS sku_owner_dept,
       p.object_id          AS product_id,
       p.canonical_ref      AS product_ref,
       p.title              AS product_name,
       spec.object_id       AS spec_id,
       spec.canonical_ref   AS spec_ref,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.box_type'))  AS box_type,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.per_box'))   AS per_box,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.outer_dim')) AS outer_dim,
       spec.golden_json     AS spec_json,
       spec.lifecycle_state AS spec_state,
       spec.owner_dept      AS owner_dept,
       spec.data_classification AS data_classification,
       spec.updated_at      AS spec_updated_at
FROM ontology_object s
JOIN ontology_link lk
  ON lk.dst_object_id = s.object_id AND lk.link_type = 'packing_spec_of_sku'
 AND lk.status = 'active'
JOIN ontology_object spec
  ON spec.object_id = lk.src_object_id AND spec.object_type = 'packing_spec'
 AND spec.status = 'active'
LEFT JOIN ontology_link lp
  ON lp.src_object_id = s.object_id AND lp.link_type = 'sku_of_product'
 AND lp.status = 'active'
LEFT JOIN ontology_object p
  ON p.object_id = lp.dst_object_id AND p.status = 'active'
WHERE s.object_type = 'sku' AND s.status = 'active';

-- ── 香规/堆叠投影：同构（堆叠方式/每叠数/整柜立方）───────────────────────────────
CREATE OR REPLACE VIEW sem_stacking AS
SELECT s.object_id          AS sku_id,
       s.canonical_ref      AS sku_ref,
       s.title              AS sku_title,
       s.owner_dept         AS sku_owner_dept,
       p.object_id          AS product_id,
       p.canonical_ref      AS product_ref,
       p.title              AS product_name,
       spec.object_id       AS spec_id,
       spec.canonical_ref   AS spec_ref,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.stack_mode'))    AS stack_mode,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.per_stack'))     AS per_stack,
       JSON_UNQUOTE(JSON_EXTRACT(spec.golden_json,'$.container_cbm')) AS container_cbm,
       spec.golden_json     AS spec_json,
       spec.lifecycle_state AS spec_state,
       spec.owner_dept      AS owner_dept,
       spec.data_classification AS data_classification,
       spec.updated_at      AS spec_updated_at
FROM ontology_object s
JOIN ontology_link lk
  ON lk.dst_object_id = s.object_id AND lk.link_type = 'stacking_spec_of_sku'
 AND lk.status = 'active'
JOIN ontology_object spec
  ON spec.object_id = lk.src_object_id AND spec.object_type = 'stacking_spec'
 AND spec.status = 'active'
LEFT JOIN ontology_link lp
  ON lp.src_object_id = s.object_id AND lp.link_type = 'sku_of_product'
 AND lp.status = 'active'
LEFT JOIN ontology_object p
  ON p.object_id = lp.dst_object_id AND p.status = 'active'
WHERE s.object_type = 'sku' AND s.status = 'active';
