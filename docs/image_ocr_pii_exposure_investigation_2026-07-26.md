# 图片 OCR PII 暴露面调查（2026-07-26）

> 触发：`docs/funnel_stability_plan_2026-07-25_DRAFT.md` §14.4-2 —— 扫描顺带发现
> `travel_subsidy` 的配图是人员信息登记表、含身份证号，且图像 OCR 的 PII 扫描默认 OFF。
> 分支：`fix/ingest-a-tier-2026-07-25`。**全程只读**（`prod_access.get_prod_readonly_conn()`，
> `fuling_ro` 账号 + `SET SESSION TRANSACTION READ ONLY`），未改任何配置、未开任何 flag。
> 开关是 Sam 的决定；本文只给判据。

**一句话结论**：§14.4 担心的那条链在现网**没有发生**——`travel_subsidy` 早已被正文
检测器整篇隔离（0 chunk）。但调查过程中挖出**两个真缺陷**和**一处已存在的未脱敏残留**，
其中最要紧的一条与 flag 无关：**`cn_id_card` 的误报抑制清单把现网 3/3 条真身份证命中
全部吃掉了**——恰恰是 `pii_patterns.py:129` 那条警告所指的方向，现在有实测数字了。

---

## 0. 先纠正三个前置判断

调查的第一步是核实前提，三条里有两条与现网不符（`feedback-verify-live-state-before-claiming-gap`
的老教训：schema/env/flag 三类不可从仓库文件外推）。

| # | §14.4 / 任务书的表述 | 现网实际 | 证据 |
|---|---|---|---|
| 1 | travel_subsidy 的图「走 ROUTE_TO_VECTOR 进 canonical JSON 与索引」 | **该文档在生产是 0 chunk**——2026-06-15 被**正文** `cn_id_card` 检测器判 high→整篇 QUARANTINE，从未进索引 | `DOC_HR_20260513120631_5D7F15`：`chunk_meta` 0 行；`document_sensitive_finding` 有 `cn_id_card/high/QUARANTINED @2026-06-15 01:10:03` |
| 2 | 「`visual_summary` 明确不在扫描范围内」 | 对 node02/03 成立；但 **G21 兜底（`RAG_IMAGE_OCR_PII_FAILCLOSED`，默认 ON）在 node05 同时扫 `ocr_text` + `visual_summary`**，且不依赖任何 flag、每轮 chunk 消费重建 | `pipeline_nodes.py:4278-4290`；现网实证见 §3 |
| 3 | 「`RAG_IMAGE_OCR_PII` 默认 OFF」 | 代码默认 OFF 属实，但**现网 DataWorks stage-2 节点确实设了 `setdefault("RAG_IMAGE_OCR_PII","true")`**——只是它没起作用，原因见 §4 | DataWorks `GetTask(1037694366)` 拉到的线上脚本正文 |

第 1 条意味着：**触发本次调查的那份文档本身不构成现网暴露**。真正的问题在别处。

---

## 1. Q1：生产库里这一类文档有多少

判据只用图内文本（`ocr_text`/`visual_summary`/`caption`），不靠标题猜；要求组合命中
（证件类关键词，或「名册类 + 姓名」，或「薪资类 + 姓名」）以压误报。
扫描面：`chunk_meta.image_refs_json` 全量 63,882 行中 1,879 行带图，
展开 **4,496 个非 DISCARD asset / 308 篇带图文档**。

**命中 24 篇**：

| 类别 | public | dept_internal | 小计 |
|---|---|---|---|
| 证件类（含身份证/证件号字样） | 10 | 3 | **13** |
| 名册类（花名册/在职人员/员工编号…） | 7 | 1 | **8** |
| 薪资类（工资/薪资/绩效 + 姓名） | 3 | 0 | **3** |
| **合计** | **20** | **4** | **24** |

注意 **20/24 是 `permission_level=public`**——这是这批文档真正的风险放大器，
不是「图里有 PII」本身。代表性的几篇：

- `DOC_IT_20260513120634_ED2762`《富岭U8+人事部操作手册.docx》— public，ERP 人事界面截图
- `DOC_IT_20260514123026_951420`《工资核算管理操作手册》— public，同上
- `DOC_HR_20260513120631_416ED9`《员工入职流程》作业指导书.pdf — public，招工登记表（姓名/身份证栏）
- `DOC_ADMIN_20260509102839_76AFFC`《新员工住宿安排》— public，在职人员花名册（姓名/部门/职务）
- `DOC_HR_20260513120631_E3CD28`《员工离职手续》— public，离职员工工资确认单（实名 + 银行账号语境）

⚠️ 这 24 篇是**「图里有人员信息」**，不等于**「有未脱敏的身份证号」**。后者见 Q3，只有 2 篇。

---

## 2. Q2：开启 `RAG_IMAGE_OCR_PII` 的 blast radius 与误报率

方法：把 node02 第 4 段的判定逻辑（含 `_image_ocr_fp_ignore`）原样跑在上述 4,496 个
真实 asset 的 `ocr_text` 上，直接用仓库权威表 `ENTITY_PATTERNS`（不另抄）。

### 2.1 最贵的后果：**0 篇**

| 文档级判定 | 篇数 |
|---|---|
| → `high`，**整文档 QUARANTINE（下架）** | **0** |
| → `medium`，就地脱敏、文档保留可检索 | 84 |

**没有任何文档会因为开这个 flag 而从索引里消失。** 这条推翻了"开 flag 可能误伤大批文档"
的直觉担忧——`stage2_node.py` 注释里那句 "shadow showed 0 high-sev currently" 到今天仍然成立。

84 篇的部门/权限分布（前几位）：marketing/public 20、production/dept_internal 14、
it/public 9、finance/public 8、admin/public 8、hr/public 4。

### 2.2 误报率：**约 93% 的新增掩码打在业务标识符上，不是个人 PII**

| 实体 | 命中 | 文档 | 裁决 | 依据（样本全部人工看过上下文） |
|---|---:|---:|---|---|
| `bank_card` | 177 | 12 | **误报** | 全是 18 位**报关单号/出口发票号**（`3101…`、`2952…`）。上下文是「预录入编号/进出口标记/海关报关单号」，不含 `订单/物料` 锚点 → 现有 FP 抑制够不着 |
| `masked_id` | 200 | 25 | **误报且有害** | 命中的是**源文档里本来就打了星的标识**（发票/银行账号）。脱敏器把整串换成 `[标识已脱敏]`，比原文**更不可读**，且丢掉后 4 位业务可用性 |
| `address` | 178 | 25 | **误报** | 绝大多数是**富岭自己的厂址**（浙江省温岭市金塘南路 88 号），出现在信头/发票/产品标签。`[地址已脱敏]` 是整词替换，会直接毁掉「富岭地址在哪」这类合法答案 |
| `uscc` | 34 | 10 | **误报** | 富岭自己的统一社会信用代码，公开工商登记信息，出现在纳税申报表 |
| `email` | 33 | 8 | **多为误报** | 公司邮箱（`user@fulingplastics.com` 等），业务联系方式 |
| `cn_mobile` | 47 | 18 | **真正例** | 抽样 8/8 全是真实个人手机（司机、验货员、员工、客户联系人），掩码正确 |
| **合计** | **669** | | | **真 PII 47 / 669 ≈ 7%；误报 622 / 669 ≈ 93%** |

`pii_patterns.py:129` 要求「启用前须用真实语料评估误报」——这就是那个评估：
**误报率 ~93%，但全部落在 medium（就地掩码），没有一条升到 high（下架）。**

### 2.3 但真正的结论是：**开这个 flag 的边际收益 ≈ 0**

关键在于 **G21 兜底已经在做同一件事，而且做得更宽**：

| | node02/03（`RAG_IMAGE_OCR_PII`，默认 OFF） | G21 兜底（`RAG_IMAGE_OCR_PII_FAILCLOSED`，默认 **ON**） |
|---|---|---|
| 字段覆盖 | 仅 `ocr_text` | `ocr_text` **+ `visual_summary`** |
| 触发条件 | 需 flag ON，且 `risk=medium or sensitive_detected` | **无条件**，每轮 chunk 消费重建 |
| 用的表 | `ENTITY_PATTERNS` + `REDACTION_MAP` | **同一套** |
| FP 抑制 | `_image_ocr_fp_ignore` | **同一个函数** |
| 额外能力 | ① 写 `image_ocr:*` 审计行 ② `high` → 整篇隔离 | 无（只掩码，从不隔离） |

也就是说，开 flag 相对现状只多两样东西：**审计可见性**，和 **cn_id_card/access_key/secret_like
的隔离升级**。而后者——**实测为 0 篇**（原因见 §3.1）。

⇒ **开 flag ≈ 只换来审计行，外加 §3.2 那个双重脱敏副作用。**

---

## 3. 调查中发现的两个真缺陷（比 flag 本身重要）

### 3.1 D1：`_image_ocr_fp_ignore` 吃掉了 **3/3（100%）** 的真身份证命中 🔴

`_MATERIAL_CODE_ANCHORS` 含**裸 `编码`** 这一项（`pii_patterns.py:131`）。而 ERP 人事界面
普遍带「员工编码 / 部门编码 / 存货编码」字样。该函数是 **text 级而非 match 级**判定：
整段 `ocr_text` 里只要出现一次 `编码`，**该图的 `cn_id_card` 检测就整体失效**。

实测（全库唯一的 3 条 `cn_id_card` 命中）：

```
DOC_IT_20260513120634_ED2762  anchors=['编码']  SUPPRESSED
DOC_IT_20260513120634_ED2762  anchors=['编码']  SUPPRESSED
DOC_IT_20260514123026_951420  anchors=['编码']  SUPPRESSED
```

上下文毫无歧义，绝非物料编码：

```
证件类型  证件号码 3310****6636  出生日期 1983-04-26  试用开始日期 2018-07-26
职等  证件到期日 2002-02-01  离职日期  证件号码 6104****2124  证件类型 (0)身份证
```

`cn_id_card` 是 `severity=high` → 整篇隔离，是这条链上**唯一承重的检测**。它在
「人员截图」这个恰恰最需要它的场景里被**默认关掉了**。

> `pii_patterns.py:129` 原文：「⚠️ 启用 RAG_IMAGE_OCR_PII 前须用真实 CE38C5 等 OCR 样本
> 验证此 allow-list，**避免过度抑制真实身份证号**」。担心的事情已经发生，抑制率 100%。

**为什么现网数据仍然没泄露**：`bank_card`（16-19 位数字）**顺带**命中了同一串 18 位数字，
且其上下文无 `订单/物料/编号` 锚点 → 未被抑制 → 掩成 `3310****6636`。
**这是巧合，不是设计**。若 OCR 文本里恰好出现 `编号` 二字，`bank_card` 也会被抑制，
两层同时失效、身份证号原样落库。

### 3.2 D2：`scrub_image_text` **不幂等**，与文档承诺相反 🟡

`pii_patterns.py:182` docstring 写「幂等」，`.env.example:130` 写「与 node 03 脱敏幂等」。
实测（`/usr/bin/python3` 直接调用）：

```
联系电话 13900139000  →  x1: 139****9000  →  x2: [标识已脱敏]   ✗ 二次改变
证件号码 331081…6636  →  x1: 3310****6636 →  x2: [标识已脱敏]   ✗ 二次改变
地址:浙江省温岭市…88号 →  x1: [地址已脱敏]  →  x2: [地址已脱敏]   ✓ 幂等
```

机理：`masked_id` 排在 dict 首位以防 `cn_mobile` 二次吞掉已掩码值，但它自己的脱敏器是
整词替换 `[标识已脱敏]` —— 于是第二遍把 `139****9000` 降级成完全不可读。

**触发路径**：node03 就地改写 `asset["ocr_text"]`（注释明说 detect/redact/chunk 共用同一
`ctx["canonicals"]` 对象），node05 的 G21 随后在**同一轮里**再扫一次 → 双重应用。
**这条路径只有在 `RAG_IMAGE_OCR_PII` 打开时才会出现。**

现网影响：**目前为 0**。全库 `[标识已脱敏]` 出现 **0** 次，`[地址已脱敏]` 出现 **83** 次
——正好印证「G21 跑了、node03 没跑」。属**潜在**缺陷，开 flag 即被激活。

---

## 4. 为什么「节点设了 true」却零效果——现网真相

线上 DataWorks stage-2 节点（`GetTask(1037694366)`）确实含：

```python
os.environ.setdefault("RAG_IMAGE_OCR_PII", "true")
```

但生产库里：

- `document_sensitive_finding` 1,152 行，`finding_type` 取值只有
  `address / bank_card / business / cn_id_card / cn_mobile / email / masked_id / pii / security / uscc`
  —— **没有任何 `image_ocr:` 前缀行**（写入代码 `pipeline_nodes.py:2465` 确实会带该前缀，
  所以这是有效判据，不是字段名错配）；
- 84 篇「本应命中」的文档里，**53 篇的 `updated_at` 晚于 2026-07-06**，即 flag 上线后确实
  重跑过 node02，仍然零 `image_ocr:*` 行。

**根因**：`pipeline_run.git_commit` 暴露了执行方——

| stage-2 运行 | git_commit | 提交性质 | 规模 |
|---|---|---|---|
| 2026-07-06 16:24 | `2c13977` | **docs: 重灌 blast-radius manifest**（文档提交） | 550 docs / 10,348 chunks |
| 2026-07-07 12:08 | `2e1be81` | fix(pii): G21 覆盖 visual_summary | 31 docs / 988 chunks |
| 2026-07-16 11:15 | `8273bc4` | **本地分支 `fix/stage1-env-failfast` 的 tip** | 2 docs / 27 chunks |

DataWorks 跑的是打包好的 zip，不可能把「本地在改的功能分支 tip」或「一个 docs 提交」
记成 `git_commit`。⇒ **近期这几次 stage-2 重跑全部是从笔记本发起的**
（与 `laptop-reindex` / 重灌战役的时间线吻合）。

而 `.env` / `.env.production` / `.env.prod_ro` **都没有定义 `RAG_IMAGE_OCR_PII`**
（只有 `.env.example` 里有一行注释说明）⇒ 本地跑 = flag OFF。

> **这才是真正的结构性问题**：这个开关的唯一 ON 点写在 DataWorks 节点里，
> 而语料实际上主要由笔记本重跑。**保护随执行路径而变**。
> 推论：Sam 即便把 DataWorks 节点的 flag 翻成显式 `true`，也**不会**覆盖笔记本重切的文档。
>
> 对照组：G21 因为默认 ON、不读任何 flag，**两条路径都生效**——这正是它在现网留下
> 83 处 `[地址已脱敏]` 而 node03 一处未留的原因。fail-closed 的设计在这里赢了。

---

## 5. Q3：已入库 chunk 里是否已带未脱敏身份证号

**是。3 个 chunk / 2 篇文档，均 `is_active=1` + `index_status=INDEXED` + `permission_level=public`。**

| doc_id | 标题 | chunk | 状态 |
|---|---|---|---|
| `DOC_IT_20260513120634_ED2762` | 《富岭U8+人事部操作手册.docx》 | `…_v3_c0034_71D968A8`、`…_v3_c0036_BCCD8D5C` | active / INDEXED / public |
| `DOC_IT_20260514123026_951420` | 《工资核算管理操作手册（2025年5月28日初版）.docx》 | `…_v3_c0030_056A5F90` | active / INDEXED / public |

- 位置：**`image_refs_json` 内的 `asset.ocr_text`**，`chunk_text` 里 **0 条**（全库 `chunk_text` 无任何 `cn_id_card` 命中）。
- 涉及 **2 个不同的真实身份证号**（其一在两个 chunk 里重复）。本文只以掩码/哈希指代。
- 写入时间 `created_at = 2026-06-15 04:17:28`，`first_created == last_created` ⇒ **自那以后从未被重切**。

**时间线解释了一切**（三层防护都晚于写入）：

| 日期 | 事件 |
|---|---|
| **2026-06-15** | **这 3 个 chunk 写入** |
| 2026-06-22 `b2cc0c9` | node02/03 图像 OCR PII 检测+脱敏代码落地（默认 OFF） |
| 2026-06-23 `b0826d1` | stage2_node 加 `setdefault(...,"true")` |
| 2026-07-06 `eb610d3` | G21 消费点兜底（默认 ON，仅 `ocr_text`） |
| 2026-07-07 `2e1be81` | G21 扩到 `visual_summary` + 凭证号 FP 护栏 |

⇒ 这是**防护上线前的历史残留**，不是现行防护失效。任何一次重切都会把它掩掉
（经 §3.1 那条 `bank_card` 巧合路径，掩成 `3310****6636`）。

### 5.1 `document_sensitive_finding` 对账：**审计表没有反映这处暴露**

这 2 篇只有 3 行 finding，全部是 `finding_type=pii / severity=medium / action=REDACTED`
（语义关键词层命中「身份证」「工资」等字样），**没有任何实体级的 `cn_id_card` 行**。

⇒ **只看审计表会漏掉这处暴露**。原因正是 §3.1：实体级检测被 `编码` 抑制，
只剩语义关键词层留下一条 medium 记录。

### 5.2 对照：正文路径的 `cn_id_card` 检测工作正常

全库 3 篇文档有正文 `cn_id_card` finding（high→QUARANTINED），**3/3 全部 0 active chunk**：

| doc_id | 标题 | 权限 | 结果 |
|---|---|---|---|
| `DOC_HR_20260513120631_5D7F15` | 《员工路费补贴》作业指导书.pdf | public | **0 chunk，已隔离** |
| `DOC_HR_20260620073928_CE38C5` | 《人员离职退保手续》作业指导书.pdf | dept_internal | **0 chunk，已隔离** |
| `DOC_RD_20260622105417_B26C2E` | `bbcce353f47f40d19a549b4dc6b7e36d.pdf` | dept_internal | **0 chunk，已隔离** |

第一行就是本次调查的起点。**正文层拦住了它，图像层从未被需要。**

---

## 6. 给 Sam 的判据（不含建议动作，开关是你的决定）

按「改动价值 ÷ 风险」排序，**注意排第一的与 flag 无关**：

| # | 事项 | 判据 |
|---|---|---|
| **1** | **修 D1：`_MATERIAL_CODE_ANCHORS`（`pii_patterns.py:131`）的裸 `编码`** | 当前抑制率 **3/3 = 100%**，且抑制是 text 级而非 match 级。收窄成 `物料编码/存货编码/料号` 等具体词、或改成 match 级 ±窗口判定，即可让 `cn_id_card` 在人员截图上恢复工作。**不动这条，开不开 flag 都拦不住身份证号。** |
| **2** | **2 篇 HR 手册的历史残留** | 3 个 active+INDEXED+public 的 chunk 带真身份证号。重切即可消除（走既有重切链路，非本文范围）。当前靠 `bank_card` 巧合掩码——但库里存的是**未掩码原文**。 |
| **3** | **D2 幂等性** | 开 flag 会激活 node03→node05 双重脱敏，把 `139****9000` 降级为 `[标识已脱敏]`。现网 0 例。修法：`masked_id` 的脱敏器改为恒等（它的职责是**占位保护**，不该二次改写）。 |
| **4** | **flag 本身** | 开：+审计可见性；0 篇下架；0 篇新增掩码（G21 已覆盖）；−触发 D2。不开：维持现状。**在 D1 修好之前，开 flag 的 PII 收益实测为 0。** |
| **5** | **执行路径不一致（§4）** | flag 只在 DataWorks 节点设，语料实际由笔记本重跑。任何「靠节点 setdefault 存续」的开关都有这个盲区。G21 那种 default-ON 才是两条路径都覆盖的形态。 |
| **6** | 20/24 篇人员类图文档是 `public` | 与 PII 检测正交的另一条线：权限分级可能比脱敏更有效。属 ACL 议题，未展开。 |

### 顺带（不在本次范围，已在既有台账）

- 线上 DataWorks stage-2 节点脚本里**内联明文写着** DASHSCOPE / RDS / OSS / HA3 的生产凭据。
  本文不复制任何取值。此项已在 `[[enterprise-agent-prod-review-verification-2026-07-18]]`
  的 B7 残项（RB-06 轮换 + DW 节点内联凭据治理）里挂着，此处仅作再次确认：**它仍然在**。
- `.env.example:129` 与 `pii_patterns.py:182` 的「幂等」表述与实测不符（D2），改代码时一并订正。

---

## 附：复现方式

只读脚本置于本次会话 scratchpad（未入库，避免把含 PII 上下文的中间产物落进公开仓）：

| 脚本 | 作用 |
|---|---|
| `probe01_chunk_scan.py` | 全量 63,882 行 `chunk_meta` 实体扫描（`chunk_text` + `image_refs_json`） |
| `probe02_would_gate_catch.py` | 对 3 条命中回放两层防护，判定谁抓得到 |
| `probe03_which_rule.py` | 逐规则复刻 `scrub_image_text`，定位是 `bank_card` 顺带掩掉的 |
| `probe04_blast_radius.py` | 4,496 asset 上复刻 node02 判定，出 blast radius + 抑制统计 |
| `probe06_q1_scope.py` | Q1 的 24 篇分类 + `[标识已脱敏]`/`[地址已脱敏]` 计数 |
| `probe07_flag_effect.py` | finding 时间线 + 84 篇的 `updated_at` 分布 |

要点：一律 `sys.path.insert(0, <repo>)` 后 `from opensearch_pipeline.pii_patterns import …`
——**用仓库权威表，不另抄一份正则**，否则测的就不是现网行为。
所有输出中的号码均在脚本内掩码后才打印。

---

## 7. 代码修复落地（2026-07-26，Sam 拍板"先不管历史残留，修复代码为以后服务"）

§6 的 6 条判据里，**#1 (D1) 与 #3 (D2) 已修**；#2（2 篇历史残留重切）Sam 明确暂不处理。

### 7.1 D1：`cn_id_card` 的 FP 抑制从 text 级改为 **match 级** + 收窄锚点

`pii_patterns.py`：

1. `_MATERIAL_CODE_ANCHORS` **删掉裸 `编码`**，补上 `存货编码`：
   `("物料编码", "物料号", "料号", "存货编码", "material code", "material no")`；
2. 新增 `_id_card_ctx_is_fp(m)` —— **±24 字符窗口逐命中判定**，形态与既有的
   `_bank_card_ctx_is_fp` / `_mobile_ctx_is_credential` 一致（仓库本来就有这个模式，
   `cn_id_card` 是唯一的例外）；
3. 新增 `_ID_CARD_POSITIVE_ANCHORS = ("身份证", "证件号", "证件类型", "id card", "idcard")`
   —— **正向锚点优先**，共现即视为真身份证。
   ⚠️ 与 `scrub_image_text` docstring 里那条禁令方向相反、不冲突：那条禁的是给
   `cn_id_card` 加**凭证**锚点护栏（`证号` 会匹配「身份证号」本身 → 漏真号）；这里加的是
   **正向**锚点，只会让抑制更难发生；
4. `_image_ocr_fp_ignore("cn_id_card", …)` 改为 **所有命中都是 FP 才判 FP**
   （与 `bank_card` 的 `_body_entity_fp_ignore` 同款 fail-closed 方向）；
5. `scrub_image_text` 给 `cn_id_card` 加逐命中分支 —— 同一张图里「存货编码 1234…」与
   「证件号码 3310…」各判各的。

### 7.2 D2：`masked_id` 的脱敏器改为**恒等**

它的职责是**占位保护**（先行捕获源文里本就打星的标识），不是"再脱敏一次"。
改完 `scrub_image_text` 才真正幂等。顺带订正 `.env.example:130` 与该函数 docstring 里
与实测不符的「幂等」表述。

**为什么恒等是安全的**（读模式表核实过，不是推测）：`139****9000` / `3310****6636`
**只**命中 `masked_id` —— 星号断开了连续数字，`cn_mobile` 的 `1[3-9]\d{9}`、`bank_card`
的 `\d{16,19}`、`cn_id_card` 的 18 位式都匹配不到它们。所以"masked_id 先跑防二次吞"这个
dict 序对**已掩码值**并不承重，恒等不会打开任何缺口。

### 7.3 风险边界（核实过，不是推测）

| 问题 | 核实结果 |
|---|---|
| 会不会有文档因此**下架**？ | **不会**。图像 `cn_id_card`→high→整篇隔离仍在 `if RAG_IMAGE_OCR_PII`（默认 OFF）门内（`pipeline_nodes.py:2397`）。默认姿态下生效的是 G21（默认 ON，**只掩码从不隔离**） |
| 默认姿态下净变化 | G21 从「靠 `bank_card` 巧合掩掉身份证」变成「按设计由 `cn_id_card` 掩掉」。掩码结果同样是 `331081****6636`，但不再依赖巧合 |
| 正文 PII 受影响吗 | **否**。正文走 `_body_entity_fp_ignore`，本次未动 |
| 若日后打开 `RAG_IMAGE_OCR_PII` | 那 2 篇 IT 手册会 high→隔离。这正是 §6 #1 预告的后果，届时是一次独立决策 |
| 代价 | 只标了裸 `编码` 的真 18 位物料码现在会被**就地掩码**（medium，不隔离）。成本不对称下这是正确的一侧 |

### 7.4 验证

`make test` **3453 passed / 1 skipped（exit 0）**、`make lint` 绿。
新增 12 条回归测试（`tests/test_image_ocr_pii_gate.py`），用报告里那三条真命中的**语境
逐字**构造，含反向钉子（真物料码仍抑制）、match 级钉子（混合文本各判各的）、正文路径
未受波及、以及"下架仍受 flag 门控"这条风险边界。

---

## 8. D1 修复后重测 blast radius（2026-07-26）——回答「要不要默认开」

§2.1 那个「开 flag → high 0 篇」是在**旧的坏抑制**下量的：裸 `编码` 吃掉了 3/3 的
cn_id_card 命中，high 这条路本来就是死的。D1 修好后它第一次真正通了，那个 0 已经过期，
而它恰恰是这个决定的决定性数字。重测（只读，`scratch/image_pii_blast_radius_postD1_20260726.py`，
扫描面 1,300 行带图 active chunk → 2,724 个有 `ocr_text` 的非 DISCARD asset）：

| | OLD（D1 前：text 级 + 裸「编码」） | NEW（D1 后：match 级 + 正向锚点） |
|---|---|---|
| → **high（整篇隔离下架）** | **0 篇** | **2 篇** |
| → medium（就地掩码，保留可检索） | 42 篇 | 40 篇 |
| `cn_id_card` 命中 | **0**（全被抑制） | **3** |
| 其余实体命中 | masked_id 47 / address 14 / cn_mobile 10 / email 8 / bank_card 4 | 同左，逐项不变 |

**新增会被隔离的恰好 2 篇，零附带**：

| doc_id | 权限/部门 | 标题 |
|---|---|---|
| `DOC_IT_20260513120634_ED2762` | **public**/it | 富岭U8+人事部操作手册.docx |
| `DOC_IT_20260514123026_951420` | **public**/it | 工资核算管理操作手册（2025年5月28日初版） |

正是 §5 认定携带未脱敏身份证号的那两篇。

⚠️ **方法论坑（第一版就踩了）**：只把 `编码` 塞回 `_MATERIAL_CODE_ANCHORS` 是**搭不出旧
行为**的 —— 修复后的函数体是 match 级且带正向锚点（身份证/证件号/证件类型），那条正向
锚点会直接把抑制否掉，两臂读数假性相同（第一次跑出来 0→0）。必须逐字复刻旧的 text 级
函数体才是有效对照。

### 8.1 判据：默认开的**唯一**实际后果是把这 2 篇从「掩码后继续服务」降级成「下架」

因为 G21（默认 ON、无条件、同一套表）**已经在做掩码**，开 flag 相对现状只多两样：

1. **审计可见性**（`image_ocr:*` finding 行，现网为 0 行）；
2. **high 升级隔离** —— 实测就是上面那 2 篇。

而这 2 篇是**在用的 public 操作手册**，其 PII 是 ERP 截图里附带的一个员工身份证号。
掩码（`331081****6636`）已经完整解决暴露；隔离则把整本手册从索引里拿掉。
**用下架去解决一个掩码已经解决的问题，是净损失。**

### 8.2 因此的建议（决定权在 Sam）

**不建议默认开。** 理由不是"风险未知"——现在已经量清楚了——而是它今天唯一的增量效果是
有害的那一侧。

真正值得单独解决的是**审计盲区**：`document_sensitive_finding` 里 `image_ocr:*` 恒为 0 行，
图像侧 PII 在审计表上完全不可见（§5.1 已实证「只看审计表会漏掉这处暴露」）。两条干净的
拿法，都不需要下架：

- **A. 给 G21 补留痕** —— 它本来就无条件跑、覆盖面还更宽（含 `visual_summary`），
  让它把命中写成 finding 行即可；
- **B. 按来源分级** —— 图像 OCR 的 `cn_id_card` 降为 medium（正文仍 high）。仓库已有此
  先例：`ENTITY_SEVERITY` 的注释明说 G5 扩展类型「与 `redaction.HIGH_TYPES` 的分级**刻意
  不同**」。正文里出现身份证号通常意味着**这篇就是人事记录**（《员工路费补贴》被正确隔离
  即是例证）；而 ERP 截图里出现，意味着**这是一张 UI 演示图** —— 同一个信号，语义不同。

另外 §4 那条结构性问题依然成立：即便决定要开，写在 DataWorks 节点的 `setdefault` 也**覆盖
不了笔记本重跑**；要开就得像 G21 那样做成**代码默认**。

---

## 9. 方案 A 落地：给 G21 补留痕（2026-07-26）

§8.2 给了两条不需下架的拿法，Sam 选 **A**。

### 9.1 为什么把留痕挂在 G21 而不是 node02

| | node02（`RAG_IMAGE_OCR_PII`，默认 OFF） | **G21**（`RAG_IMAGE_OCR_PII_FAILCLOSED`，默认 ON） |
|---|---|---|
| 覆盖执行路径 | 只有 DataWorks（`setdefault`）—— **覆盖不了笔记本重跑**（§4 实证） | **两条都覆盖**（默认 ON、不读任何 flag） |
| 字段 | 仅 `ocr_text` | `ocr_text` **+ `visual_summary`** |
| 现网留痕 | `image_ocr:*` **0 行** | 83 处 `[地址已脱敏]` 的实际执行者 |

⇒ 留痕挂在 G21 上，一次同时解掉「审计盲区」和「保护随执行路径而变」两件事。

### 9.2 实现

**`pii_patterns.scrub_image_text(text, findings=None)`** 加一个可选回收口：传入 list 时，
**每一处真正被改写的命中**追加 `(entity_name, matched_text)`。

判据刻意是「**值变了**」而不是「模式匹配上了」—— 这是关键：`bank_card` 的 FP 由
`_guarded_bank_card_redact` **在内部**就地放过（返回原值），`cn_mobile`/`cn_id_card` 的 FP
由各自的逐命中分支放过。在调用侧另抄一遍这些判据必然漂移，所以回收口必须开在 scrub 里。
（副作用：`masked_id` 的脱敏器 D2 后是恒等 → 值不变 → 不产 finding。这是对的：G21 对它
什么也没做，审计行不该声称做了。）

实测忠实度（6 个用例全过）：真身份证+真手机 → 2 条；订单号 FP → 0 条；真银行卡 → 1 条；
已掩码值 → 0 条；物料编码整体抑制 → 0 条；FDA 注册号（106E77 那类）→ 0 条。

**`pipeline_nodes._persist_image_scrub_findings(ctx, doc, findings)`**：

- `finding_type` = **`image_scrub:{entity}`** —— 刻意**不复用** node02 的 `image_ocr:`
  前缀：后者是判断「node02 那条 flag 门内路径有没有跑过」的既有判据（§4 正是这么用的），
  复用会把它毁掉。两个前缀并存即可分辨是哪一层留下的；
- `action` 恒为 `REDACTED` —— G21 只掩码、**从不隔离**；
- **纯留痕**：不参与 `final_risk`、不回写 `redaction_action`、不影响 chunk 产出
  （有一条测试用 AST 剥掉 docstring 后扫函数体钉住这点）；
- **fail-open**：任何异常只吞并打日志。与 node02 的 `raise` 刻意不同 —— 那里留痕失败意味着
  "隔离决策没落库"，这里只是少了一行审计，绝不让审计写把分块炸掉；
- 只 DELETE 本层自己的 `image_scrub:%` 行。node02 按 `(doc_id, version_no)` 先删后插且在
  DAG 2 里跑在本节点**之前**，所以两层互不清对方，重跑各自重建、自愈；
- PII 纪律：只存 SHA-256 + 掩码预览，绝不存原文（同 node02）。

### 9.3 验证

`make test` **3464 passed / 1 skipped（exit 0）**、`make lint` 绿、sim smoke exit 0。
新增 11 条测试：回收口的逐命中忠实度（含三类 FP 不得留痕）、`masked_id` 不产 finding、
不传 findings 时逐字节兼容、落库行形态（前缀/severity/action/hash+preview 无原文）、
前缀不与 node02 相撞且 DELETE 只清自己、同篇去重、两个阶段各自的 fail-open、
G21 调用点接线（源码级）、以及"纯留痕不碰任何判定"。

### 9.4 仍未做的

`RAG_IMAGE_OCR_PII` **维持默认 OFF**（§8.2 的判据不变：它唯一的增量是把 2 篇在用的
public 手册降级成下架）。§6 的 #2（2 篇历史残留重切）Sam 明确暂不处理 —— 但请留意：
代码修好后，**那 2 篇下一次被任何原因重切时的行为取决于届时 flag 的姿态**（关着=掩成
`331081****6636` 继续服务并留下 `image_scrub:cn_id_card` 审计行；开着=整篇隔离）。
