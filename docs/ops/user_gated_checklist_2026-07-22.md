# User-gated 总清单（Majors α-δ 收官版，2026-07-22）

> Majors ε2（codex 共识 2026-07-21，方案 §5.2）。**单页原则**：仓库代码侧已全部就位
> （默认 off / byte-identical），下面每一项都是**只有 Sam 能做**的外部动作——云控制台、
> secret、真实环境 apply、翻闸拍板。每项给「做什么 / 前置 / 验证」；状态列由 Sam 勾销维护。
> 铁律回顾：**flag 开启前先 apply 对应 schema**（apply-before-enable）；deploy 双门
> （release-gate 绿 + Sam 明确要求）；ack token 一律 Sam 亲设。

## A. SAE env（随下次重打包/重部署窗，一次配齐）

| # | 项 | 做什么 | 前置 | 验证 |
|---|---|---|---|---|
| A1 | 姿态四联 | SAE env 加 `RAG_REQUIRE_AUTH=true` + `RAG_ACL_FAIL_CLOSED=true`（B1 断言止血 ack 退役）+ `RAG_RDS_REQUIRE_TLS=true`（γ5）+ `RAG_OPS_ALERT_REQUIRE=true`（γ7） | TLS：CA 已随包（certs/aliyun-rds-ca.pem）+`RAG_RDS_SSL_CA` 已配；webhook：A2 | 启动日志无 LEGACY-OPEN critical；`/api/ready` 全绿；故意漏配任一 → 拒启动 |
| A2 | 告警 webhook | `RAG_OPS_ALERT_WEBHOOK`+`RAG_OPS_ALERT_SECRET`（现网 07-21 已配——确认仍在，随 RB-06 轮换更新） | — | attestation 文档 §4（`alerting_chain_attestation_2026-07-21_DRAFT.md`）回填去 _DRAFT |
| A3 | 观测翻闸 | `RAG_COST_ALERT_ENABLE=true`（γ2，纯观测）；（op0 部署形态）`RAG_AGENT_HEALTH_ENABLE` 见 E2 | A2 | 熔断触发时运维群收到 cost-breaker-run/daily |

## B. schema 054-058 三环境 apply（op0 域；**flag 开启的硬前置**）

| # | 文件 | 本地 dev | staging | prod | 开闸绑定（apply 后才准开） |
|---|---|---|---|---|---|
| B1 | 054_agent_run_request_identity | ✅ 07-21+台账 | ☐ | ☐ | `RAG_AGENT_ASK_IDEM_ENABLE`（readiness ask_idem_contract 钉住） |
| B2 | 055_agent_step_bookkeeping | ✅ 07-21+台账 | ☐ | ☐ | 无 flag（代码可先行，apply 后幂等记账自动生效） |
| B3 | 056_agent_daily_metrics | ✅ 07-22+台账 | ☐ | ☐ | `RAG_AGENT_HEALTH_ENABLE`（另见 E2 的 018 前置） |
| B4 | 057_llm_call_log_price_version | ✅ 07-22+台账 | ☐ | ☐ | `RAG_MODEL_PRICE_TABLE_JSON` 非空（readiness price_table_contract） |
| B5 | 058_agent_quota_lock | ✅ 07-21+台账 | ☐ | ☐ | 任一 `RAG_AGENT_APPROVAL_*_CAP`>0（缺表=fail-closed 全拒） |
| B6 | 018 **operation 侧** rag_runtime_contract | ☐（本地缺，γ 实测 heartbeat 用例 skip） | 核对 | 核对 | E2 开 health 且 durable dispatch on 时 worker_heartbeat 维需要它 |

apply 姿势：`RAG_ENV=<local\|staging> python scripts/apply_migration.py schema/<文件> --db operation --commit`；
prod 走 prod_access 当日 RW token。每次 apply 同会话进 `schema_migrations` 台账（apply 脚本自动）。

## C. DataWorks（与「清理stage3 节点重贴」同族，**同一窗口做完**）

| # | 项 | 做什么 | 注意 |
|---|---|---|---|
| C1 | zip 重打包+三份资源 | `deploy/build_dataworks_zip.sh <dir> --hmac`（本地 env 先设 `RAG_DW_ZIP_HMAC_KEY`）→ 上传 zip + .sha256 + .sha256.hmac | 按 SIZE/SHA 认包不按文件名；**换 zip 必换全部 sidecar** |
| C2 | 调度 env 配 key | DataWorks 调度参数加 `RAG_DW_ZIP_HMAC_KEY`（与 C1 同 key）；稳定后加 `RAG_DW_SIDECAR_STRICT=on` | key 在**env 面**（≠资源面）才构成信任边界；能读调度 env 者不在防护面（如实） |
| C3 | 节点重贴 | ops_health_monitor 正文重贴（--only 已加 agent_health/audit_digest）+ 清理stage3 过期重贴 | ⚠️ **重贴须与 C1 新 zip 同窗**——旧 zip 的 argparse 不识新作业名直接红 |
| C4 | py37 传递闭包回填 | 任一节点跑一次 → 取日志里 `pip freeze` 段回填冻结清单（δ3 已在 11 节点加打印） | 回填后 ci 审计可改对冻结清单跑（基线文件头有复评触发条件） |
| C5 | cosign/gpg 信任根 | （远期）制品签名信任根建立 | M13 残余，非本窗必做 |

## D. GitHub / CI（push 窗）

| # | 项 | 做什么 | 验证 |
|---|---|---|---|
| D1 | push 时点拍板 | main（origin+11）/ op0（origin+27）推送 | push 后 **image / stress(push) / frontend-e2e** 三 workflow 首跑绿（M12/M11/M14 的 CI 实跑验证正是此刻） |
| D2 | ACR secrets | repo secrets：`ACR_REGISTRY`/`ACR_USERNAME`/`ACR_PASSWORD` | promotion job 缺 secret 会显式红（不静默） |
| D3 | environment 保护 | GitHub Settings→Environments→`acr-promotion` 加 required reviewers | 无保护规则时 promotion 仍需手工 dispatch+push_acr=true，但少一道人审门 |
| D4 | branch protection | main 分支保护 UI（B7 族遗留） | — |

## E. Agent 面（op0 部署形态；组织 gate 未签前不动播种）

| # | 项 | 做什么 | 前置 |
|---|---|---|---|
| E1 | G1-G9 staging ratification | staging 真实档压测跑通 → G1-G9 容量门 ratify → stress 硬门翻转（owner=Sam，RB-05 E2 归属） | 不在 CI（上线手册裁决） |
| E2 | agent_health 开闸 | `RAG_AGENT_HEALTH_ENABLE=true` | B3(056)；durable dispatch on 时另需 B6(018 op 侧) |
| E3 | audit_digest 开闸 | `RAG_AUDIT_HMAC_KEY` 配好再开 `RAG_AUDIT_DIGEST_ENABLE=true`（缺 key=exit 3 fail-closed 绝不产未签名产物） | 真实 OSS；OSS 前缀默认 audit_digest/ |
| E4 | M7 根治三件套 | 审计独立**只写**RDS 账号 / OSS 摘要前缀 WORM（保留策略）/ HMAC key 进 KMS 托管+轮换 | γ6 只是检测层；做完前 M7 恒 OPEN |

## F. 本机 launchd（仓外 ~/Library，模板在 deploy/）

| # | 项 | 做什么 |
|---|---|---|
| F1 | webhook env | ops-monitor/qa-rollup plist 填真实 `RAG_OPS_ALERT_WEBHOOK/SECRET`（B7 残项；模板占位在 deploy/com.fuling.*.plist） |
| F2 | 新作业接入 | ops-monitor plist 的 --only 名单按需加 `agent_health`/`audit_digest`（当前仓外 plist 未含；flag off 时加了也只是 skipped） |
| F3 | qa-rollup exit=2 核查 | launchd qa-rollup 曾见 exit=2——按 `_job_exit` 语义（2=drift/breach 非错误）核对是真 SLO breach 还是环境性翻转（readiness 评审遗留待看项） |

## G. 状态台账（Majors §5.4，截至 2026-07-22 代码侧收官）

| M | 状态 | 残余（具名） |
|---|---|---|
| M2 钉钉 serving 防护 | **代码侧关闭**（β） | 翻闸：`RAG_DT_MAX_WORKERS`/`RAG_DT_ADMISSION_ENABLE` |
| M3 commit-ACK/记账幂等 | **代码侧关闭**（α） | 055 staging/prod apply（B2） |
| M4 ask 幂等 | **代码侧关闭**（α） | 054 apply → 开 `RAG_AGENT_ASK_IDEM_ENABLE`（B1） |
| M5 stewardship 轮换 | **代码侧关闭**（α） | — |
| M6 审批积压治理 | **代码侧关闭**（α+γ3 老化指标） | 058 apply → 设 caps（B5）；SLA 阈值默认 24h |
| M7 审计防篡改 | **OPEN**（γ6=检测层缓解） | E3 开闸 + E4 根治三件套；当日内篡改窗不在检测面 |
| M8 RDS TLS 姿态 | **代码侧关闭**（γ5） | A1 翻 `RAG_RDS_REQUIRE_TLS` |
| M9 agent 可观测 | **代码侧关闭**（γ1+γ3） | B3/B6 → E2 开闸 |
| M10 webhook 姿态 | **代码侧关闭**（γ7） | A1 翻 `RAG_OPS_ALERT_REQUIRE`；attestation 去 _DRAFT |
| M11 压测触发 | **PARTIAL**（δ1 push 通道） | E1 ratification+硬门翻转；CI 实跑随 D1 |
| M12 镜像工件 | **PARTIAL**（δ2 两 job 线） | D2/D3 + 首跑随 D1；SAE 镜像化迁移后 ZIP 退役 |
| M13 DW 供应链 | **PARTIAL**（δ3 HMAC/STRICT/审计面） | C1-C5 全串 |
| M14 Playwright CI | **代码侧关闭**（δ4，本地硬门 main 159/op0 213 全绿） | CI 首跑随 D1 |
| M15 成本可见性 | **代码侧关闭**（γ2+γ4） | A3 翻闸；价表 JSON 配置（B4 前置）；历史回填=另行 scratch 脚本 |

> 交叉引用：B7 残项族（关 8000 / RB-06 轮换含钉钉机器人 secret / B2·E2）另见
> `enterprise-agent-prod-review` 台账——本清单不重复计 owner，重叠项（A2/F1/E1）已标注。
