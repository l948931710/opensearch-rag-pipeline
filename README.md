# opensearch-rag-pipeline

一家制造企业的**阿里云原生企业 RAG 知识库**：把内部文档（SOP/作业指导书、U8+ ERP 手册、行政
制度、FAQ）变成员工自助问答服务——钉钉机器人 + 钉钉小程序 + PC 网页控制台三端提问，后端从
企业自有文档里检索作答，带**部门级权限过滤**与**图文混排**（答案内嵌截图）。

> 项目名带 "opensearch"，但检索实际跑在**阿里 HA3 向量引擎**（"OpenSearch 向量检索版"，SDK
> `alibabacloud_ha3engine_vector`），非 Elastic/AWS OpenSearch；标准 OpenSearch 仅作本地开发
> 回退。模型全家桶 = DashScope/百炼（Qwen LLM · text-embedding-v4 · Qwen-VL OCR/VLM）。
> 开发者向的权威文档是 **[CLAUDE.md](CLAUDE.md)**（架构、不变量、gotchas），本 README 是导览。
> ⚠️ CLAUDE.md **有意不入库**（.gitignore 屏蔽——内含 RDS 端点等内部细节）：clone 后本地
> 不存在属预期，协作开发向仓库维护者索取副本。

## 系统全景

| 层 | 跑在哪 | 入口 | 干什么 |
|---|---|---|---|
| **摄取**（批） | DataWorks 每日调度 | `dataworks_orchestrator.py --stage {1,2,3}` | OSS raw/ → 提取(+OCR/VLM 图片漏斗) → 分类/PII 脱敏 → 切分(step 卡片) → embedding → 推 HA3 |
| **服务**（在线） | SAE 单实例（默认态；Redis/durable 多副本能力已备） | `api.py`(FastAPI :8000) + `dingtalk_bot.py`(Stream) | 3 路混合检索 → 邻居拼接/步骤扩展 → Qwen 生成 → 图文卡片；会话/反馈/限流/QA 落库 |
| **前端** | 钉钉 + 浏览器 | 小程序 `fuling-rag-miniapp/` · 控制台 `console-app/`(Vue3+Vite) | 问答、来源溯源、知识库管理（上传/审批/授权/看板/知识贡献） |
| **评测** | 本地 | `eval_harness/` + `make release-gate` | 258 题金集端到端评测、L4/L6 质量层、发布门 |

## 摄取管线（4 DAG）

```
raw/ ─DAG1→ canonical/ ─DAG2→ rag-ready/ + chunk_meta ─DAG3→ HA3 索引
                                                        └DAG4→ 检索评测（未接入生产）
```

**⚠️ 关键安全不变量**：新 chunk 必须先落 RDS **且**成功索引，**之后**才停用旧版本
（`node_deactivate_old_chunks`）。顺序反了 = 中途任何失败都会让文档从搜索里消失。
**严禁重排 DAG3 节点或把 deactivate 提前。**

## 检索与生成（服务侧）

- **3 路混合检索**：Dense + Sparse（kNN 路）+ BM25（`chunk_text`），weighted 融合(knn 0.7/text 0.3)；
  查询 embedding 必须走 DashScope **native** API（compat 模式会丢 sparse 向量、召回悄悄崩）。
- 权限过滤在 **HA3 服务端**执行（部门值经白名单防注入）；跨部门授权走 `allowed_depts` 投影。
- 检索后处理：封面页降权 → 邻居拼接（RDS ±1）→ step 卡片扩展 → 可选 learned rerank
  （`RAG_RERANK_ENABLE`，默认 OFF，+10.5pp recall@1）。
- 答案分档 高/中/低（阈值 7.7/5.8，**校准在 weighted 融合分上，换 RRF 即失效**）；图片经
  `<<IMG:N>>`（图级 `<<IMG:N.M>>`）标记穿插进卡片。

## 快速开始

```bash
make install        # 基础依赖；make dev = +pytest/ruff；make prod = +生产 SDK
make sim            # 模拟模式跑通整条管线（无需任何外部服务）
make sim-all        # 4 场景：normal / sensitive / multi / version_update
make api            # 本地起服务 API（:8000）
make test           # 全量测试（pytest-xdist 并行；make test-serial 串行排查）
make lint           # ruff（line-length 100 / py39）
```

**模拟模式**（`RAG_SIMULATE=true`，默认）：embedding 变哈希向量、OSS 读本地文件、HA3 用
mock 客户端。**改管线必须先在模拟模式验证。** 细粒度开关：`RAG_SIMULATE_DB/OPENSEARCH/OSS/API`。

## 环境与配置

配置中心在 `config.py`（`RAG_` 前缀环境变量 + `RAG_ENV` 选 overlay）。六档环境
（SIM / LOCAL-DEV / LOCAL-EVAL / STAGING / PROD-RO / PROD）矩阵见
[docs/environment_design.md](docs/environment_design.md)。三道生产防线：

1. **生产安全闸**：production/staging 若无 DashScope key 或解析到 Gemini → 启动即硬报错；
2. **环境↔目标交叉校验**：开发标签指向生产 RDS/HA3 指纹 → 硬报错（需显式 ack）；
3. **运行时破坏性写闸**（`env_guard.py`）+ 脚本仅经 `prod_access.py` 触达生产
   （只读默认；RW 需当日 `PROD-RW:<date>` 令牌）。

## 目录导览

```
opensearch_pipeline/
  dag_engine/definitions/pipeline_nodes   # 摄取：DAG 引擎 + 4 DAG + ~19 个 node_* 实现
  dataworks_orchestrator.py               # 生产摄取 CLI（--stage/--bizdate，DataWorks 节点调用）
  chunker.py                              # 切分器：text/faq/clause/step（step 卡片 + 图片绑定）
  extraction/                             # 统一提取：pdf/docx/xlsx/text + OCR + VLM 图片漏斗
  image_funnel_processor.py               # 3 级图片漏斗（启发式→OCR 密度→Qwen-VL 语义+安全）
  api.py + routes/                        # FastAPI：热路径在 api.py，冷域拆 routes/（kb 控制台/授权/贡献）
  retriever.py / reranker.py / llm_generator.py   # 检索 / 重排 / 生成
  dingtalk_bot.py / dingtalk_card.py      # 钉钉机器人（Stream 模式）+ 图文卡片
  session_store / qa_logger / feedback_handler / rate_limiter   # 会话 · QA 落库 · 反馈 · 四层防刷
  db.py / clients.py / prod_access.py     # 连接池+守卫 / OSS·HA3 客户端 / 生产访问通道
schema/          # RDS DDL 唯一权威（编号迁移 + schema_migrations 台账，见 schema/README.md）
console-app/     # PC 网页控制台（Vue3+Vite；构建产物进 SAE 包的 webconsole/next-dist）
fuling-rag-miniapp/   # 钉钉小程序（已发布）
eval_harness/    # 端到端评测 + 报告；tests/（pytest，CI 阻塞门）
dataworks_nodes/ # DataWorks 各 stage 节点脚本
docs/            # 环境设计 / 架构审查 / 性能 backlog 等
```

## 部署（两个包，勿混）

**SAE 服务侧正式工件路线 = 镜像**（Majors δ2/M12.3，2026-07-21）：`.github/workflows/image.yml`
job1 build+smoke 产 `docker save` 工件与 attestation（零外部写）；推 ACR 走 job2 promotion
（仅 workflow_dispatch + `push_acr=true` + environment 保护，**逐字节同工件**）。下表的
SAE **ZIP 是 break-glass 备援**，带截止条件：SAE 应用完成镜像化迁移（user-gated）后退役。
δ2b 实况核查结论：SAE zip **不自装依赖**——buildpack 远端 `pip install -r requirements.txt`
（浮动；a64aa86 记录在案：精确钉版会致 buildImage exit 1，故该路径不切
requirements-prod.lock/--require-hashes；哈希锁供应链保证只在镜像路线成立——又一条弃 ZIP 的理由）。

| 包 | 用途 | 打法 |
|---|---|---|
| `opensearch_sae_rag.zip` | SAE 服务侧（**break-glass**） | zip 根 = `requirements.txt`(必须) + `opensearch_pipeline/`(含 webconsole/next-dist，**打包前先 `cd console-app && npm run build`**) + `Dockerfile` + `pyproject.toml` + `.dockerignore`；无 pyc/.env/tests |
| `opensearch_pipeline_production.zip` | DataWorks 摄取侧 | zip 根 = 仅 `opensearch_pipeline/`，**排除 webconsole/** + pyc；无 requirements.txt（节点内联 pip + pod 预装）。**B4 起用 `deploy/build_dataworks_zip.sh` 构建**：自动附 build_info.json + 生成 `.zip.sha256` sidecar（上传为同名 File 资源，节点做 sha256 校验——**摘要不一致才阻断；sidecar 缺失/不可读默认过渡期放行**，`RAG_DW_SIDECAR_STRICT=on` 硬拒；δ3 起支持 `--hmac` 产 `.sha256.hmac` 第三份资源，调度 env 配 `RAG_DW_ZIP_HMAC_KEY` 的节点硬验，见 `tests/test_dataworks_supply_chain.py`；换 zip 请同步换全部 sidecar） |

**铁律**：一律打到 `~/Downloads/dw_upload_<YYYYMMDD>[_<purpose>]/`（勿打 repo 根）；部署时
**按 SIZE/SHA-256 认包，别按文件名**（每个日期目录里文件名都一样，选错目录 = 静默部署旧版）。
SAE 启动命令在应用配置里（`--workers 1`：会话/限流/去重/token 缓存均进程内内存，**本分支
（main）无 Redis 后端、无 durable dispatch、无 `RAG_EXPECTED_REPLICAS` 拓扑守卫——单副本部署**。
多副本能力（Redis 四态后端 + durable dispatch + 拓扑守卫启动拦截）在 agent 分支
`claude/ontology-p0`，随该分支上生产时才生效）。**注**：production/staging 启动强制
`RAG_REQUIRE_AUTH` + `RAG_ACL_FAIL_CLOSED`（缺则拒启，过渡逃生口=当日
`RAG_ALLOW_LEGACY_OPEN_PROD=ack:<YYYY-MM-DD>`；见 `docs/environment_design.md` §2.1）。

## 更多

- **[CLAUDE.md](CLAUDE.md)** — 架构细节、载荷契约（image_refs）、chunk 路由 gotchas、约定
- [docs/perf_optimization_backlog.md](docs/perf_optimization_backlog.md) — 97 项性能优化（已全量落地）
- [schema/README.md](schema/README.md) — 表→库矩阵、迁移编号规则、台账流程
- `work_report.md` 偏管理层口径；客观记录以 git log + tests/eval 报告为准
