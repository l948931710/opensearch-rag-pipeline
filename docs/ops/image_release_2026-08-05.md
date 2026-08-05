# 镜像发布记录 · 2026-08-05（`f918e37`）

> ⚠️ **本次发布豁免了 `make release-gate`**。豁免是 Sam 2026-08-05 当场决定，本文件是留痕，
> 不是事后追认。
>
> **豁免依据与 08-04 的差异（重要，别把两次当同一件事）**：08-04 的理由写的是「本批改动
> 全部为缺陷修复/UX 收口，**无检索行为变更**」。本批**不能原样套用**——里面含 ACL 判定
> 链路的改动（部门名漂移修复批）。准确表述是：
>   · **检索排序与生成链路零改动**（retriever / llm_generator / reranker / chunker /
>     pipeline_nodes / embedding / answer_flow / content_blocks 一个文件未碰，已逐个核过）；
>   · ACL 锚表与祖先制解析有改动，但**整体被 `RAG_ACL_ANCESTRY`（默认关）门控**，
>     发布当日 Sam 确认现网该 flag 为**关**⇒ 切镜像对「谁能看到什么」零影响；
>   · 唯一**无条件生效**的 ACL 改动是 `routes/kb_access.py` 的外部组码防投毒扩面
>     （从只挡 `*` 扩到全部 15 个组码）——**收紧**方向。
> 评测门本身仍处真空期（258 题金集双锚仍锚着已软退役旧语料），gate 数字在重灌收敛+
> 金集重标完成前不具判别力。**替代证据**见 §4：拿 prod 只读跑了 ACL 覆盖专项核查。

## 1. 工件

| 项 | 值 |
|---|---|
| git sha | `f918e37`（`f918e37b3089b50f83fb62f61ba977ae45d40954`） |
| push 区间 | `56752e6..f918e37`（11 commits） |
| 上一版镜像基线 | `8eaef98`（2026-08-04 第四次发布） |
| ACR tag | `<ACR_REGISTRY>/fuling/rag-serving:f918e37b3089` |
| manifest digest | `sha256:51fea7710d57803b53e05696fb27f1ca043e79b82a237f37d00eef544a802fc9` |
| promotion run | [31043566017](https://github.com/l948931710/opensearch-rag-pipeline/actions/runs/31043566017)（`build-smoke` ✅ + `promotion` ✅，attestation v2 工件 ID 8948516173） |
| 发布路线 | CI 正式路线：push→build+smoke；ACR 经 `image.yml` workflow_dispatch + `push_acr=true`，过 `acr-promotion` 审批门（Sam approve） |
| 本地验证 | `make test` 4240 passed / `make lint` clean / vitest 465 / vue-tsc exit 0 / Playwright 402 passed |

## 2. 本批内容

**① 全员上传中断修复（P0，本次发布的起因）**

`schema/066_document_meta_owner_dept_nullable.sql` —— **现网漂移纠正**：生产
`document_meta.owner_dept` 是 `NOT NULL`，而权威 DDL（`schema/001`，自首个提交起）写的是
`DEFAULT NULL`。node 归属登记按 060 设计往该列写 NULL，`STRICT_TRANS_TABLES` 下 1048 →
500 → 前端兜底文案，**全员传不了**。分歧自建库起就在，node 模式是史上第一个往它写 NULL 的
调用方。已 apply：生产 2026-08-04 23:26:10 / staging 23:28:24（staging 幂等守卫判定**本就
可空** ⇒ 漂移是生产独有）。修复后真实上传验证四项全过（`acl_mode='node'` 落库 /
`kb_doc_node_grant` subtree 行 / outbox 入队 / 审计 `UPLOAD_REGISTER` SUCCESS）。

**② console 三处（本次镜像才生效）**

- 台账「归属」列对 node 文档显示空格子 —— 后端 `owner_key`/`owner_label` 一直在回，前端仍读
  旧字段（node 文档该字段按契约恒空）。08-03 组织树重设计把**看板**迁到 kind-aware 口径，
  台账没迁，当时全库零篇 node 文档故看不出来。
- 台账「归属」筛选接上 node 轴 + facet 键闭环（`_kb_owner_facet_sql` 增收 `legacy:<code>`）。
- 审批队列补 owner DTO（公开件的 node 上传直接进该队列，是本族最快现形的一处）。
- 附带：`uploadErrText` 补 429 / 5xx / 网络中断三档 + detail 分级（管理台见后端原因与 trace，
  员工侧的贡献页维持「绝不外泄」）。**这条是本次事故排查成本的主因**——一次全员中断被压成
  一句「请稍后重试」。

**③ 部门名漂移 ACL 修复批**（另一会话，flag 门控，见 §4）

**④ 测试基建**：跨 pytest 进程互斥、真库集成扩面、组织快照 fixture。

## 3. push 前 PII 自查（本次有实弹）

按纪律扫 `git diff origin/main..main`，**命中真值**：3 个真 staffId + 1 个真实员工姓名
（与其 staffId、部门三者同行出现），落在 `tests/test_audit_log.py`、
`tests/test_kb_endpoints.py`、`routes/kb_access.py` docstring，引入者 `76d6111`
（上游会话拿生产真值当测试夹具）。
⚠️ **本节刻意不复述被泄露的具体值** —— 本文件本身就在这个 public 仓库里，
把真名/真 staffId 写进事故记录等于二次泄露。这条在写初稿时踩中过一次
（真名原样落进本节，push 前扫描抓到后改成现在这样）。

处置：`git filter-repo --replace-text --refs origin/main..main --partial` 清洗后再推；
替换值**保持 18/20 位**以免 `mask_staff_id` 的 first4…last4 路径失去覆盖。
另发现同批真值已随 `feature/doc-version-notify` 推上 public 远端 ⇒ 该分支 rebase 到清洗版
基座后 force-push。**全远端最终扫描 0 命中**。详见 memory `no-real-pii-into-repo-2026-07-26`。

## 4. 替代证据：ACL 覆盖专项核查（prod 只读，零写入）

`scripts/verify_acl_coverage.py --ancestry`，2026-08-05 发布前跑。

| | 名字制（**现网生效**） | 祖先制（翻闸后） |
|---|---:|---:|
| 在职员工 | 1167 | 1167 |
| **无 ACL 组** | **279** | **81** |
| 零受众告警 | `supply` | 无 |
| 残余分布 | 11 棵树（其他70/生产中心62/获胜包装54/综合管理中心52/品技中心24/…） | 2 棵（其他 70 / 获胜包装 11） |

死键 9 个：`资材部`(→采购部)、`自动化信息部`、`获胜工厂`、`印尼公司`、`墨西哥公司`、
`海外生产中心国内办公室`、`法务`、`工程`、`PMC部`。

**翻闸 blast radius**（供将来单独决策，**不是本次发布的效果**）：
`production +111 / overseas +73 / admin +63 / quality +24 / pmc +15 / supply +15 /
marketing +9 / hr +8 / it +4 / legal +1`。

残余 81 人的归属与处置：
- **「其他」70 人** —— 钉钉树里字面叫「其他」的部门，无职能语义，**任何锚都无法自圆其说**。
  根治 = HR 在钉钉里归到真实部门；权宜 = 个人级 `user_role` seed。
- **获胜包装 11 人** —— 直挂海外树根，锚表**有意不设锚**（树根是「公司」语义、职能不明；
  在根设锚会把 89 人一次性双职能授权）。出路 = 挂到已补锚的获胜生产中心/获胜行政中心，
  或个人级 seed。**这是设计如此，不是待修缺陷。**
- 个人级授权覆盖有限：`user_role` 仅 75 行有效（26 dept_admin + 2 kb_admin + 47 employee），
  能对上在职员工 73 人，多数本就是管理员 ⇒ 那 279 人不是被个人授权悄悄兜住了。

## 5. 进度

- [x] CI 三 workflow 绿（CI / Frontend / image，`f918e37`）
- [x] push 前 PII 自查 + 清洗 + 全远端复扫 0 命中
- [x] ACR 促升完成（run 31043566017，`acr-promotion` 门 Sam approve）
- [x] SAE 切镜像（Sam，2026-08-05）
- [x] **冒烟四联绿**（2026-08-05，入口 `https://rag.fulingplastics.com.cn`）：
      `/api/version` → `git_commit=f918e37` ✅（镜像已切的硬证据）/ `/api/health` 200 (0.67s) /
      `/console/` 200 / `/api/kb/config` 200 且 `node_acl_grant=true`
      （⚠️ 记账：旧入口 `120.55.69.9:8000` 已不通——它是镜像化迁移前的直连地址，
      `http_hardening.py` 头注里还留着它的说明，日后排查别再拿它当现网入口）
- [ ] 台账归属列现网复验（**需登录，只能人工看**）：那篇 node 文档
      （`DOC_01KZ89ZJCPNHFS9Q2F2ETB790T`，考勤制度）在「文档管理」台账的「归属」列
      应显示「人力资源部」而非空格子 —— 这是本次前端修复的直接验收点
- [ ] 正门补验：重灌收敛 → 金集重标 → baseline refreeze 后跑 `make release-gate`
- [ ] 独立决策（**不随本次发布**）：`RAG_ACL_ANCESTRY` 翻闸时机；翻前翻后各跑一次
      `verify_acl_coverage.py` 做对照
