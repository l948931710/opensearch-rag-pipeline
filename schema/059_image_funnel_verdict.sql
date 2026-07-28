-- 059_image_funnel_verdict.sql — 选项 E（方案 §11，Sam 拍板 2026-07-26）：
--   把图片漏斗判决从"可淘汰的缓存"升级为"不可淘汰的记录"
--
-- 为什么：漏斗判决决定**哪些图能进知识库**，但它今天只存在 VLM 缓存里——一个
--   advisory、按 rowid FIFO 淘汰（RAG_VLM_CACHE_MAX_ENTRIES 默认 50000）、可被
--   RAG_VLM_CACHE_VERSION 一键整体作废的 SQLite KV。2026-07-25 实测（方案 §8）：
--   缓存里 07-20 写下的判决与今天的 VLM 有 6.5% 不一致、**全部单向
--   ROUTE_TO_VECTOR → DISCARD**，其中 4 张是 GT 明确期望的步骤配图。
--   ⇒ 这些文档现在图还在，靠的是缓存条目撑着；缓存一淘汰或版本一提升，图就静默消失。
--   本表让**已判过的图免疫模型制度漂移**：容量不淘汰，失效必须显式换 policy 版本。
--
-- 键为什么是内容寻址（而不是草案 §94 的 doc_id+version_no）：
--   ① 被判决的对象是图片字节的属性，不是文档的属性；
--   ② 要挡的漂移场景恰恰是**同一文档的新版本**（正文改一个错字就换版本号），按
--      (doc_id, version_no) 建键在该场景下天然不复用，等于没挡住；
--   ③ 内容寻址保住了 VLM 缓存现有的跨文档复用经济性。
--   `funnel_policy_version` 进主键 ⇒ 判据换代（如选项 C）自然产生新行、不破坏旧行。
--
-- ⚠️ 载荷刻意**不含** ocr_text / visual_summary / vlm_annotation_map（codex BLOCKER-2）。
--   那些是派生内容且可能含 PII：图像 OCR 的 PII 脱敏默认 OFF（RAG_IMAGE_OCR_PII）
--   且「仅本轮内存生效，不回写 canonical OSS JSON」（pipeline_nodes.py:2559）。它们
--   留在既有 VLM 缓存里（那个暴露面已存在且不变），**不再复制进 RDS 这个新查询面**。
--   代价明说：E 因此只交付**稳定性**，不交付"重灌不重付 VLM"——RDS 命中但缓存未命中
--   时仍要调一次 VLM 取 caption，但**强制采用本表记录的 status**（图集稳定，caption
--   允许重掷；PDF-D5 已切断 caption→绑定耦合，caption 漂移不再动绑定）。
--
-- 消费方：extraction/unified_extractor 的判决读写缝（flag RAG_FUNNEL_VERDICT_STORE
--   默认 OFF，**apply 在前、开 flag 在后**）。表缺失 1146 → 优雅降级为纯缓存行为，
--   先部署后 apply 安全。
-- 目标库：fuling_knowledge（与 document_version / chunk_meta 同库）。

CREATE TABLE IF NOT EXISTS image_funnel_verdict (
  image_sha256          CHAR(64)     NOT NULL
                        COMMENT '图片字节 SHA-256（与 VLM 缓存主键同源，_image_sha256）',
  namespace             VARCHAR(8)   NOT NULL
                        COMMENT 'pub|sec —— 必须保留缓存已有的隔离：public 文档 bypass 了敏感审计，其判决绝不能服务 quarantine 文档',
  funnel_policy_version VARCHAR(16)  NOT NULL DEFAULT ''
                        COMMENT '漏斗判据代际（ingest_flags.effective_funnel_policy_version；"" = 历史判据，"c1" = 选项 C）',
  status                VARCHAR(32)  NOT NULL
                        COMMENT 'ROUTE_TO_VECTOR|ROUTE_TO_TEXT|DISCARD_DECORATIVE|QUARANTINE_SENSITIVE',
  image_category        VARCHAR(32)  NOT NULL DEFAULT ''
                        COMMENT 'VLM 类别。真弃也要留它——否则只有 free-text reason 声称"VLM 明确表态"，没有可复核的结构化证据',
  funnel_stage          VARCHAR(16)  NOT NULL DEFAULT ''
                        COMMENT 'funnel1|funnel3 —— Funnel 1 是确定的几何判据，与 VLM 制度无关，可跨 policy 复用',
  vlm_model             VARCHAR(64)  DEFAULT NULL
                        COMMENT '判决时的 VLM 模型；缓存提升来的旧行为 NULL（不伪造历史事实）',
  provenance            VARCHAR(24)  NOT NULL DEFAULT 'fresh_decision'
                        COMMENT 'fresh_decision|cache_promoted —— 后者来自存量 VLM 缓存，无真实 decided_at/model',
  first_doc_id          VARCHAR(64)  DEFAULT NULL COMMENT '首次判决所在文档（仅溯源，不构成使用台账）',
  first_version_no      INT          DEFAULT NULL COMMENT '首次判决所在版本',
  superseded_by         BIGINT       DEFAULT NULL
                        COMMENT '预留：单图受控纠错的 append-only revision 指针。**当前无该通道**（本次刻意划出范围，见方案 §11.4）',
  decided_at            DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (image_sha256, namespace, funnel_policy_version),
  KEY idx_ifv_policy_status (funnel_policy_version, status),
  KEY idx_ifv_first_doc (first_doc_id, first_version_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='E: 图片漏斗判决记录（内容寻址，不淘汰，显式失效）——让已判过的图免疫 VLM 制度漂移';
