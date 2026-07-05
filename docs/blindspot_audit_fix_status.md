# 盲区审计修复台账（P1 + P2）

> 依据：`docs/architecture_blindspot_audit_2026-07-05.md`（55 条）。本文件跟踪 P1×2 + P2×34
> 的处置状态；P3 未启动。状态：✅ 已修复（代码合入）· 🟡 部分修复（代码侧可落的最大程度，
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

## User-gated 清单（代码已就绪，等待用户动作）

1. **schema/017 + 018 apply 生产**（fuling_operation/fuling_knowledge；随既有 016 待办同批）。
2. **SAE 重打包**（serving + console next-dist）+ **DataWorks 重打包**（pipeline/orchestrator 侧修复）。
3. **eval 基线 refreeze**（P2-23/24/25 生效后首次 release-gate 会如设计地硬失败——跑
   `make release-gate` 前先在本机 refreeze 或设 RAG_EVAL_GOLDSET=golden_50.json）。
4. 生产 env 确认：RAG_OPS_ALERT_WEBHOOK（告警通道）/ RAG_ADMIN_NOTIFY（管理员通知）。
5. P2-18 时区地面真相：下次生产跑 rollup 看 tz_probe 日志，再决策是否迁移存储时区。
6. HA3 整表重建窗口（P2-8 模型戳 + P2-31 日期字段一起上）——与下一次索引结构变更合并。
