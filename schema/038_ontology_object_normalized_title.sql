-- schema/038_ontology_object_normalized_title.sql
-- 库：fuling_ontology
-- C2（审计复核批次6，2026-07-13，docs/reverify_fix_batches_2026-07-12.md）：
--   同名聚类召回归一化感知——播种 rule②「同型同名聚类」此前靠 `title LIKE %raw%`
--   预过滤再 Python 归一比对：空白/全半角变体（"PP 龙虾杯" vs "PP龙虾杯" vs 全角空格）
--   LIKE 天然漏召回 → 同物漏合并 → 误铸新对象（false mint——S9 resolver 硬闸
--   测不到的播种侧盲区，复核确认的覆盖缺口）。
-- 修法：落 normalized_title 聚类键列（权威实现=ontology/normalize.title_key：
--   去全部空白 + 全半角统一、内容/大小写原样）+ 组合索引；store 两条 mint 路径写入，
--   find_objects 增 norm_title 等值召回臂，seeding.title_matches 以等值为主臂。
-- ⚠️ 存量 backfill：SQL 无法复刻 Python 归一（全半角映射表在 normalize.py），存量行由
--   `scripts/backfill_normalized_title.py`（dry-run 默认，--commit 分批 UPDATE）回填。
--   **真实播种开闸前置 = 本文件 apply + backfill 跑完**——NULL 行不参与等值匹配；
--   过渡期 seeding 保留 LIKE 辅臂兜底，辅臂命中而主臂 miss 时 WARNING 报 backfill 缺口。
-- 部署顺序：**先 apply 后部署**（新代码 mint 即写该列；未 apply 的库上 INSERT 报 1054）。
--   ontology 写路径生产 flag-off（RAG_ONTOLOGY_ENABLE 未开），无现网风险窗口。
--
-- 038a 修订（unknown-unknowns 批次4 P1-02，2026-07-16；台账记 038_...sql@038a，铁律 2）：
--   原版是重放家族里**唯一**未守卫的裸 ALTER（其余 011/013/027-034 全幂等）——二次执行
--   1060 duplicate column；「DDL 落定但台账写失败(exit 3)」后的恢复重跑必卡死。现改 033
--   同款 information_schema+PREPARE 守卫；已 apply 过原版的库（staging/prod）重放本文件
--   为 no-op，apply_ontology_dbs.py 按下行 revision 声明记 @038a 台账行、不改原 038 行。
-- revision: 038a

SET @has_col := (SELECT COUNT(*) FROM information_schema.COLUMNS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_object'
                   AND COLUMN_NAME = 'normalized_title');
SET @ddl := IF(@has_col = 0,
               'ALTER TABLE ontology_object ADD COLUMN normalized_title VARCHAR(512) NULL DEFAULT NULL COMMENT ''标题聚类键（normalize.title_key：去空白+全半角统一）；038 起 mint 写入，存量走 backfill 脚本'' AFTER title',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;

SET @has_idx := (SELECT COUNT(*) FROM information_schema.STATISTICS
                 WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ontology_object'
                   AND INDEX_NAME = 'idx_type_status_norm_title');
SET @ddl := IF(@has_idx = 0,
               'ALTER TABLE ontology_object ADD KEY idx_type_status_norm_title (object_type, status, normalized_title)',
               'SELECT 1');
PREPARE _s FROM @ddl; EXECUTE _s; DEALLOCATE PREPARE _s;
