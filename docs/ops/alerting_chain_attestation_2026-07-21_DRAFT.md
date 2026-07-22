# 告警链端到端闭环 · 运维 attestation（2026-07-21）— DRAFT

> Majors ε1（codex 共识 2026-07-21，`docs/majors_remediation_plan_2026-07-21_DRAFT.md` §5.1）。
> **本文两部分**：§1-§3 是仓库可证的代码/部署侧证据链（已定稿）；§4 的现网验证细节
> **以 Sam 的现网记录为准**，当前为回填槽。§4 回填并经 Sam 确认后，本文去 `_DRAFT`。
> 姿态原则（γ7/M10 同款）：**配置存在 ≠ 送达证明**——本文存在的意义就是把「送达」钉成
> 有时间、有指纹、有触发用例的记录，而不是「webhook 配了应该就通了」。

## 1. 链路形态（三个发送面 → 一个 sink）

发送面（全部经 `opensearch_pipeline/alerting.py::send_ops_alert`，fail-open 契约：
告警失败绝不阻断触发它的业务操作）：

| 发送面 | 进程 | env 注入方式 |
|---|---|---|
| SAE serving（api/qa_logger/readiness/reaper 等 15+ 调用点） | SAE 应用 | SAE 控制台 env（随重打包/重部署窗配置） |
| DataWorks 节点（ops_health_monitor / stage 节点 / retention 等） | PyODPS 粘贴节点 | 节点脚本「凭据」区 `os.environ` 赋值（正文注释含机器人**铸造步骤**：群设置→智能群助手→自定义 Webhook→安全设置选「加签」） |
| 本机 launchd（com.fuling.ops-monitor / qa-rollup） | 用户机 | `~/Library/LaunchAgents/*.plist` env 块（仓侧模板 `deploy/com.fuling.*.plist` 留占位） |

sink：钉钉运维群自定义机器人（`oapi.dingtalk.com/robot/send` + HMAC 加签），
`RAG_OPS_ALERT_WEBHOOK` + `RAG_OPS_ALERT_SECRET` 两变量成对。

## 2. 链路防护/可观测面演进（仓库证据，按时间）

| 时间 | 事件/加固 | 证据（代码/提交） |
|---|---|---|
| 2026-07-15 | **实锤缺口**：`reconcile_ha3` 的 CRITICAL 每天被 SUPPRESSED——节点日日 Failure 无人收（webhook 未配时告警只活在日志） | `dataworks_nodes/ops_health_monitor_node.py` 凭据区注释（⚠️ 必填 + 铸造步骤） |
| （P0-05 报告1） | webhook 未配不再静默 no-op：critical 升 `logger.error`（SUPPRESSED-CRITICAL 可 grep） | `alerting.py::send_ops_alert` |
| （B5 P2-08） | SSRF 防线：webhook 仅 https + 域白名单（`*.dingtalk.com`，自建网关走 `RAG_OPS_ALERT_WEBHOOK_ALLOW`），env 被污染时 file://（VPC 元数据地址）不可达 | `alerting.py::_webhook_allowed` |
| （B6 P2-15） | 压制可见化：进程内「该发未发」计数 `suppressed_stats()`，governance 看板/security_posture 消费 | `alerting.py::_note_suppressed` |
| （批次7 ultra） | 钉钉在 **HTTP 200** 里用 errcode 报失败（310000 签名不符/关键词过滤、130101 机器人限流 20 条/分）——解析 body，errcode≠0 记 ERROR 返 False，堵「配错 secret 全部告警静默蒸发且函数还返回 True」 | `alerting.py`（errcode 解析段） |
| 2026-07-21 | **成功路径留痕**：成功发送也打日志，带 title + webhook access_token **前 8 位指纹**——起因是一轮实弹诊断中「发了但进错群」（节点配了旧机器人 token）与「压根没发」在日志上无法区分 | `alerting.py::_wh_tag`；main `c747ff8` / op0 `9d07ae9` |
| 2026-07-21/22（Majors γ） | 触发面补齐：成本熔断 RUN/DAILY 接告警（`RAG_COST_ALERT_ENABLE`，默认 off）；agent_health 越阈告警（op0，`RAG_AGENT_HEALTH_ENABLE`，默认 off）；`RAG_OPS_ALERT_REQUIRE` 姿态断言（prod/staging 强制 webhook 在配且过域校验，默认 off） | γ2/γ3/γ7 提交（main `fe4371b`/`4a3e452`，op0 `c17e925`/`4dbcece`/`61c1728`） |

## 3. 部署侧事实（截至 2026-07-21，摘自运维台账）

- SAE 现网版本 `b9eeb873` 的部署包含**告警链端到端闭环**（与 RDS TLS LIVE、三生产子线中文标签同窗）。
- B7 台账中「webhook 配置」项 2026-07-21 已消（SAE / DataWorks 侧）；**本机 launchd env 的
  webhook 仍未配**（`deploy/com.fuling.*.plist` 模板留占位）——残项归 user-gated 清单
  （`docs/ops/user_gated_checklist_2026-07-22.md` §F）。

## 4. 现网端到端验证记录 ——【待 Sam 回填，回填后去 _DRAFT】

> 以下每项以 Sam 的现网记录为准；仓库侧不代填任何具体值。

- [ ] 验证时间（北京时间）：____
- [ ] 触发用例（哪条告警：parity 漂移 / SLO breach / 手工探针 / 其它）：____
- [ ] 发送面（SAE / DataWorks 节点名 / launchd）：____
- [ ] 日志留痕行（`ops-alert sent: <title> -> <token 前8位>…`）：____
- [ ] 收到告警的钉钉群名 + 机器人名：____
- [ ] token 前 8 位指纹与目标机器人一致（排除「进错群」形态）：是 / 否
- [ ] 加签 secret 生效（无 310000）：是 / 否
- [ ] （可选）群消息截图/消息 ID 存档位置：____

## 5. 残余与边界（如实）

- 送达证明是**时点性**的：机器人被移群/token 轮换（RB-06 轮换族含钉钉机器人 secret）后
  本 attestation 失效，须重新验证并更新 §4。
- launchd 发送面未配 webhook 前，本机巡检的告警仍只活在日志（SUPPRESSED 计数可见）。
- `RAG_OPS_ALERT_REQUIRE`（γ7）翻闸后，SAE/staging 启动期即强制 webhook 姿态——翻闸窗
  见 user-gated 清单 §A。
