# 工业级环境分工配置 — 设计文档

> 状态：已批准方案（六层环境矩阵 + D1-D8 决策 + P0-P6 迁移计划）。
> 配套 runbook：[local_eval_env.md](local_eval_env.md)（本地受控 A/B 评测环境的操作手册，本文是其上位设计）。
> 加载期校验已落地于 [config.py](../opensearch_pipeline/config.py)（`EnvironmentMismatchError` / `PROD_FINGERPRINTS` / `_validate_environment_target_consistency`）；运行时守卫（`env_guard.py`）与生产统一入口（`prod_access.py`）见 §7 实施状态。

---

## 1. 背景与问题

环境分工长期靠"约定 + simulate 开关"维持，两路 Explore 实证了 9 个风险点。最严重的一条：**所谓"test 环境"物理上就是生产**。

| # | 风险点 | 证据 |
|---|---|---|
| R-1 | **`.env.test` 凭证与生产完全相同**：RDS host/账号、HA3 endpoint/instance/表名、OSS AK/bucket 逐项一致（哈希指纹比对全等）。"test"实为"公网访问生产"，名实不符 | [.env.test](../.env.test) vs [.env.production](../.env.production)（值已脱敏比对） |
| R-2 | **三环境共享同一个 OSS bucket，路径无环境前缀**：本地把 `RAG_SIMULATE_OSS=false` 跑 stage-1/2，`raw/`→`canonical/`→`rag-ready/` 全部直写生产 bucket 真实路径 | [pipeline_nodes.py](../opensearch_pipeline/pipeline_nodes.py) `_get_oss_bucket`(L191) 直接用 `config.oss.bucket_name` 建真实 Bucket，无环境分流 |
| R-3 | **危险写操作零环境守卫**：HA3 删除（`_search_delete_old_chunks` L3350）、RDS 停用（`node_deactivate_old_chunks` L3421）、索引推送（`node_push_to_opensearch` L4086）、OSS 发布——全部只看 simulate 开关，simulate=false 即放行，不区分目标是不是生产 | [pipeline_nodes.py](../opensearch_pipeline/pipeline_nodes.py) L3441-3444 仅 `_resolve_simulate(ctx, ...)`；[spot_checker.py](../opensearch_pipeline/spot_checker.py) `_delete_chunks_from_index`(L20) 同 |
| R-4 | **同一把全权 OSS AK 散布本地明文文件**：实测命中 `.env.local`、`.env.local_ab_new`、`.env.local_ab_old`、`.env.test`、`.env.production` 共 5+ 个文件（含两个由脚本派生、随手复制的 overlay），任何一个泄露 = 生产桶全权 | `grep <AK>` 实测命中清单（值不入文档） |
| R-5 | **配置走私（脚本自行解析 env 文件）**：45 个 scratch 脚本 + [envboot.py](../eval_harness/envboot.py)(L47) + 2 个 scripts/ 脚本绕开 `load_config()` 自带的 `_load_envfile` 直读 `.env.production`/`.env.test`，守卫管不到它们 | `grep -rl '_load\|\.env\.production\|\.env\.test' scratch/` 共 45 文件，全名单见 §10.1 |
| R-6 | **os.environ 直读绕过配置中心**：`opensearch_pipeline/` 内非 config.py 仍有 34 处 `os.environ` 直读（行为开关、阈值），加 scripts/eval_harness 合计 40+，配置真相分散 | `grep -rn os.environ opensearch_pipeline/ --exclude config.py` |
| R-7 | **HA3 表名历史双标**：config 旧默认 `fuling_knowledge_vector`（一张从未存在的表）vs 生产实表 `fuling_kb_chunks`，端点配对、表名漏配时会静默指向错表 | [config.py](../opensearch_pipeline/config.py) L607-609 注释记录了这段历史（现默认已改空 + D7 fail-fast） |
| R-8 | **overlay `override=True` 静默遮蔽 shell 变量**：`export RAG_OPENSEARCH_INDEX=...` 无效且无任何提示，已造成切索引/开关 rerank 的实际困惑（[local_eval_env.md](local_eval_env.md) 陷阱 #1） | [config.py](../opensearch_pipeline/config.py) `_load_env_files` L81（现已补 provenance banner，见 D6） |
| R-9 | **环境标签与物理目标无交叉校验**：`RAG_ENVIRONMENT=development` 可以连生产 RDS/HA3 不报任何错——标签只影响模型守卫与 base url 选择，从不对照"你到底连的是谁" | 历史 config.py 行为；现由 §7.1 校验矩阵补齐 |

一句话总结：**事故防线只剩"人记得设 simulate"这一道**，而 R-1/R-4 意味着即使人记得，凭证本身也不构成任何边界。

---

## 2. 设计总纲：三道防线，按强度排序

```
①凭证物理边界（只读 RAM key / 最小权限账号）
   > ②资源命名边界（独立 bucket / _stg 库表 / 本地索引前缀）
      > ③代码守卫（fail-fast 交叉校验 + 运行时断言）
```

- **①凭证物理边界**最硬：拿着 `ram-rag-ro` 的只读 key，"本地误写生产桶"从"靠守卫拦截"变成**物理不可能**（PutObject 直接 403）。
- **②资源命名边界**次之：staging 独立 bucket、`_stg` 库表、`local_*` 索引，让"指错目标"在名字上就显眼，且可被 ③ 机械校验（后缀断言）。
- **③代码守卫**最软但覆盖面最广：加载期 fail-fast（任何连接建立之前）+ 破坏性操作运行时断言，兜住前两道漏掉的配置漂移。

**现状恰好倒置**：①完全缺失（一把全权 AK 走天下）、②局部缺失（OSS 共桶、HA3 双标）、只有③的雏形（simulate 开关 + Gemini 禁令）。simulate 是"要不要连真实服务"的开关，不是"连的是谁"的守卫——它一关，后面一马平川。本方案补齐前两道、把第三道从"单开关"升级为"标签↔目标交叉校验 + 按操作粒度的 ack"。

---

## 3. 六层环境矩阵（核心表）

| | SIM | LOCAL-DEV | LOCAL-EVAL | STAGING | PROD-RO | PROD |
|---|---|---|---|---|---|---|
| RAG_ENV | 空 | `local` | `local_ab_new` / `_old` | **`staging`** | `prod_ro`（`test` 为过渡别名） | 不设（平台注入） |
| RAG_ENVIRONMENT | development | development | development | **staging** | staging | production |
| RAG_READONLY | — | false | false | false | **true** | false |
| RDS | 模拟 | docker localhost（补建 fuling_operation 库） | 同左，doc_id 前缀分臂 | 生产实例 **fuling_knowledge_stg + fuling_operation_stg** 库（`fuling_stg` 账号，仅授 `*_stg` 库权限） | 生产 + **`fuling_ro` 只读账号** | 生产 `fuling_admin`（注入） |
| 检索 | MOCK_HA3_CLIENT | 本地 OpenSearch `fuling_knowledge_v1` | `locale2e_v1` / `locale2e_old_v1` | 生产 HA3 实例 **`fuling_kb_chunks_stg` 表** | 生产 HA3 公网 `fuling_kb_chunks` | 生产 HA3（注入） |
| OSS | 模拟 | **零 OSS**：管线 `RAG_SIMULATE_OSS=true` 走本地文件；语料用采样脚本（ram-rag-ro 只读 key）从生产拉取；仅 URL 签名用 ro key | 同左 | **新建 `fuling-knowledge-base-staging` 桶** + ram-rag-stg（仅该桶读写） | 生产桶公网 + **ram-rag-ro 只读** | 生产桶内网 + ram-rag-prod（注入） |
| 用途 | CI / 管线逻辑 | 开发调试 | A/B 盲评 | **全链路预演**：摄取→索引→serving 不触生产；回灌彩排 | 生产诊断/镜像/在线评测 | 真实服务 |

### 每层何时使用

- **SIM**（`make sim` / 单测默认）：改任何管线逻辑后的第一站。零外部依赖，embedding 是哈希向量，HA3 返回 mock。**所有规则守卫以"对应子系统 simulate=False"为前置**，所以 SIM 永远不会被守卫误伤。
- **LOCAL-DEV**（`RAG_ENV=local`）：需要真实 MySQL/OpenSearch 行为（mapping、SQL、版本流转）但不该碰任何云资源时。语料来自采样脚本拉到本地的副本；图片签名 URL 仍真实可用（ro key 的 GET 签名是只读操作）。
- **LOCAL-EVAL**（`RAG_ENV=local_ab_new/_old`，:8001/:8002）：受控 A/B 盲评专用，操作手册见 [local_eval_env.md](local_eval_env.md)。与 LOCAL-DEV 共享容器，仅索引名与 rerank 开关不同；doc_id 前缀（`LOCALE2E_` / `LOCALE2EOLD_`）分臂。
- **STAGING**（`RAG_ENV=staging`）：上生产之前的**全链路彩排**——摄取→索引→serving 在与生产同实例、不同库表/桶上完整跑一遍。典型场景：HA3 重建彩排、schema 迁移演练、新 DAG 节点首跑。资源后缀受强约束校验（§7.1 R6），配置半生不熟时直接 raise。
- **PROD-RO**（`RAG_ENV=prod_ro`）：生产诊断、镜像导出、在线评测。`RAG_ENVIRONMENT=staging` 标签是**刻意的**：staging 进 Gemini 禁令守卫（对生产索引查询必用 Qwen 向量），且自动选 DashScope 公网域名（笔记本可达）。`RAG_READONLY=true` + `fuling_ro`/`ram-rag-ro` 双保险。
- **PROD**：仅 SAE / DataWorks 平台。环境变量平台注入，仓库内**不存在**生产写凭证文件（D5）。

---

## 4. 关键决策记录（D1-D8）

| # | 决策 | 一句话理由 |
|---|---|---|
| D1 | `RAG_ENV`（配置来源选择器）与 `RAG_ENVIRONMENT`（安全等级）**保留正交**，新增合法组合校验矩阵 | 生产路径无 .env 文件、RAG_ENV 为空，二合一会让守卫依赖生产中不存在的机制 |
| D2 | 新增 `RAG_READONLY` 第三守卫（env 声明、代码强制） | HA3 可能不支持只读子账号，应用层兜底 |
| D3 | **本地零 OSS + staging 独立桶**，不做前缀代理 | 事故面从"靠守卫拦"变为"物理不存在" |
| D4 | `.env.test` → `.env.prod_ro`（名实归一），凭证降级只读；symlink + 别名 + DeprecationWarning 过渡 | 它的真实用途就是"从公网只读访问生产" |
| D5 | 生产写凭证出仓：`.env.production` 删除 → 提交 `.env.production.template`（无值），真值 SAE/DataWorks 注入；AK 轮换 | stage*_node.py 的 required_vars 校验骨架已就绪 |
| D6 | overlay `override=True` **保留**（file-wins），补 provenance banner + `RAG_ALLOW_SHELL_OVERRIDE` 白名单逃生口 | shell-wins 最坏失效 = 残留 export 把生产端点拼进本地运行 |
| D7 | HA3 表名默认改**空**；production/staging 且非 simulate 且 endpoint 非空时表名为空 → fail-fast | 消除 `fuling_knowledge_vector` 双标；不误伤 local（用 OpenSearch）与 simulate |
| D8 | 增加**真 STAGING 层**：同 RDS 实例建 `*_stg` 库、同 HA3 实例建 `_stg` 表、独立 staging 桶 | 现有实例承载，近零成本；完整预演不触生产 |

### 展开

**D1 — 为什么 RAG_ENV 与 RAG_ENVIRONMENT 保留正交。** 两个变量回答两个不同的问题：`RAG_ENV` 回答"从哪个文件读配置"（笔记本上的便利机制），`RAG_ENVIRONMENT` 回答"这是什么安全等级"（守卫的判断依据）。生产路径（DataWorks/SAE）根本没有 .env 文件、`RAG_ENV` 恒为空——如果把守卫挂在 RAG_ENV 上，生产恰好是守卫失明的那个环境。正交保留后，由 §7.1 的合法组合矩阵约束二者关系（如 `RAG_ENV=staging ⇒ RAG_ENVIRONMENT=staging`），而非合并变量。

**D3 — 为什么本地零 OSS。** 管线全部 OSS 读写都经 [`_get_oss_bucket`](../opensearch_pipeline/pipeline_nodes.py)（L191）这一个咽喉，`simulate_oss=true` 即整体走本地文件——机制现成，本地根本不需要桶。语料经采样脚本（`scripts/sample_corpus.py`，P2 落地）用 ram-rag-ro 只读 key 从生产拉到 simulate 布局目录。唯一的例外是图片 URL 签名（[oss_url.py](../opensearch_pipeline/oss_url.py) L80 独立构建 Bucket，不经管线咽喉）：给它配同一把只读 key，本地 serving 仍能出真实图片 GET 链接（签名是只读操作）。这套设计简化掉了原方案的 `RAG_OSS_KEY_PREFIX` 整套前缀代理——"本地写生产桶"的事故面不是被拦住了，而是物理不存在（只读 key 想写也写不了）。

**D4 — 名实归一。** `.env.test` 这个名字让 45 个 scratch 脚本理直气壮地拿生产全权凭证当"测试连接"。改名 `prod_ro` 后语义自明；`RAG_ENV=test` 经别名 + DeprecationWarning 过渡（[config.py](../opensearch_pipeline/config.py) L70-74 已实现），存量脚本经 symlink 零改动可用。

**D6 — 为什么 override=True 保留。** file-wins 保证**环境身份原子性**：选定 `RAG_ENV=local` 就意味着整组 local 配置生效，不会被三天前残留的 `export RAG_RDS_HOST=<生产>` 拼出一个"半本地半生产"的缝合环境——那是 shell-wins 的最坏失效模式，且静默。代价（R-8 的遮蔽困惑）用两个补丁支付：启动 banner 显式列出被遮蔽的 shell 变量（L128-130 已实现）；确需单变量临时覆盖时用 `RAG_ALLOW_SHELL_OVERRIDE=VAR1,VAR2` 白名单回填（L88-94 已实现）。

**D8 — STAGING 的前置代码改动。** qa_logger / feedback_handler / dingtalk_bot 共 11 处硬编码 `fuling_operation.` 限定名，需机械替换为新配置 `RAG_RDS_OPERATION_DATABASE`（默认 `fuling_operation`），staging 才能整体切到 `fuling_operation_stg`。

---

## 5. 资源命名规范

### 5.1 RDS（同一生产实例 rm-bp15j7…）

| 库 | 用途 | 授权账号 |
|---|---|---|
| `fuling_knowledge` | 生产知识库（chunk_meta 等） | `fuling_admin`（平台注入）、`fuling_ro`（只读） |
| `fuling_operation` | 生产运营库（qa_session_log、feedback） | 同上 |
| `fuling_knowledge_stg` | staging 知识库（schema/001 + 002 同构） | `fuling_stg`（仅 `*_stg` 库） |
| `fuling_operation_stg` | staging 运营库 | 同上 |

账号纪律：`fuling_ro` 仅 SELECT；`fuling_stg` 对 `*_stg` 库全权、对生产库**零授权**（连 SELECT 都不给，staging 串生产即刻报错）。

### 5.2 HA3（同一实例 ha-cn-kgl4…）

| 表 | 用途 |
|---|---|
| `fuling_kb_chunks` | 生产索引（唯一真表） |
| `fuling_kb_chunks_stg` | staging 索引（复制生产表结构） |
| ~~`fuling_knowledge_vector`~~ | 历史双标默认值，**从未存在**，已废（D7：默认改空 + fail-fast） |

### 5.3 本地 OpenSearch 索引

命名 `local_{用途}_{vN}`，例：`local_dev_v1`、`local_chunkexp_v2`。**Grandfathered**：`locale2e_v1` / `locale2e_old_v1`（受控 A/B 在用，见 [local_eval_env.md](local_eval_env.md)），不强迁。

### 5.4 OSS

| 桶 | 用途 | 访问身份 |
|---|---|---|
| `fuling-knowledge-base` | 生产（`raw/` `canonical/` `rag-ready/`） | ram-rag-prod（写，仅平台）、ram-rag-ro（只读，本地采样/签名/PROD-RO） |
| `fuling-knowledge-base-staging` | staging（同 key 布局，零代码改动） | ram-rag-stg（仅该桶读写） |

本地**不设桶**（D3）。staging 桶语料用 `ossutil` 跨桶 sync 子集灌入。

### 5.5 端口

| 端口 | 角色 |
|---|---|
| :8000 | 默认 serving（`make api`）；PROD-RO 诊断服务（`RAG_ENV=prod_ro uvicorn ...`） |
| :8001 | LOCAL-EVAL 新管线臂 |
| :8002 | LOCAL-EVAL 旧对照臂 |
| :8003+ | 新增长驻实例从 8003 起顺延（如 staging serving 本地预览），用前查 `make ab-status` |

---

## 6. 凭证分离

### 6.1 RAM 子账号

| 子账号 | 权限范围 | 使用场景 | 落点 |
|---|---|---|---|
| `ram-rag-ro` | 生产桶只读（GetObject/ListObjects） | 本地语料采样、图片 URL 签名、PROD-RO | `.env.local*`、`.env.prod_ro` |
| `ram-rag-stg` | staging 桶全权，生产桶零授权 | STAGING 全链路 | `.env.staging` |
| `ram-rag-prod` | 生产桶全权 | 生产摄取/发布 | 仅 SAE/DataWorks 注入，**不落仓库** |

最小权限策略原文（阿里云 RAM policy 语法）：

**ram-rag-ro**（只读，挂生产桶）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:GetObject", "oss:ListObjects"],
      "Resource": [
        "acs:oss:*:*:fuling-knowledge-base",
        "acs:oss:*:*:fuling-knowledge-base/*"
      ]
    }
  ]
}
```

**ram-rag-stg**（staging 桶全权；不授生产桶任何 Action，最小权限模型下即默认拒绝）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:*"],
      "Resource": [
        "acs:oss:*:*:fuling-knowledge-base-staging",
        "acs:oss:*:*:fuling-knowledge-base-staging/*"
      ]
    }
  ]
}
```

**ram-rag-prod**（生产桶全权 + 显式拒绝 staging 桶，防生产代码误配 staging 目标）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:*"],
      "Resource": [
        "acs:oss:*:*:fuling-knowledge-base",
        "acs:oss:*:*:fuling-knowledge-base/*"
      ]
    },
    {
      "Effect": "Deny",
      "Action": ["oss:*"],
      "Resource": [
        "acs:oss:*:*:fuling-knowledge-base-staging",
        "acs:oss:*:*:fuling-knowledge-base-staging/*"
      ]
    }
  ]
}
```

### 6.2 RDS 账号建法

RDS 控制台 → 实例 rm-bp15j7… → 账号管理 → 创建账号：

- `fuling_ro`：普通账号，授权库勾选 `fuling_knowledge` + `fuling_operation`，权限选**只读**。等价 SQL：`GRANT SELECT ON fuling_knowledge.* TO 'fuling_ro'@'%'; GRANT SELECT ON fuling_operation.* TO 'fuling_ro'@'%';`
- `fuling_stg`：普通账号，仅授 `fuling_knowledge_stg` + `fuling_operation_stg` **读写**；生产两库不出现在其授权列表。
- 应用层再加一道：`opensearch_pipeline/prod_access.py`（PR3，§7.4）的只读连接强制 `init_command="SET SESSION TRANSACTION READ ONLY"`，即使误用了写账号，会话级也写不进去。

### 6.3 DashScope key

- 生产 key 仅 SAE/DataWorks 注入，不落仓库（与 D5 同步出仓）。
- 本地/评测用独立 key（独立配额与账单归属，泄露可单独吊销，不连坐生产）。
- HA3 的 user/password：尝试在控制台建只读用户（待验证项，见 §10.3）；若不支持，由 `RAG_READONLY` 应用层守卫兜底（D2）。

---

## 7. 代码守卫层（第三道防线）

### 7.1 config 加载期校验矩阵 — 已实现

落点：[config.py](../opensearch_pipeline/config.py) `_validate_environment_target_consistency()`（L409，于既有 Gemini 模型守卫之后、任何连接建立之前调用）。生产指纹常量 `PROD_FINGERPRINTS`（L28：rds=rm-bp15j7… / search=ha-cn-kgl4… / oss=fuling-knowledge-base，均为非密钥实例标识）。**所有规则以"对应子系统 simulate=False"为前置**，`make sim` 与既有单测天然兼容。

| 规则 | 条件 | 行为 |
|---|---|---|
| R1 | dev 标签 + 远程 RDS | raise；豁免 `RAG_ALLOW_REMOTE_DB=read_only_ack` |
| R2 | dev 标签 + 生产检索指纹 | raise；豁免 `RAG_ALLOW_REMOTE_SEARCH=read_only_ack` |
| R3 | staging/test 标签 + 生产指纹且非 `_stg` 库/表 | 需对应 ack（PROD-RO 形态的显式声明） |
| R4 | production 标签 + localhost RDS | raise，**无豁免**（必为配错） |
| R5 | production 标签 + 无任何检索后端 | raise，无豁免 |
| R6 | `RAG_ENV=staging` 时资源后缀强约束（`_stg` 库表 / `-staging` 桶 / `RAG_ENVIRONMENT=staging`） | raise，无豁免（防 staging 配置半生不熟指向生产） |
| R7 | 豁免变量取值非法（typo） | raise（防 `read_only_ackk` 静默放行） |
| D7 | production/staging + HA3 endpoint 非空 + 表名空 | raise（消除双标默认值） |

### 7.2 豁免变量语义总表

| 变量 | 合法取值 | 语义 | 时效 |
|---|---|---|---|
| `RAG_ALLOW_REMOTE_DB` | `read_only_ack` | "我知道这是远程/生产 RDS，本会话只读" | 进程级 |
| `RAG_ALLOW_REMOTE_SEARCH` | `read_only_ack` | 同上，针对 HA3/OpenSearch | 进程级 |
| `RAG_DESTRUCTIVE_PROD_ACK` | `<op>:<YYYY-MM-DD>` | 非生产环境对生产目标执行指定破坏性操作的当日授权，如 `deactivate_old_chunks:2026-06-11` | **当日有效**，过期失效（防残留 export） |
| `RAG_ALLOW_SHELL_OVERRIDE` | `VAR1,VAR2,…` | overlay file-wins 的白名单逃生口：列出的变量保留 shell 值 | 进程级 |
| `RAG_READONLY` | `true` | PROD-RO 会话声明；所有写路径守卫强制拦截（HA3 不支持只读账号时的应用层兜底） | 进程级 |

`.env.prod_ro` 内置 `RAG_ALLOW_REMOTE_DB/SEARCH` 两行 ack（它是合法的"标签≠目标"用例）；[envboot.py](../eval_harness/envboot.py) 同理 `setdefault` 两行 ack。

### 7.3 env_guard 运行时断言 — 已实现（`opensearch_pipeline/env_guard.py` + 9 调用点 + GuardedBucket；测试 `tests/test_destructive_guard.py`）

新模块 `opensearch_pipeline/env_guard.py`：`assert_destructive_write_allowed(op, target, kind)` —— production 环境放行；非生产目标放行；**非生产环境 → 生产目标**需当日 `RAG_DESTRUCTIVE_PROD_ACK`。接入 9 个调用点：`node_deactivate_old_chunks`(L3421)、`_search_delete_old_chunks`(L3350，咽喉点同时覆盖 spot_checker reconcile)、`spot_checker._delete_chunks_from_index`(L20)、`node_write_chunk_meta`(L3085)、`node_acquire_index_lock`(L3262)、`node_push_to_opensearch`(L4086)、`node_update_index_status`(L4410) 等。守卫在首个网络调用前触发，无半删状态；DAG 2h 失效锁接管天然兜底重入。

OSS 咽喉加 `_GuardedBucket` 代理（`_get_oss_bucket` 真实分支返回）：拦 `put_*`/`delete_*`、透传读/签名；非生产环境写**生产桶**（桶名命中指纹）需当日 ack，staging 桶/其他桶放行。注意这只是兜底——本地正常形态是 `simulate_oss=true` 根本不进真实分支，该守卫只防"本地误设 simulate_oss=false + 生产桶"的配置漂移。

### 7.4 prod_access 统一入口 — 已实现（`opensearch_pipeline/prod_access.py`；已迁 local_e2e_ingest/reset_index_for_repush/seed_reingest_batch/envboot；测试 `tests/test_prod_access.py`）

新模块 `opensearch_pipeline/prod_access.py`：`load_prod_env()`（dict 返回，**不污染 os.environ**）、`get_prod_readonly_conn()`（pymysql 会话级 READ ONLY）、`get_prod_rw_conn(ack)`（要求 `PROD-RW:<today>`）、`get_prod_oss_bucket(readonly=True)`、`get_prod_ha3_client(readonly=True)`。

纪律：**脚本不许再自行解析 `.env.production`**（R-5 的根治）。需要生产连接的脚本一律 `from opensearch_pipeline.prod_access import ...`，守卫与审计点收敛到一个模块。存量脚本迁移清单见 §10.1。

---

## 8. 用户控制台操作 checklist

按序执行（代码侧 P0-P3 完成后启动）；每步附验证方法。

- [ ] **1. 建 RAM 子账号 ram-rag-ro**，挂 §6.1 只读策略，生成 AK。
      验证：`ossutil ls oss://fuling-knowledge-base/raw/ -i <ro-ak>` 成功；`ossutil cp localfile oss://fuling-knowledge-base/tmp_probe -i <ro-ak>` 应 **AccessDenied (403)**。
- [ ] **2. 建 ram-rag-stg / ram-rag-prod**，分别挂 §6.1 对应策略。
      验证：stg key 读生产桶应 403；prod key 写 staging 桶应 403（显式 Deny 生效）。
- [ ] **3. 新建 `fuling-knowledge-base-staging` 桶**（同区域、同 key 布局），`ossutil sync` 灌语料子集。
      验证：stg key 对该桶 put/get/delete 全通；`ossutil ls` 子集文档数符合预期。
- [ ] **4. RDS 建 `fuling_ro` 只读账号**（§6.2）。
      验证：`mysql -u fuling_ro` 下 `SELECT count(*) FROM chunk_meta` 成功，`UPDATE chunk_meta SET ...` 应 **ERROR 1142 (权限拒绝)**。
- [ ] **5. RDS 建 `fuling_knowledge_stg` / `fuling_operation_stg` 库 + `fuling_stg` 账号**，跑 [schema/](../schema/) 001 + 002 两套 DDL。
      验证：`fuling_stg` 对 `*_stg` 库 CRUD 全通；`USE fuling_knowledge` 应 **ERROR 1044**。
- [ ] **6. HA3 控制台建 `fuling_kb_chunks_stg` 表**（复制生产表结构：字段/向量维度/分析器逐项核对）。
      验证：推 1 条测试 doc 到 stg 表并查回；生产表 doc 数不变。
- [ ] **7. HA3 尝试建只读用户**（产品能力待验证，见 §10.3）。
      验证：若支持，只读用户执行 push/delete 应被拒；若不支持，记录结论，依赖 `RAG_READONLY` 应用层兜底。
- [ ] **8. 替换各 .env 凭证**：`.env.local*` / `.env.prod_ro` 换 ram-rag-ro AK + `fuling_ro`；`.env.staging` 填 ram-rag-stg + `fuling_stg`。
      验证：`make ab-up && make ab-smoke` 通过；`RAG_ENV=prod_ro python scratch/run_preflight.py` 只读路径正常。
- [ ] **9. SAE + DataWorks 录入生产环境变量**（对照 `.env.production.template` 全变量名 + ram-rag-prod AK）。
      验证：SAE 重启后 `/api/ask` 正常应答含图片；DataWorks 三个 stage 节点空跑（无待处理文档日）绿色退出。
- [ ] **10. 删除 `.env.production` 本体**（模板已提交，注入已验证）。
      验证：`git status` 干净；`RAG_ENV=production python -c "from opensearch_pipeline.config import load_config; load_config()"` 在本地因缺凭证 fail-fast（这是期望行为）。
- [ ] **11. 禁用旧全权 AK**（控制台禁用，**禁用≠删除**），观察 48h。
      验证：SAE 日志、DataWorks 日运行、本地采样/签名全部无 `InvalidAccessKeyId` / 403。
- [ ] **12. 删除旧 AK**。
      验证：RAM 控制台该 AK 状态为已删除；全链路再观察一个日批次。

---

## 9. 分阶段迁移计划（P0-P6）

| 阶段 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| **P0** | env 文件重组：创建 `.env.prod_ro`（拷自 .env.test + `RAG_READONLY=true` + 双 ack，凭证暂沿用）；`.env.test` → symlink；提交 `.env.production.template`；`.gitignore` 加 `.env.prod_ro`；`.env.example` 同步。**`.env.production` 本体暂不删** | `RAG_ENV=test` 经 symlink + 别名 + DeprecationWarning 正常加载 | 删 symlink 恢复原文件名，零代码依赖 |
| **P1** | PR1 config 加载期校验（§7.1 矩阵 + provenance banner + `RAG_READONLY` 字段 + D7 fail-fast + envboot ack 适配）——**已落地** | `make test` 新增用例全过、既有失败集与 24 项基线 diff 一致；手测 dev 标签 + 生产指纹 → `EnvironmentMismatchError`，加 ack 通过；`make sim` 正常 | revert config.py 单文件 |
| **P2** | PR2 运行时守卫：`env_guard.py` + 9 调用点 + `_GuardedBucket` + `scripts/sample_corpus.py`；`.env.local*` 改 `RAG_SIMULATE_OSS=true` | `tests/test_destructive_guard.py`（当日 ack / 过期 ack / production 放行 / simulate 不触达 / 桶前缀分级）；模拟写被 `DestructiveOpBlocked` 拦截 | 守卫函数改恒放行（单点开关），调用点不回退 |
| **P3** | PR3 `prod_access.py` + 代表性脚本迁移（`local_e2e_ingest::_prod_conn`→readonly、`reset_index_for_repush`/`seed_reingest_batch`→rw+ack、envboot→读 `.env.prod_ro`）；D2 staging 使能（`RAG_RDS_OPERATION_DATABASE` + 11 处机械替换 + `.env.staging`） | `tests/test_prod_access.py`（mock pymysql 验 init_command 与 ack）；`RAG_ENV=test python scratch/local_e2e_ingest.py --status` 正常 | 被迁脚本逐个 revert，prod_access 可空置 |
| **P4** | 控制台操作 §8 步骤 1-9（RAM/桶/RDS/HA3/凭证替换/平台注入） | 每步内联验证（403 / 1142 / 1044 探针） | 新账号禁用即回滚；旧 AK 此阶段仍有效 |
| **P5** | §8 步骤 10-12：删 `.env.production` 本体 → 禁用旧 AK 观察 48h → 删除 | 48h 无认证错误 | 禁用期内一键重新启用旧 AK（这就是"禁用≠删除"的意义） |
| **P6** | 遗留收尾：§10.1 存量脚本机械迁移、§10.2 os.environ 治理、HA3 只读用户结论回填本文档 | ruff 零告警；`grep '\.env\.production' scratch/` 归零 | 各脚本独立改动，单点 revert |

---

## 10. 遗留与后续

### 10.1 存量脚本机械迁移清单

`grep -rl '_load\|\.env\.production\|\.env\.test' scratch/` 共 45 个文件 + `scripts/` 2 个 + `eval_harness/envboot.py`。处置分三组（写语句判定基于 grep `INSERT/UPDATE/DELETE/push_documents/put_object`，**迁移时逐个核对**）：

| 组 | 文件 | 处置 |
|---|---|---|
| **A. 含生产写，迁 `get_prod_rw_conn(ack)`** | `reset_index_for_repush` `seed_reingest_batch` `seed_rotation_docs` `seed_versions` `reset_empty` `reset_stuck` `tier3_sandbox` `hold_backlog` | P3 已迁 2 个（reset_index_for_repush / seed_reingest_batch），其余 P6 |
| **B. 读生产 + 写本地，迁 `get_prod_readonly_conn()`** | `local_e2e_ingest`（`_prod_conn`）`local_e2e_old_mirror` `abtest_adsampling` `export_jsonl_v2` | P3 已迁 local_e2e_ingest |
| **C. 纯只读诊断/监控（~33 个），经 `.env.test` symlink 零改动可用，P6 批量换 `load_prod_env()`** | `analyze_pdf_layout` `bot_query_test` `check_extraction_quality` `check_progress` `diag_ann_selfquery` `diag_backlog` `diag_blast_radius` `diag_cosine_touchdian` `diag_ha3_index_def` `diag_paths_touchdian` `diag_relevance_touchdian` `diag_schema` `diag_target_vec` `ha3_query_laptop` `ha3_query_prod` `local_ab_eval` `local_e2e_eval` `monitor_rebuild` `monitor_stage2` `monitor_stage3` `poll_live` `poll_reindex` `poll_repush` `reembed_missing` `repro_faq_empty` `run_preflight` `tier1_preflight` `trigger_reindex` `verify_oss_canonical` `verify_stage2` `verify_state` `watch_activation` `watch_v2_build` `watch_v3_build` `watch_v4_build` | 凭证换 ro 后自动降权，迁移只是收敛入口 |
| **D. scripts/** | `export_full_to_oss_for_v2.py`（OSS 写→rw+ack）、`validate_v2.py`（只读） | P6 |

### 10.2 os.environ 直读治理（A/B/C 分类）

`opensearch_pipeline/` 非 config.py 仍有 34 处直读，scripts/eval_harness 另有若干。P6 按三类处置：

- **A 类（迁入 config 字段）**：行为开关/阈值类直读（rerank 开关族、pure-text 开关、context cap 等）——它们是"配置"，应收敛进 `load_config()`，享受守卫与 provenance banner。
- **B 类（合法进程引导，保留）**：进程启动前的注入与校验（[dataworks_nodes/](../dataworks_nodes/) required_vars 检查、envboot、uvicorn 入口）——它们必须在 config 之前运行，保留但集中注释声明豁免理由。
- **C 类（测试/模拟，保留）**：tests 的 monkeypatch、run_simulation 的 setdefault——不进生产路径，不治理。

### 10.3 HA3 只读用户 — 待验证

HA3（OpenSearch 向量检索版）控制台是否支持实例级只读用户尚未实证（§8 步骤 7）。两种结局都已兜底：支持则 PROD-RO 的检索凭证也物理降权（第一道防线补全）；不支持则依赖 `RAG_READONLY` + `prod_access.get_prod_ha3_client(readonly=True)` 只读代理（第三道防线兜底，D2 的设计动机）。结论出来后回填本节。

### 10.4 其他

- `locale2e_*` 索引名 grandfathered（§5.3），新索引一律 `local_{用途}_{vN}`。
- 本文档与 [local_eval_env.md](local_eval_env.md) 互链维护：那边是 LOCAL-EVAL 层的操作手册，环境矩阵以本文为准。
- 守卫豁免变量新增/变更时，同步更新 §7.2 总表与 [CLAUDE.md](../CLAUDE.md) 配置节。
