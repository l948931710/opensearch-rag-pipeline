-- schema/034_sem_views_product_acl.sql
-- 库：fuling_ontology
-- P0-03（重评审计 2026-07-11）：sem 视图关联 product 无独立 ACL——
--   030 的行过滤键（owner_dept/data_classification）绑的是 **spec**，product 的
--   id/ref/name 与 spec 字段并列 SELECT 却不带 product 自身的归属/密级：
--   公开 SKU + 公开 spec + confidential product → 未授权方拿到受限 product 三字段。
-- 修法：视图补投 product 自身 ACL 列（product_owner_dept/product_classification），
--   服务层唯一读取口 sem.py 在出参前按 product 自身密级独立裁决（can_read_object），
--   不可读 → product_id/product_ref/product_name 一并置 None（拒绝=当作不存在）。
-- 兼容：旧视图行（无这两列）在 sem.py 侧 **fail-closed 同样遮蔽**（升级窗口宁可少给），
--   apply 本文件后恢复正常裁决。CREATE OR REPLACE 幂等，无数据变更。

-- ── 箱规投影（030 同形 + product ACL 两列）─────────────────────────────────────
CREATE OR REPLACE VIEW sem_packing AS
SELECT s.object_id          AS sku_id,
       s.canonical_ref      AS sku_ref,
       s.title              AS sku_title,
       s.owner_dept         AS sku_owner_dept,
       p.object_id          AS product_id,
       p.canonical_ref      AS product_ref,
       p.title              AS product_name,
       p.owner_dept         AS product_owner_dept,
       p.data_classification AS product_classification,
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

-- ── 香规/堆叠投影（同形）─────────────────────────────────────────────────────
CREATE OR REPLACE VIEW sem_stacking AS
SELECT s.object_id          AS sku_id,
       s.canonical_ref      AS sku_ref,
       s.title              AS sku_title,
       s.owner_dept         AS sku_owner_dept,
       p.object_id          AS product_id,
       p.canonical_ref      AS product_ref,
       p.title              AS product_name,
       p.owner_dept         AS product_owner_dept,
       p.data_classification AS product_classification,
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
