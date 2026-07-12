-- schema/033_ontology_link_invariants.sql
-- 库：fuling_ontology（PR-B P0-02 独立库隔离——与 027-030 同库；ci_load_schema target=ontology）
-- P1-8 / P1-9 收口（2026-07-11，外评 2026-07-10 重评报告 §6）：
--
-- ① P1-8 link 基数 DB 级兜底：029 的 uk_src_dst_type 只防「同 src 同 dst 同型」重复行，
--    缺 (src, type, active) 维度——一个 SKU 可挂两条 active sku_of_product（指向两个产品），
--    sem_packing/sem_stacking 直接翻倍/串味。服务层闸在 store.add_link（LINK_TYPE_SPECS
--    端点/基数校验，FOR UPDATE 查重），本文件加 MySQL 8 生成列 + 唯一键兜底：
--    对 single 基数的 link 型，active 行生成 'src:type' 键，其余（multi 型 / retired 行）
--    恒 NULL（MySQL 唯一键中 NULL 不冲突）——服务层被绕过或并发窄窗时 DB 以 1062 拦截；
--    retire（UPDATE status='retired'）会让 STORED 生成列重算为 NULL，自动腾出关系位。
--
--    ⚠️ 扩展方式（新增 single 基数 link 型时，与 store.LINK_TYPE_SPECS 同一 PR 改）：
--    MySQL 不支持原位修改生成列表达式，须新迁移文件按「先删后建」重做：
--      ALTER TABLE ontology_link DROP INDEX uk_link_active_single, DROP COLUMN active_single_key;
--      再按本文件语句重加（IN 列表补新类型）。表量级=关系行（试点期 <10^5），重建秒级。
--    multi 型（现仅 material_of_product）不进 IN 列表；改基数（single→multi）同上重建。
--
-- ② P1-9 引用列补 FK（新库零数据，本地/staging 已核实无悬空行；见文件尾三条）：
--    - ontology_object.merged_into → ontology_object（S3 mark_duplicate 的合并指针悬空=
--      身份链断裂；事务内 target 已 FOR UPDATE 校验，FK 是绕服务层直写的兜底）；
--    - ontology_identifier.source_case_id → ontology_resolution_case，ON DELETE SET NULL
--      （case 是可 retention 的队列史，identifier 是长寿账本——case 清理时证据回链置空，
--      而非 RESTRICT 把队列史钉成永久留存）；
--    - ontology_resolution_case.resolved_identifier_id → ontology_identifier，
--      ON DELETE SET NULL（同理反向；置空后 invariants.py 的 resolved_case_broken 扫描
--      会把它显式暴露为待处置违例，而不是 FK 静默阻断清理）。
--
--    刻意**不加**的 FK（评审记录，防后人"补全"）：
--    - identifier.superseded_by / repoint 链：repoint_identifier 事务内先 UPDATE 旧行
--      superseded_by=<新 id> 再 INSERT 新行（顺序不可倒——先插新行会撞 uk_ns_norm_active
--      的 active 唯一键），MySQL FK 检查即时无 deferred，此 FK 会使 repoint 永远 1452；
--    - stewardship.steward_dept / object.owner_dept：ACL 组码白名单权威在代码侧
--      （kb_authz），库内无引用表；
--    - ontology_source_population.namespace：自由态观测键，无主表。
--
-- 幂等：全部经 information_schema 守卫 + PREPARE（027 尾注同款），支持重复 apply。

-- ── ① active_single_key 生成列 ───────────────────────────────────────────────────
SET @has_col := (SELECT COUNT(*) FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_link'
                   AND COLUMN_NAME = 'active_single_key');
SET @ddl := IF(@has_col = 0,
               'ALTER TABLE ontology_link ADD COLUMN active_single_key VARCHAR(64) GENERATED ALWAYS AS (IF(status = ''active'' AND link_type IN (''sku_of_product'',''packing_spec_of_sku'',''stacking_spec_of_sku'',''mold_of_product''), CONCAT(src_object_id, '':'', link_type), NULL)) STORED',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;

SET @has_uk := (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_link'
                  AND INDEX_NAME = 'uk_link_active_single');
SET @ddl := IF(@has_uk = 0,
               'ALTER TABLE ontology_link ADD UNIQUE KEY uk_link_active_single (active_single_key)',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;

-- ── ② 引用列 FK 兜底 ────────────────────────────────────────────────────────────
SET @has_fk := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_object'
                  AND CONSTRAINT_NAME = 'fk_object_merged_into');
SET @ddl := IF(@has_fk = 0,
               'ALTER TABLE ontology_object ADD CONSTRAINT fk_object_merged_into FOREIGN KEY (merged_into) REFERENCES ontology_object (object_id)',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;

SET @has_fk := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_identifier'
                  AND CONSTRAINT_NAME = 'fk_identifier_source_case');
SET @ddl := IF(@has_fk = 0,
               'ALTER TABLE ontology_identifier ADD CONSTRAINT fk_identifier_source_case FOREIGN KEY (source_case_id) REFERENCES ontology_resolution_case (case_id) ON DELETE SET NULL',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;

SET @has_fk := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_resolution_case'
                  AND CONSTRAINT_NAME = 'fk_case_resolved_identifier');
SET @ddl := IF(@has_fk = 0,
               'ALTER TABLE ontology_resolution_case ADD CONSTRAINT fk_case_resolved_identifier FOREIGN KEY (resolved_identifier_id) REFERENCES ontology_identifier (identifier_id) ON DELETE SET NULL',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;
