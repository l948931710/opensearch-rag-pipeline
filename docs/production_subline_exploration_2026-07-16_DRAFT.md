# 生产体系子部门全景探索(RDS + OSS + 钉钉组织架构)

日期:2026-07-16 · 状态:DRAFT(待 Sam 拍板策略)
方法:6 代理工作流(3 路并行只读探索 → 交叉映射 → 双重对抗复核)。全程 prod_ro 只读;
原始数据落盘 scratchpad `explore_rds/`、`explore_oss/`、`explore_dingtalk/`、`crossmap/mapping.md`。
复核结论:112 项数字/逻辑核对,无编造;3 处小错已在本文纠正(见附录)。

---

## 1. 入库全景(核心健康)

**RDS(document_meta / chunk_meta):**

| 维度 | 数值 |
|---|---|
| production 家族文档总数 | 867 = active 766 + inactive 64 + superseded 37 |
| active 按权限 | dept_internal 672 + public 94 |
| 活跃 chunk | 18,577 条,**100% INDEXED** |
| 双活版本文档(全部门) | 0 |
| 退役文档残留活跃 chunk | 0 |
| 入库时间线 | 2026-05: 152 · 2026-06: 715 · 2026-07: 0 |

**OSS(raw/production\*/,6 个目录):** 1,522 对象(7 个目录 marker,真实文件 1,515),约 2.41GB。
`raw/production/` = internal/ 676 + \_quarantine/ 710(709 文件 + 1 marker)。

**OSS↔RDS 对账(最重要的健康结论):**
- **active 文档 current 版本丢原件 = 0**;internal/ 676 个对象全部有 RDS 注册。
- OSS 有而 RDS 未注册 = 724 文件:709 个是 \_quarantine/(scanner-skipped staging,设计使然,即既有 PII/待裁决积压)+ **15 个 production_straw 旧格式文件(真缺口,见 §3)**。
- RDS 注册但 OSS 404 = 76 key,全部无服务影响:34 个 `#redacted` 虚拟衍生 key(wave-v2 退役,404 属预期)+ 42 个 2026-05-13 批旧格式(.doc/.xls)注册行,原件已被 05-20 的 docx 转换件替换清理(inactive)。

## 2. 组织架构 ↔ OSS 目录 ↔ 文档 映射表

钉钉生产中心(dept_id=599318766,faceCount 2,968 人)子树 87 节点,**相对代码快照
`_PRODUCTION_WORKSHOP_DEPTS` 零漂移**(85 名 + 有意排除的资材部),白名单无需刷新。
直接子部门(二级部门)共 11 个:

| 二级部门 | 人数(faceCount) | OSS 目录 | active 注册 | 检索白名单 |
|---|---|---|---|---|
| 纸杯事业部 | 889 | raw/production_paper_cup/ | 46(+31 inactive) | ✅ |
| 注塑事业部 | 677 | raw/production_injection/ | 18(+12) | **❌ 缺** |
| 吸塑事业部 | 508 | raw/production_thermoforming/ | 14(+9) | ✅ |
| 吸管事业部 | 40 | raw/production_straw/ | 6(+6;另 15 个旧格式从未注册) | **❌ 缺** |
| 模具车间 | 35 | raw/production_mold/ | 9(+5) | ✅ |
| **吹膜车间** | **172** | **无目录** | 0 | — |
| **纸箱车间** | **169** | **无目录** | 0 | — |
| **纸浆模塑事业部** | **92** | **无目录** | 0 | — |
| 生产部(管理部门) | 49 | 无专属目录 | 0 | — |
| 资材部(归 supply/pmc) | 16 | 无(有意) | — | — |
| 精益部 | 5 | 无目录 | 0 | — |

`raw/production/` 本体 = 伞级 catch-all,对应「生产中心」整体,不配给任何二级部门。
海外中心(599944033,印尼/获胜工厂/墨西哥/国内办公室)完全不在 production 伞组——海外产线归属口径未拍板。

**两个核心问题的答案:**
1. **生产文档是否都映射到子部门目录?否。** active 766 篇中仅 93 篇(12.1%)在子部门目录下可从
   raw_key 恢复归因(paper_cup 46 / injection 18 / thermoforming 14 / mold 9 / straw 6);
   673 篇(87.9%)在 raw/production/internal/ 下不可归因;raw_key 异常 = 0。
   doc 层 owner_dept 766 篇全是伞值 `production`,子部门信息只存在于路径(+9 篇的 chunk 层)。
2. **哪些部门没文档、后续需要上传?** 吹膜车间 + 纸箱车间 + 纸浆模塑事业部——合计 433 人
   完全没有专属目录也没有文档;生产部/精益部如有 SOP 也无处可放(无目录)。

## 3. 问题清单(按严重度,已复核)

| # | 级别 | 问题 | 证据 |
|---|---|---|---|
| 1 | P1 | **版本漂移在服**:「茶话弄 16oz中空杯.pdf」(DOC_PRODUCTION_20260622085623_CFB1E5)current_version=2 为 SKIPPED_EMPTY/NEEDS_REVIEW,但 44 条 v1 chunk 仍在服;账面版本与在服内容不一致,NEEDS_REVIEW 无人闭环 | explore_rds/05c |
| 2 | P2 | **幽灵 active 文档 ×11**:图稿/刀线类 PDF(text 18–180 字符)status=active+PUBLISHED 但活跃 chunk=0(EMPTY),检索永远不可见,台账虚高覆盖率;全部 06-21/22 批次 | explore_rds/05b |
| 3 | P2 | **straw 静默摄取缺口 ×15**:吸管事业部上传的旧格式 SOP(xlsb 6/doc 6/xls 1/pptx 1/xlsx 1)因格式不支持从未注册,部门无从知晓 | explore_oss/straw_unregistered.txt |
| 4 | P2 | **白名单缺口**:production_injection、production_straw 不在 `_PRODUCTION_UMBRELLA_OWNERS`——今天 0 实际影响(doc 层全是伞值),但一旦这两目录出现 dept_internal 子线文档即 fail-closed 静默不可见 | retriever.py L359-364 |
| 5 | P2 | doc↔chunk owner_dept 分裂 ×9(doc=production,chunk=paper_cup 7/thermoforming 2);对齐方向须遵守「不归一化 subline」拍板,且 doc 层改 subline 前要先让 kb_authz 管理面支持伞形匹配 | explore_rds/06 |
| 6 | P2 | 退役未回写 ×21:inactive 文档 current version 仍 PUBLISHED(纯台账;活跃 chunk 已 0,HA3 侧本次未核) | explore_rds/04 |
| 7 | P2 | 一级部门「lzdqr」(dept_id=417762615)挂 461 人,名字占位/拼音,身份归一 fail-closed 仅 public——与 07-03 部门映射缺口扫描同方向 | explore_dingtalk/level1 |
| 8 | info | \_quarantine/ 709 文件账外属 staging 设计,但为 Windows 目录原样倾倒(5 个 Thumbs.db、中文子目录、2 个 0 字节 PDF),上传无清洗;內容即既有「脱敏重灌」积压 | explore_oss/quarantine_orphans.txt |
| 9 | info | admin 部门 status 大写 ACTIVE(59) 与其余部门小写不一致(DDL 默认值即大写);SQL ci 掩盖,Python 精确比较会漏 | explore_rds/01 |
| 10 | info | permission_level NULL ×1(DOC_PROD_20260518_003,inactive 早期 seed);42 个旧格式原件删除无审计痕迹(历史清理);子线目录平铺无 internal/ 结构、mold 混入 2 png | explore_rds/08 等 |

## 4. 策略建议(待拍板)

**先前拍板延续**:存量 673 篇伞目录文档**不做人工归因**(Sam 2026-07-15:已入库没标就没必要)。
子部门粒度走**增量路线**——机制全部现成,缺的只是目录和习惯:

### A. 代码侧(低风险,可先做)
1. `_PRODUCTION_UMBRELLA_OWNERS` 补 `production_injection` + `production_straw`(消 #4 陷阱);
   吹膜/纸箱/纸浆模塑的 owner 值在建目录同一批加入(白名单原则=只收 approved+live)。
2. 管理台 DocTable 加「来源车间」派生列/筛选(从 raw_key 前缀只读派生):93 篇立刻可分,
   其余显示「未分流」。零 ACL/HA3 影响。

### B. 运营侧(Sam/业务动作)
3. 建 3 个新目录:`raw/production_blown_film/`(吹膜)、`raw/production_carton/`(纸箱)、
   `raw/production_pulp_molding/`(纸浆模塑),并定规范:**生产各事业部后续上传一律进自己车间目录**
   (建议子线目录也统一 internal/ 子结构,与 raw/production/ 对齐)。
4. straw 15 个旧格式文件:请吸管事业部转存 docx/xlsx 后重传(.xls/.xlsb 不支持是明确决定)。
5. 海外产线(印尼/获胜/墨西哥)知识归属口径拍板:并入 production 伞?独立组?暂不覆盖?

### C. 数据修复(生产写,逐批授权)
6. 茶话弄 #1:裁决 v2(空抽取)回滚 current_version 或请部门重传有效 v2。
7. 幽灵 11 篇 #2:图稿类要么走图片管线重灌,要么明确退役/标注不可检索,别虚计覆盖。
8. 台账清理脚本一次收口:#6 21 篇回写 + #5 9 篇对齐 + #10 NULL 一例 + admin 大小写归一。

### 明确不做
- 存量 673 篇人工归因(等自然升版/增量分流稀释)。
- \_quarantine/ 709 不新开工作流,维持既有「脱敏重灌」user-gated 路线。

---

## 2026-07-16 P1+P2 修复裁决与执行记录(修正原表若干结论)

**代码侧(已落 ontology-p0 `cfc1bb9`,已推;测试全绿)**:
- #4 白名单:injection+straw 已加入 `_PRODUCTION_UMBRELLA_OWNERS`;
- 新增 `kb_authz.expand_managed_owner_depts`(管理面 production 伞形展开,读≠管理,
  接入 `_kb_can_manage`/`_kb_owner_scope_sql`/kb_access 三处清单 SQL)——为未来子线
  目录入库的文档铺平 dept_admin 管理面;console 补注塑/吸管展示 label。

**生产数据侧(脚本 `scratch/fix_p1p2_prod_ledger_20260716.py`,dry-run 预览已核,
执行等 Sam 当日 `PROD-RW` 令牌)**:
- #1 茶话弄:根因坐实——v2(07-06)是对**空文件路径**跑的抽取
  (`pdfplumber: No such file or directory: ''`,retry×3,page_count=0,checksum=NULL),
  文件本体 06-22 后从未变过;v1 44 chunk 因 deactivation 不变量幸存。
  修复=current_version 2→1 + 删幻影 v2 行(不删则下次升版撞 `uk_doc_version` 唯一键)。
- #10 admin 大小写:原报告「59 行大写」是 ci collation 在 GROUP BY 的显示假象,
  **实际大写行只有 1 行**(脚本用 BINARY 精确匹配)。
- #10 permission NULL:按路径段 resolve 补 `public`(raw/production/ 根,无 internal 段)。

**修正/推翻的原结论**:
- #5 「doc↔chunk owner 分裂×9」实况修正:不是两层分裂,而是**同一文档内
  text/step chunk=production、仅 image chunk=子线**(06-15 重 chunk 批次 image 伴生
  chunk 用路径推导、文本 chunk 用 doc 层 owner 的双口径)。根因已由 F-19 在代码侧
  统一(image chunk owner 取 RDS 权威值)。真对齐须连 HA3 重推而 9 篇全 public、
  伞形/marketing 两条读路径均覆盖 → **NO-FIX,留自然升版自愈**。
- #2 幽灵 11 篇:**降级 no-fix**——console 徽章与 kb_stats 已统一按 chunk_status=EMPTY
  诚实显示「未入索引」(#7 修复过),无"虚高覆盖";属 06-24 已裁决 image-only 类,
  要检索可见走 B 档图片管线重灌(运营决策)。
- #6 21 篇 inactive+PUBLISHED:**降级 no-fix**——徽章第一优先级=非 active→已退役,
  无消费方 bug;publish_status 是"曾发布过"的历史事实,不应改写。
- #7 lzdqr:强假设=「**离职待确认人**」(拼音首字母吻合;根级、无子部门、无简介、461 人)。
  若属实,fail-closed 仅 public 是**正确行为**,不加映射。成员级确认被 API 权限挡
  (应用缺 `qyapi_get_department_member` scope),需 Sam 在钉钉后台肉眼确认或开 scope。
- #3 straw 15 个未注册:细分为 13 个旧格式(需部门转 docx/xlsx 重传)+ **2 个支持格式
  (xlsx/pptx)未被扫描**——后者指向"生产 7 月新增注册=0"的扫描停摆疑点,待查。
  移交清单=docs/straw_files_handoff_2026-07-16.md。

## 2026-07-16 「扫描停摆」疑点调查结论

**结论:不是停摆——摄取管线从未有过自动调度;顺带抓到一个每天被吞的 CRITICAL。**

1. **DataWorks 实况**(项目 609583,ListTasks/ListTaskInstances 实查):stage1/2/3 + 清理
   节点全部 `Recurrence: Manual`(手动触发,建成即未挂 cron);周期调度上只有
   `ops_health_monitor`(每日 02:30)和虚拟根节点。**所有入库历来都是手工批**:
   5-6 月 cohort 批、07-06/07 的 586 行版本重灌批(即 HA3 sparse 失明 485 事件的重推批,
   茶话弄幻影 v2 也产自它)。
2. **「7 月零新增」的准确口径**:7 月无新文档注册(document_meta),但有 07-06/07 升版
   活动(document_version +586 行,全 v2)。06-16 落 OSS 的 15 个 straw 文件(含 184MB xlsx
   + 23.6MB pptx 两个支持格式)之后,再无任何扫描碰过 raw/——最后一次全量新文件 sweep
   在 06-16 之前(06-20/21/22 的 1068 行注册全是 cohort 点名批)。
3. **每天被吞的 CRITICAL**:ops_health_monitor 每日正确检出 `reconcile_ha3: ALERT`
   (RDS↔HA3 parity drift)并 exit 2,但 DataWorks 节点未配 `RAG_OPS_ALERT_WEBHOOK` →
   `ops-alert SUPPRESSED-CRITICAL`,节点日日 Failure 无人接收(实例日志实查,07-14/07-06
   两日抽样一致;另见日志内 P0-02 RDS TLS 未配警告=已知 user-gated 尾巴)。
4. **drift 本体量化**(本地 PROD-RO 全量对账 142s,报告存 scratchpad
   parity_report_20260716.json):RDS active+INDEXED **27,457** vs HA3 PK **27,357**,
   恰 **100 条 chunk 缺失**,散布 ~80 篇文档(整篇消失 0 / stale 0 / orphan 0);
   96% 为 06-21/22 注册的 production 文档,类型混合(clause 28/table 24/image 24/ocr 11/
   text 10/step 3)。与已知「HA3 静默丢推 ~1%」家族吻合,疑似 07-12 融合修复重推波的掉件
   (100/万级 ≈ 1%)。影响=80 篇文档各有局部内容检索不到(非整篇盲)。

**建议(均 user-gated)**:A. bounded re-push 修 100 条丢件(老配方:置 NOT_INDEXED→
stage-3 重推→settle 双趟复验);B. DataWorks 节点配 `RAG_OPS_ALERT_WEBHOOK`(钉钉机器人),
让监控喊得出声;C. 拍板长期口径——stage1/2/3 挂真日调度 or 轻量「raw/ 新文件检测→告警」
节点;同时定 >20MB 大文件摄取策略(184MB xlsx 即使被扫也过不了现行深水区门槛)。

## 2026-07-16 深夜 /goal 执行记录(A/B/C+straw+海外,Sam 授权当日 PROD-RW)

**A. 100 条丢件重推 ✅**:标脏(`scratch/repush_missing_chunks_20260716.py`)→ stage-3 四轮:
run1 卡 embedding 镜像同步(261MB OSS mirror,已 kill;教训=laptop 跑加
`RAG_EMBED_CACHE_OSS_MIRROR=false`);run2(main)推送 4 次全网络超时 failed=100;
run3(worktree,读超时 120s)11/100 进;run4(**子批 10 条≈240KB**)89/89 全中。
**终局:100/100 点查 present**(逐 PK filter 查询)。三个新 RCA 沉淀:
① 本机↔HA3 公网链路当晚劣化(ICMP 丢包 66%),**大 POST 尺寸敏感**——1.2MB 必死、
240KB 稳过 → 烂链路下 `RAG_HA3_PUSH_BATCH_SIZE=10` 是标准解;
② HA3 SDK 默认读超时过短已修(`clients.py` runtime_options,ontology-p0 `46b2272`);
③ **id-range 枚举对增量段可见性滞后远超既往 ~120s 经验值**——双趟对账仍报 100 缺失时
点查却 100/100 在,枚举收敛前「>0 missing」不可作为推送失败的证据(反向:push 响应
success 也不可作为落地证据,唯点查/枚举收敛为准)。明晨 02:30 监控应自然转绿,不绿再查。
另修 run2 被 kill 残留的 80 行 PROCESSING 索引锁(复位脚本化)。

**straw 2 文件入库 ✅(全链路完成+终验)**:点名注册(`scratch/register_straw_two_20260716.py`,
owner=production_straw,查重零命中)→ stage-1 首跑暴露**环境坑:/usr/bin/python3 缺
openpyxl/python-pptx,优雅降级把 ImportError 吞成 warning、canonical 0 块还标 SUCCESS**
(已复位换 stack-test python 重跑;「canonical 0 块+SUCCESS 硬告警」已开后续任务)→
重跑成功:xlsx 579 字符 34 块+24 图全 VLM 摘要(8 张去重,1 张降级待重扫)、pptx 270 字符
+14 图;184MB xlsx 的**资产上传耗时约 1h**(24 张巨型截图 ~270MB,烂链路)——>20MB 政策
实测数据点 → stage-2:27 chunk 全有效零隔离(xlsx=1 text+24 image;pptx=1 text+
1 visual_knowledge),owner=production_straw/public/PUBLISHED → stage-3:27/27 一批全中
(2.6s,链路已恢复)。**终验:RDS 27 INDEXED + HA3 点查 27/27 present。**
吸管事业部的两篇 SOP 正式可检索(公开级,图文齐全)。

**B. 告警接通(差最后一步=Sam 铸造 webhook)**:节点粘贴源已含铸造步骤注释+作业清单
(`dataworks_nodes/ops_health_monitor_node.py`,`6f7bf14`);钉钉群「智能群助手→自定义
机器人(加签)」拿到 URL+SEC 后填入节点即通。
**C. 调度口径 ✅**:新增 CS4c `unregistered_raw` 检测(可摄取+超24h 未注册才告警;
旧格式/隔离区/自助上传形状不驱动红)接入 ops_monitor;**决策=stage 保持 Manual**
(部署 zip 陈旧+sparse 风险),监控检测→告警→人工点名批,待 refreeze/重打包后再议真调度。
**海外并伞 ✅(实为已完成项)**:5 个海外部门名 2026-07-03 拍板批就已映射
`["overseas","production"]`(main+分支双有,实测解析通过)——本报告原 P2-#9 判错,收回。

## 附录:对抗复核纠错记录
- OSS 对象 1,522 中目录 marker 为 **7** 个(非 6),真实文件 **1,515**(非 1,516)。
- straw 未注册 15 个的扩展名分布:doc 为 **6**(非 5),即 xlsb 6/doc 6/xls 1/pptx 1/xlsx 1。
- `raw/production/` 1,387 = root marker 1 + internal/ 676 + \_quarantine/ 710。
- 两个「867」是不同口径的巧合等值(doc 数 vs distinct raw_key),已各自闭合验证;
  867−76(404)=791=1,522−731(未注册) 闭合 ✓。

## 2026-07-20 拍板落地:三子线开通 + 归属口径收口(main a54ea32 / op0 2a70622)

**触发**:dept_admin 归属能力问询 → 钉钉 live 全枚举复核(生产中心 599318766)发现
包装车间(生产部下 3 组)在本报告"二级部门"口径中被漏盘(0 提及)。

**Sam 拍板**:
1. **开通** §建议 B3 缓办的三条子线:`production_blown_film`(吹膜)/`production_carton`
   (纸箱)/`production_pulp_molding`(纸浆模塑)——伞形白名单 6→9,单一来源级联
   检索/上传下拉/写校验;OSS 目录不预建,随首篇上传自动落位(白名单原则 as-built 调整);
2. **包装车间归伞值**(横跨产品线,不设子线);
3. **三级部门一律先归伞**(机修/班组长/料房等子组不设 owner 值);
4. 海外产线归属口径维持未拍板(overseas 用户组叠读 production 已生效)。

**读侧核验**:`_PRODUCTION_WORKSHOP_DEPTS` 06-21 快照与 2026-07-20 live 树**零漂移**
(85 节点,含包装/吹膜/纸箱/纸浆模塑全部子组)——读侧归伞早已覆盖,无需动。
**生效前置**:SAE 重打包(B7 既有项);上传下拉在重打包后即出现三新子线。
