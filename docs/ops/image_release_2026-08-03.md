# 镜像发布记录 · 2026-08-03（`d34a1bd`）

> ⚠️ **本次发布豁免了 `make release-gate`**。豁免是 Sam 2026-08-03 当场决定，
> 本文件是留痕，不是事后追认。

## 1. 工件

| 项 | 值 |
|---|---|
| git sha | `d34a1bd8e9d47c266ffc9cefcc709adef48e0cce` |
| 镜像 | `fuling-registry.cn-hangzhou.cr.aliyuncs.com/fuling/rag-serving:<CI promotion run 的 12 位 sha>` |
| 发布路线 | ✅ **CI 正式路线**（`image.yml` workflow_dispatch + `push_acr=true`，过 `acr-promotion` 审批门）|
| 大小 | 412MB |
| 上一版镜像基线 | `4a01d0a`（本次含 **41 个 commit**） |

✅ **最终采用 CI 正式路线**（Sam 2026-08-03 选定）。本地曾构建并开始推送同内容镜像，但**未完成 manifest、ACR 无可用 tag**，已主动中止以免与 CI 抢同一 tag。

以下关于「本地构建」的说明保留作背景：本地 `docker build` 产物与 CI 工件的差别在于—— 与 `.github/workflows/image.yml`
的正式路线相比，**缺少 attestation**（v1/v2 两份 immutable artifact 与 manifest digest 记录）。

✅ **更正（2026-08-03，核 workflow 后）**：本文件初稿曾写「CI 会在 main 推送后自行构建同 sha
并可能覆盖本 tag」——**这是错的**。`image.yml` 的 ACR 推送 job 条件是
`if: github.event_name == 'workflow_dispatch' && inputs.push_acr == 'true'`（:132），
**只有手动 dispatch 且显式勾 `push_acr` 才推 ACR**；推 main 仅触发 build+smoke。
⇒ 本次本地推送的 tag **不会被 CI 自动覆盖**。
（`d34a1bd` 的 push run 已 completed/success——CI 侧的 build+smoke 独立复现了本地结果。）

📌 **若要正式（带 attestation）的工件**：手动 dispatch `image.yml` 并勾 `push_acr=true`。
该 job 挂在 `environment: acr-promotion`（:136，required reviewers）⇒ **那时才需要你 approve**。
上一版 `4a01d0a` 正是走的这条路（记录里有其 workflow_dispatch run）。

## 2. 为什么豁免 release-gate（不是"忘了跑"）

`deploy/eval_release_gate.sh` 头部写明**强制 live**，需 prod-READ 凭据（DashScope key +
HA3 endpoint/instance/creds）。而**当前处于语料真空期**：本会话早前实测生产 HA3
**0 条 active chunk**（节点 ACL 重设计后 1562 篇软退役 + HA3 清除，语料待重传）。

⇒ 对空索引跑检索评测，recall 必然接近 0、gate 必然红。**该闸此刻不适用，而非未通过。**

## 3. 替代闸（全绿）

| 闸 | 结果 |
|---|---|
| `make lint` | 0 |
| `make test` | **4121 passed / 0 failed** |
| console `npm run typecheck` | 0 |
| console `vitest` | **408 passed** |
| console Playwright `e2e` | **216 passed** |
| 镜像构建 | exit 0，412MB |
| 镜像冒烟 | `/api/health` 200 · `/console/` 200 · 挂载点齐全 |
| **UI 产物实证** | 在镜像内 `next-dist/assets/*.js` 里逐条 grep 到本批修复文案（见下） |

UI 实证（证明镜像装的是新代码，不是旧产物）：
`需知识库管理员退役/恢复`（P2-12）· `暂不支持翻页`（复审任务翻页收窄）·
`正在回答上一个问题`（retry 忙时禁用）· `该会话的问答记录将从本地和服务端移除`（删除会话确认）·
`加载更多` ×2（我的贡献 + 复审任务）。

## 4. 🔴 上线后必读（否则会误判"修了没生效"）

1. **schema 063 未 apply。** C9/B′（可见范围意图持久化）读写两侧**都有 capability 探测**，
   未 apply 时**降级跳过、不报错** ⇒ **镜像上了 ≠ C9 生效**。次序本就是「先部署后 apply」，
   apply 需 Sam 当日 `PROD-RW` 授权。
2. **`purge_subject` 行为已变（C5=方案A）。** 它现在会查 OSS 冷归档面，**OSS 不可达即判否**。
   SAE 若未配 OSS 凭据，在其上跑主体擦除会得到失败而非"成功"——这是有意的 fail-closed。
3. **retention 的 DataWorks 节点需补 OSS 四件套凭据**（endpoint/bucket/AK/SK），
   否则 preflight 启动即失败（同样是有意 fail-closed）。⚠️ 生产 OSS 账号的 `PutObject`
   权限**未实测**；首跑请保持 `DRY_RUN=True`。
4. **监控可能开始变红**（`fecf060`）：ops_monitor 的探针 SQL 失败此前被静默吞成 exit 0，
   现改为 **exit 3**。定时任务转红 = 探针真的坏了，不是新 bug。

## 5. 未过外部评审的部分

codex 额度用尽（至 **2026-08-07**）。本批有 **6 条未过 codex**，清单与优先级见
`docs/ops/codex_recheck_backlog_2026-08-03.md`。其中风险最高的两条**都在本镜像里**：
- `e5e29ce` cosurface 补图复核 —— **检索路径 + 授权语义**，backlog B1，建议最先补审；
- `e6ca2f4` C9/B′ —— 授权语义变更（但 063 未 apply 前惰性，见 §4.1）。

## 6. 回滚

上一版镜像 tag（`4a01d0a` 对应者）仍在 ACR；SAE 应用改回旧 tag 即可，分钟级。
另有 **旧 ZIP 应用** 作 break-glass 备胎（其 `DINGTALK_STREAM_MODE=false` 防误启抢流，
回滚需启动 + 改回 true + DNS 切回），详见
`docs/ops/user_gated_checklist_2026-07-22.md` M12 行。
