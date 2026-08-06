# 镜像发布记录 · 2026-08-06（`efac342`）

> ⏱ 时钟口径:本文用**北京时间**(与 DataWorks/组织快照/拒绝台账 stat_date 同口径)。
> 开发机与 RDS 均为 Pacific(-07:00),故 CI 与 git 时间戳会比正文日期早一天,勿误读为两次发布。
>
> ⚠️ **本次发布豁免了 `make release-gate`**(Sam 当场决定,本文件是留痕不是事后追认)。
> **豁免依据比前两次都实,三次的理由各不相同,别当成同一件事**:
>   · 08-04:「缺陷修复/UX 收口,**无检索行为变更**」;
>   · 08-05:我特意留痕**不能套用**上一条 —— 该批含 ACL 判定链路改动(部门名漂移修复),
>     只是整体被默认关的 `RAG_ACL_ANCESTRY` 门控;
>   · **本次:检索链路【逐文件核过】零改动** —— retriever / llm_generator / reranker /
>     chunker / pipeline_nodes / embedding / answer_flow / content_blocks 一个文件没碰,
>     改动面只有上传 / 限流 / 前端。**且补跑了两个语料无关的门作为实证**(§4),不是空口豁免。

## 1. 工件

| 项 | 值 |
|---|---|
| git sha | `efac342`（`efac3426…`） |
| push 区间 | `bd3a6bd..efac342`（2 commits；本轮全部改动见 §2，跨 `f918e37..efac342`） |
| 上一版镜像基线 | `f918e37`（2026-08-05 第五次发布） |
| ACR tag | `<ACR_REGISTRY>/fuling/rag-serving:efac34260d3a` |
| manifest digest | `sha256:4574d0a65b1f77f943def215391aea2e9f00ee1b2548ba302723b906f919f472` |
| promotion run | [31072091493](https://github.com/l948931710/opensearch-rag-pipeline/actions/runs/31072091493)（`build-smoke` ✅ + `promotion` ✅） |
| 本地验证 | `make test` 4254 passed / `make lint` clean / vitest 470 / vue-tsc 0 / Playwright 402 |

## 2. 本批内容（全部服务于"各部门自助批量上传"这一场景）

**① 误重传两层防线**（当日实际造成 66 个重复件，见 §3）
- `d3f5887` 前端：批末选择列表**收敛成失败集** —— 此前批末不清空，部分失败后"再点一次
  上传"会整批重传，而每次 `action=new` 现铸新 doc_id+upload_id ⇒ raw_key 必不同 ⇒
  register 幂等键对不上 ⇒ **已成功的悄悄变重复件**（ETag 查重是 advisory 只提示不拦）。
- `e2acdd9` 服务端：同内容 + 同归属 + 24h 内已入库 → **409 硬拦**。三个限定条件都是
  有意收窄（跨部门各留一份是真实场景；三个月后重传是正当操作；已退役重传是"恢复"）。

**② `bd3a6bd` 登录换令牌拆出独立桶** `auth_per_min`（`RAG_RATE_AUTH_PER_MIN`，默认 300）。
`/api/auth/dingtalk` 在身份建立**之前**，只能按 IP 计数，而全公司共用一个 NAT 出口 ⇒ 该
actor 实际代表整个公司。挂共享 aux 桶时，一条广播让几十人同时开小程序就把桶打满 ⇒
**他们登不进来**（症状远比"控制台慢"严重，却长得像"服务挂了"）。
**观测收益**：拒绝台账从此能区分 `auth_per_min` / `aux_per_min` —— 08-05 那次 344 次拒绝
定不了性，正因两者混在同一 reason 码里，而现网无 SLS 可查。

**③ `d2a684c` 单文件上限 50MB → 150MB**，并配置化 `RAG_MAX_UPLOAD_MB`。
改前核过三个下游边界：抽取下载闸 `RAG_EXTRACT_MAX_BYTES` 默认 200MB（150 在其下）、
xlsx >100MB 自动切 read_only、50MB 那道闸只在自助上传路径上。
⚠️ **下一个天花板是 30 分钟 TTL**（签名 PUT 与 upload token 共用）：150MB 需 ≥0.67 Mbps
持续上行，弱网可能超时。再往上调必须连 TTL 一起调。

**④ `efac342` 限流拒绝码文案补齐 + 跨语言 parity 守卫**。后端 11 个码、前端表只有 7 个，
其中 `auth_per_min` 正是当日 ② 引入的漂移 —— 文案缺失会让运营面板原样吐机器码，把 ② 的
观测收益抵消掉。新增 `tests/test_admission_reason_parity.py` 双向守卫（跨语言契约编译器
管不到，前端兜底是 `|| r` ⇒ **漂移不报错、只是悄悄变难读**）。

## 3. 当日语料事件（与本次发布互为因果）

各部门经控制台上传 **799 篇**（全 `acl_mode='node'`，走组织树归属），其中 **66 组误重传**
（同 etag + 同归属节点 + 同文件名，间隔 47s~18min）。已按 `kb_retire` 语义清理：保留
`created_at` 最早的一篇、退役较晚的一篇，66 篇全部 `PENDING_DELETE` 入清除队列，
审计 `trace_id=dedupe-20260806` 可追溯，**操作可逆**（控制台「恢复上线」）。
清理时全部 0 chunk（尚未摄取）⇒ 纯元数据翻转，零 HA3/chunk 连带清理 —— 这也是**为什么
必须在摄取跑起来之前处理**：等 66 个副本各自抽取/嵌入/推 HA3，成本高一个量级。
脚本：`scratch/dedupe_reupload_20260806.py`（dry-run 默认，`--commit` 需当日 PROD-RW 令牌）。

⚠️ 判据前置实证：66 组**全部恰好 2 篇**、文件名 66/66 相同、可见级别/归属/标题/上传人
**四项零差异**、跨节点 0 组 ⇒ 不存在"各部门各留一份"的正当情形被误伤。

## 4. 替代证据：两个语料无关的门（现在就有判别力）

`release-gate` 的第 2-4 段（live run → judge 面板 → merge --strict）此刻**没有判别力**：
全库仅 6 个活跃 chunk，258 题金集会几乎全挂，挂的是"没语料"不是"质量回归"。故改跑：

| 门 | 结果 |
|---|---|
| `make general-ability-eval`（**离线零网络**） | ✅ **PASS**：金集 258 题**被劫持 0 题**；门3 分级路由 **84/84**（adversarial 15/15 · enterprise_uncovered 15/15 · general_knowledge 10/10 · office 14/14 · office_boundary 6/6 · realtime 4/4 · sensitive 10/10 · smalltalk 10/10） |
| `make sim-all`（4 场景全链路模拟） | ✅ 通过：版本更新正确停用旧 chunk、chunk_meta 19 条、bulk payload 正常 |
| 基线 regime 漂移核查 | ✅ **未漂**：当前 LLM `qwen3.7-plus` / embedding `text-embedding-v4` 与 `baseline.json` 冻结时一致 ⇒ 语料回来后基线**可直接复用**，无需因模型换代重冻 |

## 5. 进度

- [x] CI 三 workflow 绿（CI / Frontend / image，`efac342`）
- [x] push 前 PII 自查（长数字串 / 26 个真实姓名 / 手机证件形态，三类零命中）
- [x] ACR 促升（run 31072091493，`acr-promotion` 门 Sam approve）
- [x] SAE 切镜像（Sam，2026-08-06）
- [x] **冒烟四联绿**：`/api/version` `git_commit=efac342` ✅ / `/api/health` 200 (0.89s) /
      `/console/` 200 / `/api/kb/config` **`max_upload_bytes=157286400`（=150MB，本批改动的直接实证）**
- [ ] **799 篇待摄取**：活跃 chunk 仍 6，DataWorks 一次 100 篇 ⇒ 约 8 轮
- [ ] 摄取跑起来后复查：是否再出现新的重复件（两层防线已上线，应为 0）
- [ ] 正门补验：重灌收敛 → 金集重标 → baseline refreeze 后跑 `make release-gate`
