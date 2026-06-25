-- ════════════════════════════════════════════════════════════════════════════
-- 007_kb_etag_dedup_index.sql — document_version.etag 索引（自助上传 register 跨部门内容查重用）
--
-- 背景：kb_register 现把 OSS ETag（自助上传单次 PUT ⇒ 内容 MD5，与路径/部门无关）写入
--   document_version.etag，并在登记后按 etag 跨全库找其它 active 文档做【字节级内容查重】
--   （advisory，不拦上传；隐私分级：范围外只计数）。该查询 `WHERE v.etag=%s` 需此索引避免全表扫。
--
-- ADDITIVE / 非破坏 / 与 005 同纪律：
--   * 仅加二级索引，不改列、不改既有行（存量 etag 多为 NULL，多 NULL 不冲突、不命中）。
--   * 不动检索路径、不动 HA3 → 对问答用户零影响。
--   * ⚠️ NOT YET APPLIED——gated 生产 DDL。代码【不依赖】此索引即可正确运行（仅查询变快）；
--     按需通过 information_schema 守卫的 apply 脚本（RW token）应用，同 003/004/005。
--   * MySQL 8.0 无 CREATE INDEX IF NOT EXISTS；apply 脚本须守卫 information_schema.STATISTICS。
--
-- 非唯一索引（绝不能 UNIQUE）：同一文件合法地被多个部门各自上传（不同 ACL）——内容相同是
--   advisory 提示，不是冲突；唯一约束会误拒合法的跨部门同内容上传。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge
CREATE INDEX idx_etag ON document_version (etag);
