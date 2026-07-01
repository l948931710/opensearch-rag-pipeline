# 全仓架构与生产级代码质量审查报告

- **仓库**：opensearch-rag-pipeline（富岭塑胶企业 RAG 系统）
- **审查基线**：`git HEAD 39cf4fd`（main，工作区干净）
- **日期**：2026-07-01
- **方法**：17 个分区/横切审查员并行深审（摄取 3 段 / API 2 段 / 检索 / 生成 / 钉钉 / 分块 / 抽取 / 配置守卫 / 对账运维 / 数据模型 / 前端 / 性能 / 安全 / 测试）→ 跨审查员按 file+line 去重 → 对每条 P0/P1 双视角（代码真实性 + 部署现实性）、P2 单视角**对抗验证**（验证员被要求"尽力推翻"）。共产出 91 条原始发现，去重后 90 条新发现；60 条进入对抗验证，**确认 55 条、推翻 4 条、存疑 1 条**；另有 97 条性能优化机会。

---

## 一、总体结论

这是一套**单人维护、但工程纪律远高于同类项目平均线**的生产系统。关键安全不变量有单一咽喉把守、复制代码用 parity 测试钉住、配置阈值带评测出处（251 题金集、A/B 裁决直接写进注释）、1627 个测试覆盖几乎每个生产模块、三套守卫（生产安全 / 环境交叉验证 / 破坏性操作）设计成熟。历次外审的 P0/P1 大多已闭环。

**没有发现 P0（正在生产造成数据丢失/泄露/中断）级问题。** 真正需要处置的是 **4 条 P1**（有现实触发路径的正确性/安全缺陷）和 **~40 条 P2**（健壮性/安全/一致性实质问题）。结构债高度集中在两块巨石文件（`pipeline_nodes.py` 7074 行、`api.py` 4905 行）与若干"防护做了但在生产路径上是死代码"的失效面上。

**成熟度评级：约 6.5–7 / 10（生产可运行，有明确的加固清单）。** 距离"8"的差距不在功能，而在：① 巨石拆分带来的可测试性/隔离性；② 一批"修复在测试里绿、在生产路径上不生效"的死代码；③ PII 脱敏与传输安全的几处旁路；④ 无界日志表的留存治理。

---

## 二、架构评估

### 2.1 分层与耦合
- **摄取**（dag_engine / dag_definitions / pipeline_nodes / dataworks_orchestrator）与**服务**（api / retriever / llm_generator / answer_flow）与**前端**（dingtalk_* / console-app）三层职责清晰，但**服务层隐式背上整个摄取栈**：`qa_logger → redaction → pipeline_nodes(ENTITY_PATTERNS) → chunker`，服务进程写第一条 QA 日志就要能导入 7074+2886 行摄取代码（详见 F-A1）。
- `config.py` 名义是配置中心，实测 114 个 `RAG_` 环境键仅 51 个进 `config.py`，其余散读在各模块——多为有意的就近运维开关，但削弱了 banner/shadowing 覆盖面。

### 2.2 数据一致性架构
三套 outbox/自愈模式（`document_version.PENDING_DELETE`、`kb_acl_projection_outbox`、`chunk_meta` 标脏+NOT_INDEXED）实现各异但不变量一致，全部在 stage-3 收敛 drain，另有 reconcile/parity/spot_checker 全扫兜底。`schema/009` 的"同事务 enqueue + UNIQUE(doc_id) 复活"是教科书级 at-least-once 投影。**模式统一度可接受，暂不必强行归一。**

### 2.3 扩展性天花板
单实例 `--workers 1`（有意，会话内存态）+ AnyIO 默认 40 线程令牌 + DB 池 `maxconnections=10`（blocking=True）构成三层并发天花板。当前 QPS 安全，但**40-token 层最先失守**（F-P2），10 连接的 DB 池在"每请求 ACL 复核逐请求建连"时是第二瓶颈。这条链是已文档化的权衡，但 Dockerfile 清单已漂移（P3）。

### 2.4 数据模型
两库分治（`fuling_knowledge` 知识/管线状态 / `fuling_operation` 用户运营）语义清晰。三个结构性依赖需记录：
1. **跨库 JOIN 假设同实例**（insights/governance/gaps/contribution 全依赖 `{op_db}.x ⋈ {kb_db}.y`）——将来拆实例这批查询整体失效，建议升格为 schema 头注显式契约。
2. **无外键是刻意选择**，孤儿风险由应用层（幂等续跑 + 唯一键 + reconcile/outbox）兜住，现状可接受。
3. `qa_session_log.retrieved_docs_json` 同时承担"审计快照"与"归因分析事实表"双职，是所有 `JSON_TABLE` 全表扫查询的根源。
- **暗礁**：`created_at` 全库存太平洋墙钟时间、靠 `CONVERT_TZ` 补偿并依赖 RDS 已加载具名时区表（正确但脆）；状态列全 VARCHAR 无 ENUM/CHECK；`document_version.index_status='SUCCESS'` 与 `chunk_meta.index_status='INDEXED'` 同名列不同词表；001 里有 **7 张零代码引用的死表**（尤其 `document_acl_rule` 长得像 ACL 权威但真权威在别处，建议标 DEPRECATED 防误接）。

---

## 三、确认发现（按严重度）

> 每条都经对抗验证确认。文件行号以 HEAD 39cf4fd 为准。

### P1 — 有现实触发路径的正确性/安全缺陷（4 条）

**F-1 [P1] PII 正则 `\b` 在中文邻接处失效 → 身份证号/AccessKey 紧跟中文时漏检、明文入索引**
`pipeline_nodes.py:48` — `cn_id_card` 与 `access_key` 用 `\b` 边界，而 Python `\w` 含 CJK，`\b` 只在 word/非word 交界成立。实测：「身份证号: 110101199001011234」命中，但「身份证号110101199001011234」**不命中**、「密钥LTAIabcdefgh12345」**不命中**。对比 `cn_mobile` 用的是 `(?<!\d)…(?!\d)` 能正确命中。
- **触发**：HR/名册文档正文或 OCR 输出中号码紧贴中文（OCR 丢冒号/空格极常见）→ `node_detect_sensitive` 零命中 → 不隔离 → 同一 `ENTITY_PATTERNS` 也不掩码 → 完整 18 位身份证 / 高危 AK 进 HA3 被检索。
- **修复**：`cn_id_card` 改 `(?<![0-9Xx])…(?![0-9Xx])`、`access_key` 改 `(?<![A-Za-z0-9])(LTAI|AKIA)[A-Za-z0-9]{12,}(?![A-Za-z0-9])`，并对既有语料回扫复检。

**F-2 [P1] `xlsx_layout_type` 在生产 Stage-2 重载时被丢弃 → 防误判修复在生产路径上是死代码**
`dataworks_orchestrator.py:266` — Stage-2 loader 手工构造 `canonical_doc` 字段白名单，缺 `xlsx_layout_type` 和 `filename`；而 DAG1 已把它写进 canonical JSON（`pipeline_nodes.py:809`）。消费点 `pipeline_nodes.py:3723` 在生产 Stage-2 永远读到 None → 落入回退重分类，`filename` 同样为空。代码注释自己写明了后果。
- **触发**：任何 `procedure_image_guide` 类 xlsx（全屏截图型作业指导书，靠文件名得分）走生产 `--stage 2`：重载后判成 `normal_spreadsheet` → step_card/图片绑定结构静默丢失、chunk 家族翻转。本地 sim（ctx 直传 dict）不复现，测试因此漏掉。
- **修复**：`canonical_doc` 补 `"xlsx_layout_type": content_json.get("xlsx_layout_type")` 与 `filename`；加一条 stage-2 重载合同测试。

**F-3 [P1] `cosurface_doc_images` 的 HA3 kNN 查询缺 `order="DESC"`（G29 陷阱复发）→ 每文档选中最不相关的图片**
`retriever.py:1300` — 该 `QueryRequest` 无 `order` 参数，而索引是 InnerProduct（越高越相似），无 DESC 时引擎返回**升序（worst-first）**。下游 `1314-1321` 行"每个文档取首个"实际取到最不相关的图片。主检索路径有 DESC 正常，恰好掩盖了这里。
- **触发**：控制台 `/api/ask/stream`（`RAG_IMAGE_COSURFACE` 默认 ON）检索结果无 image chunk 时触发补图 → 截图密集的 SOP/ERP 文档系统性把错图附到答案上——正是本项目"图绑错步骤"核心痛点的静默复发。
- **修复**：加 `order="DESC"`；更根本是抽统一 dense-query helper（照 `eval_harness/ha3live.py` 的 `order` 默认 DESC）收敛所有 `client.query()` 调用点，让 G29 不可能第三次复发。

**F-4 [P1] step 模式前导文本被 Phase 2 与 Phase 4.9 双重发块 → 每个带前言的 SOP 产出重复 text_chunk**
`chunker.py:1270` — Phase 2（L992-1007）与 Phase 4.9（L1267-1282）对同一份 `preamble_texts` 各发一遍块。实测复现：1 段前言 + 2 步骤的文档产出 5 个 chunk，其中 index 0 与 3 逐字节相同。git 溯源：Phase 2 于 883605c 引入，Phase 4.9 于 0fe49fd 叠加，Phase 2 未删；下游无 intra-doc 去重。
- **触发**：任何 step 路由文档（SOP/作业指导书，生产语料主力）只要有前导段落（目的/范围/职责几乎必有）→ 同一段文字两个 chunk 各自 embedding、各自入库；检索 top_k=7 时两个相同 chunk 同时占坑挤掉本应召回的内容，来源面板出现重复引用，存储/嵌入成本按重复量白付。
- **修复**：删 Phase 2 或 Phase 4.9（二选一，建议留 4.9 合并式发块）+ 回归测试。**注意这是 chunk-family 变更**，按 CLAUDE.md 需走冻结 re-chunk + count/type_mix manifest 门。

### P2 — 健壮性/安全/一致性实质问题（重点子集，共约 40 条）

#### 安全 / PII

- **F-5 [P2] 匿名 `/api/ask` 可用任意 `user_id` 向他人私有历史注入伪造问答** — `api.py:536`。攻击者不带 Bearer，POST `{"question":"<钓鱼文本>","user_id":"<受害者staffId>"}`（org 内 staffId 半公开可枚举）→ 服务器以受害者身份写 `qa_session_log` → 受害者在自己"历史问答"里看到伪造记录、审计链归错人。**修复**：匿名请求禁止用请求体 `user_id` 作落库身份，落 `anon:<ip_hash>`；四条链路统一在 `build_qa_log_kwargs`。
- **F-6 [P2] 钉钉 `/dingtalk/webhook` 签名只覆盖 timestamp、不认证 body** — `dingtalk_bot.py:125`。生产 EIP 为明文 HTTP，on-path 攻击者抓一次合法 `(timestamp, sign)` 头对，1 小时窗口内重放并替换 body：改 `senderStaffId` 为财务员工越权读财务内容、改 `sessionWebhook` 为自己的服务外发（SSRF/外泄）。**修复**：强制 HTTPS 或全量走 Stream WSS；`sessionWebhook` 加钉钉域名白名单；收紧时间窗到 ~300s。
- **F-7 [P2] 钉钉消息原文（含身份证/手机号）无条件明文写应用日志** — `dingtalk_bot.py:761`。主路径把完整 body 以 INFO 级写 logger+stdout，SAE 汇聚长期留存，不受 `RAG_QA_LOG_PII_REDACT` 约束，与作者在异常路径已做的脱敏自相矛盾。**修复**：主路径只记结构化元数据，问题文本先过 `redact_query_text`。
- **F-8 [P2] qa_session_log PII 掩码被 `content_blocks_json` 旁路** — `qa_logger.py:150`。`query_text/answer_text` 已掩码，但同一行 `content_blocks_json` 的 markdown 块明文保留身份证/手机号。**修复**：入库前对 blocks 做结构感知掩码（仅 `type=markdown` 的 content 与 image caption 走 `_redact_for_log`，保 url/oss_key 不动）。
- **F-9 [P2] REDACT 路径只掩码 5 类正则实体** — `pipeline_nodes.py:2103`。银行卡/社保/护照号仅有关键词分级、无对应正则；「工资卡号：6222…678」命中关键词→medium→走 REDACT 但卡号原样保留入索引，`document_sensitive_finding` 还记成 REDACTED（"已脱敏"假象）。**修复**：为 keyword 已声明的号码类补正则+掩码（银行卡加 Luhn 降 FP），或升级为人工 review 而非直接放行。
- **F-10 [P2] redaction 正则漏 15 位旧式身份证号** — `redaction.py:67`。老 HR 花名册的 15 位身份证既不匹配 `cn_id_card` 也不匹配 `bank_card` → `redact_text` 原样保留、rescan 零残留 → 通过"零高危残留"发布闸。**修复**：增加 `legacy_id` 15 位日期结构校验模式，归入 HIGH_TYPES。
- **F-11 [P2] `spot_checker` 把 LLM 原始输出直接写 `permission_level`** — `spot_checker.py:558`。违反"permission_level 绝不由 LLM 决定"约定；LLM 返回带注释的长值 → 检索过滤永不匹配（文档凭空不可见）；>64 字符 → RDS Data too long → 整个隔离事务 rollback、连索引删除都跳过（文档既没隔离也没删）。**修复**：写库前白名单归一化，隔离动作照常执行。

#### 摄取正确性 / 数据丢失

- **F-12 [P2] `_push_chunks_to_ha3` 的 HTTP 状态码分支对真实 HA3 SDK 是死代码** — `pipeline_nodes.py:6051`。真实 SDK 任何 2xx 都会把整个 sub-batch 标 INDEXED，即使 body 含 doc 级 errors；若 `RAG_STAGE3_PARITY_VERIFY` 未开（默认 OFF，笔记本手工重灌即此形态）→ 被拒 chunk 静默消失（与已治愈的 96 例同类）。**修复**：`json.loads(body)` 后走 per-doc 解析；改在 `except TeaException` 里区分 400/429/5xx；04b 常开。
- **F-13 [P2] 并发分类路径吞掉本应中止节点的 DB 写失败** — `pipeline_nodes.py:1892`。批次 ≥2 篇（生产常态）时 RDS 中途故障：持久化异常被吞、全部进 failed → 节点标 SUCCESS、DAG2 对空列表"成功"跑完。同一故障在单文档批次会正确 abort——**错误语义随批次大小改变，测试守护的不变量在真实并发路径上不成立**。**修复**：区分业务失败(返回 False) 与意外异常，对后者取消剩余 futures 并 re-raise。
- **F-14 [P2] classify 失败写 FAILED 不自增 retry_count** — `pipeline_nodes.py:1791`。确定性坏文档（畸形文本触发 400 / 持续 429）每个 drain 批次都重认领→FAILED（retry_count 恒 0）→ pending 不降 → orchestrator no-progress 守卫 raise → **此后每天 stage-2 都以 RuntimeError 收场**。**修复**：fail-safe UPDATE 里加 `retry_count = retry_count + 1`（与 canonical 读失败路径一致）。
- **F-15 [P2] `final_risk = max(llm, entity)` 在主路径失效** — `pipeline_nodes.py:1731`。非隔离路径文档的 LLM 风险评估被强制降为 low：语义敏感但不命中 regex 的文档（换措辞的薪酬明细、价格表扫描件）LLM 判 high 也被抹成 low → CLEAN 直接发布，且 `risk_level` 落库为被抹后的值（审计失真）。**修复**：若有意收紧则改为 high→medium+建 review_task 且改名说明；若非有意则删除覆盖。
- **F-16 [P2] SKIPPED_DUPLICATE 跳过闸的哈希忽略图片/blocks** — `pipeline_nodes.py:852`。ERP 改版只换全部截图（本语料核心场景）→ v2 被判重复跳过、current_version 回退 v1 → 旧截图永远服务、上传者无报错（静默数据丢失）。`RAG_SKIP_UNCHANGED_REINGEST` 已按运维决策在 DataWorks 置 true。**修复**：把资产 MD5 集折进比较哈希，或数量/集合不一致时绕过 skip。
- **F-17 [P2] `_dedup_table_chunks` 删块后不重编号 → chunk_id 撞号 → 整批写库失败** — `chunker.py:1874`。非 step 路由 DOCX 去重页眉表 + 带一张 clause 载体外的图片 → 图片兜底用 `len(chunks)` 生成 chunk_id 与幸存 chunk 撞号 → MySQL 1062 → node 整批失败。**修复**：dedup 后重编号 chunk_index 并重生 chunk_id（或兜底改用 `max(chunk_index)+1`）。
- **F-18 [P2] `section_title` 超 255 字符无防线** — `pipeline_nodes.py:4917`。Fix B 只覆盖 clause/text/section 三类；step_card/faq/table 类继承的超长标题 → `executemany` 抛 1406 Data too long → 整批（最多 100 文档）chunk 写入回滚、毒文档每日重试每日失败拖住日更管线。**修复**：`node_validate_chunks` 截断为 252+'...' 记 warning，而非丢 chunk。

#### ACL / 检索

- **F-19 [P2] 图片/幻灯片 chunk 的 owner_dept 取自路径、文本 chunk 取自 document_meta → 同文档 ACL 归属分裂** — `pipeline_nodes.py:4421`。管理员改正 `owner_dept` 后升版：文本 chunk='sales'、图片 chunk='marketing' → sales 员工检索到正文拿不到内嵌图，marketing 员工反而能召回本不该见的图片 chunk。**修复**：统一用 `doc.get("owner_dept")`（RDS 权威），路径推导仅留给 OSS 路径拼接。
- **F-20 [P2] 纯向量降级分支同样缺 `order="DESC"` 且 max_distance 语义与 InnerProduct 相反** — `retriever.py:629`。`HA3_ENABLE_HYBRID=false` 逃生路径下按升序返回、最不相关排第一；配 `max_distance>0` 时 `score <= max_distance` 会把最相关结果全部滤掉。**修复**：加 DESC；max_distance 改下限过滤或删除+修注释。
- **F-21 [P2] `expand_step_context` 意图筛选可能把命中 chunk 本身丢出上下文** — `retriever.py:1036`。10-12 步 SOP、命中第 10 步、`full_procedure` 截 `siblings[:8]` → 命中卡被截掉、家族 ≤12 不触发 cap 兜底 → 最佳匹配文本从未进 LLM 上下文 → 答案只讲前 8 步。**修复**：意图筛选后统一兜底，命中 chunk_id 不在 selected 则强制加入（cap 分支已有 `keep_ids={chunk_id}` 的正确模式，提升为公共不变量）。
- **F-22 [P2] `_resolve_user_dept` 的 user_role 缓存永不刷新 + `_fetch_dept_name` 瞬时失败静默丢部门** — `dingtalk_identity.py:257`。多部门用户首解析时某部门 `department/get` 超时 → dept 缓存缺一个组、**永久少授权**，机器人路径无 TTL、无 API 复核，只能人工改库。**修复**：user_role 缓存加时间戳+短 TTL 复核；任一 dept 解析返回空视为"不完整"不落缓存。

#### 生成 / 会话

- **F-23 [P2] `/api/ask/stream` 落库漏传 cited_docs → cited_docs_json 恒 NULL** — `api.py:858`。控制台流式全部命中：看板"帮助用户数/被引用"把控制台流量记零、反馈取不到引用上下文。**修复**：`event_generator` 的 finally 补 `cited_docs=_extract_sources(chunks)`；parity 测试补 stream 用例。
- **F-24 [P2] 含 `<<IMG:N>>` 占位符的回答被写进 LLM 会话历史 → 跨轮错图绑定** — `api.py:649`。第 2 轮 LLM 模仿历史格式复用 `<<IMG:3>>`，但新检索文档 3 已换 → 按新编号把无关截图插进答案。客户端历史是干净的，唯独服务端脏，说明非有意。**修复**：入史前 `strip_image_markers`，blocks 构建仍用原始 answer。

#### 抽取 / 多模态

- **F-25 [P2] txt/md/csv/html 用 `errors="ignore"` 硬按 UTF-8 读 → GBK 中文整体静默剥除** — `unified_extractor.py:1103`。国内制造业遗留 GBK/GB18030 文件中文整段丢弃，要么 0-chunk 要么索引成乱码 chunk。**修复**：先 utf-8 strict，`UnicodeDecodeError` 回退 gb18030，仍失败才 replace + warning。
- **F-26 [P2] xlsx 图片提取无条件全量 `load_workbook`，绕过 read_only 大文件守卫** — `image_extraction_utils.py:424`。60 万行 xlsx 文本抽取靠 read_only 幸存，图片提取全量解析同一文件 → 数 GB 内存 → OOM-killer 直接杀进程、整批当日失败。**修复**：入口复用同一闸（>100MB 或 max_row>50000 跳过图片提取+loud log）。
- **F-27 [P2] VLM 缓存 OSS 持久化是整文件 last-write-wins，本地存在时永不回读 OSS** — `unified_extractor.py:1246`。笔记本重灌与 DataWorks 每日 Stage-1 同日跑 → 后写者整包抹掉对方当天新增标注 → 被抹条目全量重打 VLM（真金白银）且结果可能漂移。**修复**：保存前 GET OSS dict-merge 再 PUT；加载后也 merge（条目只增语义让 merge 天然安全）。
- **F-28 [P2] `_enrich_xlsx_annotations` 假设 `drawingN.xml ↔ 第 N 个 sheet`** — `image_extraction_utils.py:582`。首 sheet 无图/有隐藏 sheet 时映射错位 → ①②③ 标注静默丢失 → 同锚多图绑错概率上升。**修复**：解析 `sheetN.xml.rels` 建立权威 sheet→drawing 映射。

#### 分块

- **F-29 [P2] FAQ 模式静默丢弃全部 image_ref 块** — `chunker.py:1516`。"绝不丢图"契约在 faq 路径缺位（clause 已修）：ERP FAQ 是多截图典型，ROUTE_TO_TEXT 的 UI 截图无任何 serving 可达载体，用户永远看不到图。**修复**：仿 `_chunk_by_clause` 用 `pending_image_refs` 挂到最近 faq_chunk。
- **F-30 [P2] clause 收尾把全文档图片 caption 无 token 预算拼进末 chunk** — `chunker.py:1835`。30+ 图的规程文档末条款 token 超 2000 → 验证丢弃 → 条款文本连同全部 image_refs 一起消失，"绝不丢图"修复被自身无预算追加击穿。**修复**：给 `_finalize_clause_with_images` 加与 step 同款预算，溢出拆续接块或只留 refs 载荷。

#### 配置 / 运维 / 前端 / 测试

- **F-31 [P2] 环境标签未归一化、无白名单校验** — `config.py:757`。`'Production'`/`'prod'`/尾随空格静默绕过 Gemini 禁令与签名密钥强制，且各守卫层解释不一致（auth_token 生成临时密钥致重启后会话失效、env_guard 视为非生产转 READ ONLY 致 serving 写静默失败）。**修复**：`load_config()` 里 `.strip().lower()` 归一化一次，非白名单值直接 raise。
- **F-32 [P2] `send_ops_alert` 把 HTTP 200 当送达、不解析 errcode** — `alerting.py:68`。签名/关键词配错时每次收 200+errcode=310000、返回 True、无 warning → 运维通道死了而所有指标显示健康，直到真实事故复盘才发现。**修复**：解析 errcode，非 0 则 warning+return False；加 `--selftest`。
- **F-33 [P2] G30 loop-until-stable "本轮无新增即判稳定"** — `reconcile.py:192`。首轮返回空（文档自述最坏情形，2026-06-20 已实际发生一次 false-FAIL）→ 立即判稳 → 发出并不存在的召回丢失 critical 告警。**修复**：改"连续两轮无新增"或与 RDS 行数对比。
- **F-34 [P2] HA3 孤儿 PK 自愈的唯一挂载点无生产调用方** — `ha3_reconcile.py:23`。`reconcile_ha3_orphan_pks` 只挂在 `run_spot_check_pipeline`，而后者生产从不自动运行 → 维护性 re-chunk 后新旧 PK 并存（同内容双行、一行陈旧）静默累积，直到有人记得手工跑。**修复**：ops_monitor 加只读 stale 数告警 job；commit 模式挂进 stage-3 pre-drain。
- **F-35 [P2] schema/010 缺 `normalized_gap_query` 列 → DDL 与生产表漂移** — `schema/010_kb_contribution.sql:55`。按 schema/ 目录重建的环境（staging/灾备/新部署）提交任何贡献都 1054 Unknown column → 功能整体不可用。同表现存三份互相漂移的 DDL 副本。**修复**：补列，并以 schema/ 为唯一 DDL 权威。
- **F-36 [P2] 日志/审计类表全部无界增长、零清理策略** — `schema/001_opensearch_pipeline.sql:245`。`qa_session_log`（携 MEDIUMTEXT）、`kb_audit_log`、`document_sensitive_finding`、`pipeline_run` 只进不出：看板窗口全表扫延迟逐月爬升；且"无限期保留全部员工问答"本身是数据治理暴露面。**修复**：定留存策略并落 DataWorks 日任务/MySQL EVENT（qa_session_log 保留 N 月后仅留 rollup、content_blocks_json 先期置 NULL 瘦身）。
- **F-37 [P2] 退役文档可经"升版/审批"被静默复活进索引** — `api.py:2848`。版本生命周期端点均不检查 `document_meta.status`：dept_admin 对已退役文档 upload-url(version)+register → 次日正常认领入 HA3 → 全员可检索。**修复**：version 分支与 register 的 FOR UPDATE 带出 status，非 active → 409。
- **F-38 [P2] kb_register 升版双击/重试竞态落成两个版本** — `api.py:2832`。幂等 SELECT 先于行锁且 raw_key 无唯一约束 → 同一上传登记成两个 NOT_STARTED 版本、管线双倍抽取/嵌入。**修复**：raw_key 幂等 SELECT 挪到 FOR UPDATE 之后；落地已预留的 `UNIQUE(raw_key_hash)`。
- **F-39 [P2] 缺口闭环只匹配 question_hash，gap_query_hash 写入后无读者** — `api.py:4770`。采纳时修订问题或贡献者改写措辞 → 缺口永不关闭、长期挂列表顶、员工反复重复回答。**修复**：覆盖查询改 `question_hash IN (...) OR gap_query_hash IN (...)` + 加索引。
- **F-40 [P2] 跨部门授权去重只挡 pending、重复 approved 使撤销静默失效** — `api.py:3377`。同 (doc, requester) 两条 approved 行 → 撤销其一 API 返回成功但另一条仍把码并回 want → 检索授权实际仍有效，管理员确信已收回。**修复**：幂等检查扩为 `status IN ('pending','approved')`。
- **F-41 [P2] `/api/ready` 是 async 却做阻塞式 RDS+HA3 I/O** — `api.py:402`。RDS 主备切换时阻塞事件循环线程 → 循环无法派发 sync 的 `/api/ask` → 整实例在故障期（正应快速返回 503 时）反而集体卡死数十秒。**修复**：改普通 `def` 或 `run_in_threadpool` 包裹并加独立短超时。
- **F-42 [P2] 前端 401 重登无单飞守卫** — `console-app/src/composables/useAuth.ts:175`。并发 401 触发多路钉钉免登竞态，中途 `setToken('')` 剥掉其他请求重试的 Authorization。**修复**：给 reauth 加模块级单飞 Promise。
- **F-43 [P2] reranker.py 内部逻辑零单测** — `reranker.py:146`。所有测试整体 mock 掉 `rerank_chunks`，而校准 regime 声明生产 rerank 已开启：results 少于送入 documents 时缺失 index 的 chunk 静默消失、api_base_url 变化致永久 404 fail-open，测试全绿。**修复**：新增 `tests/test_reranker.py` mock HTTP 边界，覆盖路由/缺 index/异常降级。

### 值得关注的 P3（未单独验证，30 条中的高信号项）
- `pipeline_nodes.py:497` scan 补全 doc_id 时 DB 查询失败被吞 → 已存在文档静默分叉出新 doc_id（孪生文档）。
- `retriever.py:809` stitch 在中心 chunk 行缺失/被过滤而邻居仍在时，拼接文本会**替换掉命中 chunk 自身的正文**。
- `llm_generator.py:386` 检索内容零转义直进 prompt，正文/标题/visual_summary 可伪造 `=== 用户问题 ===` 边界或植入 `<<IMG:N>>`——**众包贡献入库后成为现实注入面**。
- `llm_generator.py:255` `_extract_sources` 按"标题去扩展名"折叠来源 → 跨部门同名不同内容文档被并成一行、第二篇 doc_id 从 cited_docs 消失。
- `prod_access.py:45` 只读入口 overlay fallback 链末端是 `.env.production`（fuling_admin RW 账号）：`.env.prod_ro` 缺失时诊断脚本静默升格为管理员凭证。
- `unified_extractor.py:796` `_extract_xlsx` 的 `wb.close()` 不在 finally → 异常泄漏文件句柄；`unified_extractor.py:1066` PPTX 整包一个 try，单张坏 slide 放弃其后所有（与 PDF 逐页 fallback 不一致）。
- `schema/001:12` `CREATE DATABASE` 未指定 CHARSET/COLLATE → 1267 collation 事故根因层未修，防线只靠每张新表手写 COLLATE 的纪律；`schema/002_step_card_enhancement.sql:13` 迁移版本号冲突 + 无 `schema_migrations` 台账。
- `dingtalk_bot.py:995` `_rebuild_card_param_map` 是生产死代码（~100 行 + 一次 qa 查询）。
- `ops_monitor.py:10` 模块 docstring 示例 `--only reconcile` 是非法 choice，照 runbook 抄进 crontab 会 argparse 报错退出。

---

## 四、已被推翻 / 存疑（对抗验证的价值展示）

以下 4 条初审认为是缺陷、但对抗验证**推翻或降级**——列出以说明验证严谨度，并保留其中真实的加固价值：

1. **embedding 共享缓存投毒（初判 P1）→ 推翻为理论性**。机制属实（命中零校验直标 DONE、守卫全失明），但触发路径被三事阻断：eval 脚本 main() 硬编码 `simulate_api=True`（零向量路径死代码）；eval 用 `raw_text` 取 key、生产用带前缀 `chunk_text`（md5 空间基本不相交）；实测本机 36MB 缓存 1537 条 dense 全配对 sp_、零污染。**残留价值**：命中时加零范数/sp_ 存在性校验（1 行）是廉价卫生债。
2. **VLM JSON 解析失败 fail-open 成 CLEAN（初判 P1）→ 推翻**。代码不对称属实，但唯一 `bypass_safety=False` 来源是 `_quarantine/` 路径，而 `node_scan_raw_files` 默认丢弃所有 `_quarantine/` 任务、`process_quarantine` 全仓无处置 True——是未接线的未来钩子。**残留价值**：一旦启用即变活,值得顺手对齐 fail-closed。
3. **无"生产却 simulate"防线（初判 P2）→ 推翻核心场景**。serving 检索栈完全不消费 simulate 标志（retriever/embedding/llm 无 mock 分支），且 DB POOL GUARD 在 simulate_db=True 命中生产 RDS 指纹时对每次连接抛 RuntimeError（响亮非静默）。**残留价值**：config 加载期不拒绝 production+simulate、`/api/ready` 在 simulate 下恒绿，是 P3 级加固缺口。
4. **spot_checker HA3 删除不校验响应状态（初判 P2）→ 推翻**。前提"SDK 4xx/5xx 返回响应对象而不 raise"为假：已安装的 `alibabacloud-ha3engine-vector` 1.1.18/1.1.19 对任何 4xx/5xx 抛 TeaException，`PushDocumentsResponse` 根本没有 `status_code` 属性——被引"对照代码"是死防御代码,真实失败走异常路径被正确 catch、行保持 PENDING_DELETE 下轮重试。

> **存疑（1 条）**：`pipeline_nodes.py:5224` `_search_delete_old_chunks` 一次性推全部旧 chunk 删除命令不按 100 上限切批（推送路径和 spot_checker 都切了）——降为 P3，视 HA3 单请求删除上限而定。

---

## 五、已知问题基线现状核实

| 基线问题 | 现状 | 证据 |
|---|---|---|
| #1 会话内存态 + `--workers 1` | **OPEN**（有意权衡） | Dockerfile 仍 `--workers 1` |
| #2 stage-1/2 乐观锁无年龄守卫 | **FIXED** | `_reset_stale_stage2_locks` 2h 接管 |
| #3 HA3↔RDS 无两阶段提交 | **MITIGATED** | PENDING_DELETE + reconcile 兜底 |
| #4 部分批次失败致双版本 | **MITIGATED** | parity-verify + deactivate 排序守卫 |
| #5 qa_session_log 明文 PII | **MITIGATED**（但 F-8 有旁路） | 查询侧掩码，content_blocks 未覆盖 |
| #6 session_id 可预测 IDOR | **FIXED** | `_prepare_ask`/`session_clear` 已加归属校验 |
| #7 DashScope classify 429 | **MITIGATED**（但 F-14 使重试失效） | `post_json_with_retry` |
| #8 阈值 7.7/5.8 与 fusion 耦合 | **OPEN**（文档化，换 RRF 即失效） | config.py 注释 |
| #9 卡片回调签名 Track1 待启用 | **PARTIAL** | apiSecret 占位 |
| #10 chunk 路由重分类翻家族 | **MITIGATED** | freeze + fail-close 守卫 |

---

## 六、性能优化机会（按 ROI 排序，共 97 条）

热路径本质是严格串行链（query-embed → HA3 混检 → [rerank] → RDS enrich → LLM → blocks → 日志），各阶段有真实数据依赖、**无"可并行的串行 await"大鱼**——延迟杠杆集中在连接复用、减少 RDS 检出、把簿记移出响应路径。RDS 查询本身质量高（stitch/step 扩展已显式消除 N+1）。以下按投入产出排序，工作量 S/M/L：

### 第一梯队（高杠杆 / 低成本 —— 建议优先做）
1. **[S] `qa_session_log` 加 `(answer_status, created_at)` 复合索引** — `schema/002:106`。5+ 看板/rollup/热问端点按时间窗全表扫，该表逐问答追加、无清理，**是唯一随上线时间单调变慢的服务侧查询族**；表增长到数十万行后看板从秒级降回毫秒级。
2. **[S] DashScope 调用改模块级 `requests.Session`** — `embedding_client.py:88`。每问答 2-3 次全新 TCP+TLS 握手（各 ~3 RTT），embedding/llm/rerank 三处；摄取全量重灌省 ~1800 次握手。改动 ~数行，可用已落库的 `retrieval_latency_ms/llm_latency_ms` 前后验证。
3. **[S] 给 `_resolve_user_dept_cached` 加 30-60s 进程内 TTL 缓存** — `api.py:323` / `dingtalk_identity.py:314`。`RAG_LIVE_ACL_REREAD` 默认开，每个带令牌请求多付 1 次阻塞 RDS 往返；RDS brownout 时逐请求卡 10-30s 拖住不依赖 RDS 的回答路径。撤销语义几乎不变（令牌 2h 兜底 + 跨部门另有实时拒绝）。
4. **[S] 调大 AnyIO 线程令牌到 100-200** — `api.py:875`。默认 40 令牌是单进程全局硬上限，~40 个不同用户同窗口提问即打满、之后 feedback/webhook/鉴权全排队。这些线程 99% 在等网络，代价仅内存。
5. **[S] qa 落库移出响应关键路径（BackgroundTasks）** — `api.py:674`。从用户可见延迟剔除掩码+落库耗时，RDS 抖动时 P99 不再被日志写入放大。
6. **[S] 看板聚合加 TTL 缓存** — `api.py:2038`。`kb_insights/governance` 每请求现算 4-14 条聚合子查询（含 JSON_TABLE JOIN），30 天窗口对分钟级 staleness 不敏感；与索引叠加后 DB 端看板负载基本归零。
7. **[S] query embedding 加小 LRU 缓存** — `retriever.py:1500`。重复/FAQ/示例问题快捷栏必重复流量省一次 DashScope 往返，同输入确定性风险为零，~10 行。

### 第二梯队（摄取吞吐 —— 重灌/日更批处理）
8. **[M] 状态回写逐 chunk UPDATE 改批量 upsert** — `pipeline_nodes.py:6419`。18k-chunk 全量重建从 ~18k 次串行往返降到 ~36 次（数十秒-分钟级 → 秒级），缩短长事务锁窗口。
9. **[M] HA3 pushDocuments 子批并行化（2-4 路）** — `pipeline_nodes.py:5999`。推送墙钟近似按并行度线性下降；**不触碰"先索引后 deactivate"不变量**（deactivate 在整个 push+verify 之后）。
10. **[M] `node_extract_text_with_ocr` 跨文档并发** — `pipeline_nodes.py:642`。stage-1 逐文档串行（OSS 下载+解析+OCR/VLM），而同管线 classify 已 8 线程并发；循环体除共享 tmp_dir 外无跨文档依赖。
11. **[M] 巨型 JSON embedding 缓存换 sqlite + OSS 镜像 + 并发上调** — `pipeline_nodes.py:5700`。每 run 加载/序列化 100-220MB JSON 的数十秒 + 2-3 倍峰值内存；OSS 镜像使生产重跑复用已算 embedding 直接省 DashScope 费。
12. **[S] `qa_rollup` 谓词改 sargable** — `qa_rollup.py:217`。`DATE(CONVERT_TZ(created_at,...))=%s` 函数包裹列使索引失效，改为常量侧范围条件（语义不变）。
13. **[M] 归因链物化为 `(message_id, doc_id)` 事实行** — `api.py:1974`。6 处热查询每次实时 `JSON_TABLE` 打散 `retrieved_docs_json` 再跨库 JOIN，无法用索引；物化后变普通索引 JOIN，同时消除 collation 陷阱依赖面。

### 第三梯队（前端流式 / 抽取重复解析 / 对账并行 —— 各有明确证据）
14. **[M] 前端流式吐字泵改增量渲染** — `useAsk.ts:180`。每 tick 全量 `stripImg + renderMd + innerHTML` 替换是 O(n²)，长答案办公低配机流式期 CPU/重排显著；配合去掉 persist deep watch（`useAsk.ts:540`）是打字卡顿两大来源。
15. **[M] 消除重复文件解析** — DOCX 被解析两次（`unified_extractor.py:498`）、xlsx 最多三次（`:625`）、PDF `_pages_needing_ocr` 算两遍（`:1740`）；图多 SOP 每文档省数百 ms~秒级，回灌线性放大。
16. **[S] OCR 整页渲染改 JPEG** — `ocr_client.py:273`。2x PNG 常 1-3MB，JPEG q78 约 200-400KB，base64 体积降 ~5x，上传超时概率同步下降（vlm_retry 记载 381KB 已触发过上传超时）。
17. **[M] 对账扫描并行化** — `reconcile.py:176` HA3 全量桶扫描逐桶串行、`spot_checker.py:400` 安全复审逐文档串行调 LLM（90s 超时）：均无跨项数据依赖，墙钟可除以并发度。
18. **[S] 批量 helper 复用连接/客户端** — `oss_url.py:240` 每 key 重建 Auth/Bucket、`pipeline_nodes.py:843` 每文档重建 OSS bucket + 3 连接：批量场景省大量连接借还。

> 其余 ~80 条（多为 S 级微优化：正则预编译、O(n²) token 估算、LIST 分页 max_keys、CI pip 缓存、测试并行化等）见 workflow 完整输出。

---

## 七、做得好的地方（工程亮点）

1. **关键不变量单一咽喉**：`_search_delete_old_chunks` 两处共用防漂移；DAG3"先索引成功后 deactivate"是真逻辑测试（拓扑序断言 + 失败注入 + SQL 级 COMMIT 顺序断言）而非 mock 剧场。
2. **复制代码用 parity 测试钉住**：`register_new_files` 内联 fallback vs `ingest_policy` 有 parity 测试；`answer_flow` 纯函数簿记单一来源。
3. **配置带评测出处**：阈值 7.7/5.8、weighted vs RRF、rerank +10.5pp 全部直接写进 config 注释并指向金集报告——决策可追溯。
4. **注入面基本无洞**：SQL 全参数化，f-string 只插 config 库名；HA3 filter 值经白名单 + fail-closed 归一化；令牌服务端 HMAC 签名、请求体 user_dept 显式忽略、typ 判别防冒充。
5. **一致性架构成熟**：`schema/009` 同事务 enqueue + UNIQUE(doc_id) 复活是教科书级 at-least-once 投影；reconcile/parity/spot_checker 三层全扫兜底。
6. **graceful degradation 贯彻**：拼接/step 扩展/问答日志 fail-open 降级、跨部门 ACL 复核 fail-closed 保守丢弃——辅助失败不破坏答案。
7. **测试资产扎实**：1627 个测试、几乎每个生产模块有对应文件；conftest 双层生产写防护门 + 12 个专门自测；历史 P1（eval 无 WHERE DML）已修。

---

## 八、建议处置顺序

**立即（本周，安全/数据丢失）**：F-1（PII 正则 `\b`）、F-5（匿名 user_id 注入）、F-7/F-8（PII 进日志/blocks）、F-6（webhook 签名+HTTPS）。多为小改动、直接堵漏。

**近期（本迭代，正确性）**：F-2（xlsx_layout 透传）、F-3（cosurface DESC）、F-4（前导双发，走冻结 re-chunk）、F-12（HA3 2xx 误判 + parity 常开）、F-13/F-14（并发吞异常/retry_count）、F-18（section_title 截断）、F-37/F-38（退役复活/双击竞态）。

**中期（结构债 + 治理）**：F-A1/F-A2（抽 `db.py`/`clients.py`/`pii_patterns.py` 小模块 + api.py 按 APIRouter 拆分，均用 re-export 垫片零风险机械搬移）、F-36（日志表留存策略）、F-35（schema/010 补列 + DDL 单一权威 + `schema_migrations` 台账）、性能第一梯队 7 项。

**顺手（低优先级卫生债）**：4 条被推翻发现的残留加固（缓存校验、fail-closed 对齐、simulate-in-prod 拒绝）、死表标 DEPRECATED、死代码清理。

---

*本报告由 17 个并行审查员 + 对抗验证工作流生成（87 个 subagent、~850 万 token、1743 次工具调用）。所有发现均带 file:line 与逐字代码证据并经二次验证；性能收益依据从代码结构推导，未使用未经验证的数字。*
