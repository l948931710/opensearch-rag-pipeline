---
name: fuling-twin-governance
description: 富岭 RAG 语料重复/孪生文档治理 runbook —— 盘点同名多身份文档（docx+pdf 转换对、跨部门复制、版本漂移）、内容包含性对比、图片审计、分级退役、注册侧防重与回归验证。当用户提到 重复文档/孪生/同名文档/双格式/语料去重/退役文档/答案来源出现两个同名文件/同一文档两个版本都在索引/防止重复注册 等任何语料重复治理话题时使用本 skill，哪怕只是问"这两个文档是不是重复的"。只读诊断（scan/coverage/图审计/源文件回查）可自动执行；任何生产写（--commit 退役、seed 回灌、金集改向落盘）必须先出预览并取得用户逐次确认。
---

# 富岭语料孪生治理

治理目标：同一份文档在索引中只保留一个身份。重复身份的实测危害（WI-007 案例）：
检索面被孪生灌满 → LLM 图片引用配额浪费在重复内容上 → 后位步骤图被挤出答案。

## 安全等级（先读）

- **只读诊断随时可做**：`prod_access.get_prod_readonly_conn`（会话强制 READ ONLY）+ HA3 只查。
- **生产写三道闸**：① 当日令牌 `PROD_RW_ACK=PROD-RW:<YYYY-MM-DD>`；② 每批执行前
  出预览（组数/chunk 数/keep-retire 明细）等用户一句"确认"；③ 执行后立即双侧验证
  （RDS 残留=0 ∧ HA3 残留=0 ∧ keep 侧 HA3 在线）。跳过任何一道都不行——这是
  用户的既定授权文化，也是出错时唯一的审计链。
- **退役安全不变量**：retire 一个身份之前，keep 身份必须是其"信息超集"（文本上
  被包含、图上不丢独有信息），且 keep 必须在 HA3 中**实查在线**（本系统发生过
  RDS 全 INDEXED 而 HA3 为空的裂脑事故 G20——只信 RDS 状态会把文档退没）。

## 心智模型

同名孪生（title/raw_key 去扩展名后同 stem）有三种本质，处置完全不同：

1. **转换对**（同文档导出的 docx+pdf）——内容理论上相同，差异多为**抽取器假象**
   （pdf 表格行丢失、docx 文本框 `w:txbxContent` 丢失、扫描件无文本层）。
2. **跨部门复制**（同文件被放进多个部门目录）——内容 100% 相同，留哪个是 ACL
   归属决策，不是内容决策。
3. **版本漂移**（一侧是更新的修订版）——真内容差异，可能双向都有独有内容。

判断属于哪种**不能靠猜**，要走"源文件回查"：把一侧的独有内容拿到**对方源文件**
（docx 搜全部 XML `w:t`、pdf 搜 pdfplumber 全文）里搜——在源里但不在抽取结果里
= 抽取器缺口（修抽取器，不是文档问题）；不在源里 = 真版本漂移。

## 工作流

```
① scan 盘点 → ② coverage 包含性 → ③ 图审计 → ④ 裁决分类 → ⑤ 分批执行+验证+同步
```

### ① 盘点（只读）

`python scratch/retire_twins_batch.py scan` —— 按 **raw_key basename** 去扩展名分组
（不要用 document_meta.title：有 `.pd`/`.do` 截断脏数据），产出 manifest JSON +
人读审核表 + 金集命中标记。组可以 >2 成员（同文件注册过 4 次的先例）。

### ② 内容包含性（只读，决定性证据）

`python scratch/twin_content_coverage.py` —— 双向句级精确 + 8-gram 模糊双门判定
"keep 是否包含 retire 全部内容"。**度量有三个已踩实的假象会把结论完全带偏**
（VLM 图片描述块、多行 OCR 增强段、`【文档:标题】`前缀），清洗规则与判读标准
见 `references/coverage-metrics.md`——改这个脚本前必读。

关键认知：coverage 差距源自**源文件/抽取层**，回灌（重切）不会改变它；回灌改变的
是**图绑定质量**。两者要分开归因。

### ③ 图审计（文本安全 ≠ 图安全）

文本被包含但 retire 侧图更多的组，图无法用文本证明包含。
`python scratch/twin_image_audit.py --download` 做 OCR/视觉描述匹配 + 下载
"无对应"图**亲眼看**（OCR 匹配对照片/裁切差有大量假阴，目检是最终裁决——
本项目 36 张"独有"图目检后全是同批截图的不同裁切，零真独有）。

### ④ 裁决分类

按 `references/decision-framework.md` 的五类裁决表分类：安全组 / 图差组 /
翻转组 / 修订版组 / 真漂移组。两条铁律：

- **chunk 级拼接一票否决**（把 A 的独有 chunk 挂到 B 名下）：chunk 是源文件的
  派生物，下次回灌/版本 bump 时 DAG3 停用旧版本全部 chunk 由新抽取重建——拼接
  chunk 不在源文件里，**重建后凭空消失且无告警**。同目标的正确实现：修抽取器 /
  源文件级合并（文控出完整版走正常入库）/ 暂缓双服务。
- **独有内容必须逐条定性**再裁决：文件头套话、表格渲染变体（合并单元格 "200"vs
  "2001"）、扫描碎片 = 可弃；填写说明、责任条款、操作步骤 = 真内容不可弃。

### ⑤ 分批执行 + 验证 + 同步

`python scratch/retire_twins_batch.py execute --manifest <json> [--commit]`。
工具自带：keep 侧 RDS INDEXED + **HA3 实查**双预检、stem 现查断言、journal 逐步
落盘断点续跑、review 闸（needs_human_review 组默认拒绝）。用法细节与字段契约见
`references/tools.md`。

执行纪律：
- **小批分级**：先退"完全安全组"，再图差组，再裁决组——每批独立预览/确认/验证。
- **金集先行**：金集 expected_doc_ids 命中将退役 doc_id 的，退役**前**改指 keep 侧
  （带 .bak 备份）。⚠️ 翻转 keep 之后要**重新**核对金集——改向发生在翻转前的话
  会漏掉新的退役侧。
- **永远不用 `--force-reviewed` 偷懒**：它是全局的，会把还没裁决的组一起放行。
  正确做法是逐组在 manifest 里清 review 标记并写 review_reason 留痕。
- 顺序固定 RDS-first（单事务停用 → HA3 幂等删除）：中断残留态 = 孪生继续服务
  （现状），且 `清理stage3` 步骤0 会清扫 `is_active=0` 的 HA3 残留兜底。
  反过来 HA3-first 中断会留下 RDS 幽灵 active 行（G20 裂脑形态）。
- 收尾跑回归快测确认零回归——口径陷阱（退役→keep 归一映射、基线环境一致性、
  标题别名漂移）见 `references/sync-and-regression.md`。

## 防复发（注册侧）

退役治标，注册防重治本：`ingest_policy.py` 的 `raw_key_stem`/`stem_twin_action`
（同部门同 stem 跳过+告警，跨部门仅告警——拦截与否是 ACL 问题，防重不替它做
决定）。已退役孪生不会复活：注册查重按 raw_key 比对**全部** document_version 行
（不过滤 status），退役保留的 RDS 行天然挡住重注册——这也是"退役只置 inactive
不删行"的设计原因之一。`register_new_files.py` 是 DataWorks PyODPS 内联粘贴节点，
改正本后必须在发布窗口**重新粘贴**（带凭证注入块），日志核对 `INGEST_POLICY_REV`。

## 速查

| 要做什么 | 入口 |
|---|---|
| 盘点孪生组 | `scratch/retire_twins_batch.py scan` |
| 内容包含性 | `scratch/twin_content_coverage.py`（先读 coverage-metrics.md） |
| 图审计+目检 | `scratch/twin_image_audit.py --download` |
| 退役执行 | `retire_twins_batch.py execute`（预览→确认→`--commit`→双侧验证） |
| 单文档回灌 seed | 仿 `scratch/seed_round_20260612.py`（候选断言+队列空检+salt 哈希） |
| 历史档案 | manifest/journal/coverage 报告均在 `scratch/`；收官记录 `docs/corpus_cleanup_worklist.md` §7 |
