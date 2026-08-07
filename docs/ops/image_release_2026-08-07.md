# 镜像发布记录 · 2026-08-07（`5b0b384`）

> ⚠️ **本次发布豁免了 `make release-gate`**。豁免是 Sam 2026-08-07 当场决定（口令
> 「跳过 release gate」），本文件是留痕，不是事后追认。
> 豁免依据与 08-03/08-04/08-06 同例：评测门处于语料真空期（258 题金集双锚仍锚着已软退役
> 的旧语料，regime 无语料指纹），gate 数字在重灌收敛 + 金集重标完成前不具判别力；本批
> 改动全部为权限修复 / 缺陷修复 / UX 收口，**无检索行为变更**。正门补验排在 refreeze 之后。
>
> 🔴 **豁免的只是 `make release-gate`（评测门）。CI 的 CVE 阻断门没有绕**——它在本次
> 实红两次，两次都是真修（见 §3），CI 在 `5b0b384` 全绿后才促升。这两件事不要混为一谈。

## 1. 工件

| 项 | 值 |
|---|---|
| git sha | `5b0b384`（push 区间 `c239fb7..5b0b384`，12 commits） |
| 上一版镜像基线 | `efac342`（2026-08-06 第六次发布，digest `sha256:4574d0a6…`） |
| 发布路线 | CI 正式路线：push→build+smoke；ACR 促升=`image.yml` workflow_dispatch + `push_acr=true`，过 `acr-promotion` 审批门（Sam approve） |
| 促升 run | 31213040000（workflow_dispatch，head `5b0b384`） |
| ACR tag | `fuling-registry.cn-hangzhou.cr.aliyuncs.com/fuling/rag-serving:5b0b384e48de` |
| manifest digest | `sha256:eb73e1b015c38b667a0d84a9a297349b0b0266d3d9a02dd3c4ee0377b98af63d` |
| 工件 attestation v2 | image_id `sha256:71aab838c708…` / tar `97de7ea37de3…` / lock `ca09de84df18…`（同 run 31213040000 逐字节，build 与 promotion 同 run 不重 build） |
| 本地验证 | `make test` 4469 passed / 2 skipped、`make lint`、console `vue-tsc` exit 0 + vitest 531 passed + build 成功 |
| CI（`5b0b384`） | CI 全绿（db-integration / baseline-freshness / test 3.10+3.11 / security 均 success） |
| push 前自查 | 用仓库自己的 `pii_patterns.ENTITY_PATTERNS` 扫 4215 行新增：email 命中 5 条全为误报（`@router.get`/`@pytest.fixture` 被邮箱正则吃）、secret_like 1 条为测试桩（`token='t'` / `'dev-preview'` / `'e2e-fake-token'`）⇒ 真实命中 0 |

## 2. 本批内容

### 2.1 贡献域迁 node 轴（主线，`f9257fe` + `2f1ca8a`）

- `f9257fe` M1–M11：队列 / 采纳 / 驳回 / 重试 / 入库授权 / 通知**八个消费点**从组码轴迁到 node 轴。
  此前只被 `dept_admin_node_grant` 授权的管理员在贡献域**全域失明**。
- `2f1ca8a` **实现级双盲评审**的四条修复。⚠️ 判例：此前四轮 Codex 双盲评的**全是方案**
  （`.claude-review/claude-model-contribution-node-axis.md` 头一行自述「评审对象：方案，非 diff」），
  641 行实现一个字节没被评过；补评一跑就出两条 MAJOR：
  - **C1**（双方独立提出）提交端只校验节点 active、不校验节点归谁 ⇒ 任一员工可投稿到任意
    119 个活跃节点。定性关键：legacy 轴历来同样不校验 ⇒ **平权非回归**，Codex 据此从
    BLOCKER 降 MAJOR。Sam 裁决补服务端校验。
  - **C3** `myDeptsReady` 三态契约写在注释里但**生产代码从未读过它** ⇒ `/my-depts` 未落地时
    开弹窗提交 = 静默降轴成 legacy 行。
  - 记账：**Codex 误报 0 / Claude 误报 2**（`d>1 静默丢根`、`_CONTRIB_AXIS_PRESENT 未清理`，
    均被 Codex 以 `文件:行` 推翻）。

### 2.2 审批历史两段按轴分流（`daa3a8e`，源自评审 C2，Sam 裁决独立立单）

- (a) 旧实现「组码 grant 为空 ⇒ 整页返空」⇒ prod-ro 实测 **27 名 node-only 管理员全员**
  看不到任何审批历史（连自己部门的 `kb_access_request` 也没了）。
- (b) contribution 段只按 `category_dept` 收窄 ⇒ M8 迁移行有意保留的 `hr` 留痕残值会泄露给
  未来任何持 hr 组码的人（实测当前持有者=空集 ⇒ 零受众，潜伏）。

### 2.3 其余

- `165d960` 台账「异常」筛选自己弹回「全部」——伪徽章的**显示条件 ≠ 有效性**（现网报障）。
- `7b1659a` 三个 node 轴真库模块补进 CI db-integration **显式清单**（不在清单=CI 里哪儿都没跑）。
- `291f539` C3′ ACL 投影版本轴 E1 + 收敛度监控 G10（`RAG_ACL_VERSION_AXIS` **默认关**，本次发布不生效）。
- `8db1e15` 批量上传失败清单跨轮存活。

## 3. CI CVE 阻断门本次实红两次（都没绕）

| # | 命中 | 处置 |
|---|---|---|
| 1 | `requirements-prod.lock` 的 `pypdf 6.14.2` → **CVE-2026-71852**（构造 PDF 的字体宽度条目 ⇒ 文本抽取时超长运行时 + 内存爆） | **实修**（`a8126ca`）：按 lock 头部原命令重编译 + `--upgrade-package pypdf --refresh-package pypdf` → 6.15.0。变动面只有 pypdf 一个包，未手改任何 hash。 |
| 2 | `requirements-dataworks-py37.txt` 的 `pypdf 3.17.4` 同一 CVE | **无路可修** ⇒ 进 py3.7 审计基线（`5b0b384`）。补丁只在 6.15.0（`requires-python >=3.9`），DW 钉死 py3.7；PyPI 全量核对：声称支持 py3.7 的最高版本是 5.0.0，**仍在补丁之前**。 |

⚠️ 第 2 条的放行**不是**「用不到」型：`dataworks_nodes/stage1_node.py:194`、`stage2_node.py:197`
都 import pypdf，`extraction/pdf_extractor.py:878` 真的用 `PdfReader` 抽文本，管线每天处理员工
上传的 PDF。放行依据是【影响面可承受 + 无路可修】：后果是批处理作业 DoS 而非 RCE/数据泄露；
只打 DataWorks 离线摄取、**不碰 serving**（serving 已实修）；语料来自内部员工上传而非公网
任意投递；DAG 有既有 `retry_count`/毒文档机制兜底。真出路已立单：**把 DW 运行时抬离 py3.7**，
届时该条必须从基线删除。

顺带订正：`ci.yml` 的基线计数注释写着「59 条」，实测加本条之前已 63 条——注释漂了至少 4 条，
已改为 64 并加提醒。

## 4. 进度

- [x] 本地全门绿（`make test` / `make lint` / console typecheck+vitest+build）
- [x] push（`c239fb7..5b0b384`），PII/密钥自查零真实命中
- [x] CI 在 `5b0b384` 全绿
- [x] **ACR 促升完成**（run 31213040000，`acr-promotion` 门已批）
- [x] tag / manifest digest 已回填
- [x] SAE 切镜像（Sam，2026-08-07）
- [ ] 切后冒烟四联：`/api/version` git_commit=`5b0b384` / `/api/health` 200 / `/console/` 200 /
      `/api/kb/config` node_acl_grant=true
- [ ] 本批专属验收（见 §5）
- [ ] 正门补验：重灌收敛 → 金集重标 → baseline refreeze 后跑 `make release-gate`

## 5. 本批专属验收清单

前置全齐：`schema/067` 已 apply 生产+staging（02:57:55，`PROD-RW:2026-08-07`）/ M8 存量 4 行已迁
`category_dept_id=34265162`（人力资源部）/ `RAG_NODE_ACL_GRANT` 现网本就是 on。

| # | 验什么 | 判据 |
|---|---|---|
| 1 | HR 管理员看得到队列 | `341359176426169362` 的审核队列出现 4 条 |
| 2 | 越界提交被挡 | 拿他人令牌 POST 不属己的 `category_dept_id` → **403**，库里不留 pending 行 |
| 3 | 归属加载中不降轴 | 弱网开弹窗 → 按钮显「归属加载中…」且 disabled |
| 4 | 27 人的审批历史 | 任取 3 名 node-only 管理员 → 历史页**非空** |
| 5 | 台账「异常」待得住 | 点异常 chip → 等 faceted 计数回来 → **仍停在「异常」** |
| 6 | 组码轴零回归 | 那条 finance/rejected 行 + 组码管理员 → 与发布前逐字节一致 |
| 7 | 采纳链路端到端 | 采纳 1 条 → `document_meta.acl_mode='node'` + `owner_dept_id=34265162` + `kb_doc_node_grant` 有 subtree 行 |

第 7 条是唯一产生**生产写**的，执行前须 Sam 逐次授权。

## 6. 已知未做

- DW 摄取 zip 仍是另一条发布线（本次不含），现网资源位与仓库 HEAD 早有漂移，**引用前现查**。
- `RAG_ACL_VERSION_AXIS` 保持默认关；翻 on 会触发不可逆的旧版本退役，不在本次范围。

## 7. 后续增量（本次镜像**不含**）

`7381811 fix(console): 「异常」筛选值收敛到单一来源` —— 发布后 Sam 现网报的第二形态
（`已驳回 1` / `异常 1` 同物两名，切回「全部」又消失）。根因是待办条与台账 chip 有**两套
存在条件**，新增 `anomalyFilterTarget` 作单一来源。**要到现网需再发一版镜像**
（console 产物由镜像 builder 阶段自建，不是仓库工件）。
