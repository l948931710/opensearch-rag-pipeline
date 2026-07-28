# 摄取管线优化审计（2026-07-25，DRAFT）

## 方法与可信度（阅读前必读）

- **产出方式**：六维并行扫描（调度与编排 / 吞吐与延迟 / 成本 / 可靠性与数据完整性 / 产出质量 /
  入口与可观测性），每维最多 7 条；每维发现再交给一名**默认立场为"推翻"**的复核员，逐条打开引用行号核实。
- **计分**：41 条进入终审 —— CONFIRMED 17 / PARTIAL 24 / **REFUTED 0 / ALREADY_FIXED 0 / BY_DESIGN 0**。
  校正后定级：P1 × 1、P2 × 22、P3 × 18。
- **可信度提示**：零推翻本身是一个**软告警**——本仓历史外审的推翻/降级率从来不是 0。
  复核层确实起了作用（24 条被降级为 PARTIAL，多条的"影响"被显著收窄、建议被替换、
  并明确否决了若干看似顺手的改法），但**零 REFUTED 意味着不能把这份清单当作已去风险的结论**。
  任何一条在动手前仍需按仓库规矩走 `codex-review`，并自行复核引用行号。
- **不在本清单内的四条既有事实**（本轮已单独查实，作为背景喂给了各 agent，不重复上报）：
  1. DataWorks 上 stage1/2/3 三节点 `Trigger.Recurrence=Manual`，无 cron；07-21~07-24 实查无任何 stage 实例。
  2. `dataworks_nodes/register_new_files.py` 与 `scan_oss_sync_keys.py` 均 `DRY_RUN=True`（空转）。
  3. `ops_health_monitor` 每日 exit 2，但未配 `RAG_OPS_ALERT_WEBHOOK`，告警被吞。
  4. console 上传后 RDS 行同步写入（doc_id/raw_key/etag/size 齐全），状态 `NOT_STARTED`，等人工批。
- **本文只是审计结论，未做任何代码改动，未触碰生产。**

---

# 摄取管线优化清单（main 工作树，2026-07-24）

## 一、总体判断

这条管线是**按「有人值守的日调度」设计、却按「人工批」运行**的，所有自愈与补偿机制都挂错了地方：五个修复器（stranded / pending-delete / orphan-PK / ACL outbox / allowed_depts）全部只挂在手工 `stage-3` 里（`dataworks_orchestrator.py:707-770`），而真正每天在跑的 `ops_health_monitor` 只做检测不做修复，且 `--only reconcile_ha3 reconcile_oss` 把两个摄取积压探针排除在外（`dataworks_nodes/ops_health_monitor_node.py:203`）。

更严重的是**运行本身的成功信号不可信**：stage-2 批次级崩溃不回滚认领行，而 `_count_pending_rows(2)` 的谓词不认 LOADING/PROCESSING（`dataworks_orchestrator.py:649-656`），于是下一次点 stage-2 会打印「drained: 0 pending rows」并 exit 0——一边报绿一边有 100 篇楔死；三次崩溃后 `retry_count>=3`，这批行同时从认领谓词和计数谓词消失，**永久绿灯 + 永不入库**。这是本轮唯一能把「新文档永不入索引」和「运行报成功」同时做到的路径。

第三条主线是**已经写好的性能与缓存基础设施在生产从未被接通**：`RAG_EXTRACT_CONCURRENCY` / `RAG_LOADER_FETCH_CONCURRENCY` / `RAG_PUBLISH_CONCURRENCY` 三个旋钮的并发骨架都在代码里，`/usr/bin/grep` 在 `dataworks_nodes/` 与 `deploy/` 下**零命中**；OCR 页缓存 `OSS_MIRROR_KEY = None`（`extraction/ocr_client.py:333`），在每次全新 pod 的 DataWorks 上跨运行命中率恒 0。这一档改动几乎都是「加一行 env / 补一个已有模式」。

结论：**先把「运行结果可信 + 积压可见」这两件事补齐，再谈挂调度**；挂调度这个动作本身是前置依赖最重的一步（无单实例互斥、retry 语义未修之前不能挂）。

---

## 二、档 A：立刻做（S 工作量、低风险、收益明确）

### A1. 三个并发旋钮在生产从未注入，最贵的一段是纯串行 —— 【配置/运维】
- **结论**：文档级抽取并发、stage-2 OSS 预取并发、rag-ready 发布并发的代码路径全部就绪，但没人开。
- **证据**：`opensearch_pipeline/pipeline_nodes.py:379` 默认 `"1"`、线程池分支在 `:478-491`；`dataworks_orchestrator.py:46-50,266` 的 `RAG_LOADER_FETCH_CONCURRENCY`；`pipeline_nodes.py:5383` 的 `RAG_PUBLISH_CONCURRENCY` 默认 `"1"`、并发骨架 `:5386-5393`。`/usr/bin/grep -rn` 三个变量在 `dataworks_nodes/` 与 `deploy/` 下零命中（本轮实跑确认）。对照组：`RAG_CLASSIFY_CONCURRENCY` 默认 8（`:1784`）、OCR 页级默认 4（`extraction/ocr_client.py:366`）。
- **影响**：stage-1 墙钟 = Σ 每篇（OSS 下载 + 解析 + OCR + VLM），是操作者实际等待时间的主体。加速倍数未量化。
- **改法**：先在 DataWorks 调度 env 里配（`setdefault` 语义允许 env 覆盖，不动代码包、不用重贴节点）：`RAG_EXTRACT_CONCURRENCY=4`、`RAG_LOADER_FETCH_CONCURRENCY=4`、`RAG_PUBLISH_CONCURRENCY=4`。量稳后再写回 `dataworks_nodes/stage1_node.py:40-41` 旁固化。
- **工作量**：S（配置侧为零代码）。
- **风险与验证**：唯一真实风险是 DashScope 429——4（文档级）× 4（`ocr_client.py:366` 页级）= 16 路 OCR 并发，叠加 VLM 漏斗 8 路打在同一账号（`ocr_client.py:360-364` 的注释明确警告该账号的 429 脆弱性），所以取 4 不取 8。验证：staging 跑 20 篇，记墙钟 + 429 计数。**不需要**同步调 `RAG_DB_POOL_MAX`——`node_build_canonical` 是纯串行 for 循环（`:593/616`），`_doc_conn` 每篇惰性取一条 finally 归还（`:801/997/1059-1062`），与抽取并发无重叠。

### A2. OSS 取件失败被吞成 print，产 0 字 canonical 却写 `extraction_status='COMPLETED'` —— 【代码】
- **结论**：一次 OSS 瞬断把健康文档变成不可自愈的死件，且状态列字面谎报 COMPLETED。
- **证据**：`pipeline_nodes.py:418-423` except 只 `print` + `task["local_path"]=""`，无任何标记；**同一函数**的 oversize 分支 `:406-412` 却会写 `oversize_note → partial_loss_notes → NEEDS_REVIEW`——处置不对称。ENV-DEP 守卫 `:694-700` 只匹配 `'No module named'/'not installed'`，取件失败文案（`extraction/pdf_extractor.py:930`、`extraction/unified_extractor.py:1690`）不命中；定稿路径 `:1012-1040` 无条件写 `canonical_json_key` + `extraction_status='COMPLETED'`。stage-1 认领谓词要求 `canonical_json_key IS NULL`（`:116-119`，`dataworks_orchestrator.py:644-651` 同款）→ 再不重捡。
- **影响**：单篇需人工改库把 canonical key 置回 NULL 才能重抽；重试预算全烧在 stage-2 重读同一份空 canonical 上。不静默（`:5715-5745` 会判 suspected failure 落 `NEEDS_REVIEW` + `content_process_error`，控制台徽章「处理失败」），但不可自愈。
- **改法**：`:421-423` 打显式标记 `task["fetch_error"]=str(e)`（**用异常本身作判据，不做 warning 文案匹配**，避免误伤真空文档）；`node_build_canonical` 见标记即 `continue`，不写 canonical 文件/keys，只 `UPDATE extraction_status='FAILED' + content_process_error='[OSS-FETCH] …' + retry_count=retry_count+1`；`retry_count<3` 时 `content_process_status` 保持 `NOT_STARTED`（下轮 stage-1 自动重捡自愈），`>=3` 置 `FAILED` 落人工队列。循环末按本轮失败数并入 `env_dep_failures` 一起 raise。
- **工作量**：S。
- **风险与验证**：sim 下 monkeypatch `bucket.get_object_to_file` 抛异常，断言 `canonical_json_key` 仍 NULL、`extraction_status='FAILED'`、下次 scan 能重捡。存量空壳可在 `:5701-5745` 分支里对 reasons 含 `[DOWNLOAD_FAILED]` 的行顺手把 canonical keys 置 NULL，免写一次性脚本。

### A3. 封堵 stage-2 假绿：drain 收尾必须报出 LOADING/PROCESSING —— 【代码】
- **结论**：`_count_pending_rows(2)` 看不见被楔住的行，于是「0 pending = 全清 = exit 0」。
- **证据**：`dataworks_orchestrator.py:649-656` 的 stage-2 计数谓词只认 `NOT_STARTED` / `FAILED&retry<3`；`:787-790` 据此打印「drained: 0 pending rows」并正常退出；`_reset_stale_stage2_locks` 只在 `stage==2` 的 drain 循环里调且要求 `updated_at < NOW()-2h`（`:786`、`:595-625`）。
- **影响**：楔死的批次在下一次运行里表现为绿灯。三次批次级崩溃后 `retry_count>=3`，认领谓词（`:220`）与计数谓词（`:652`）同时失效 → 永久绿灯 + 永不入库。
- **改法**：drain 结束时额外查一次 `content_process_status IN ('LOADING','PROCESSING')` 的行数并打印「另有 N 行处于 LOADING/PROCESSING（不可认领）」，N>0 时不允许打印「全清」字样。**只做可见性，不动认领谓词**（认领语义的修复见 B1）。同时加一条死信探针 `FAILED AND retry_count>=3`。
- **工作量**：S。
- **风险与验证**：纯只读 SELECT + 打印，无行为变化。验证：sim 下人为把若干行置 LOADING，跑 drain 断言输出含该行且退出码语义未变。

### A4. `index_retry_count` 成功从不清零，且瞬态读失败（PARITY_UNKNOWN）也烧预算 —— 【代码】
- **结论**：健康 chunk 累计 3 次（含瞬态读失败）就转 DEAD，旧版本停用被永久推迟。
- **证据**：`pipeline_nodes.py:7658-7661` 无条件 `+1` 且 `IF(index_retry_count >= %s,'DEAD','FAILED')`；两条成功路径 UPDATE（`:7500-7530`、`:7532-7562`）12 个 SET 字段中无复位；全仓 grep `index_retry_count` 只有这一处写。UNKNOWN 入桶：`:7723-7730` 三桶同等调 `_fail_chunks_with_retry_budget`，而 `_point_read_one`（`:7882-7896`）对任何异常返回 `'unknown'`。后果链：`reindex_states.py:98-100` 重选集不含 DEAD；deactivate 完整性闸 `:6190-6200` 会永久推迟。
- **影响**：双版本长存 + 只能人工复位（`_mark_docs_needs_review_for_dead` `:7690-7704` 是显式死信队列，不是静默）。
- **改法**：(1) 两条成功路径 UPDATE 加 `index_retry_count = 0`；(2) `_persist_parity_failed_and_raise` 里 unknown 桶走一条不带 `+1` 的 FAILED 回写，死信资格只留给 DROP/DRIFT。顺带删掉 `:7809-7813` 那段「无 per-chunk retry 上限」的过期注释（G9 已实现）。
- **工作量**：S。
- **风险与验证**：**前置**——这两条成功路径**没有** 1054 回退分支（不像 `:7672-7680`），若 `schema/019` 未 apply 会直接炸掉成功路径，必须先确认列存在或同步补 try/except。`tests/test_tier2_pipeline_fixes.py:123-124` 只覆盖失败 SQL，不受 (1) 影响；(2) 需补新用例。

### A5. OCR 页缓存在生产恒为冷缓存（无 OSS 镜像 + 相对路径 + 无容量上限）—— 【代码】
- **结论**：三个持久缓存里只有 OCR 页缓存没接 OSS 镜像，DataWorks 每次新 pod 命中率归零。
- **证据**：`extraction/ocr_client.py:333` `OSS_MIRROR_KEY = None  # 本地缓存即可；OSS 镜像留给后续需要时开启`（本轮逐字确认）、`:336` 相对路径 `os.path.join("scratch","ocr_page_cache.sqlite3")`、全文件只有 `:372-380` get_many 与 `:401` put_many，**无 finalize/evict_to**；对照 `vlm_cache.py:52` 与 `embedding_cache.py:54/57-75` 都有镜像键。`deploy/build_dataworks_zip.sh:46-47` 只 `git archive HEAD opensearch_pipeline`，`scratch/` 不随包走。立项理由写在 `ocr_client.py:314-315`，未兑现。
- **影响**：stage-1 失败重跑 / 主动全量重抽时，扫描件页级 OCR 全额重付。注意**维护性 re-chunk 不受影响**（`reindex_states.py:95` 是 KEEP-CANONICAL 重置，stage-1 谓词要求 `canonical_json_key IS NULL`，根本不重跑 stage-1）。
- **改法**：照抄既有模式：① 补 `OSS_MIRROR_KEY = "processing/cache/ocr_page_cache.{model}.sqlite3"`（按模型名命名空间化，仿 `embedding_cache.py:57-72`）+ `MIRROR_ENV="RAG_OCR_PAGE_CACHE_OSS_MIRROR"`；② 路径换成与另两个缓存一致的绝对路径；③ **必须**新增一个 flush 调用点——`_push_oss_mirror` 只从 `finalize` 走（`embedding_cache.py:229-232`），而全仓 `finalize(` 调用点只有 `pipeline_nodes.py:6754/6818`（embedding），OCR store 今天没有任何调用者；在 `pipeline_nodes.py:507-512` 紧挨 `flush_vlm_cache_to_oss()` 处加一次并传容量上限 `RAG_OCR_PAGE_CACHE_MAX_ENTRIES`。
- **工作量**：S。
- **风险与验证**：加镜像后坏条目会跨运行传播，必须守住两条既有约定不变：空文本不入缓存（`:399-401`）、200 但响应体不可解析要 raise 而非写空缓存（`:490-494`）。验证：用 `:379-381` 的 `[ocr-cache]` 命中日志确认镜像真被拉到。

### A6. stage-3 每次开工前全量扫 HA3 id 空间，而默认 dry-run 一行都不删 —— 【代码】
- **结论**：每次 stage-3 运行在 drain 循环前多做一遍全 id 空间枚举，默认配置下只产出一行统计。
- **证据**：`dataworks_orchestrator.py:714-744` 的 `if stage == 3:` 前置块在 drain 循环（`:773` 起）之前；`:736-737` `_orphan_purge = os.environ.get("RAG_STAGE3_ORPHAN_PURGE","")...` / `reconcile_ha3_orphan_pks(dry_run=not _orphan_purge)`（本轮逐字确认）。`ha3_reconcile.py:170-176` 先全量 fetch active chunk_meta，`:189-191` `_id_hi = max_id + _ID_SCAN_HEADROOM`、`_enumerate_ha3_pks` 不传 `id_lo`（全扫），`:211-216` dry_run 提前 return。桶大小/headroom 常量在 `:32-33`。日检侧 `ops_monitor` 的 `reconcile_ha3`（`reconcile.py:488-505`，docstring `:547-549`）已承担同一方向。
- **影响**：纯墙钟（HA3 是包实例，query 不按次计费），与本次工作量无关且只增不减。
- **改法**：在 `dataworks_orchestrator.py:734-744` 外层加 `if _orphan_purge:` 包裹。**不要**改在 `reconcile_ha3_orphan_pks` 内部提前 return——那会破坏 standalone CLI（`__main__` 默认就是 dry-run，是该工具的正常用法）与 `spot_checker.py:689` 的调用路径。
- **工作量**：S。
- **风险与验证**：失去 stage-3 内的 dry-run 报数，需一并把 orphan 报数接进日常巡检（见 C2）。验证：sim 跑 stage-3，断言未设 flag 时不再调用 reconcile。

### A7. DEACTIVATE 审计是逐条旧 chunk 一次连接 + INSERT + COMMIT 的 N+1 —— 【代码】
- **结论**：版本升级重灌的 stage-3 收尾按**旧 chunk 条数**而非文档数付 RDS 往返。
- **证据**：`pipeline_nodes.py:6295-6297` 在 `for r in rows` 里 append，rows 来自 `:6281-6288` 的 `SELECT doc_id, version_no, id FROM chunk_meta WHERE (OR-链) AND is_active=1`（长度 = 旧 chunk 条数）；`:6524-6537` 逐条 `write_audit(...)`（本轮确认）；`audit_log.py:96-107` 的 `cursor=None` 分支每次 `_get_db_conn` → execute → `conn.commit()` → `conn.close()`；`db.py:51-55` 每次构造 `GuardedDBConnection` 并 `_begin_txn`（`:78-86` 真发 `conn.begin()`），池 ping=1 在 `:247`。
- **影响**：万级旧 chunk 即万级提交（ping + BEGIN + INSERT + COMMIT 四个 round-trip 级操作；`conn.close()` 是归还池不是真断连）。不影响正确性（write_audit fail-open）。
- **改法**：`audit_log` 加 `write_audit_many` 用 `executemany` + 单次 commit，整体 try/except 保持吞异常。`:7605-7611`（per-doc,version）与 `:311-314`（per-task）并入是顺手，不是瓶颈。
- **工作量**：S。
- **风险与验证**：零语义变化。**不要**顺手改行粒度（每 (doc_id, old_version) 一行）——`kb_audit_log` 是 append-only 取证表，改粒度前需确认无看板/取证脚本按 per-chunk 查。

### A8. OSS payload 归档失败被当致命错误 raise，把已成功的 HA3 推送判成整批失败 —— 【代码】
- **结论**：与本仓「辅助功能失败绝不打断主流程」的既定约定直接冲突。
- **证据**：`pipeline_nodes.py:7347-7364`（真实 OSS：copy_object `:7355` + delete_object `:7357`）except 里 `raise RuntimeError('Failed to archive OSS payload object during archive')`，模拟分支 `:7334-7346` 同款；发生在 HA3 push 之后、`node_update_index_status` 之前；TODO 注释自认应落 `opensearch_bulk_job` 的 archive 列。
- **影响**：一次归档抖动让整轮 stage-3 白跑并回滚（可自愈重跑，embedding 走缓存基本全命中）；若归档失败是确定性的（权限/生命周期规则），stage-3 对全语料停摆直到人工修 OSS。
- **改法**：两个归档段的 raise 改成只 print + `ctx.setdefault('validation_warnings',[])` 记一条，保留 `batch['oss_key']` 原值继续走 04/04b/05。落库那半（archive_status/archive_error 列）先不做——加列要走 `schema/` + ledger，不该卡住修复。
- **工作量**：S。
- **风险与验证**：sim 下 monkeypatch copy_object 抛异常，断言 stage-3 仍走完 04/05 且 index_status 正常。

### A9. HA3 per-doc 错误项归因缺下界，`err_idx=-1` 会误标本批最后一个 chunk —— 【代码】
- **结论**：一个真 bug（判据只有上界）+ 一个盲点（响应形态从未被采样）。
- **证据**：`pipeline_nodes.py:7104-7114` 判据是 `err_idx is not None and err_idx < len(sub_chunks)` → `-1 < len` 为真 → `sub_chunks[-1]` 被误标 FAILED；`:7115-7120` 其余一律置 INDEXED；`:7122-7127` body 解析失败时整个 sub-batch 标 INDEXED（注释自认保守）。
- **影响**：错误被归到完全无关的 chunk。**不构成静默丢失**——`node_verify_and_repush` 默认常开（`:7823` `os.environ.get('RAG_STAGE3_PARITY_VERIFY','true')`，`dataworks_nodes/stage3_node.py:70` 是 `setdefault('true')`），逐 PK point-read 会抓成 PARITY_DROP 并阻断 deactivate。
- **改法**：(1) 边界修正 `isinstance(err_idx,int) and 0 <= err_idx < len(sub_chunks)`；(2) errors_list 非空但存在不可归因条目、以及 body 解析失败两种情况，把原始 body 片段打印并写进 `ctx['validation_warnings']`——**先拿到一次真实生产批的响应形态**。
- **工作量**：S。
- **风险与验证**：**不要**在采样确认前把不可归因错误升级为 sub-batch fail-closed，那会把正常批判死并触发全量重推。

### A10. 摄取分类器不钉 `enable_thinking`，失败模式是文档静默进隔离 —— 【代码】
- **结论**：全仓其余五个小判别调用点都钉死了，只有摄取分类没钉。
- **证据**：`pipeline_nodes.py:1374-1380` 的 payload 只有 model/messages/temperature（本轮逐行确认），无 `enable_thinking`、无 `max_tokens`，`:1386` timeout=90 是唯一输出长度约束。对照 `intent_router.py:325`、`query_rewriter.py:153`、`query_decomposer.py:94`、`general_answerer.py:231` 全部写死 `enable_thinking: False`。
- **影响**：一旦供应商把该模型的 thinking 默认翻成开，content 被 reasoning 挤空 → JSON 解析失败 → `:1910-1912` 捕获 → `:1918-1931` fail-safe 把文档打成 `permission_level='restricted'` / `redaction_action='QUARANTINE'` / `PENDING_AUDIT` / `llm_risk_level='high'` 并插 review_task。**这是可靠性加固，成本只是副产品。**
- **改法**：payload 补 `"enable_thinking": False`。`max_tokens` 若要加请设 **≥1024 而非 512**——太紧的失败模式不是截断答案而是走同一条隔离链路。
- **工作量**：S。
- **风险与验证**：sim 下跑 normal / version_update 两个场景 + 一批真实文档对照分类结果。**不要**在这一期顺手换便宜模型（见 B17）。

### A11. 图片漏斗把「解码失败」记成「装饰图」，且丢弃零留痕 —— 【代码】
- **结论**：归因错误 + 无持久信号，运维看日志会当噪声跳过。
- **证据**：`image_funnel_processor.py:191-200` `_static_heuristics` 用 `Image.open` 取尺寸，`except Exception` 一律 `return 0,0,0.0` → 必然命中 `:67` 的 `width<50 or height<50 ...` → `DISCARD_DECORATIVE`；`extraction/unified_extractor.py:1794-1811` 的 Phase-1 预过滤是**同一判据的拷贝**，命中只 print + `discard_count += 1`，不进 warnings 也不进 partial_loss_notes（且因为 `_static_heuristics` 内部已吞异常，`:1806` 的外层 except 永远看不到解码失败）。
- **影响**：若现网确有 PIL 读不出的编码，对应文档的 step_card / 钉钉卡片彻底无图而无人知晓。**「确有」这一前提目前无证据**——未量化。
- **改法**：① `_static_heuristics` 解码异常返回显式哨兵（如 `(-1,-1,-1.0)`），漏斗侧落 `DISCARD_UNREADABLE` + reason 带异常类型与扩展名；② 把 `:1794-1811` 的重复判据抽成共享 helper（**消除两份拷贝**，顺带消除阈值漂移风险），unreadable 计数照 `_warn_skipped_vector_images`（`unified_extractor.py:181-192`）同型写进 `result.warnings` 并计入 partial_loss_notes。
- **工作量**：S。
- **风险与验证**：**第三条（fitz.Pixmap 转 PNG 兜底）暂缓**——先靠 ①② 跑一轮拿到现网 unreadable 张数与扩展名分布；确认非零量级才做，否则是拿家族变动 + VLM/OCR 付费面换一个假想问题。注意 `aspect_ratio > 8.0` 误杀宽幅截图是**有意的启发式**（`RAG_PDF_STRIP_STITCH` 是配套救援），不要混为一谈。

### A12. 全仓无单实例互斥，重叠运行重复付 OCR/VLM —— 【代码】
- **结论**：`/usr/bin/grep -rn 'GET_LOCK|advisory lock|flock|fcntl' opensearch_pipeline/ dataworks_nodes/` 本轮实跑**零命中**。
- **证据**：`pipeline_nodes.py:107-124` 为纯 SELECT（无 `FOR UPDATE OF dv SKIP LOCKED`，对照 stage-2 在 `dataworks_orchestrator.py:202-243` 有）；`:683-684` 的 docstring 自认「Balanced 级别未加 Stage-1 原子抢占，因此该守卫是必需的」；`pipeline_run.run_start`（`:26-55`）INSERT 失败只 `logger.warning` 返 None，构不成互斥。
- **影响**：重复付 OCR/VLM。**不是数据损坏**——canonical 键确定性（`:674-677`）、`node_register_metadata` 幂等（`:259-268` ON DUPLICATE KEY）、`:1022-1040` 同列同值 UPDATE；后果是多花一份钱 + canonical 措辞可能取到另一实例的版本。今天人工批、单操作者，概率低；**一旦挂 cron 与人工批并存立刻升级**。
- **改法**：在 `main()` 取一条**独立**连接（不走 `db.py` 的 DBUtils 池，否则连接归还即释放会话锁）执行 `SELECT GET_LOCK('rag_ingest_stage{N}', 0)`，拿不到就打印并 exit 0，finally 显式 `RELEASE_LOCK`。三个 stage 同样有效。
- **工作量**：S。
- **风险与验证**：**明确否决**给 stage-1 加 LOADING 认领——它同时踩两个既有不变量：`:338-345` 把「stage-1 认领不写任何状态」当作「装好依赖后重跑即全量自愈」的前提；加了状态必须同步改 `_count_pending_rows(1)`（`dataworks_orchestrator.py:642-648`）与陈旧锁接管（今天 `:786` 只在 `stage==2` 调），任一漏改即命中 `ingest_policy.py:56-60` 的「计得到、领不走 → 无进展守卫永久判死」陷阱。

### A13. 独立图片文档完全绕过跨文档 VLM 缓存 —— 【代码】
- **结论**：缓存基础设施已建好，这条路径忘了接。
- **证据**：`extraction/unified_extractor.py:2085-2088` 直接 `processor.process_image`，前后无 `_load_vlm_cache()` / `_vlm_cache_lookup` / 回写；对照 `_process_embedded_images` 的 `:1820` 加载、`:1839` 查缓存、`:1948-1961` 回写。可达性已确认：`:862-863` 把 png/jpg/jpeg/webp/tif/tiff/gif/bmp 路由到 `_extract_image`，这些扩展名在 `ingest_policy.py:52-56` 白名单内且不在 `:64` 的 stage-1 SQL 排除集。
- **影响**：每次首灌/升版/重试重付 1 OCR + 1 VLM（金额小）。**真正的理由是确定性**：visual_summary 每次重掷正是 xlsx 绑定漂移那场战役的根因。
- **改法**：抽公共小函数 `_funnel_one_image(local_path, is_public, task)`，让两处共用「算 sha256 → `_vlm_cache_lookup`（含 `:1843-1852` 的 ocr_text 反幻觉复洗）→ miss 才 process_image → 按 `:1948-1950` 的条件与键形回写」。`degraded` 不入缓存的约定必须原样照抄（否则一次超时被固化成该图永久标签）。
- **工作量**：S。
- **风险与验证**：回归点——`assets[0]` 的 `oss_key=raw_key`（`:2102-2103` 注释明确说构造 `processing/assets/` 路径对 ROUTE_TO_TEXT 是 403 死图）与 `:2139-2145` 补的 image_ref 块必须逐字段不变。排在 A5 之后（A5 金额面大得多）。

---

## 三、档 B：值得排期（M/L 工作量，收益明确）

### B1. stage-2 批次级失败不回滚认领行 + `retry_count` 语义错位（本轮唯一 P1 的实质修复）—— 【代码】
- **结论**：健康文档三次后静默出队，且陈旧扫无差别 `retry_count+1`。
- **证据**：`dataworks_orchestrator.py:368-370` 失败分支无回滚（对照 stage-3 的 `:557-583` 有逐条回滚）；认领在 `:225/236-243`；`pipeline_nodes.py:5781-5789` 成功收口不重置 `retry_count`；全仓仅 `reindex_states.py:102` 与 `routes/contribution.py:536` 会置零。
- **影响**：见总体判断。注意**已有局部缓解**：读 canonical 失败的单篇逐条置 FAILED + continue（`:301-314`）不牵连同批；crash-resume 由 `_partition_prior_rechunk`（`pipeline_nodes.py:1528-1608`）auto-freeze 续跑，其 docstring `:1543-1544` 正是记录同族事故的既有修复。
- **改法**：① 回滚**按本批认领集合**而非状态名——把 `claimed_ids` 提到 try 外层，失败分支比照 stage-3 逐条 `UPDATE ... SET content_process_status='NOT_STARTED' WHERE id IN (...) AND content_process_status IN ('LOADING','PROCESSING')`，**不动 retry_count**；② `pipeline_nodes.py:5781-5789` 成功收口加 `retry_count=0`，让它变成「连续失败次数」——**必须与①同批上**，否则确定性坏文档无限重处理。
- **工作量**：M。
- **风险与验证**：**不要**按状态名区分「LOADING 只复位 / PROCESSING 才加 retry」——DAG-2 第一个节点 `node_classify_and_risk_assess` 在 `pipeline_nodes.py:1720-1725` 就把整批 LOADING→PROCESSING，该判据在真实路径上几乎恒不生效。正确判据是「本批是否整体中止」。验证：sim 下在 DAG-2 中段注入异常，断言全批回到 NOT_STARTED 且 retry_count 未变。

### B2. stage-1 全批抽取 → 逐篇定稿：第 k 篇定稿失败丢弃 k+1..N 已付费的 OCR/VLM —— 【代码】
- **证据**：`pipeline_nodes.py:490-494` 无 per-doc try（`:491` 注释自述「单文档异常冒泡 fail 整节点」）；`:988` 与 `:1047` 逐篇 raise；stage-1 只有 ENV-DEP（`:704-720`）与 COST-DEFER（`:737-753`）写 FAILED。
- **影响**：崩在**首篇**才是整批白花（尾部失败时前面的 canonical 已逐篇落 OSS+RDS）。不含数据丢失。
- **改法**：**只做逐篇隔离这一半**——`node_build_canonical` 循环体（`:802-1062` 的 try 已存在，把 raise 换成收集）与 `_extract_one` 调用点（`:493-494`）各包 try/except → 单篇记 `extraction_status='FAILED' + content_process_error`（复用 `:709-713` 写法）→ continue，循环后按 ENV-DEP 同款（`:1069-1089`）统一 raise 保持运行变红。
- **工作量**：M。
- **风险与验证**：**明确不做**给扫描谓词加 `retry_count<3`——stage-1 全链路今天没有任何一处给 `retry_count` 自增，加谓词就是纯减法，且会命中 `ingest_policy.py:56-60` 的永久判死陷阱。**也不做**「抽完一篇立刻定稿」（`_upload_clean_assets` 在 node 03 的 finally 整批上传，见 `:495-505`，收益与逐篇隔离重叠）。

### B3. xlsx 步骤卡图片绑定：作者显式「如图N」被位置计数器 图N→步骤N 抢先覆盖 —— 【代码】
- **结论**：本批最扎实的一条，代码与其**自身注释**直接矛盾。
- **证据**：`pipeline_nodes.py:4471-4476` 的数字直连排在 `:4478-4483` 的 `figure_refs` 精确匹配**之前**（本轮逐行确认）；`:4369-4372` 的 `figure_no_meaningful` 是**文档级**布尔；`:4349-4359` 的注释白纸黑字写着信任条件只有 (a) 某 figure_no 被多 asset 共用、(b) 步骤文本显式引用该 figure_no，且明确说「(b) 命中后的绑定走 figure_refs 分支」——实现让 (b) 同时打开了数字直连并让它抢先。figure_no 是位置计数器（`unified_extractor.py:1444-1456` 按 filename 排序后 `fig_counter` 递增）。
- **影响**：两类确定性错绑（步骤6 写「如图3」而步骤3 空着 → 图绑到步骤3；任意一句「如图2」把全篇无人引用的图按序号硬绑）。经 `step_card.extra.image_refs → chunk_meta.image_refs_json` 直达钉钉卡片，一线员工看到错图。**削弱条件**：优先级 0 的证据匹配（`:4419-4460`）先跑，强匹配的图已被绑走，所以是「P0 信号弱且同号步骤恰好空着」时才翻。
- **改法**：① 交换优先级（把 `:4478-4483` 移到 `:4471` 之前）；② `figure_no_meaningful` 改成 **per-figure** 判定（只有 `fno_counts[fno]>1` 或 `fno in all_step_fig_refs` 才允许该 fno 走数字直连）——这一条把注释原意如实落到代码上，改动最小，**可以先单独上**；③ figure_refs 命中时即便步骤已在 `bound_nos` 也追加为第二张图。
- **工作量**：M（含存量生效）。
- **风险与验证**：`eval_harness` l4 xlsx jaccard 硬线 ≥0.891665、docx ≥0.9891（`baseline.json:62-63`），跑前先确认 `run_eval.py:145/150` 的 `vlm_model`/`l4_gt_sha` 哨兵未漂移否则分数不可比。**存量生效需冻结重灌**（`RAG_MAINTENANCE_ROUTING` + manifest 预编码 count/type_mix）——这半属 C 档决策。

### B4. 超长 `table_chunk` 被整块丢弃，且无行级切分 —— 【代码】
- **证据**：五处 table_chunk 创建点（`chunker.py:814/1590/1746/2301/2479`）全无长度判断；`pipeline_nodes.py:5117-5118` `token_count > 2000` 判无效、`:5109` `_STRUCTURAL` 含 table_chunk、`:5129-5133` 只打一条日志；`chunker.py:304-306` 中文 1.5 字/token ⇒ ~3000 中文字符触顶；对比 step_card 有 `:1196-1222` 拆分、procedure_parent 有 `_PARENT_MAX_TOKENS=1800`（`:1364/2862`）——表格是唯一没有上限的类型。
- **影响**：暴露面比想象窄——`chunker.py:2048` 的 `_is_prose_table` 已把「整本 SOP 塞进一张表」救走，剩下的是 `_is_prose_table` 有意排除的**真数据表**（`:341-354`）。表现为「问规格参数查不到」。频次未量化。
- **改法**：**先做纵深防线（S、零家族变动）**：`pipeline_nodes.py:5117` 命中 `too_many_tokens` 且 `chunk_type=='table_chunk'` 时往 ctx 落一条**按 doc 归因**的记录并接进 partial_loss_notes/NEEDS_REVIEW 通道，先拿到现网真实计数。确认值得后再做行级切分：按 markdown 行分片、每片重复**第一行**（`extraction/docx_extractor.py:561` 产出的是 `"\n".join(f"| {row} |")`，**没有分隔行**，所以「重复表头 + 分隔行」对 DOCX 不成立；PDF 侧先用 `_md_table_cols` 判断），写 `extra.table_part_no/total`。
- **工作量**：留痕 S / 切分 M。
- **风险与验证**：切分属家族变动，必须走冻结重切 + manifest，并跑 L6 与 GT eval 确认表头重复不劣化 BM25。

### B5. 三处页级上限静默截断内容，均未接入 `partial_loss_notes` —— 【代码】
- **证据**：`extraction/ocr_client.py:260-264` `page_idxs = page_idxs[:self.max_ocr_pages]` + 一条 stdout（本轮逐字确认）；`unified_extractor.py:2399-2406` 的 partial_loss_notes 只从 `ocr_result.pages` 的 FAILED 派生，被砍页不产生 OCRPageResult；`extraction/image_extraction_utils.py:333` `range(min(len(pdf), max_pages))`；`unified_extractor.py:981-988` 原生文本截断只 append 一条 `[TRUNCATED]`（该条**已持久化**——`pipeline_nodes.py:633` 把 warnings 写进 canonical JSON，`:5715` 还会读回，只是只在 0-chunk 分支消费）；`pipeline_nodes.py:5825-5828` 的 NEEDS_REVIEW 通道只认 vlm_degraded_count 与 partial_loss_notes。
- **影响**：长文档尾部内容不进索引而 document_version 落 DONE+INDEXED，运维无法用一条 SQL 圈出需重扫的对象。命中面**未量化**（无法离线统计现网 page_count 分布），这是本条最大未知数。
- **改法**：**只做留痕，不动抽取行为、不动上限**：① `OCRResult` 加 `skipped_pages`，`:260-264` 填入；`unified_extractor.py:2399-2406` 旁照 `ocr_partial:` 同型追加一条 note；② `extract_images_from_pdf` 经 `stats["skipped_pages_beyond_cap"]` 回传，在 `_warn_skipped_vector_images` 旁加同型 helper；③ `pipeline_nodes.py:633` 附近把 `extract_truncated`/`extracted_pages` 一并拷进 canonical。
- **工作量**：S（但见风险）。
- **风险与验证**：生效后存量长文档会**批量转 NEEDS_REVIEW**（该状态不在任何认领谓词内，不会形成重试循环，但收尾状态会变），建议先在 staging 统计影响面。**提高上限本身先不做**——`unified_extractor.py:782` 的注释明写 `pdf_image_max_pages` 是付费漏斗的保守默认，而 `config.py:315` `RebuildConfig.enabled=False` 使 cost_breaker 恒 no-op，**这三个页上限就是当前唯一的摄取侧成本闸**，拆之前必须先有 breaker（见 C4）。

### B6. 嵌入图 OCR 按原分辨率上传 —— ❌ **WONTFIX（2026-07-25 实测判定，两个变体都不做）**

> **裁决**：不做。实测证明「省的是带宽（本来不是瓶颈），赔的是检索内容本身」。
> 下面保留原始条目正文供追溯，但**改法一节已作废**。

**实测一：体积（1424 张真实语料图，免费，无 API 调用）**

`compress_page_png` **不短路** —— 只有缩放是条件的（`max(im.size) > 1568`），**JPEG q78 重编码是无条件的**。所以接到 OCR 路径不是"只影响大图"，而是每张都被重编码。

| 语料事实 | 值 |
|---|---|
| 最长边 p50 / p90 | 566px / 1264px（**97.96% ≤ 1568px**） |
| 文件大小 p50 | 19KB（**99.16% ≤ 500KB**） |
| 400 张抽样：压缩后**变大** | **173 张，+6154KB** |
| 400 张抽样：压缩后变小 | 105 张，−9006KB |

变大最多的全是 ERP 操作手册截图（`it_工资核算管理操作手册_img0048` +149KB、`oss_富岭U8+贸易部操作手册_img0090` +119KB）：平坦色块的 UI 截图 PNG 存得极小（50–68KB），转 JPEG q78 反而暴涨 2–3 倍。**而这正是本系统的主力语料**（CLAUDE.md 明写重点是 screenshot-heavy SOP/ERP 文档）。
⇒ **审计原方案（无脑压缩）对主力语料是净负，WONTFIX。**

**实测二：OCR 质量（40 张 × 2 次 = 80 次真实 qwen-vl-ocr-latest 调用，0 失败）**

针对 size-aware 变体（压完更小才用，样本内省 60% 体积）：

```
字符级相似度: min=0.000  p10=0.769  p50=1.000  mean=0.906
文本长度变化: min=-56    p50=0      max=+1065
★ 跨 120 字边界: 3/40 张 (7.5%)
    71 → 281 字  sim=0.00   oss_富岭U8+贸易部操作手册_img0130
   128 → 114 字  sim=0.77   it_富岭U8+财务部操作手册_img0243   ← ROUTE_TO_TEXT 翻成 DISCARD
   131 → 117 字  sim=0.93   oss_富岭U8+品质部操作手册_img0003   ← 同上
```

中位相似度 1.000（多数图无影响），但**尾部是灾难性的**：`sim=0.000` 71→281 字（输出完全不同的文本）、`sim=0.365` 445→1510 字（几乎肯定是幻觉）、`sim=0.541` 27→10 字（降采样把内容读没了）。这几张全是 U8+ 操作手册截图。
**7.5% 跨 120 字边界**，其中两张是 `>120 → <120` 方向 —— 那正是 asset 直接消失的方向（数据丢失级）。原条目"风险与验证"里预判的路由翻面是**真的且高频**。
⇒ **size-aware 变体同样 WONTFIX。**

**顺带修正原条目的一处表述**：所谓"同图 VLM 走 1568/q78"的不对称其实很小 —— VLM 侧的门是 `file_size > 500KB`（`image_funnel_processor.py:375`），而 99.16% 的图 ≤500KB，**两条路径今天其实都在传原图**。

复现脚本：`scratch/b6_ocr_compression_ab_20260725.py`（按原始体积分层抽样，输出相似度分布 + 跨边界张数）。
⚠️ `scratch/` 在 `.gitignore` 里，该脚本**不随仓库分发**，只在原机器上；要复现请照本节参数重写
（分层抽样 → 每张 OCR 原图与 `compress_page_png` 输出各一次 → 比相似度与 120 字边界）。

---

<details>
<summary>原始条目正文（改法已作废，仅供追溯）</summary>

### B6. 嵌入图 OCR 按原分辨率上传，同一张图的 VLM 调用却走 1568px/q78 压缩 —— 【代码】
- **证据**：`extraction/ocr_client.py:435-436` 直接 `open(...).read()` 后 base64，全程无降采样，`:438-439` 按扩展名硬猜 mime（本轮逐字确认）；对照 `_real_pdf_ocr` `:281-284` 显式 `compress_page_png`、`image_funnel_processor.py:333-347/247-253` 在 >500KB 时压缩；`vlm_rebuilder.py:60-78` 注释确认 1568/q78「生产验证过」且「OCR 精度已在版面重建路径验证」。
- **影响**：上传像素数不受我们控制——`vlm_endpoint.py:56-88` 的 `build_image_chat_payload` 不带 `max_pixels/min_pixels`，上界由供应商默认值决定。**倍数无法从代码核实，不要写死倍数。**
- **改法**：`:435-436` 复用 `compress_page_png`（照抄 `:282-284` 的 try import / 失败原样回退 / 按返回值设 mime，顺带替掉 `:438-439` 的扩展名硬猜）。不必加体积阈值——helper 自带 `max(im.size) > 1568` 短路。
- **工作量**：S 改动 / L 验证。
- **风险与验证**：**最硬的回归面是路由翻面**：`image_funnel_processor.py:84` 的 `is_text_heavy = len(ocr_text.strip()) > 120` 会把 ocr_text 变化反馈到路由，从 125 字掉到 118 字就会让该图在 `:113-128` 的 LOW_RELEVANCE 分支从 ROUTE_TO_TEXT 翻成 DISCARD_DECORATIVE，**asset 直接消失**。上线前必须：① 真实 ERP 截图/扫描页压缩前后 OCR 字符级差异对比；② 逐张统计有多少图跨过 120 分界；③ bump `RAG_VLM_CACHE_VERSION`（`unified_extractor.py:299` 默认 "2"）让旧 ocr_text 条目干净失效。跑 L4 全格式闸。注意与 B16 重叠：B16 若先落地，整页扫描图不再走漏斗 OCR，本条最大受益面消失。
  > ①②**已于 2026-07-25 实测完成，结论为否决**（见本条开头）：7.5% 跨边界，尾部 OCR 结果性质改变。

</details>

### B7. 同版本重切留下的 HA3 孤儿 PK 被 serving 陈旧投影护栏放行 —— 【代码】
- **证据**：`pipeline_nodes.py:5533-5540` 按 (doc_id, version_no) 全量 DELETE 再 INSERT，chunk_meta.id（=HA3 PK）重分配（本轮确认，注释 `:5530-5536` 自述这是为消除 shrink strand）；`node_deactivate_old_chunks` 取数是 `WHERE (doc_id=%s AND version_no<%s) AND is_active=1`（`:6283-6287`）——同版本重切时旧行已被 DELETE，RDS 连 id 都不再持有，**结构上不可能删**；serving 侧 `clients.parse_ha3_response:140-142` 同时给出 chunk_id 与 HA3 pk `id`，而 `retriever.py:629/654` 复核**只用 chunk_id** → 孤儿行 is_active/permission 全对得上被放行，`:906` 融合以 pk 为键 → 两行并列，`:1806-1812` 按 chunk_id 取高分去重，**胜者可能是陈旧行**，且主命中 chunk_text 直接来自 HA3 不回 RDS 重读。
- **影响**：被修文档的旧文本可能被当作现行内容投给 LLM。不丢数据。触发需一次人工维护性重切（该运维手册本身带「按 PK 显式清除旧 chunk」步骤，属流程性缓解）。
- **改法**：**先做服务侧根治**：`_revalidate_main_hits` 的 SELECT 加 `id`，用 (chunk_id, HA3 返回的 id) 双轴比对，不一致即丢弃（`HA3_DEFAULT_OUTPUT_FIELDS` 已含 `id` 且 `parse_ha3_response` 恒填）。
- **工作量**：M。
- **风险与验证**：**必须**对 `id` 缺失/为空的命中 fail-open 保留（历史 chunk_id 为空的行用 id 兜底做键，别把它们全丢）。真删提权仍留 user-gated（见 C2）。

### B8. stage-3 批级 all-or-nothing 把同批全成功的首版文档也标 FAILED —— 【代码】
- **证据**：`pipeline_nodes.py:7614-7622` 只要 `total_failed>0` 就整批 raise；`dataworks_orchestrator.py:564-576` 对 `preempted_doc_versions` 里**全部**键 CAS PROCESSING→FAILED，不区分成败；loader `:441-448` 只重选 `chunk_meta.index_status IN (NOT_INDEXED, FAILED)`，全 INDEXED 的文档无 chunk 可装 → `node_acquire_index_lock`（`:5875-5905`）从 `ctx['valid_chunks']` 反推 doc_versions，没有 chunk 就永远不会再被认领；`spot_checker.py:461-463` 的 `EXISTS(version_no 更小 且 is_active=1)` 把 v1 文档排除在修复器外。
- **影响**：**纯状态列失真**——内容已正确入索引并可检索，只是 `document_version.index_status` 永久停在 FAILED，误导人工排障、可能引发重复重灌。
- **改法**：**不要**在 orchestrator 侧把非失败键 CAS 成 SUCCESS（会让「SUCCESS = 已索引且旧版本已停用」的语义失真）。改 `reconcile_stranded_versions`：把 `EXISTS(更旧 active chunk)` 从必要条件降为分支条件，新增一条「当前版本 ≥1 INDEXED 且 0 非 INDEXED、且**不存在**更旧 active chunk、`dv.index_status ∈ (FAILED, NOT_INDEXED)`」的候选——这一支**不做任何 HA3 删除**，只在锁内重验后做时间戳绑定 CAS→SUCCESS。
- **工作量**：M。
- **风险与验证**：锁内重验 + CAS 语义与 2h PROCESSING 排他必须逐字保留。上线前先只读统计命中数。

### B9. console 登记只有 ETag 精确内容 advisory，同名双格式孪生零拦截 —— 【代码】
- **证据**：`ingest_policy.py:105-129` 的 `raw_key_stem`/`stem_twin_action` 全仓只有 `dataworks_nodes/register_new_files.py:364/404` 调用；console 侧 `routes/kb_console.py:2343-2345` 唯一查重是 `_kb_content_dups`，谓词是 `v.etag = %s`（字节级），docx↔pdf 必然不命中（本轮确认）。**关键**：`api.py:2749` 那句「docx↔pdf 跨格式孪生由管线 canonical_sha256 去重处理」**双重不成立**——(a) 两个 canonical 去重闸都是 flag-gated 且默认 OFF（`pipeline_nodes.py:809` `RAG_SKIP_UNCHANGED_REINGEST`、`:865` `RAG_DEDUP_CROSS_DOC`；虽然 `stage1_node.py:40-41` 把它们 setdefault 成 true，但 (b) 跨文档闸比的是 canonical_sha256 **精确相等**（`:882-886`），docx 与其 pdf 转换件的 canonical 文本几乎不可能逐字节相同）。
- **影响**：人真正在用的那条入口对同名双格式件零防线，孪生 doc_id 直接入库，来源列表出现两个同名文件——正是 `ingest_policy.py:14-18` 记录的 FL-ZS-WI-005 双注册复发形态。
- **改法**：在 `kb_register` 的 `action=='new'` 分支、`_kb_content_dups` 调用处紧邻加一次 stem 判定（按 `raw_key_stem(filename)` 查同 owner_dept 的 active 注册，JOIN 形态照抄 `register_new_files.py:279-290`），命中即在 `KbRegisterResponse` 加 `stem_twin` 字段，前端在「已提交」提示里给显式警告 + 「去退役旧件」入口。
- **工作量**：M。
- **风险与验证**：**advisory 不硬拦**（console 有真人在场，静默跳过会被读成上传失败）；放在 commit 之后、fail-open，与 `_kb_content_dups` 同款姿态。升版不走本判定。

### B10. 自助上传孤儿无回收/无对账/无可见性 —— 【代码】
- **证据**：`dataworks_nodes/register_new_files.py:300-306` 是 `continue` + 注释「孤儿存储由 GC 清理」；`/usr/bin/grep` 全仓复核 orphan 只有 HA3 orphan-PK 与 chunker orphan images，**无上传孤儿 GC**；`reconcile.py:872-893` 的 `run_raw_parity_check` 是单向（RDS→OSS）。
- **影响**：少量 OSS 字节长期滞留且无计数口径；用户认知与系统状态不一致。不丢数据（用户看到显式失败）。
- **改法**：**先做只读 orphan 探针**：ops_monitor 加 job，LIST `raw/` 前缀（复用 `reconcile.py:900` 的 `_list_oss_keys`），取 `is_self_serve_raw_key` 形状（`register_new_files.py:131-139`）、`last_modified` 早于 `UPLOAD_TOKEN_TTL=1800s`（`kb_upload.py:22`）、在 `document_version.raw_key` 查不到的对象，只报 count + oldest_age，**不删**。
- **工作量**：M。
- **风险与验证**：「复用 upload_token 重试 register」比看上去贵——`useKb.ts:885/926` 的 catch 只把行标失败，token 随闭包丢弃，UI 根本没有重试控件；要做得新增 UI 状态并处理 30 分钟 TTL 过期回退（`kb_console.py:2170-2172` 返 400）。先探针后重试。

### B11. 「OSS 新文件未注册」数被污染，且该报告无人读 —— 【代码】
- **证据**：`dataworks_nodes/scan_oss_sync_keys.py:118-127` 只跳 `/` 结尾与 `_quarantine`，不 import 也不内联 `ingest_policy`，无 `is_self_serve_raw_key`；`:208-212` new_files、`:225` 打印；对照 `register_new_files.py:231-243` 逐 key 过 `should_ingest_raw_key` 并按 reason 分桶。另有一个漏报的噪声源：`scan_oss_sync_keys.py:165-171` 的 `db_raw_keys` 只从 `WHERE dv.status='active'` 构建，退役/被取代版本的 raw_key 也会被算成「新文件」。
- **影响**：今天**零实际损害**——脚本 DRY_RUN=True，产物是节点日志里一行 print，无任何消费者。纯 latent。
- **改法**：**不要单独修这个打印**。直接在 ops_monitor 新建一个只读 job（LIST `raw/` → `should_ingest_raw_key` 过滤 → 减去 `document_version.raw_key` **全集**而非只减 active）分三桶报数并上浮 kb_governance，把 `scan_oss_sync_keys` 的那行报告降级为纯 debug（它的正经职责是 raw_key 路径修复）。
- **工作量**：M。
- **风险与验证**：parity test（`register_new_files.py:158-160` 所述）不覆盖它，若仍改本脚本必须同时纳入对拍，否则又是第三份会漂的副本。

### B12. drain 无进展守卫按全局 COUNT 判定 + 无墙钟预算；顺带一个 `_quarantine` 谓词缺口 —— 【代码】
- **证据**：`dataworks_orchestrator.py:772` 默认 `RAG_DRAIN_MAX_ITERS=100000`、`:787` 计数、`:792-798` 守卫（在跑批**之前**）、`:801` 才跑批，全函数无墙钟。缺口：`pipeline_nodes.py:151` 的 `_quarantine/` 过滤**只在 Python 侧**，而认领 SQL（`:107-124`）与 `_count_pending_rows(1)`（`:642-648`）两边都没有对应谓词——这类行会被计数、被选进名额、然后被丢弃且永远留在待处理集，正是 `ingest_policy.py:56-60` 警告的形态。
- **影响**：守卫的所有失效模式都收敛到「运行 raise、DataWorks 变红」，不丢数据、不产生静默绿灯。
- **改法**：① 加 `RAG_DRAIN_MAX_SECONDS`，到点 **raise 专用异常**并在 metrics 标 `drain_timeboxed=1`——**不能用 exit 0 收尾**，`:778-782` 的注释把「绝不静默成功」立为不变量；② `RAG_DRAIN_MAX_ITERS` 默认调到 500 量级（零风险）；③ 先用只读 SQL 查现网 `raw_key LIKE '%_quarantine/%' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL`——查得到就是真 bug，查不到就补个 SQL 谓词做防御。
- **工作量**：M。
- **风险与验证**：**不做**「按本批认领集合判进展」——只有 stage-2 现成有 `claimed_ids`，stage-1/3 要新造回传通道，成本≫收益。

### B13. NEEDS_REVIEW 在控制台无对应徽章（窄口）—— 【代码】
- **证据**：`/usr/bin/grep` 复核 NEEDS_REVIEW 在 api.py/routes/console-app **零出现**，`_KB_BADGE_VOCAB`（`api.py:2626-2629`）十值无对应项。三条写路径里只有 `pipeline_nodes.py:7698`（stage-3 DEAD 死信只改 chunk_status）会出问题——`:5741` 那条同时写 `content_process_status='FAILED'` → 徽章「处理失败」已可见；`:5844` 那条（VLM 降级）文本照常可检索，dv.index_status 变 SUCCESS → 徽章「已上线」，**是正常在线**。
- **影响**：只有「部分 chunk 永久 DEAD 却显示已上线」这一窄口。
- **改法**：**不要**把 NEEDS_REVIEW 整体映射成「待人工处理」并塞进 `_KB_BAD_BADGES`——那会把 VLM 降级路径（`pipeline_nodes.py:5809` 明写「文本照常可检索、占位图注照常可服务」）翻成异常，与既定优雅降级取舍冲突。窄口修：`_kb_status_badge` 的 `:2650`「已上线」分支前加 `if str(chunk_status or '').upper()=='NEEDS_REVIEW': return '未入索引'`（复用既有词表），`_KB_BADGE_CASE_SQL:2680` 同位加 WHEN。另把 `queue_monitor.py:151-157` 已实现的 `needs_review_or_failed` 桶上浮到 kb_governance。
- **工作量**：S。
- **风险与验证**：改前跑一次全库 badge 分布 diff。

### B14. 字节完全相同的重传（升版）要先付完整抽取才被 skip-gate 拦下 —— 【代码】
- **证据**：`pipeline_nodes.py:436-445` 先算 raw 字节 sha256，`:447` 才 `extractor.extract`；skip-gate 在 `:810-853`，判据是 `:787` 的 canonical_sha256（抽取后才存在）且要求 `version_no>1`。
- **影响**：白跑一遍抽取 + 扫描件的页级 OCR。**图片侧基本零成本**——漏斗对每张图先按 sha256 查跨文档持久缓存（`unified_extractor.py:1839`），而该缓存有 OSS 镜像（`vlm_cache.py:52`，默认开），字节相同 ⇒ 全命中。
- **改法**：**源头拦截**：`routes/kb_console.py` 的 `kb_register` 里，`action=='version'` 时用已拿到的 `etag_val` 与本文档当前版本的 etag 比对，相同则提示「与当前版本字节相同，无需升版」。**不要复用 `_kb_content_dups`**——它的 SQL 写死 `AND m.doc_id <> %s`（`api.py:2766`），是跨文档查重，这个场景一条都查不出。
- **工作量**：S。
- **风险与验证**：管线侧兜底（可选）：闸放在 `:445` 之后 `:447` 之前，条件严格限定「同 doc_id 存在 `version_no<本版` 且 `checksum_sha256` 完全相等 且该前版 canonical_json_key 非空」，命中才复用并按 `:828-836` 的终态收尾。**不要指望它解决 stage-1 重跑**——那条路径 `canonical_json_key IS NULL`，本来就没有可复用产物。

### B15. HA3 路径上 bulk NDJSON payload 的构建/上传/归档全是无人读取的死重 —— 【代码】
- **证据**：`pipeline_nodes.py:6916-6917` build 调用；`bulk_helpers.py:56-80` 每 chunk 一次 `json.dumps(chunk.to_opensearch_doc())` 且 `chunker.py:224-225` 把 `embedding_vector` 塞进 doc；`:6952` put；`:7256-7259` HA3 分支走 `_push_chunks_to_ha3` 用内存对象，payload 只在 `:7280` 非 HA3 分支被消费；`:7355-7357` copy+delete 归档。`/usr/bin/grep -rn payload_oss_key` 全仓只有 `:6978/6986/7464` 与 `schema/001:458`，**确无读回方**。
- **影响**：单批被 `dataworks_orchestrator.py:450` 的 `LIMIT 1000` 限死，约 13MB NDJSON（估算）；收益主线是**峰值内存**（`ctx["bulk_batches"]` 全程持有整批 NDJSON）而非 CPU/OSS 费用。
- **改法**：新增 `RAG_BULK_PAYLOAD_ARCHIVE`（**默认 on 保持现状**，HA3 部署侧显式关），关时 `build_opensearch_bulk_actions` 走 `materialize=False` 分支——仍按累计字节切批并返回 payload_size，但不拼字符串；`:6952` 的 put 与 `:7349-7360` 的归档一并跳过。
- **工作量**：M。
- **风险与验证**：**必须保留 payload 的两条分支**：`:7280`（标准 OpenSearch 本地回退）和 simulate 路径 `:6926-6941`（写本地 pending JSONL，sim 断言依赖）。

### B16. 扫描 PDF 每页付两次 OCR，漏斗那次的产物在同一函数里被丢弃 —— 【代码】
- **证据**：`extraction/unified_extractor.py:910` `extract_images_from_pdf` → `:915` `_process_embedded_images` → `:953` `_pages_needing_ocr` → `:955` `_apply_ocr_fallback`（本轮确认顺序）；`:2254-2256` `if bt in ("image_ref","ocr_text"): continue`，而漏斗产物块 block_type 恒为 `"ocr_text"`（`:2035-2049`），所以漏斗文本 100% 不计入「该页已有文本」→ 扫描页必然二次进页级 OCR。
- **影响**：扫描页首灌每页多付 1 次整页 OCR，上界 = 每文档 `pdf_image_max_pages=20`（`config.py:882`）；漏斗那次命中 vlm_cache 时为 0。**削弱**：只在 ROUTE_TO_TEXT + containment≥0.85（`:662`）被 `_drop_double_ocr_dups`（`:2374`）删掉的那部分才是纯白付；走 ROUTE_TO_VECTOR 的整页图其 ocr_text 留在 asset_dict 进 image_refs 契约。同一现象已在 `docs/tier3_decision_2026-07-06.md:27-33` 记录过（当时关切是 canonical 翻倍，选了「产物近重复合并」这条路），不是 WONTFIX 也不是无人看过的盲区。
- **改法**：① 把 `_pages_needing_ocr(result)` 上提到 `:915` 之前（它只读 native blocks 且本来就跳过 image_ref/ocr_text，结果逐字节相同——零风险，可单独先做）；② 把 `_apply_ocr_fallback` 拆成 `_run_page_ocr(needy)→ocr_blocks` 与 `_merge_page_ocr(result, ocr_blocks)`（后者保留 HF 裁剪/garbled 剔除/thin 去重/`_drop_double_ocr_dups` 全部现有逻辑，一行不动）；③ 次序改为 native → `_run_page_ocr` → 漏斗（给 `process_image` 传 `page_ocr_text`，让 Funnel-2 直接用该页文本，跳过自己那次 `ocr_image`）→ `_merge_page_ocr`。
- **工作量**：L。
- **风险与验证**：**两个看似更简单的方案都不能做**：(b)「ocr_text 留空」会改路由（`image_funnel_processor.py:84` `is_text_heavy=len>120` 留空即 False → `:113-128` 返 `DISCARD_DECORATIVE` → asset 被 `:2000` 的 Phase-3 丢弃 → 整页扫描图消失、不上传 OSS，**数据丢失级回归**）；(a)「先跑 fallback 再跑漏斗」会破坏 `:2374` 的去重次序，6B37DC 的 canonical 翻倍原样复发。验收：真实扫描件新旧 canonical 逐字节 diff 预期为 0，**assets 数量与 status 分布必须完全一致**，再跑 L4 全格式闸 + GT chunk-eval。

### B17. step 模式升级门槛过低：先加可观测，别急着改判据 —— 【代码】
- **证据**：`pipeline_nodes.py:4046-4051` clause 分支 → `:4065` `if m_mode in ("text","clause") and _detect_step_patterns(doc): m_mode = "step"` 无条件覆盖；`_detect_step_patterns`（`:2377-2432`）的 `sop_keywords` 含「操作/手册/流程/规程/检验/培训」，`_STEP_DETECT_RE`（`:2358-2373`）的备选分支会吃任何行首编号，`:2431` 只要求前 10000 字内 ≥2 处。唯一站得住的实害：`chunker.py:53` 的 `_FIX_B_TYPES` 不含 step_card，`:422` 的章节标题治理拿不到。
- **影响**：**命中面完全未量化**——全条没有一篇真实被误路由的文档。检索侧影响也比想象小：`retriever.py:1409-1412` 的步骤扩展按意图分档（`locate_field` 不扩展、`specific_step` +1、`general` ±1，只有 `full_procedure` 取全部兄弟）且受 `max_steps`（`:1570`）与 `RAG_STEP_EXPAND_FAMILY_CAP`（`:1624-1670`）截断。
- **改法**：**只先做可观测（S、零家族变动）**：把最终 `m_mode` 与命中信号（命中的关键词、`_STEP_DETECT_RE` 命中的分支与次数）写进 `chunk.extra` → 随 extra_json 落 chunk_meta，让「有多少篇制度类文档实际走了 step、因为哪个词」能用一条 SQL 查出来。另外可独立评估把 step_card 纳入 `_FIX_B_TYPES`（这是唯一实害的直接修法，不动路由判据、不产生家族迁移）。
- **工作量**：S（可观测）/ M（收紧判据）。
- **风险与验证**：**「标题改一个字就翻家族」不是缺陷**——CLAUDE.md 明写 freeze 只钉 routing *input*，resolved family 是 (frozen category + canonical text + detector version) 的确定性函数，manifest 门是安全网。收紧判据须走 route-v2 迁移流程。

---

## 四、档 C：需要拍板 / 有前置条件

### C1. 把摄取积压变成「有人会被告知」的信号 —— 【配置/运维 + 拍板】
- **证据**：`dataworks_nodes/ops_health_monitor_node.py:203` `sys.exit(main(["--only","reconcile_ha3","reconcile_oss"]))`（本轮逐字确认），`:204-205` 是注释状态的阶段 2；`deploy/com.fuling.ops-monitor.plist:25-27`、`deploy/run_ops_monitor.sh:27-29` 同款；`ops_monitor.py:29-32` 六作业全是检测型；`queue_monitor.py` 三探针 SQL 在 `:133-137/140-149/151-157`，`:180` `"ok": not problems`。
- **顺序不能反**：既有事实 3 已确认该节点未配 `RAG_OPS_ALERT_WEBHOOK`，`alerting.py` 的 `send_ops_alert` 在无 webhook / 域不在白名单（`_webhook_allowed` `:55-76`）时只记 `_note_suppressed`（`:41-45`）；节点退出码今天已因 reconcile_ha3 恒 ALERT 而为 2（`ops_monitor.py:68-76` 取 max）。**先配 webhook + secret，再加探针**，否则只是把更多信号写进没人看的日志。
- **拍板点**：① 配 `RAG_OPS_ALERT_WEBHOOK` / `RAG_OPS_ALERT_SECRET`（Sam 的凭据动作）；② `--only` 扩成 `reconcile_ha3 reconcile_oss queue_aging ingest_funnel`；③ **必须先定基线**——`run_ingest_funnel_check` 是 `if int(cnt) > 0`（`:170`）→ `"ok": not problems`，而现网 `registered_not_indexed` 桶里天然常驻存量（78 篇 EMPTY 的 index_status 留在 NOT_INDEXED 会被计入；注意这些文档的 `content_process_status` 是 DONE（`pipeline_nodes.py:5747` `_chunk_status,_cps = "EMPTY","DONE"`），**落不进** `needs_review_or_failed` 桶；2 篇隔离被 `:149` 的 `publish_status <> 'QUARANTINED'` 显式排除）。不加 `RAG_FUNNEL_*_MIN` 或「相对昨日增量」判定，探针一上就恒红。
- **附带**：`queue_aging` 的 user_feedback 探针查 op_db（`queue_monitor.py:74`），DataWorks 节点凭据须对 `fuling_operation` 有 SELECT，否则该单探针记 error（不拖垮其余）。
- **工作量**：S（配置）/ M（含基线定义）。

### C2. 把补偿层从「手工 stage-3」搬到日常调度 —— 【拍板 + 代码】
- **证据**：五个修复器全在 `dataworks_orchestrator.py:709/721/737/753/765` 的 `if stage == 3:` 里，是全仓唯一生产调用点（`spot_checker.run_spot_check_pipeline:673-689` 未被任何调度节点引用）；控制台退役只置 `chunk_meta.is_active=0` + `index_status='PENDING_DELETE'`（`routes/kb_console.py:2498-2508`），HA3 行不删。
- **为什么值得拍**：`config.py:375/1009` 的 `main_hit_revalidate` 默认 True，但 `retriever.py:645-651` 在 RDS 不可达时 fail-open，此时 ACL_FAIL_CLOSED 只保留 `permission_level=='public'` 的命中——**「刚改成 restricted 但 HA3 投影仍写 public」的行恰好会被当 public 留下**。RDS 故障 + 投影陈旧的复合窗口里确实会越权投放。
- **改法（收紧的上线序）**：第一步只把 `reconcile_pending_deletes` 与 `reconcile_stranded_versions` 挂进已在跑的 `retention_node`(03:30)（两者都是逐 doc 事务 + 锁内重验 + fail-open，删除集来自 RDS 权威）；orphan reconcile 仍只以 `dry_run=True` 报数接入，并把 `stale_subtypes.dup>0` 纳入 warning 级告警判据（`reconcile.py:101` 明写「HA3 stale 行单独不置 ok=False」，所以那份日检今天不会翻红）。
- **前置**：C1 的 webhook 必须先通——否则是把不可逆删除放进既无人值守又无告警的窗口。部署可行性已确认：`ops_health_monitor_node.py:88-95` 已要求 `RAG_HA3_ENDPOINT/TABLE_NAME` 并跑同一代码包，不需要新凭据面。
- **工作量**：S（挂载）/ M（含告警判据）。

### C3. 是否给摄取挂日调度 —— 【拍板】
- 这是本轮最大的结构决策。**前置硬依赖**：A12（GET_LOCK 单实例互斥）+ B1（retry 语义 + 回滚）+ A3（假绿封堵）必须先落地。理由：一旦 cron 与人工批并存，A12 的重复付费从 P3 立刻升级；B1 未修时调度会把「三次崩溃 → 永久绿灯」从偶发变成日常。
- 另注：`dataworks_nodes/register_new_files.py` 与 `scan_oss_sync_keys.py` 均 `DRY_RUN=True`（既有事实 2），挂调度前需一并决定它们是否转实跑。

### C4. 图片漏斗的成本护栏 —— 【拍板 + 配置】
- **证据**：`extraction/cost_breaker.py:200` `self.enabled = cfg.rebuild.enabled if enabled is None else enabled`、`:238-239` `if not self.enabled: return True, None`；`dataworks_orchestrator.py:75` 与 `:701` 两处都是 `CostBreaker(config)` 从不传 enabled；`config.py:961` `enabled=_env_bool("REBUILD_ENABLED", False)`——**唯一熔断器焊死在 `RAG_REBUILD_ENABLED` 上**，而那是 VLM 重建的总开关。`dataworks_nodes/stage1_node.py` 全文 grep REBUILD/FUNNEL/COST 零命中。`unified_extractor.py:1870` 的 `RAG_FUNNEL_MAX_IMAGES` 默认 `"0"`=不限；`config.py:325` `daily_budget_rmb` 默认 0.0；`cost_breaker.py:115-121` `RAG_COST_ALERT_ENABLE` 默认关。docx/xlsx/pptx 三个抽取器（`image_extraction_utils.py:50/462/886`）零图片数上限。
- **量级校准**：按项目自己的单价常数（`config.py:324` `vlm_image_rmb=0.04`），300 图 DOCX ≈ 12 RMB/篇；且只有每张**唯一**图（sha256 去重 + vlm_cache 跨文档跨运行命中）才付费。这是「无界」而非「正在烧」。
- **按可行性倒序的动作**：① **今天就能生效、零代码**——在 stage-1 调度 env 设 `RAG_FUNNEL_MAX_IMAGES`（它直接读 env 不经 breaker），取值前用只读 SQL 统计现网单文档唯一图数 P99；cap 触发走 `_budget_skipped_count → NEEDS_REVIEW` 自愈通道（`unified_extractor.py:1874-1884` + `:2068-2072`），不会静默丢图。② 打开 `RAG_COST_ALERT_ENABLE`（纯观测）。③ 再谈解耦 breaker：新增 `RAG_COST_BREAKER_ENABLED`（缺省回落 `cfg.rebuild.enabled`）——**但必须同时给漏斗一条独立的单元上限**，因为 `_gate_estimate` 的第一道闸是 `est.raw_units > rb.max_pages`（`cost_breaker.py:214-218`，默认 50）而 `estimate_doc_cost` 的 `raw_units` = 唯一图数 + OCR 页数（`:178`），>50 图的 SOP 会被**整篇 DENY**，一批图密集文档会成建制进 NEEDS_REVIEW。④ 日预算闸放最后，且先确认 `schema/018` 账本已 apply（`cost_breaker.py:252-262` 在账本不可用时按瞬态拒绝顺延）。
- **工作量**：①② S / ③④ M。

### C5. embedding 缓存：每批推整文件镜像 + 容量默认过小 —— 【拍板 + 代码】
- **证据**：`pipeline_nodes.py:6754` 与 `:6818` 各调 `_store.finalize(_CACHE_MAX_ENTRIES)`；`embedding_cache.py:229-233` finalize = evict_to + `if self._dirty_puts>0: self._push_oss_mirror()`；`:280-301` `_push_oss_mirror` 做 `PRAGMA wal_checkpoint(TRUNCATE)` 后整文件 `put_object_from_file`；镜像生产默认开（`:311/121-123`）。对照 `vlm_cache.py:63-69` 的 `RAG_VLM_CACHE_OSS_SYNC_EVERY` 默认 10——**只有 VLM 侧有降频**。上限常量在 `:6612`。
- **校准**：`:6754` 在 `if is_dashscope and miss_chunks:` 分支内，**全命中的批次不进这个分支**，重跑已缓存语料时为零。
- **拍板点**：`RAG_EMBEDDING_CACHE_MAX_ENTRIES` 默认 20000 ≈ 1 万 chunk，明显小于现网语料（代码在 `:6659-6668` 已会为此发 cap-pressure 告警，等于自己承认默认值不够）；提容量到覆盖全量需要 Sam 定 OSS 存储与镜像体积的取舍。**这条的钱味比降频重**——超容后每批淘汰最旧条目，被淘汰的下一轮重付 DashScope embedding。
- **改法**：降频那半（代码 S）：把 `:6754/6818` 改成只 `evict_to(...)`，由 `run_stage_drained` 在 drain 循环结束后统一 finalize 一次（finalize 幂等，靠 `_dirty_puts>0` 门控，移出去不会重复推）。
- **工作量**：S（降频）/ 拍板（容量值）。

### C6. VLM 漏斗缓存键不含模型名/prompt 版本 —— 【拍板 + 代码】
- **证据**：`extraction/unified_extractor.py:289-300` `_vlm_cache_ns` 只返回 `f"{pub|sec}:{RAG_VLM_CACHE_VERSION 默认 '2'}"`，不含模型（本轮逐字确认）；写入键 `:1950`；模型在 `image_funnel_processor.py:311` 运行时解析。对照 `extraction/ocr_client.py:341-343` 键含模型名、`:315` 注释自陈「无需手工 bump」——**同仓两套做法不一致**。
- **补充一个更严重的同键洞**：`RAG_VLM_DOC_CONTEXT`（`unified_extractor.py:302-310` `_funnel_doc_title`）会把**文档标题**拼进 funnel prompt 使 caption 成为文档的函数，而键是文档无关的 → 该 flag 一旦打开，同一张图在两篇文档间会把**第一篇的 caption 服务给第二篇**。这是正确性 bug，不只是陈旧问题。
- **已有兜底**：`eval_harness/run_eval.py:129-150` 把 `vlm_model`/`vlm_cache_version`/`l4_gt_sha` 写进 regime 指纹，`baseline.py:153-173` 在指纹不匹配时使基线不可比——「静默换模型 → 门禁悄悄退化」这条链已切断，剩下的是**预防缺位**。
- **改法**：`_vlm_cache_ns` 改成 `f"{ns}:{ver}:{model_name}:{prompt_hash8}"`（prompt_hash 取 funnel prompt 模板 + 生效 flag 组合的 sha256 前 8 位）；`RAG_VLM_DOC_CONTEXT` 为 ON 时 key 再拼 doc_title 的 hash（代价是该 flag 下跨文档去重收益归零——这正是它该被单独定价的理由）。`RAG_VLM_CACHE_VERSION` **保留**为人工总闸（`qwen3-vl-plus` 是别名，服务端可无声换代，模型名进 key 是必要不充分条件）。
- **拍板点**：切换当次存量缓存整体 miss → 下一轮再摄取的图全部重过 VLM。因 `config.py:315` `RebuildConfig.enabled=False` 使 cost_breaker 当前 no-op，**必须先启用 breaker 或临时压低 `RAG_VLM_CONCURRENCY` 定界**再切。切换前后各跑一次 L4 xlsx/docx 绑定门取对照。
- **工作量**：S（代码）/ 拍板（成本界）。

### C7. B3 存量生效需要冻结重灌 —— 【拍板】
- xlsx 绑定修复只对新摄取生效；存量要改 `image_refs_json` 必须走 `RAG_MAINTENANCE_ROUTING` 冻结重切 + 预编码 count/type_mix manifest，属生产写操作，需 Sam 逐次授权并确定批次范围。

---

## 五、全量条目表

| 编号 | 标题 | 档位 | 类别 | 工作量 |
|---|---|---|---|---|
| A1 | 三个并发旋钮（extract/loader/publish）生产从未注入 | 立刻做 | 配置/运维 | S |
| A2 | OSS 取件失败被吞成空 canonical 并定稿，不可自愈 | 立刻做 | 代码 | S |
| A3 | 封堵 stage-2 drain 假绿：收尾必须报出 LOADING/PROCESSING | 立刻做 | 代码 | S |
| A4 | index_retry_count 成功不清零 + UNKNOWN 烧预算 | 立刻做 | 代码 | S |
| A5 | OCR 页缓存恒冷（无 OSS 镜像/相对路径/无 flush） | 立刻做 | 代码 | S |
| A6 | stage-3 每轮前置全 HA3 id 空间扫描，dry-run 下无产出 | 立刻做 | 代码 | S |
| A7 | DEACTIVATE 审计逐条一次连接+提交的 N+1 | 立刻做 | 代码 | S |
| A8 | OSS payload 归档失败 raise，把成功推送判成整批失败 | 立刻做 | 代码 | S |
| A9 | HA3 错误项 err_idx 缺下界（-1 误伤）+ 未采样响应形态 | 立刻做 | 代码 | S |
| A10 | 摄取分类器不钉 enable_thinking，失败即静默隔离 | 立刻做 | 代码 | S |
| A11 | 漏斗把解码失败记成装饰图，且两份重复判据零留痕 | 立刻做 | 代码 | S |
| A12 | 全仓无单实例互斥（GET_LOCK 零命中），重叠运行重复付费 | 立刻做 | 代码 | S |
| A13 | 独立图片文档绕过跨文档 VLM 缓存 | 立刻做 | 代码 | S |
| B1 | stage-2 批次失败不回滚认领集合 + retry_count 语义错位 | 值得排期 | 代码 | M |
| B2 | stage-1 全批抽取→逐篇定稿，中途失败丢弃已付费成果 | 值得排期 | 代码 | M |
| B3 | xlsx「如图N」被位置计数器抢先覆盖（实现与自身注释矛盾） | 值得排期 | 代码 | M |
| B4 | 超长 table_chunk 整块丢弃，无行级切分 | 值得排期 | 代码 | S+M |
| B5 | 三处页级上限静默截断，未接入 partial_loss_notes | 值得排期 | 代码 | S |
| B6 | ~~嵌入图 OCR 不压缩~~ **WONTFIX**（2026-07-25 实测：无脑压缩体积净负；size-aware 有 7.5% 跨 120 字边界致 asset 消失） | ❌ 不做 | — | — |
| B7 | 同版本重切孤儿 PK 被 serving 陈旧投影护栏放行 | 值得排期 | 代码 | M |
| B8 | stage-3 批级 all-or-nothing 把首版文档永久标 FAILED | 值得排期 | 代码 | M |
| B9 | console 登记只有 ETag 查重，同名双格式孪生零拦截 | 值得排期 | 代码 | M |
| B10 | 自助上传孤儿无回收/无对账/无可见性 | 值得排期 | 代码 | M |
| B11 | 「OSS 新文件未注册」数被污染且无消费者 | 值得排期 | 代码 | M |
| B12 | drain 无墙钟预算 + MAX_ITERS 十万 + _quarantine 谓词缺口 | 值得排期 | 代码 | M |
| B13 | NEEDS_REVIEW 无控制台徽章（stage-3 DEAD 窄口） | 值得排期 | 代码 | S |
| B14 | 字节相同重传要先付完整抽取才被 skip-gate 拦下 | 值得排期 | 代码 | S |
| B15 | HA3 路径上 bulk NDJSON payload 全链路死重 | 值得排期 | 代码 | M |
| B16 | 扫描 PDF 每页付两次 OCR，漏斗产物被同函数丢弃 | 值得排期 | 代码 | L |
| B17 | step 模式误路由：先落路由可观测，别急着改判据 | 值得排期 | 代码 | S+M |
| C1 | 配 ops 告警 webhook 并把 queue_aging/ingest_funnel 挂上（含基线） | 需拍板 | 配置/运维+拍板 | S+M |
| C2 | 补偿层从手工 stage-3 搬到 retention_node（含 ACL 越权窗口） | 需拍板 | 拍板+代码 | S+M |
| C3 | 是否给摄取挂日调度（硬前置：A12+B1+A3） | 需拍板 | 拍板 | — |
| C4 | 图片漏斗成本护栏（FUNNEL_MAX_IMAGES / 解耦 breaker / 日预算） | 需拍板 | 拍板+配置 | S→M |
| C5 | embedding 缓存镜像降频 + 容量默认 20000 提容 | 需拍板 | 拍板+代码 | S |
| C6 | VLM 缓存键加模型/prompt hash（含 DOC_CONTEXT 串味洞） | 需拍板 | 拍板+代码 | S |
| C7 | B3 存量生效的冻结重灌授权与批次范围 | 需拍板 | 拍板 | — |

**建议的最短路径**：A1（配置，今天）→ A3 + A2 + A4（让运行结果可信）→ C1（让积压有人被告知）→ B1（把 P1 假绿的根修掉）→ 然后才谈 C3 挂调度。A5–A13 可并行插空做，互不依赖。

---

## 六、端到端验证：L4-ingestion 绑定硬闸 A/B（2026-07-25）

A+B 两档共 8 个 commit 里，**唯一改变"入库内容"的是 B3**（xlsx 图↔步骤绑定优先级）；其余均为流程/可观测/serving 侧。因此端到端验证只针对绑定质量硬闸。

**方法**：base `071a39c`（本批之前）建 detached worktree，**复制同一份 VLM 缓存**（3797 条）与同一份 `fuling_chunk_exp/` 夹具进去，两侧跑同一支柱（`cases=[]`，serving 支柱不触发 ⇒ 零 LLM 调用）。33 篇 GT 文档 + 43 篇 docx 严格夹具，合计 49 个 per_doc 条目。

| 指标 | BEFORE `071a39c` | AFTER（A+B 8 commits） | baseline 阈值 | 判定 |
|---|---|---|---|---|
| `l4ing.jaccard.docx` | 0.9891 | 0.9891 | ≥0.9891 | ✅ |
| `l4ing.jaccard.pdf` | 0.8111133 | 0.8111133 | ≥0.8111133 | ✅ |
| `l4ing.jaccard.xlsx` | 1.0 | 1.0 | ≥0.891665 | ✅ |
| `img_dup_factor_p95` | 1.0 | 1.0 | =1.0 | ✅ 无 over-attach |
| per_doc 逐篇 | — | — | 49/49 **零差异** | ✅ |

**结论必须分两句说，不能合并**：

1. **B3 无回归** —— 逐篇位位相同，`img_dup_p95` 保持 1.0（B3 第三步"追加第二张图"没有引入多绑）。
2. **B3 的收益本次未被验证** —— xlsx 在 base 就已经是 1.0（07-20 那次绑定修复的既有成果，baseline.json 冻结于该修复之前所以还记着 0.891665）。金集里**不存在**能区分新旧绑定优先级的样本，B3 无处可涨。B3 的实际效果仍待 C7 的冻结重灌后在真实存量上观察。

**regime 指纹的一处盲区（如实记录）**：`vlm_model=qwen3-vl-plus`、`vlm_cache_version=2` 与 baseline 匹配；但 baseline 里 `l4_gt_sha` 为 `None`（冻结于该哨兵引入之前，落入 `baseline._LENIENT_REGIME_KEYS` 宽容窗口），因此"GT 自 baseline 以来有没有被重标"这一维**无从校验**。本次 A/B 因为两侧同 GT、同缓存，内部一致性不受影响；但与 baseline 数值的绝对比较带这个前提。

### D1（验证中新发现，非本批引入）✅ 已修复：VLM 缓存畸形条目命中即整篇抽取失败 —— 【代码】

- **现象**：`fuling_chunk_exp/it_工资核算管理操作手册（2025年5月28日初版）.docx` 抽取抛 `AttributeError: 'int' object has no attribute 'get'`，位置 `extraction/unified_extractor.py:1959`（`cached.get("ocr_text")`）。base 树一字不差地报同一条 ⇒ **不是 A/B 两档引入的**。
- **根因（数据侧）**：`scratch/vlm_cache.sqlite3` 3797 条里 **439 条的值是 JSON 数字而非 dict**（值域 2140–5787）。按 key 形态精确拆分：

  | key 形态 | hash | 条数 | 当前是否可达 |
  |---|---|---|---|
  | `{md5}:pub` | MD5 | 394 | ❌ 不可达 |
  | `{md5}` 裸键 | MD5 | 38 | ❌ 不可达 |
  | `{md5}:sec` | MD5 | 4 | ❌ 不可达 |
  | **`{sha256}:pub:2`** | **SHA-256** | **3** | **✅ 活路径地雷** |

  可达性判据：`_vlm_cache_ns` 默认版本为 `"2"` ⇒ 现行查询 key 是 `{sha256}:pub:2`；且主键自 B4（2026-07-17）起由 MD5 换 SHA-256，仓库里 427 条裸键**全是 32 位 MD5**，`vlm_cache.get(file_hash)`（sha256，64 位）永不匹配 ⇒ 裸键回退在实践中是死路。所以**真正的活跃面是 3 条，不是 439 条**。
  rowid 6778–7216 为**单一连续区块** ⇒ 一次性批量写入；仓库内 5 个写入点全部经 `_vlm_cache_entry()` 返回 dict，遗留 `vlm_cache.json` 2110 条**全是 dict**。具体来源无法从仓库证据确证，不作结论。
- **根因（代码侧，这条才是修的对象）**：`_vlm_cache_lookup` 主路径 `if entry is not None: return entry` **没有形态校验**；紧邻的遗留裸键分支反而有 `isinstance(legacy, dict)`。坏条目被原样返回 → `cached.get(...)` 抛 → **整篇文档抽取失败**（与"辅助失败绝不打断抽取"的降级约定相悖）。A13 新接的独立图片路径同一形状、同样无守卫。
- **修法（codex 六阶段评审 FULL CONSENSUS，thread `019f9c24`）**：
  - 新增 `_vlm_cache_entry_usable()` 作为条目契约单一来源，`_vlm_cache_lookup` 主路径非法即**视同未命中并 fall-through**（不 early-return，忠实"就当这个 key 不存在"）；
  - 校验到 **status 属于四值集合** `_VLM_CACHE_USABLE_STATUSES`，而不止 `isinstance(dict)`：未知状态不抛异常，却会安静绕过 `DISCARD_*` 等值跳过、带空字段流进 `asset_dict`——比崩溃更难查；
  - 与 legacy public 回退的三值白名单 `_VLM_CACHE_LEGACY_PUBLIC_STATUSES` **显式分离**（后者刻意排除 `QUARANTINE_SENSITIVE`：裸键全产生于 public-bypass 时代，复用会跳过敏感审计）。套用它到主路径会让敏感图缓存整体失效、白重跑 VLM 审计；
  - 守卫内先判 `isinstance(status, str)` 再判集合成员——status 若是 list 等不可哈希值，直接 `in <set>` 会抛 `TypeError`，守卫自己崩掉等于没修（该分支由单测钉死，实施中确实被自己的测试抓到过一次）。
- **自愈**：非法条目→miss→重算→同 key 被合法 dict 覆盖。实测 A/B 中修复臂缓存坏条目 439→436，3 条活路径地雷全部转为合法 dict；对照臂原样 439/3。
- **L4-ingestion 硬闸 A/B（两臂各用独立同源缓存副本，快照 sha256 `7629e6fd…` 一致）**：

  | fmt | A（无 D1） | B（有 D1） | 现行硬闸 | 基线地板(−0.03) | |
  |---|---|---|---|---|---|
  | docx | 0.9891 | **0.9898** | ≥0.95 | ≥0.9591 | ✅ |
  | pdf | 0.8111 | 0.8111 | ≥0.78 | ≥0.7811 | ✅ |
  | xlsx | 1.0 | 1.0 | ≥0.85 | ≥0.8617 | ✅ |

  `img_dup_p95` 1.0→1.0；errors 1→0；**旧 cohort 逐篇 0/49 变化**；恢复文档按两臂成功集合差集计得 **1 篇**（`it_工资核算管理操作手册`，`is_sop=True`、31 张步骤卡、correct/checked = 49/49、acc 1.0、img_dup 1.0）——docx 主闸因此**上升** 0.9891→0.9898，非回归，无需重冻 baseline。
- **爆炸半径仍有未定项**：**生产 OSS 镜像 `processing/cache/vlm_cache.sqlite3` 是否也含畸形条目——未查**（本会话未获授权访问实时层）。守卫落地后即使中招也只是多付几次 VLM 重算，不再整篇失败。
- **存量清理（可选，未执行）**：`scratch/purge_vlm_cache_scalars_20260725.py` —— 默认 dry-run；`--apply` 必须显式给 `--db`；备份走 SQLite online backup API（WAL 安全，裸 `cp` 会漏未 checkpoint 的 WAL）、序号递增绝不覆盖、删前对备份跑 `PRAGMA integrity_check`；单事务只删非 object 行。**⚠️ 脚本自身零 OSS 调用，但缓存镜像是"整库上传"：下一次开着 `RAG_VLM_CACHE_OSS_MIRROR` 的真实运行会把清理后的整库推到生产镜像，即本地删除会间接传播。** 是否执行 `--apply` 由 Sam 拍板。
- **本次验证过程对现网无副作用**：全部跑次均无 OSS 镜像推送（日志零 `Pushed ... mirror to OSS`、零上传失败告警），仓库缓存文件 sha256 与跑前快照一致。
- **MINOR 待办（本次未改）**：`RAG_VLM_CACHE_VERSION` 在 `_vlm_cache_ns` 取默认 `"2"`、在 `_vlm_cache_lookup` 的版本判据取默认 `""` —— "未设置"因此落进"带版本命名空间 + 仍允许裸键回退"的不一致态。因裸键回退实际不可达（MD5 vs SHA-256）故 inert；改它会打破 `tests/test_ocr_sanitize.py::test_legacy_bare_key_hits_for_public` 换零收益，故仅记档。

---

## 七、PDF 绑定质量：失分拆解与 PDF-D2 修复（2026-07-25）

L4-ingestion 三格式里 PDF 最低（0.8111，= 30 个 step-chunk 的 **micro 均值**，故每题值 3.33pp）。到满分的 18.9pp 空间分布极不均匀：

| 文档 | 题数 | 失分 | 占空间 |
|---|---|---|---|
| pdf_xs_wi_007 | 9 | 4 题 0 分 | 13.3pp（70%） |
| pdf_sop | 11 | 3 题部分失 | 4.4pp |
| pdf_it_xxh_003 | 10 | 1 题 0.667 | 1.1pp |

**四个 0 分不是同一回事**，逐条归因后只有两条是真绑定 bug：

| GT 题 | 判定 |
|---|---|
| 步骤3 / 步骤4 图集**整组对调** | ✅ 真绑定 bug —— 单一根因，即 PDF-D2 |
| 步骤2 得图（GT 期望空） | 真绑定错，但**位置对、语义错**（图在步骤2 文字下方，GT 按语义判给步骤1）。需内容绑定，几何修法治不了 |
| 步骤5.1 | **评测匹配问题**：产出有 4 张 step-5 子卡，评测把 GT「5.1」匹到了「3）假如」那张。指标在此低估管线 |

（`ingestion_binding.py` 的 GT→产出匹配**没有"已消费"集合**，同一张卡可被多条 GT 命中——这解释了本篇 步骤1「蒙对」1.0 而 步骤2 得 0。）

### PDF-D2（已修复）段落块 y 包络跨栏膨胀 → 图↔步骤整组错位 —— 【代码】

**根因是两个缺陷叠加**，缺一都不发生：

1. **页眉裁剪的跨线残留**：`_pass1_analyze` 的页眉候选带是页高 10%（本文档 84.19pt），而该模板页眉最后一行 `生效日期：` 在 **top=91.90**，超出 7.7pt ⇒ **从未进入页眉候选**。它又跨在 `crop_top=95` 上（91.90 → 102.36），pdfplumber 的 `crop` **保留跨线字符并把 `top` 钳到 95.0**（实测：裁剪后该词 `top == 95.00`）。于是一个带**假 y** 的页眉残片混进正文流。
2. **段落切分只认向下的间隙**：`pdf_extractor.py` 的 `(top - prev_bottom) > 40` 是**有方向**的。而双栏页的阅读序是「左栏整栏 → 右栏整栏」（`_detect_column_split`），从左栏末行跳到右栏首行时 gap **恒为负**。本例 `91.90 − 507.68 = −415.78`，不 `> 40` ⇒ 不切段。

结果：`步骤4`（真实 y 区间 `[495.5, 507.7]`）与页眉残词并块，**块 y 包络膨胀成 `[95.0, 507.68]`（412pt，罩住整页）**。图片按「与锚点块 y 正重叠最大」锚定 ⇒ 同页图 26/27/28（y0=140/142/340）全塌进步骤4；仅有的例外是 y0=509.2 的图 29（比包络底边只低 1.5pt），落在包络外被甩给步骤3 —— **两个步骤的图集恰好对调**。

**修法（codex 六阶段评审 FULL CONSENSUS，thread `019f9c59`）**：判据取绝对值。跨栏本就该切段——段落块的 y 包络跨栏时没有意义。同时 bump `EXTRACTOR_VERSION` 1.0.0 → 1.1.0（输出形状变化的溯源纪律；其消费者是 `build_run_provenance()`，不自动触发重抽取）。

**为什么不修更"根"的那一层（跨线残留）**：曾实现过"丢弃被裁剪线钳住的残词"（记为 F1），在 49 篇本地真实 PDF 上普查后**否决**——F1 变动 10 篇，其中 9 篇只丢富岭 SOP 模板样板（净收益），但 **1 篇真内容损失**：`WPQH230467G…堆肥证书`（20 页）的 p2「注意事项」与 p3「检验报告」被删，那是**页面标题不是页眉**（同一「检验报告」在 p4–p20 位于 top≈100.9，不受影响）。判据"跨了裁剪线"≠"是页眉模板"。F1 需要更精确的谓词，列为独立后续项，该证书即现成测试用例。

**验证证据**：

- **49 篇本地真实 PDF 前后普查**（纯文本路径 `_extract_with_pdfplumber`，不触发 VLM）：49 文件 / **44 唯一 SHA**、**0 抽取失败**、**0 字符丢失**、**恰好 1 个 SHA 输出变化**（WI-007：blocks 23→24，最大块 y 跨度 **413→74**）。
- **合成夹具 hermetic 回归**（`tests/fixtures/two_column_header_straddle.pdf`，19KB，PyMuPDF 生成，生成器在 `make_fixtures.py`）：三层 11 个断言——L1 钉几何前提（`crop_top==95`、残词跨线且裁剪后钳位、`_detect_column_split` 非空）、L2 钉分块（两块分离 + y 区间 + 正向 gap 仍切段 + 双栏阅读序不变 + 零文本损失 oracle）、L3 钉端到端 `image_refs`（真实 `_inject_image_ref_blocks` + `DocumentChunker`，伪 assets 无需 VLM）。**旧判据下 3 个断言失败、新判据下 11 全过** —— 夹具确实复现故障，不是碰巧通过。
- **L4-ingestion A/B**（两臂独立同源缓存副本，快照 sha256 `7629e6fd…`）：

  | fmt | A（无 D2） | B（有 D2） | 硬闸 | 基线地板(−0.03) | |
  |---|---|---|---|---|---|
  | pdf | 0.8111 | **0.8778** | ≥0.78 | ≥0.781113 | ✅ |
  | docx | 0.9898 | 0.9898 | ≥0.95 | ≥0.9591 | ✅ |
  | xlsx | 1.0 | 1.0 | ≥0.85 | ≥0.861665 | ✅ |

  `img_dup_p95` 1.0→1.0；errors 0→0；**50 篇里仅 1 篇变化**（pdf_xs_wi_007 0.5556→0.7778），**非 PDF 文档零溢出**。
  ⚠️ 口径说明：`baseline.compare()` 在本次只跑 ingestion 支柱的结果上短路为 `REGIME MISMATCH`（serving 侧 regime 字段未填），**不是**真的指纹漂移；上表用的是它自己的 `delta=0.03` 与 `extract_metrics()` 口径。正式发布门仍需完整 `make release-gate`。
- `make test` 3259 passed / `make lint` / `make sim-dag1` / `run_simulation --dag 1 --scenario embedded_images` 全 exit 0（后者才真正走 PDF 抽取路径，`sim-dag1` 默认场景只用 mock DOCX）。
- 真实文档旁证（opt-in，不进 `make test`）：`scratch/verify_pdf_d2_on_real_wi007_20260725.py` → 步骤3 `[26,27,28]`、步骤4 `[29]`，与 GT 一致。

**PDF 剩余空间与前提**：修完后 0.8778，距满分仍有 12.2pp，但其中 3.33pp 是评测匹配问题（步骤5.1）、3.33pp 需要语义绑定（步骤2）、约 2.2pp 是一对互相抵消的跨页需求（`pdf_sop 3.1` 需要跨页图、`it_xxh 第六步` 必须拒绝跨页图，粗规则净收益为零）。**PDF 金集只有 3 篇 / 20 题，1 题 = 3.33pp** —— 继续在这条线上优化前应先扩 GT，否则容易拟合噪声。

**顺带记档（未改）**：`eval_harness/report.py` 里 PPTX 标为 `soft`，但该 gate 没有 `advisory=True`，strict 模式对任意非 advisory 的 `pass=False` 都阻断 ⇒ "soft" 目前只是文案不是语义。`_pass1_analyze` 的频次阈值 `max(2, int(num_pages*0.6))` 是向下取整，并不严格等于注释所称的 ≥60%。

---

## 八、评测匹配器 M1–M4：先修尺子（2026-07-25）

继续在 PDF 绑定上抠分之前先修尺子——**剩余 12.2pp 里有相当一部分根本不是管线缺陷**。33 条 judge_bundle 里 **6 条卷入"同一张产出卡被多条 GT 命中"**，分 3 簇，成因**各不相同**：

| 簇 | 成因 |
|---|---|
| `xs_wi_007` 步骤1+2 | 过滤顺序倒置 |
| `pdf_sop` 步骤4.1+4.2 | 同上 |
| `xs_wi_007` 步骤5.1+5.2 | 竞争无仲裁（两卡均 step_no=5、无 section_no） |

### M1 结构标签必须先于图片偏好（核心；这是**评测泄漏**）

`_match_gt_chunk_to_produced` 的 `with_imgs` 硬过滤（"GT 期望有图 ⇒ 只留含图候选"）排在**结构标签过滤之前**。于是"真卡恰好无图"时，真卡被提前淘汰、结构过滤在残池里再也找不到它。

**逐级打印候选池的实测**（GT「步骤1 按产品标识卡清点实货」，期望图 `[1,2]`，标签→`step_no=1`）：

| 候选 | recall | density | imgs |
|---|---|---|---|
| `step_no=1` | **1.00** | 0.667 | `[]` |
| `step_no=2` | 0.67 | 0.187 | `[1,2]` |

`with_imgs` 先跑 → **recall 满分、step_no 精确命中的真卡被淘汰** → 落到 density-max → 选中步骤2 的卡。**GT 步骤1 靠匹到别人的卡拿了假 1.0**，同时把步骤2 挤成 0。`pdf_sop` 的 `sec=4.1`（真卡无图）同形。

> 这条的本质：**用被测对象（有没有绑到图）去挑选题目**。

**但不能简单把 `has_imgs` 降级到 recall 之后**——codex 评审提的方案 B 被实测否决：`it_xxh_003`「第一步 安装CPU处理器」（该文档 GT label 是「第N步」形态，抽不出结构号）池内两张卡 **recall 完全并列 0.83**，一张 `imgs=[]` density 0.248、一张 `imgs=[8]` density 0.219 —— 纯 recall→density 会选中无图那张得 0。

**定案的判定链**：`covering → chunk_type → 结构标签(sec_no > step_no) → recall → has_imgs 仅作 recall 并列裁决 → density`。GT 显式负例不参与该裁决（否则等于奖励"给不该有图的步骤绑了图"）。

### M2 no-cover 真弃权

旧实现无 covering 时"任选 recall 最高者"，哪怕 recall=0 —— 一条完全没匹上的 GT 仍可能因图片偶合拿到非零甚至 1.0。改为返回 `MatchResult(None, below_threshold)`，记 0 进分母，并产出规范化空的 bundle 条目（旧实现里弃权直接 `continue`，在 bundle 中完全消失）。**实测爆炸半径 = 0**：63 条 strong GT 中 max recall < 0.3 的有 **0** 条，今天一分不动，是纯加固。

### M3 `l4_ingestion_evaluator_version` 进 regime（**关键**）

`_regime()` 记录 `code_commit` 但 `_REGIME_KEYS` **不含它**（它对任何无关提交都变，进闸即噪声源）——结果**"改了尺子"与"改了管线"在差量网里完全无法区分**，新尺子会静默比对旧 baseline。新增语义版本键（`1.0.0` = 本次之前的隐式口径，`2.0.0` = M1+M2），**刻意不进** `_LENIENT_REGIME_KEYS`：老 baseline 缺该键即 mismatch，强制重冻。

⚠️ **接受的 blast radius**：`regime_matches` 是全局判断，mismatch 会在指标比较**之前**短路 ⇒ **重冻之前 `--strict` / release-gate 会硬失败**。这正是目的，但它会挡发布——重冻时机是 Sam 的决定。

### M4 碰撞留痕，**不**引入 consumed set

many-to-one **有时是合法的**：chunker 真把两个 GT 子步骤合成一张卡时，两条 GT 都该打在它上面、各得部分分，那正是对"合并"的正确惩罚；一刀切拆开是假阴性。所以只按稳定 `chunk_id` 做**两遍**记账（第一遍匹配建 `chunk_id → [GT labels]`，第二遍写分数与 `shared_with_gt`），把 many-to-one 从"看不见"变成"可审计"。定位是**人工审计证据**，不进任何自动 gate。

### 实测结果（两臂同缓存，证据落盘 `docs/evidence/l4_matcher_20260725/`）

| fmt | 旧匹配器 | 新匹配器 |
|---|---|---|
| **pdf** | 0.87778 | **0.82778** |
| docx | 0.9898 | 0.9898（strict 路径，不走本匹配器） |
| xlsx | 1.0 | 1.0 |
| img_dup_p95 | 1.0 | 1.0 |

bundle 33 → 33 条，**逐题只有 2 条变化**：`步骤4.1` 0.5→0.0、`步骤1` 1.0→0.0。碰撞簇 **3 → 1**（仅剩 5.1/5.2，且已带 `shared_with_gt` 留痕）。`match_status` 全 `matched`（0 弃权，与 M2 实测半径一致）。

**分数下降是本次的预期结果，不是回归**：它把两个被掩盖的真实绑定缺陷（步骤1、步骤4.1 —— 这两张卡本该有图却没有）从"假 1.0 / 假 0.5"变成可见的 0.0。硬闸 0.78 余量 4.78pp、基线地板 0.781113 余量 4.67pp，均不破。

**剩余未做**：`步骤5.1/5.2` 的竞争性碰撞需要仲裁，但"合法合并 vs 竞争抢占"目前无法可靠区分，而"让落败者重新挑"若以 jaccard 为准则又是评测泄漏 —— 等 GT 扩充后再做。GT 侧应提供稳定 `section_no`，不再依赖从 label 文本猜测。

---

## 九、PDF GT 扩充：3 → 6 篇（2026-07-25）

**标注方法（防自证）**：只依据**文档正文的显式图号引用 + 图片 OCR/visual_summary 内容 + 同页共现**，**绝不看 chunker 产出**；歧义处开图逐张核对。每行的证据写进 `_note`。工具链沿用既有 `gen_image_manifest` → 人工标注 → `validate_gt_refs`。

| | 之前 | 之后 |
|---|---|---|
| PDF 文档 | 3 | **6** |
| strong 行 | 30 | **48** |
| 每题权重 | 3.33pp | **2.08pp** |

新增：`pdf_zs_wi_009`（注塑发货拖柜，9 行/13 图）、`pdf_u8_unapprove`（U8 弃审，6 行/6 图）、`pdf_travel_subsidy`（员工路费补贴，4 行/2 图）。`validate_gt_refs --strict` 6 篇全过。

**两处刻意不编造**：
- `travel_subsidy 步骤1` 引用的花名册截图**不在文档图片资产里**（漏斗只弃了 3 条装饰条带，均非表格）→ 标 weak（不进主闸分母），不硬猜。
- **《吸管车间盘点》未收**：它的步骤是跨 3 页的长散文（步骤2 从 p1 拖到 p3、牵涉 5 张图），而 GT 一行只能匹一张产出卡 ⇒ 这种行**天生拿不到满分**，会往尺子里掺噪声。收它之前得先定"跨页长步骤怎么切"的口径。

⚠️ GT 文件与 manifest 都在**仓外数据仓 / 未跟踪目录**（与既有 7 份 manifest 同约定）；GT 已备份为 `gt_pdf_analysis.json.bak-pre-expand-20260725`。GT 变动会移动 `l4_gt_sha`（regime 已因 M3 mismatch，重冻一次覆盖两者）。

**扩充当场的回报**：新增 18 行里 12 行满分，同时**立刻揪出两个真缺陷** —— 下面的 PDF-D3，以及 `zs_wi_009` 的 image 5 跨步骤错位（查询条件界面本属步骤3，被绑到步骤2）。后者未修，留档。

---

## 十、PDF-D3（已修复）纯数字标注号被判成 heading，孵伪步骤卡抢图 —— 【代码】

**根因**：`《U8弃审》作业指导书` 的图内标注号是**纯阿拉伯数字 1–24**（不是圈号 ①②③），以标题字号渲染 → PDF 的**字号启发**判成 heading。`is_pseudo_heading` 只 veto 圈号，纯数字不在其列。

**两层危害**：
1. `section_path` 污染 —— 真步骤段的章节变成 `'7'` / `'13  16'`；该值会进 chunk 文本的「章节：」前缀（→ embedding）、RDS `chunk_meta`、HA3 字段、retriever 的同章节伙伴保留、LLM context 展示、API 溯源与 QA 血缘；
2. chunker 按 heading 建步骤组 —— 孵出正文只有 `"18"`/`"21"`/`"22"` 的 `step_card`（步骤正则把 `"18"` 回溯成编号 `"1"`+尾字符 `"8"` ⇒ `step_no=0`），**把图全抢走**，真正的 `step_no=4/5/6` 卡 `image_refs` 为空。

**修法（codex 六阶段评审 FULL CONSENSUS，thread `019f9cc3`）**：新增独立判据 `is_bare_numeric_callout`（整行去空白后全为整数，含多 token 的 `13  16`；含点的 `4.1` 刻意不匹配），**只接进 `pdf_extractor` 的 `looks_callout`**。

> **刻意不并入 `is_pseudo_heading`** —— 它是 PDF/DOCX **共享接口**（`docx_extractor.py:54,83`），而我的 49 篇普查只覆盖 PDF，拿它断言"改共享函数安全"是无效外推。DOCX 的 heading 来自样式（作者意图），纯数字标题在那里未必是误判。

`looks_callout` 同时 veto **字号与加粗两路**（第三路中文正则 fallback 不受影响 —— 纯数字本就不命中它）。`EXTRACTOR_VERSION` 1.1.0→1.2.0、`DETECTOR_VERSION` 1.0.0→1.1.0（输出与边界检测器都变了）。

**验证**：
- 49 篇本地真实 PDF 前后普查：**仅 1 篇变化**（就是该文档，blocks 34→20、heading 22→0）、**零字符丢失**（降级不是删除，数字仍以段落留在正文）；
- L4-ingestion：**pdf 0.81598 → 0.87848**，三条 0 全变 1.0；docx 0.9898 / xlsx 1.0 / `img_dup_p95` 1.0 **均不动**，**其余题一条未动**；该文档 `section_title` 从 `'7'`/`'13  16'`/`'20'`/`'21'` 变为 `None`；
- 新增 28 个测试（判据正反例含全角数字、PDF 字号/加粗两路各一、点分标题反向钉、section_path 不污染、降级不删字、**DOCX 共享接口语义未变** + `_detect_heading_level("Heading 1","18")==1`）：**旧判据下 4 个断言失败、新判据 28 全过**；
- `make test` 3303 passed、`make lint`、`make sim-dag1`、`--scenario embedded_images` 全 exit 0。

**实现取舍记录**：原计划做合成 PDF 夹具（同 PDF-D2），但 pdfplumber 的 text-策略表格检测会把规整的合成文字网格整页吞成 table，反复调坐标只会让夹具变脆 ⇒ 改用 **stub page 驱动 `_pass2_extract_page`**（它只用到 width/height/crop/find_tables/extract_words 五个口）。真实 PDF 路径的覆盖由 49 篇普查 + 目标文档的 L4 承担。

**已知次要（未修）**：降级后的数字作为尾随噪声留在步骤卡正文（`…点击确定见图20 18`）。圈号有 `extra.circled_label` 几何标记可被 chunker 吞掉，纯数字没有等价标记；要做得给 pdf_extractor 增加 callout 几何标记，属独立改动。

---

## 十一、PDF-D4（已修复）圈号归属只守了一半，游离箭头指示符被当图号 —— 【代码】

**根因**：策略 1b（`_insert_image_ref_blocks` 的"覆盖层圈号归属"）用几何包含把页面上的独立圈号标记归属到图片，若一张图**恰好只拥有一个**圈号，就把它当作"这张图的图号"，再去正文里找提到「图X」的步骤块插入。它守住了"**一个标记点**同时落入多张图"（`len(containing) == 1`），**没守对称的另一半**：

> **同一个圈号由多个独立标记点分别落入不同图** —— 每张图各自都"唯一拥有"它，于是全被当成"图X"，一起被拖到引用「图X」的那个步骤。

实证 `FL-ZS-WI-009` p1：三个游离 `①`（画在截图上的箭头指示符）中，两个分别落在 image 2 与 image 5 内 ⇒ 两者都被认作"图①"，而正文「（图①）」在步骤2 ⇒ **步骤3 的『货位存量查询』查询条件截图被拖到步骤2**。

关键放大器：**1b 命中即互斥屏蔽几何**（`geo_assets` 只从 `page_fallback` 构造）。纯几何本会**正确**锚到步骤3（与其 y 重叠 16.46pt 为最大，其次 `⑦仓库编码` 15.10、`⑧数量` 10.45）—— 错误结论压过了正确结论。

排除的其他路径（均实测未触发）：`annotation_num` 六张图全为 `None`；`[img-reconcile]` 日志未出现（Path C 未触发）；`RAG_IMAGE_CONTENT_OVERRIDE` 默认 OFF（Path A/B/D 未触发）。

**修法（codex 六阶段评审 APPROVE，thread `019f9ce7`）**：加反向歧义守门。判据口径三条都是语义的一部分：
1. 歧义 owner 集合**只**由 overlay 反向构造 `(page, char) → set(id(image))`，**不**把 `vlm_annotation_map` / `visual_summary` 的候选计入"多少张图认领"；
2. 一旦判歧义，该字符对该页 **三源全部弃用** —— 歧义是"该字符在该页不能唯一指图"，与证据源无关；只堵 overlay 会被后两级回退**绕回**（这个洞是 codex 指出的）；
3. **先过滤歧义字符、再做各源 `len(...) == 1` 唯一性判断**，同源里未歧义的其他字符仍可用。

同一张图内重复同字符**不算**跨图歧义（`set()` 后只贡献一个 owner），它本就被 `len(overlay_circled) == 1` 挡掉，仍可经下一级证据走 1b —— 这条由专门的可观察测试钉死（同图两个 `①` + 该图唯一的 map `①` ⇒ 仍应走 1b），否则无法区分"落空于 overlay 长度"与"落空于误判歧义"。

`CHUNKER_VERSION` 1.0.0→1.1.0（改的是 step_card 的 `image_refs` 与随之进 chunk_text 的图注，属分块阶段输出；`versions.py` 里过窄的说明一并订正）。顺带订正 1b 注释里"±4pt"的漂移（实现早已是严格 `_OVERLAY_TOL=0.0`）。

**验证**：
- L4-ingestion **OFF arm（生产姿态）：pdf 0.87848 → 0.89236**；`步骤2` 0.6667→1.0（产出 `[2,3,5]`→`[2,3]`）、`步骤3` 0.6667→1.0（`[4,6]`→`[4,5,6]`）；docx 0.9898 / xlsx 1.0 / `img_dup_p95` 1.0 均不动，**其余题一条未动**；
- **ON arm（`RAG_IMAGE_CONTENT_OVERRIDE=1`）：相对 OFF arm 0 条退化**，image 5 仍归步骤3（codex 要求的验收项——回落的图在 ON 环境会继续吃 Path A/B/C/D，需确认不被二次改坏）；
- 新增 5 个 tracked 单测（新故障形态 / map 与 summary 两个绕回分支各一 / 同图重复不算歧义的可观察断言 / 单一 owner 正例不回退）：**旧代码下 3 个失败、新代码 5 全过**；既有 `test_figure_label_binding.py` 19 个保持绿；
- `make test` 3308 passed、`make lint` exit 0；
- **`python -m opensearch_pipeline.run_simulation --dag 1,2 --scenario embedded_images` exit 0** —— codex 指出 `node_chunk_documents` 属 **DAG2**（`dag_definitions.py:122`），此前几轮验证清单里的 `make sim-dag1` 根本没覆盖本改动的路径，这是实质纠正。

**未做（记档）**：`anno_ref_index` 在同页同字符被多个正文块引用时静默 first-wins —— 是**另一种失效模式**（引用侧歧义），且当前 6 篇 GT PDF 扫描下来**零实例**，无证据不动。`_vs_circled_re` 的语境词表接受"箭头…①"，本次由歧义守门间接缓解，词表本身未改。Path C 注释存在漂移（写"OCR+map"/bigram≥8，实为 OCR-only/≥3），不在本次编辑区，未夹带。

---

## 十二、`RAG_IMAGE_CONTENT_OVERRIDE` 评估（2026-07-25）——附一个更要紧的发现

**背景**：D3/D4 修完后 PDF 仍有 4 个 0 分题，逐条看**全是同一类**：`pdf_sop 步骤4.1`（图10 该在 4.1 卡、实在 4.2 卡）、`xs_wi_007 步骤1/步骤2`（图1,2 该属步骤1、实在步骤2）、`xs_wi_007 步骤5.1`（图30 按 XS-D06 该属子流程 1)、实在 2) 卡）—— **语义归属 vs 版面位置**。而 `RAG_IMAGE_CONTENT_OVERRIDE`（默认 OFF，一次 gate 住 Path A/B/C/D 四条机制）正是治这一类的。

口径澄清：flag 注释说的"评测开"指专用 A/B 工具 `chunker_ab.py`，**标准 `run_eval` 不设该 flag**，baseline 也冻结于 OFF —— 本节所有数字口径一致。

### 2×2 实测（L4-ing pdf 支柱，6 篇 GT / 48 strong 行）

| | 暖缓存（caption 复用） | 冷缓存（caption 全部重掷） |
|---|---|---|
| **OFF（现网姿态）** | 0.89236 | **0.84722** |
| **ON** | **0.96528** | 0.89931 |

- **ON 的增益是真的**：暖 **+7.29pp**、冷 **+5.21pp** —— 两种 caption 制度下都为正。
- 暖缓存 ON 修掉全部 4 个语义/位置缺陷（`步骤4.1` 0→1.0、`步骤4.2` 0.5→1.0、`步骤1` 0→1.0、`步骤2` 0→1.0 —— 最后一条因 empty-vs-empty 不进 judge bundle 而"看不见"），**0 条退化**；docx 0.9898 / xlsx 1.0 / `img_dup_p95` 1.0 全程不动。
- **三轮 warm-ON 逐题签名 byte-identical** —— 同缓存重复跑稳定。

### ⚠️ 比 flag 决策更要紧的发现：caption 耦合在 **OFF 姿态下就已存在**

我原本假设 OFF 下绑定是纯几何、与 VLM 无关 —— **实测推翻**。冷缓存 OFF 掉 **4.51pp**，而且**不只是评测匹配器换了卡，绑定本身就变了**：

```
zs_wi_009 暖缓存: 步骤2=(3,2)   步骤3=(4,5,6)
zs_wi_009 冷缓存: 步骤2=(3,5)   步骤3=(4,6)     ← image 5 又跑回步骤2
```

即：**同一份 PDF、同一份代码，只要 caption 重掷一次，图↔步骤绑定就可能变** —— 这是 xlsx 绑定漂移那场战役的同款机制，活在 PDF 侧、活在**当前生产姿态**里。ON 只是把幅度放大（−6.60pp vs −4.51pp），**不是它引入的**。

caption 何时会重掷：新文档首次摄取、缓存条目被容量淘汰（`RAG_VLM_CACHE_MAX_ENTRIES=50000`）、`RAG_VLM_CACHE_VERSION` 提升、VLM 换代。

### 结论与建议

1. **flag 本身值得开**：两种 caption 制度下增益都为正（+5.2 ~ +7.3pp），0 退化，同缓存 byte-stable，且这正是 D8 战役当年留的 chip（"扩 3 doc 多轮稳定后切默认 ON"，现已 6 篇）。
2. **但开之前建议先补溯源**：`build_run_provenance()` 目前**不记录该 flag 的值**，翻转后新旧 chunk 在 provenance 里无法区分。这是个小改动，但没有它就无法回答"这批 chunk 是在哪种绑定制度下产出的"。
3. **真正该排期的是 caption 耦合本身**（与 flag 无关）：绑定不该是 VLM 措辞的函数。方向是让绑定只吃**稳定特征**（圈号、bbox、OCR 中的强标识如 XS-D06/单据号），而不是 caption 措辞的 bigram 重叠。这条比再抠几个 pp 更值。
4. 证据落盘 `docs/evidence/content_override_eval_20260725/`（四组 per-row 产出 + summary）。

**未做**：`步骤5.1/5.2` 的竞争性碰撞仲裁 —— 实测证明它**不改任何分数**（仲裁后 5.1 会匹到它真正的语义卡「1）」，产出 `[41]`→`[]`，仍 0.0），收益仅限 judge bundle 的诊断准确性，故按用户拍板暂缓。

---

## 十三、PDF-D5（已修复）caption 耦合——重复圈号 fail-closed，判据改建在文本层

第十二节测到"caption 重掷 → 绑定漂移"在 OFF 姿态下就存在。**根因链条不是"caption 喂 bigram"，而是经由漏斗路由**：

> caption 重掷 → 漏斗对同一张图的**留/弃判定变**（`pdf_zs_wi_009` 冷抽下 image 2 被丢弃）→ 能"认领"覆盖层标记的图集合变 → **D4 的"几张存活图各自认领它"歧义判据失效** → image 5 又被 1b 拖回步骤2。

而**页级标记计数是文本层属性、caption 无关**：暖/冷两次抽取都给出 `{(1,'②'):1, (1,'①'):3, (2,'⑫'):1, (2,'⑬'):1, (3,'⑮'):1}` **逐字相同**，存活图集却不同（`it_xxh_003` 冷抽丢了 12/23/29/31，`zs_wi_009` 丢了 2）。

**修法（codex 明确选 (a)，thread `019f9ce7`）**：把歧义判据从「几张**存活图**认领它」换成「该页**出现几次**」（基于 `label_points` = `pdf_extractor` 的 `circled_label` 块）。它**包含** D4 的判据且不依赖漏斗。

判据的正确表述（codex 纠正了我的说法）：这**不是**"两次出现必然指向不同图"的数学推导（两个点可能都在同一张图内），而是**可采信门**——1b 会压过几何，所以只接受**页内唯一出现**的圈号作为高精度 figure-ID 证据；重复即 fail-closed，而不是从不稳定的存活图集里"恢复唯一性"。

⚠️ **声明收紧**（同样是 codex 纠正）：本守门只消除「重复圈号歧义判定」对存活 asset 集的依赖，**不**等于 1b 已 caption-independent —— 1b 仍只收 `ROUTE_TO_VECTOR/TEXT` 的资产，且 `vlm_annotation_map` / `visual_summary` 两级证据本身就是 VLM 产物。

**语义变更**：同一张图内重复同字符现在**也**判歧义，D4 时代那条相反语义的测试已按裁决改写。

**验证**：
- **同一份缓存快照重放 D4 vs D5**（消除 VLM 重掷随机性，codex 要求的对照）：`0.84375 → 0.85417`，**差异只有目标两行**：`步骤2 [3,5]→[3]`（0.333→0.5）、`步骤3 [6,4]→[5,6,4]`（0.667→1.0）；
- **暖缓存零回归**：OFF 0.89236 / ON 0.96528，与 D4 完全相同；
- **漂移性质**：D5 下 warm↔cold 的**每一条差异都精确对应"该图被漏斗丢弃"**，**零条"图跑到别的步骤"**（`步骤3 [4,5,6]→[5,6,4]` 集合相同）；D4 下 image 5 是真的跑了；
- 测试：改写"同图重复"用例 + 新增**核心因果钉子**（同一组 blocks，assets 从 `[2,5]` 变成 `[5]`，两次都必须 image 5 留在步骤3）+ 跨页隔离用例。**D4 判据下 2 个失败、D5 下 7 全过**；既有 `test_figure_label_binding.py` 19 个保持绿；
- `make test` 3310 passed、`make lint`、`--dag 1,2 --scenario embedded_images` 全 exit 0；`CHUNKER_VERSION` 1.1.0→1.2.0。
- 证据落盘 `docs/evidence/caption_coupling_d5_20260725/`。

**方法论声明**：两次独立冷跑各自重掷 caption，**分数不可互比**；因此因果结论全部来自「同一缓存快照重放」与「各自 warm↔cold 内部的漂移性质」。

### 残余（本次未动，属另一层）

**漏斗的留/弃判定本身随 caption 重掷而变** —— 冷抽下 5 张图被丢弃（`it_xxh_003` 的 12/23/29/31、`zs_wi_009` 的 2）。这是 VLM 分类稳定性问题，不在绑定层，修法与本次完全不同（例如把 funnel 结论纳入缓存的稳定判据、或对 borderline 图引入多数表决）。D5 之后绑定对"给定存活图集"是确定的，不确定性整体上移到了这一层——**这是当前 PDF 侧最大的确定性缺口**。

---

## 十四、`RAG_IMAGE_CONTENT_OVERRIDE` 默认翻 ON（已落地，2026-07-25）

Sam 拍板同意开启。第十二节的 2×2 是依据：暖 OFF 0.89236 → ON 0.96528、冷 OFF 0.84722 → ON 0.89931，**两种 caption 制度下增益都为正**，0 条退化。这也正是 D8 战役当年留的 chip（"扩 3 doc 多轮稳定后切默认 ON"，现已 6 篇）。

**改动**：
1. 新建轻量模块 `opensearch_pipeline/ingest_flags.py` 作**单一来源**（codex 指出不能让 `versions.py` 这个 pure 模块反向 import 8000 行的 `pipeline_nodes`；依赖方向固定为 `pipeline_nodes → ingest_flags ← versions`）。四个 gate 全部改调它，顺带消掉原实现不 `strip()`、`"on"` 不算真两个坑。
2. **默认 ON，只认显式 off 值关闭**（`0/false/no/off`，大小写与首尾空白不敏感）。未知值保持 ON —— 默认 ON 的开关若"配错就关"，会静默退回旧行为且无人察觉。
3. `CHUNKER_VERSION` 1.2.0 → **1.3.0**。
4. **provenance**：`build_run_provenance()` 增 `image_content_override`（存**有效布尔值**，不是原始字符串），并**显式**加进 `chunk_meta.extra_json._provenance` 的固定白名单 —— codex 指出光加 key 两处都不落盘。
   ⚠️ **本次只落 chunk 级**；`pipeline_run` 是逐列清单，加列需迁移 059（052–058 被另一分支占），列为后续项；同时记档一个既有缺口：正式 DataWorks 节点直接调 `run_stage_drained()`、绕过 `run_start/run_finish`，因此生产 DataWorks 路径本来就不写 run header。

**测试姿态必须显式固定**（翻默认后"不设 env"不再等于 OFF）：`test_overlay_label_cross_image_ambiguity.py` 的 autouse fixture 从 `delenv` 改 `setenv("0")`；`test_figure_label_binding.py` 与 `test_pdf_column_gap_binding.py` 各加 autouse `setenv("0")`；`chunker_ab.py` 的 **CLI 示例/help/docstring** 一并改（`off:` 只表示"不覆盖、继承默认"，OFF arm 必须写 `off:RAG_IMAGE_CONTENT_OVERRIDE=0`，否则两个 arm 都跑 ON、A/B 静默失真）。

**验收（同一 warm-cache 快照，逐题签名而非只比总分）**：

| 姿态 | pdf | 逐题签名 |
|---|---|---|
| 默认未设 | 0.96528 | `3bd45a97…` |
| 显式 ON | 0.96528 | `3bd45a97…` |
| 显式 OFF | 0.89236 | `d787892d…` |
| 历史 OFF | 0.89236 | `d787892d…` |

① 默认未设 **== 显式 ON**（签名相同）✓　② 显式 OFF **== 历史 OFF** ✓　③ ON 相对 OFF **零退化** ✓
`make test` 3336 passed、`make lint`、`python -m opensearch_pipeline.run_simulation --dag 1,2 --scenario embedded_images` 全 exit 0。证据落盘 `docs/evidence/content_override_default_on_20260725/`。

### ⚠️ 部署前置与回滚（Sam）

- **先发含本 commit 的新包**，不要在旧包上直接注入 env —— 那会产出 ON 结果却标 `CHUNKER_VERSION=1.2.0` 且无 flag provenance，事后无法归因。
- **部署前只读核对 SAE/DataWorks 控制面**是否已显式设 `RAG_IMAGE_CONTENT_OVERRIDE=0/false`；若已设，代码默认翻转不生效（仓库无法证明控制面现状）。
- **回滚**：设 `0` 只影响之后的 Stage2。已入库 chunk 要真回滚，须对受影响 doc 集做**冻结分类的 OFF re-chunk + Stage3 重索引 + count/type_mix manifest 门**。
- **不重灌存量**：本次只让未来 ingest 默认 ON。
