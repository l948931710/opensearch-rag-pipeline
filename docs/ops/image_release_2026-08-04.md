# 镜像发布记录 · 2026-08-04（`7b0b5d6`）

> ⚠️ **本次发布豁免了 `make release-gate`**。豁免是 Sam 2026-08-04 当场决定（口令
> 「push + CI 跳过 release gate」），本文件是留痕，不是事后追认。
> 豁免依据与 08-03 同例：评测门处于真空期（258 题金集双锚仍锚着已软退役的旧语料，
> regime 无语料指纹），gate 数字在重灌收敛+金集重标完成前不具判别力；本批改动
> 全部为缺陷修复/UX 收口，无检索行为变更。正门补验排在 refreeze 之后。

## 1. 工件

| 项 | 值 |
|---|---|
| git sha | `7b0b5d6`（push 区间 `9beb6a7..7b0b5d6`，37 commits） |
| 上一版镜像基线 | `00921ad`（2026-08-03 第三次发布） |
| 发布路线 | CI 正式路线：push→build+smoke；ACR 需另行 `image.yml` workflow_dispatch + `push_acr=true`，过 `acr-promotion` 审批门（Sam approve） |
| 本地验证 | `make test` 4217 passed / `make lint` / `make sim` / vitest 453 / vue-tsc 全 exit=0 |
| push 前自查 | 名单全名+手机号+staffId+密钥模式扫描 diff 与提交信息双零命中（一处真名在 amend 中洗净后才推送） |

## 2. 本批与重灌波次直接相关的 serving 侧修复

- `c96edb1` 升版取号纳 dv 侧最大号 + 1062 兜底 409——skip-gate 残留行撞号在
  「误重传同文件后再传修正版」场景下不再 500（DW zip 侧同族 `afc4777` 已随
  cf1af1b8 包生效，本件补齐 console 侧）。
- `fa8672c` resign-images node 判定补传 grant/enforce——node 文档进入历史会话后
  图片重签不再全拒。
- `7b0b5d6` 成员管理直进时组织树块隐身——members tab 补拉 config；修复上线前
  绕法=先点「文档台账」再进「成员管理」。
- 独立核验修复批（B1-B8/P3 族）+ console UX 八提交 + C8（`a951b9b`，flag 默认
  off）+ R1（`45f7113`）。

## 3. 待办（本文件写就时）

- [ ] push 触发的 CI/image/Frontend 三 workflow 绿（进行中）
- [ ] `image.yml` workflow_dispatch + `push_acr=true`（dispatch 后 Sam 在
      GitHub `acr-promotion` 环境 approve）
- [ ] SAE 控制台切镜像（Sam）+ 冒烟四联
- [ ] 正门补验：重灌收敛 → 金集重标 → baseline refreeze 后跑 `make release-gate`
