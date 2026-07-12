# 重审计（2026-07-11 裁决版）逐项修复台账 — 2026-07-12

对应输入：六大类逐条裁决（§1 可靠性 / §2 数据完整性 / §3 Agent 完整性 / §4 性能 /
§5 UX / §6 供应链）。本文档记录每条的处置、落点与验证；分支 `claude/ontology-p0`。

验证基线：Python 全套 pytest 绿（含新增 `tests/test_agent_reaudit_fixes.py` 24 例）+
`make lint` 范围（opensearch_pipeline/ + tests/）全绿 + console vitest 302 绿（+5 新例）+
**Docker 镜像真实构建通过 + 镜像内导入冒烟通过**（本机 docker build）。

## §1 可靠性 / durability

| 子项 | 处置 | 落点 |
|---|---|---|
| 完成侧 CAS 静默吞（怀疑者 bug） | ✅ 修 | `executor.py` RunCompleted 分支改 **checked CAS**：running→succeeded 失败 = 失去所有权 → 抑制 on_complete/done 帧，发 RunFailed + error 日志。qa_log 与 durable 状态不再分叉 |
| 心跳只在轮边界刷 → 活持有者被误杀 | ✅ 修 | `executor._start_heartbeat_ticker`：per-run 后台心跳线程（默认 30s，`RAG_AGENT_HEARTBEAT_INTERVAL_S`，<=0 关闭回历史行为）；过 deadline 停止续命（真僵死仍交 reaper） |
| 无 SIGTERM drain（atexit wait=False 直弃） | ✅ 修 | `executor.drain(timeout)`（拒新 → 限时等收尾 → 超时 run 诚实标 failed，与 fencing 一致）；`routes/agent._drain_runtime` 挂 ASGI shutdown + atexit 双通道，`RAG_AGENT_DRAIN_TIMEOUT_S` 默认 20s |
| 无运行中 checkpoint | ✅ 修（有界） | `RAG_AGENT_MIDRUN_CHECKPOINT`（默认 **off**）：loop 每模型轮边界发内部事件 `RunCheckpointReady`，driver 持久化（turn 语义与 resume start_turn=turn+1 对齐）。**边界诚实：是状态保全/取证底座，不是崩溃自动回放**（failed 仍是终态） |
| 无 durable event stream（进程内 queue） | ✅ 修（flag off） | 新模块 `agent_runtime/event_relay.py`：`RAG_AGENT_EVENT_RELAY=redis` 时事件镜像 XADD 进 Redis Stream（MAXLEN+TTL），新端点 `GET /api/agent/runs/{id}/events` 跨实例回放 SSE 到终态（owner/kb_admin 门禁同 run 详情）。v1 局限：sources/content_blocks 帧不进中继（artifacts 进程内旁路），已注明 |
| 无 fencing token / 持有者标识 | 🟡 部分 | 完成侧 CAS + 后台心跳 + drain 组合后，「活持有者被抢占后仍产生副作用」路径已封死；agent_run 加 owner_instance 列的完整 fencing 未做（多副本真部署前的下一刀） |

## §2 数据完整性

- **manifest 未绑输入** ✅：`auto_activation_enabled()` 必填字段增 `source_sha256`/`environment`；
  seeding/backfill 实算快照 sha256（`CsvSnapshotSource.fingerprint()` + `source_fingerprint()`
  duck-type）穿线进 `may_auto_activate(source_fingerprint=…)`——指纹不符/给不出/环境不符一律
  auto 关闭（fail-closed）。`backfill._prod_ack_valid` 收紧为三段式
  `<op>:<date>:<docset_hash>`（第三段=快照 sha256 前缀 ≥8 hex，错绑/两段式旧 token 一律拒），
  拒绝提示会打印当前快照的前 12 位供 Sam 铸 token。
- **retention node OSS 死锁** ✅：`dataworks_nodes/retention_node.py` 显式
  `RAG_RETENTION_ARCHIVE=false`（setdefault，可覆盖）+ 阶段2 指引同步 blindspot 台账
  P3 部署注记；要删前归档须同时开 ARCHIVE + 真 OSS + 凭据（注释写明）。
  ⚠️ 生效需要把节点脚本重新粘贴到 DataWorks 控制台（user-gated 部署项）。

## §3 Agent 完整性（复核=计划内中间态，不接线）

组织 gate ①③④ 未签，PMC-1 工具面归 PR11-13——**维持排除**（这是决策不是缺陷）。本轮只做
可观测性：`approval_store.resolve_scope_live` 空解析器回退快照 scope 时加**一次性响亮告警**；
`agent_tools.build_default_registry` docstring 写明排除依据与解除条件。

## §4 性能

- **~801 次冷启动逐条 embedding** ✅：`resolve.py` 新 `_ensure_title_vecs`——持久缓存点查
  （复用摄取侧 `SqliteKVStore`，同键契约 `md5(model_dim_text)`）→ miss 走
  `embed_texts_native` **批量** API（config.embedding.batch_size，DashScope=10）→
  `_TITLE_VEC_LOCK` 单飞去重。注入 embedder（单文本契约）不受影响；批量任何失败回退
  逐条兜底（纯优化路径，候选结果不变）。
  实测（单测锁定）：25 对象 → 3 次批量调用（此前 25 次单发）；二次 resolve 0 次；
  跨实例冷启动经持久层 0 次。
- **负载/压测报告** ⚪ 未做：真实压测需 staging/prod 环境窗口（user-gated）；本轮交付的是
  调用数级的行为回归锁（`test_title_vectors_*`），不编造压测数字。

## §5 UX

- **审批后 requester 拿不到答案** ✅：`schema/036` agent_run 增 `message_id`（**先 apply 后
  部署**；本地 dev 库已加列+台账，staging/prod 未 apply）——submit 回填、
  `GET /api/agent/runs/{id}` 经 `qa_logger.fetch_answer_by_message_id` 带回
  `final={message_id, answer_text, answered_at}`（owner 门禁不变，fail-open）；
  前端运行中心（AgentRunCenter.vue）新增「最终答案」块 + succeeded 无文本时的引导文案。
- **续跑反馈悬空（怀疑者第二 bug）** ✅：`_resume_callbacks` 复用 `agent_run.message_id`
  （历史行回退新生成），前端投票锚定的 message_id 与落库行一致。
- **desktop token 走 query** ✅（收敛残余面）：`/console→/console/` 307 把 token 从 query
  挪进 **fragment**（`#token=`，浏览器不回传服务器 → 跟进请求日志零 token；其它 query
  深链参数原样保留）；前端 `useAuth` 新增 `hashParam/scrubHash`，`captureUrlCredential`
  双形态摄取（?token 兼容不变，链接生产者今后可直接发 `#token=` 全程零服务端日志暴露）。
  初始 `GET /console/?token=` 首跳日志仍在（旧链接形态决定），生产者迁移 fragment 后消失。

## §6 供应链

- **Python lock** ✅：`requirements-prod.lock`（uv pip compile，--generate-hashes，
  linux/py3.11，extras=api+production，87 pin），Dockerfile 改
  `pip install --require-hashes --no-deps -r requirements-prod.lock`。
- **基础镜像 digest 固定** ✅：node:20-slim / python:3.11-slim 均按 sha256 digest pin
  （升级流程注释在 Dockerfile）。
- **CI** ✅：security job 增 ① lock 定向 pip-audit（阻塞）② syft SBOM（SPDX artifact）
  ③ trivy fs 扫描（lockfiles+Dockerfile misconfig，fixable HIGH/CRITICAL 阻塞）。
  CI 步骤在推送后首跑生效（本地未验证 Actions 运行本身）。
- 更正确认：npm 侧本就有 lock（package-lock.json + npm ci），未动；miniapp 零依赖。
- **镜像验证** ✅：本机 `docker build` 全程通过（hash 校验安装成功），容器内
  `import fastapi/pymysql/oss2/dashscope/redis/…` 冒烟通过。

## 未验证 / user-gated 残留

1. schema/036 staging/prod apply（先 apply 后部署，同 035 纪律）。
2. SAE/DataWorks 重打包部署（本轮全部改动现网生效的前提）；retention 节点脚本重粘贴。
3. Redis 中继/midrun checkpoint/心跳间隔等新 flag 的生产开启节奏（全部保守默认）。
4. CI 新增 security 步骤的首跑（推 origin 后看 Actions）。
5. 真实负载压测（§4 遗留，需环境窗口）。
