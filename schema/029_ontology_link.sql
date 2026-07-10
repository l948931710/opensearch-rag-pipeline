-- schema/029_ontology_link.sql
-- 库：fuling_operation
-- 本体关系（Palantir Link Types 语义的最小自建形态）：sku_of_product / mold_of_product /
-- material_of_product 等。P0 只建结构 + 播种期写 sku_of_product；revision-diff、BOM 串味
-- 检测等富语义在 P2。取号勘误：文档所写 026 已被 agent v2 占用，实际取 029。
-- P0 收口修订（PR-A，P0-03，2026-07-10 外审）：加 src/dst FK——悬空引用 DB 层拦截（分支未发布，直接修订原文件）。

CREATE TABLE IF NOT EXISTS ontology_link (
  link_id       CHAR(26)    NOT NULL,                 -- ULID
  src_object_id CHAR(26)    NOT NULL,                 -- 语义：src --type--> dst（如 SKU --sku_of_product--> Product）
  dst_object_id CHAR(26)    NOT NULL,
  link_type     VARCHAR(32) NOT NULL,
  attrs_json    JSON        NULL,                     -- 关系属性（如绑定的 revision 轴）
  status        ENUM('active','retired') NOT NULL DEFAULT 'active',
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (link_id),
  UNIQUE KEY uk_src_dst_type (src_object_id, dst_object_id, link_type),
  KEY idx_dst_type (dst_object_id, link_type, status),
  KEY idx_src_type (src_object_id, link_type, status),
  CONSTRAINT fk_link_src FOREIGN KEY (src_object_id) REFERENCES ontology_object (object_id),
  CONSTRAINT fk_link_dst FOREIGN KEY (dst_object_id) REFERENCES ontology_object (object_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
