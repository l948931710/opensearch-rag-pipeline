# 盲区审计修复台账（P1 + P2 + P3）

> 依据：`docs/architecture_blindspot_audit_2026-07-05.md`（55 条）。本文件跟踪 P1×2 + P2×34
> + P3×19 的处置状态。状态：✅ 已修复（代码合入）· 🟡 部分修复（代码侧可落的最大程度，
> 剩余 user-gated）· 📋 延期（需用户拍板/基建，附论证）。
> 全部修复的公共 user-gated 尾巴：**SAE/DataWorks 重打包部署** + **schema/017、018 apply 生产**。

## P1

| # | 摘要 | 状态 | 落点 |
|---|---|---|---|
| P1-1 | 熔断触顶静默 + SLO 假绿 | ✅ | rate_limiter 触顶 critical 告警×2 触发点；拒绝聚合落 qa_admission_reject（schema/017）；qa_rollup `global_cap_rejected` 专项 breach。**有意分叉**：比率分母不掺拒绝量（per_min 反滥用会造假告警），docstring 论证 |
| P1-2 | 转人工只写不读 | ✅ | notify_escalation（归属部门管理员/kb_admin 兜底）+ console 工单队列（按龄）+ 答复钉钉推回提问者 + 文案改诚实 |

## P2

| # | 摘要 | 状态 | 落点 / 论证 |
|---|---|---|---|
| P2-1 | kb_retire 不清 HA3 | ✅ | retire 喂 PENDING_DELETE outbox（与 set-visibility 同握手）；restore 对称撤销 |
| P2-2 | 批处理覆盖 PENDING_DELETE 握手 | ✅ | deactivate/update_index_status 收尾改 CAS（仅 PROCESSING→终态），跳过即保令牌交 reconcile |
| P2-3 | HA3 丢失有检测无补救 | ✅ | scripts/rebuild_from_rds.py（dry-run 默认 / --commit 置 NOT_INDEXED 交 stage-3 重推） |
| P2-4 | 嵌入宕机无 BM25 兜底 | ✅ | retriever 降级纯文本检索（RAG_DEGRADED_BM25_ENABLE 默认开；ACL 过滤原样保留；结果带 degraded 标志） |
| P2-5 | 无 PIPL 主体擦除路径 | ✅ | retention.purge_subject（跨 5 表硬删；dry-run 默认 + env_guard 双门）。离职钩子/内存 session 清除 = 后续 |
| P2-6 | 驻留守卫键于标签 | ✅ | 与 P2-28 合并修复：供应商守卫按物理指纹 OR 标签触发 |
| P2-7 | 热门问题/改写池跨部门泄露 | ✅ | 两聚合查询加 user_dept cohort 谓词 + 分部门缓存；匿名只给静态兜底 |
| P2-8 | 嵌入模型契约缺失 | 🟡 | RDS 契约行（rag_runtime_contract，schema/018）：stage-3 写 / serving 启动比对 + /api/ready 降级。**HA3 文档级模型戳需整表重建**（modify_table 不能加字段）= user-gated |
| P2-9 | 嵌入缓存键无维度 | ✅ | 键加 dimension + OSS 镜像按 model/dim 命名空间（存量缓存等价冷启动） |
| P2-10 | CostBreaker 每批重置 | ✅ | run_stage 注入共享实例，drain 全程一个预算台账 |
| P2-11 | 熔断计数重启归零/不计权重 | ✅ | 准入量随拒绝同批持久化（__admitted__ 行）+ 重启回种（max 语义）；深思按 RAG_GLOBAL_CAP_THINKING_WEIGHT=8 计。DB kill 开关延期（见 P2-12） |
| P2-12 | 无「今日 DashScope 总花费」owner | 📋 | 统一支出台账 = 跨两平面全调用点插桩的独立项目。本批已收窄逃逸口：P2-10（run 预算真聚合）+ P2-11（serving 计费权重+持久化）+ 嵌入缓存减少重复调用。完整台账建议单独立项（设计：RDS 按北京日 ledger + 每调用方预留 + 全局 kill 标志） |
| P2-13 | 零/塌缩流量假绿 | ✅ | 基准=近 4 周同星期日中位数；zero_traffic / traffic_collapse 两 breach（bootstrap 无历史不误报） |
| P2-14 | 无死人开关 | 🟡 | ops_monitor 每跑写心跳（rag_runtime_contract）；kb_console 治理看板读心跳 >26h 亮红 + 兜底告警（serving=被动监工）。**真外部死人开关（cron-ping 服务）= user-gated 基建** |
| P2-15 | 无摄取漏斗监控 | ✅ | queue_monitor.run_ingest_funnel_check：卡 LOADING/PROCESSING 超时 / 注册超龄未入索引 / NEEDS_REVIEW·FAILED 积压，入 ops_monitor 作业集 |
| P2-16 | 单管理员直发 public 投稿 | ✅ | dept_admin 采纳 public → 转 kb_admin 复核（与上传涉公开同纪律）；kb_admin 采纳直通 |
| P2-17 | `<<IMG:N>>` 投稿注入 | ✅ | 投稿正文 submit/accept 双侧转义标记。渲染侧标记↔源 chunk 绑定改造 = 后续（load-bearing 契约） |
| P2-18 | created_at 存储时区争议 | 🟡 | 机制声明属实（qa_logger 不写 created_at、池不 pin tz），但推论与生产多次实测（太平洋 +15/16h）矛盾 → **不改存储**，rollup 每跑探测 @@time_zone 落日志+report，地面真相确认后再决策迁移 |
| P2-19 | 周报 +15h 与 rollup 分桶分叉 | ✅ | weekly_qa_report 改用 qa_rollup 的 DST-correct 区间助手（fail-open 回退旧式） |
| P2-20 | prompt 不可重建 | ✅ | qa_session_log.gen_meta_json（schema/018）：装配后 prompt sha + 生效规则 flags |
| P2-21 | 采样参数不落库 | ✅ | gen_meta_json 记 temperature/top_p/model。**不改采样行为本身**（质量决策未拍板） |
| P2-22 | 检索制度不落库 | ✅ | gen_meta_json 记 fusion/权重/阈值/rerank/top_k/嵌入模型+维度指纹 |
| P2-23 | 发布门 goldset 与基线失配→差量网静默关闭 | ✅ | gate 预检 sha 失配 FATAL + strict 路径 regime_mismatch 硬失败（非 strict 语义不变）。**生效后需 refreeze 或 RAG_EVAL_GOLDSET=golden_50** |
| P2-24 | judge 模型不在制度指纹 | ✅ | run_judge pin --model（RAG_EVAL_JUDGE_MODEL）；regime 加 judge_model/rubric_version（老基线宽容窗口） |
| P2-25 | 基线覆盖洞（L0/L2/L5/L6/负例） | 🟡 | extract_metrics 增 16 类指标 + coverage 闸（informational 不阻断）。**refreeze 需在用户机器跑 live eval**（沙箱 403） |
| P2-26 | 发布门无源级完整性 oracle | 📋 | 需「源文档→期望 chunk/图片数」独立锚（L4 再抽取或人工 GT 扩容），是 eval 工程项目而非补丁；现有缓解=GT chunk-eval 30-doc 基线 + L6 边界族 + P2-25 的 coverage 闸。建议与下一轮 goldset 扩容合并立项 |
| P2-27 | 生产可静默关 PII 掩码 | ✅ | load_config 生产姿态断言：production/staging 下 qa_log_pii_redact=false 硬 raise |
| P2-28 | 反 Gemini 守卫信标签 | ✅ | 守卫触发 = 标签 OR 解析目标命中生产指纹（RDS/HA3/OSS）；模拟态/带 DashScope key 的合法评测不误伤 |
| P2-29 | reconcile 扫描无界+无时长告警 | ✅ | buckets/elapsed 进报告 + 超时告警（RAG_RECONCILE_DURATION_ALERT_S=1800）；上界改服务端 MIN/MAX。**起点默认保持 0 全扫**（孤儿 PK 恰在 MIN(id) 以下，min-start 会废掉孤儿检测——2026-06-15 事故根因）；RAG_RECONCILE_SCAN_FROM_MIN 仅对召回丢失方向 opt-in |
| P2-30 | parity 全表进内存 | ✅ | 按 id 桶流式 diff（峰值 O(桶)+轻量 active 集合），报告结构逐键不变；顺带修 --hi 截窗误报 |
| P2-31 | 无时间元数据（排序/上下文/来源） | 🟡 | serving 侧已落：retriever 现查 RDS 附 doc_date（当前版本落库日）→ LLM 上下文头 `(文档日期: …)` + sources[].doc_date（前端渲染后续接）。**HA3 可索引日期字段需整表重建** = user-gated；时效 tiebreaker 待有真实日期信号后再校准 |
| P2-32 | VLM 降级烘进 INDEXED 终态 | ✅ | degraded 逐层传播（funnel→extractor→canonical→stage-2 回读→收尾）：文档改落 `NEEDS_REVIEW` + `content_process_error='vlm_degraded: N'`（**照常服务**——挡索引会因辅助失败断文本答案；NEEDS_REVIEW 不在任何认领谓词中=无重试循环，VLM 恢复后按标记定位走 reset_for_rechunk 重灌自愈；degraded 从不入 VLM 缓存） |
| P2-33 | review_task 只写不读 | ✅ | console 复审队列（kb_admin，按龄）+ 处置写 reviewer_*；实际整改用既有工具 |
| P2-34 | 人工队列无老化/SLA 监控 | ✅ | queue_monitor.run_queue_aging_check：三队列积压+最老龄，超 SLA 告警，入 ops_monitor |

## P3（2026-07-05 批：值得修的 13 条已落地，6 条延期立项）

| # | 摘要 | 状态 | 落点 / 论证 |
|---|---|---|---|
| P3-1 | 主命中不经 RDS 复核 | ✅ | `retriever._revalidate_main_hits`（RAG_MAIN_HIT_REVALIDATE 默认开）：主命中按 chunk_meta 复核 is_active + permission_level/owner_dept 一致性，漂移即丢弃。**有意不比 allowed_depts**（授权新增先落 RDS 后投影，比对会把授权变更放大成整天不可检索；撤销向已有 `_deny_revoked_cross_dept` fail-closed 兜底）。权威不可达/整体空集 → 保留并告警（HA3 服务端过滤是第一道边界，不把 RDS 故障放大为全站无答案） |
| P3-2 | 「摄取平面唯一写者」文档谬误 | ✅ | architecture.md §2 改为按列所有权声明（HA3=摄取独写 · chunk_meta ACL/状态列=服务共写经 outbox/PENDING_DELETE 握手 · index_status=CAS 令牌）+ reconciler 推理指引 |
| P3-3 | kb_audit_log 无防篡改 | 📋 | hash 链（prev_hash+HMAC+密钥隔离）+ 审计清除角色分离 = 独立项目；唯一触发是持 PROD-RW 令牌的带外特权内部人，无应用可达利用（审计自评 PLAUSIBLE）。本批 P3-18 已把 retention 对 audit 的 DELETE 改为**归档后删**——证据不再无痕消失，残余风险=直连 DBA UPDATE，属密钥/角色治理域，建议与云安全加固批次合并 |
| P3-4 | 无同意/目的/合法性记录 | 📋 | PIPL 同意记录子系统（告知流+同意表+目的校验）= 审计主题三点名的独立治理工作流，跨钉钉认证/schema/挖掘脚本三面，非补丁可达；P2-5 主体擦除已先行落地。建议与「数据治理与可复现性」专项立项 |
| P3-5 | 部门 ACL 无单一咽喉（4 处重导 2 存储） | 📋 | 单一 `is_doc_visible`/`permission_filter` 权威模块 = load-bearing 检索路径大重构；审计自评 PLAUSIBLE、当前无可达可见性 bug、resign 读的本就是权威存储。重构收益兑现在「下次 ACL 语义变更只改一处」——建议与下一次 ACL 演进（如 dept_id 子树制迁移）合并做，孤立重构纯风险无当期收益 |
| P3-6 | 会话无身份绑定，钉钉 key 可伪造窃取历史 | ✅ | `_SessionEntry.owner` 绑定：建时存已验证身份、访问校验（他人/匿名→SessionOwnershipError→API 403）；匿名 UUID 条目持有即所有+首个认证者就地绑定；钉钉 bot `trusted=True`（已验签回调=权威身份，抢注条目被丢弃重建而非 DoS 真实用户）；miniapp 前缀检查保留（挡"条目尚不存在时的抢注"） |
| P3-7 | embedding_version 死常量只写不读 | ✅ | `versions.embedding_regime_version()` 从实时 config 派生 `model@dimension`（仿 acl_policy_version 自变哲学）；stage-3 写侧、/api/version 改用派生值；**读者半边**=`rebuild_from_rds --stale-embedding`（按行级真值列 embedding_model/dimension 比对选重嵌范围——有意不比指纹串，避免历史行格式差异误判全语料陈旧） |
| P3-8 | Gemini 嵌入兜底制度不兼容 | ✅ | load_config 嵌入制度守卫：真嵌入（非 simulate）+ 已配检索后端 + 解析到非 DashScope 嵌入 → EnvironmentMismatchError（无 sparse/维度错配，索引与 eval 全失真）；RAG_ALLOW_INCOMPATIBLE_EMBEDDING=ack 显式实验放行 |
| P3-9 | trace 基建定义未接线 | ✅ | `install_request_id_logging()` 真正挂 RequestIdLogFilter（api.py 装配时，bot 同进程一次覆盖）；bot 三入口（webhook body/卡片回调/RAG 线程显式传参——Thread 不复制 ContextVar）ensure/set_request_id；两处本地 uuid trace 改用同一 rid；gen_meta_json 新增 request_id——成功但答错的落库行首次可 grep 关联 |
| P3-10 | bizdate 名义参数 + 容器本地时钟兜底 | ✅ | 三个 stage 节点兜底改固定 UTC+8 求北京 T-1；orchestrator docstring/CLI help 如实声明「纯状态 drain，bizdate 仅溯源标注，不能按日回填」并指向 reset_for_rechunk/rebuild_from_rds |
| P3-11 | judge 校准门从不接入自动路径 | ✅ | phase_merge 按同目录约定（judge_calibration_labels.json）/RAG_JUDGE_CAL_LABELS 拾取人工标注→compare→results['judge_calibration']（门自动出现）；无标注时显式打印+report meta 标注 NOT ACTIVE。**有意不插 not_executed 门**——人工标注是稀缺产物，插门会让所有无标注 strict 跑恒挂 |
| P3-12 | 依赖四清单漂移无 lockfile | 🟡 | 文档侧已修：dataworks_deployment.md 包清单补齐核心 SDK+抽取依赖（旧清单照抄会 ImportError）+ 声明 pyproject 为权威；monitor Dockerfile 谎称 pinned 的注释改如实。**lockfile 本体 user-gated**：pip-tools/uv 锁定+`--require-hashes` 需在有网环境生成并对四个运行时逐一验证，建议单独一次过 |
| P3-13 | 嵌入缓存 20k 硬上限无信号 | ✅ | 上限本就 env 可配（RAG_EMBEDDING_CACHE_MAX_ENTRIES，E-K 批）；本批补齐信号半边：每次运行记命中率（含 backend/entries/cap），容量压力（本批需求超上限/存量顶上限=驱逐中）发 ops 告警（dedup_key=embed-cache-cap） |
| P3-14 | effective_date/expiry_date 死列 | 📋 | 语料里**不存在**真实生效/失效日期信号（封面日期未采集），populate 是抽取特征项目而非补丁；P2-31 已用版本落库日给了时效近似。与 P2-8/31 的 HA3 整表重建窗口一并拍板：届时要么接真实日期采集+检索过滤，要么删列（避免暗示不存在的治理保证） |
| P3-15 | 无跨文档矛盾/取代检测 | 📋 | 语义级冲突检测 = 缺失能力项目（审计自评降级：真实语料以良性格式重复为主，价值级矛盾未被数据佐证）；现有缓解 = twin-governance skill + corpus_cleanup_worklist 人工台账 + 注册侧精确哈希防重。待有真实矛盾案例佐证 ROI 再立项 |
| P3-16 | 无周期复审/文档老化作业 | 📋 | 依赖两个前置：effective_date 数据（P3-14）与「哪些类别多久复审」的政策拍板（无规范强制）；消费端（console 复审队列，P2-33）已就绪，接线成本低——政策定了即可加 queue_monitor 型作业 |
| P3-17 | 向量无权威存储 | ✅ | 按审计自评（"至多值 DR runbook 一条"）落文档：architecture.md §10.7 DR 注记——全量 HA3 丢失的恢复路径（rebuild_from_rds→stage-3 重嵌）、成本与模型代差警告、--stale-embedding 核对指引 |
| P3-18 | retention 删唯一审计流水无归档 | ✅ | qa_rows/audit 两作业 commit 时改 select→OSS gzip JSONL 归档→按已归档 id 精确 DELETE（无 ORDER BY 的 DELETE..LIMIT 与 SELECT 可能选中不同行）；**归档失败即中止（fail-closed，绝不先删后补）**；RAG_RETENTION_ARCHIVE=false 显式退回直删（默认开） |
| P3-19 | faq_review_queue 死表 | 🟡 | 文档侧如实标注（architecture.md §8.1：零生产者/消费者、闭环从未实现、converted_to_faq 从不置位），并指明将来实现应复用 contribution 管线而非激活死表；**闭环本体延期**——升级→FAQ→语料发布器需产品拍板（谁审、如何去重、何种权限），P1-2 的工单队列+contribution 管线已备齐两端积木 |

## User-gated 清单（代码已就绪，等待用户动作）

1. **schema/017 + 018 apply 生产**（fuling_operation/fuling_knowledge；随既有 016 待办同批）。
2. **SAE 重打包**（serving + console next-dist）+ **DataWorks 重打包**（pipeline/orchestrator 侧修复）。
3. **eval 基线 refreeze**（P2-23/24/25 生效后首次 release-gate 会如设计地硬失败——跑
   `make release-gate` 前先在本机 refreeze 或设 RAG_EVAL_GOLDSET=golden_50.json）。
4. 生产 env 确认：RAG_OPS_ALERT_WEBHOOK（告警通道）/ RAG_ADMIN_NOTIFY（管理员通知）。
5. P2-18 时区地面真相：下次生产跑 rollup 看 tz_probe 日志，再决策是否迁移存储时区。
6. HA3 整表重建窗口（P2-8 模型戳 + P2-31 日期字段一起上；P3-14 日期列去留同窗拍板）。
7. （P3-12）**Python lockfile**：有网环境跑 pip-tools/uv 生成锁定（`--generate-hashes`），
   四个运行时（SAE 镜像 / DataWorks 资源组 / monitor 镜像 / CI）改 `--require-hashes` 安装，
   pip-audit 指向 lock。
8. （P3 批部署注记）P3-1/6/9 属 serving（随 SAE 重打包生效）；P3-7/10/13/18 属
   pipeline/DataWorks（随重打包+节点脚本更新生效）；retention 节点如需关闭删前归档
   （无 OSS 环境）显式设 RAG_RETENTION_ARCHIVE=false。
