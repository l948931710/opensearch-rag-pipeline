-- ═══════════════════════════════════════════════════════════════
-- Step Card 增强 — chunk_meta 表扩展
-- ═══════════════════════════════════════════════════════════════
-- 新增字段支持 step_card / procedure_parent chunk 类型：
--   parent_chunk_id: step_card → procedure_parent 的父子关联
--   step_no: 步骤编号（在 procedure 内的排序）
--   image_refs_json: 绑定到此 chunk 的图片引用列表
-- ═══════════════════════════════════════════════════════════════

USE fuling_knowledge;

-- 1. 新增 parent_chunk_id 字段（step_card 关联 procedure_parent）
ALTER TABLE chunk_meta
  ADD COLUMN parent_chunk_id VARCHAR(128) DEFAULT NULL
    COMMENT 'step_card 的父 chunk ID，指向 procedure_parent',
  ADD INDEX idx_parent_chunk (parent_chunk_id);

-- 2. 新增 step_no 字段（步骤编号）
ALTER TABLE chunk_meta
  ADD COLUMN step_no INT DEFAULT NULL
    COMMENT '步骤编号，用于 step_card 在流程内的排序';

-- 3. 新增 image_refs_json 字段（图片引用列表）
-- 存储格式: [{"image_index": 0, "source_image": "...", "oss_key": "..."}]
ALTER TABLE chunk_meta
  ADD COLUMN image_refs_json JSON DEFAULT NULL
    COMMENT '绑定到此 chunk 的图片引用列表（step_card 使用）';
