# 设计 v2（已过门）：深思二轮 prompt 瘦身——agent 工具文本对齐普通路径上下文预算

- 日期：2026-07-11 · 分支 `claude/ontology-p0` · **v2 = 三评审（质量/架构/评测）条件全采纳后的修订版**
- 评审裁决：三方均「有条件通过」；本版逐条落实全部阻塞条件（正文中以 R①-n/R②-n/R③-n 标注来源）
- 状态：**已过门（2026-07-11）**——评审→实现（commit `bff49d0`）→质量评测三层全绿→默认翻 on（见 §8）

## 1. 问题（实测数据，v1 不变）

深思档 run `782b14ff`（2 次检索）二轮 prompt **22,165 tokens → 42.4s**；light 单检索 5.5-8.8k → 10-16s。
普通问答路径的 context 打包有预算与截断语义（`_format_context_ex`，签名默认 6000 chars），
budget_ab（2026-06-09）实测该口径下 60-75% 的普通问答上下文本就被截断——**全部答案质量基线
（251 金集/阈值/图文评测）建立在这个口径上**；而 agent 工具文本无任何预算，22k 是
无预算 × 多次检索的自然结果。

**事实修正（R②-3）**：普通路径实际生效的是 `llm_generator` 函数签名硬编码默认 6000；
`config.rag.max_context_chars` 在 serving 侧是**死配置**（api.py/dingtalk_bot 均不传参）。
因此本设计的预算缺省取**字面 6000** 并注明与签名默认同值；「接通 config 让普通路径可调」
是普通路径的独立行为面改动，与本设计解耦（记入相邻议题清单 §7）。

## 2. 设计原则（措辞按 R②-11.3 收窄）

复用普通路径打包器 `_format_context_ex`：**header/相关度标签/图标记/截断语义与普通路径同源**
（非逐字节同文——agent 侧另有诚实注记两行，见 §3.1）。质量参照系=普通路径的**截断语义**，
而非其尚未调优的默认值本身（R①-10）。

次生收益不变：相关度标签、半截防漏（#F-mm11）、included 截断感知（#8 反污染）。

## 3. 方案（v2）

### 3.1 打包（每次检索）

```python
context_str, included = _format_context_ex(chunks, max_chars=_budget(),
                                           pure_text=(call_index > 1), meta_out=meta)
```

- `_budget()` = `RAG_AGENT_TOOL_CONTEXT_CHARS`，缺省 **6000**（=llm_generator 签名默认同值）。
- **第 2+ 次检索 `pure_text=True`**（R①-11 采纳）：保留 [📷 图片] 语义文本、不再注入
  `<<IMG:N>>` 标记——v1 出图门本就只允许单检索 run，多检索的标记只会变成裸标记泄漏面
  与编号漂移面，一并消灭。
- `meta_out`（打包器新增的向后兼容出参，R①-1 前置）：回填
  `{full_idx, halfcut_idx, salvaged_idx, dropped_idx}`——去重登记只认 `full_idx`。
- **诚实注记分形态**（R①-4）：被预算丢弃的块中，与已含块同 `parent_chunk_id` 的 step_card
  记 M → `（该流程还有 M 个后续步骤因篇幅未展开，可继续检索）`；其余记 N →
  `（另有 N 条较低相关度资料因篇幅未展开，可换关键词再检索）`。两行都不进 marker 体系。
- flag off：走现有手工格式，**逐字节不变**。

### 3.2 跨检索去重（v2：字节等同 + 送达点提交）

- **key = `(chunk_id, sha1(完整 chunk_text))`**，chunk_id 缺失退 `(doc_id, sha1(完整文本))`
  （R①-2/3）：只在字节等同时去重——「零信息损失」从声明变为构造性保证；stitch fail-open
  不对称/重拼形态差异自然放行（保守方向）。
- **seen 只登记完整展开块**（`meta_out.full_idx`；半截/salvage 压缩条目不登记，R①-1）。
  由此获得**翻页涌现性质**（R①-4 正面确认）：首查被截的尾步不登记 → 模型再检索时
  前排命中被去重腾出预算 → 尾步完整展开。单测锁定该性质。
- **登记走送达点提交**（R②-4，开 flag 的硬前置）：工具只把 keys staged 进
  `result.artifacts["dedup_keys"]`；**executor 驱动线程**在收到 `status=="succeeded"`
  的结果、`gen.send` 之前提交进 `ctx.search_session`（duck-typed `commit_keys`，
  executor 不 import agent_tools）。超时孤儿线程/义务扣留路径的结果永不被消费 →
  keys 永不提交；所有写收敛到驱动线程，**无锁**且无「登记了没送达」语义竞态。
- 状态通道：`ctx.search_session`（瞬态三律：服务端构造/绝不序列化/resume=None→
  去重自动禁用 fail-open——**审批挂起续跑段去重失效**是已声明的可感知行为，R②-6）。
  前提声明（R②-5）：依赖同 run 工具调用串行（executor 单驱动线程）；引入并行调度须重审。
  与 `speculative_search` 合并为单一 scratch 槽位记为后续清理（R②-6 非阻塞）。
- 膨胀下界声明（R②-7）：去重只按块身份，不折叠拼接窗内的内容包含性重叠。

### 3.3 否决表（v2 增补第六行，R①-9）

| 否决项 | 理由 |
|---|---|
| A. 盲目按字符截正文 | 数字/期限常在尾部；半截语义已含护栏 |
| B. 降 top_k / 砍 step 扩展 | 动召回与流程完整性校准前提；budget_ab 证明 k7→k10 是升分方向 |
| C. 第二次检索更紧档位 | 未校准新面；减脂由去重+pure_text 承担 |
| D. LLM 二压摘要 | 加一次调用=负延迟 + 保真风险 |
| E. 改写历史里旧工具消息 | 破坏 DashScope 前缀缓存（实测 1024-块粒度）；缓存收益限 run 内多轮，跨轮 session 裁剪本非本设计所辖（R②-8） |
| F. 同检索内拼接窗合并（新） | 相邻命中窗互含（[4,5,6]/[5,6,7]）是单次结果内的另一块水分，但合并窗=动 header/来源/标记粒度=动校准面；不做，且 §6 收益估算据此下调 |

### 3.4 契约（v2 修正）

- `artifacts = {"chunks": <传给打包器的那个列表>, "included": [...], "dedup_keys": [...]}`
  ——**chunks 恒等于打包列表**（首检索=全量；2+ 次=去重过滤后），`<<IMG:N>>` 编号基准
  由此钉死（R①-5）；v1 出图门（仅单检索）下首检索无去重，blocks 对位与普通路径一致。
- sources 帧用 `included`；flag off 时 artifacts 无 "included" 键 → 回退 "chunks"（R②-1/11.1）。
- `receipt.doc_ids` 维持全量检索遥测；新增 `ctx_chars/dropped/dedup` 计数（R①-8）。
  agent qa 归因（retrieved/cited 双口径）是既有相邻缺口，不在本设计内（R①-8 注记）。
- **输出卫生（R①-7/R②-2）**：`_AGENT_SYSTEM_PROMPT` 在 flag on 时追加两句——
  ①标签语义（高/中可直接依据；「低」须核对内容后取舍，能直接支撑答案则照用）
  ②不得在答案中引用「文档N」编号（规则 8 等价）；同时修正「N 为条目编号」措辞。
  **提示词做成 flag 条件化**（off 臂=今日原文，保证 commit 1 零行为变化，R③-5/7 的臂位一致性前提）。
  final_text 在 `_remember` 落库与 blocks 构建前过 `strip_doc_citations`
  （顺序对齐 api.py：先清编号引用、blocks 用带 IMG 标记原文）；流式增量无法整流清洗
  =已声明残余，靠 prompt 压制。前端 agent 渲染管道对裸 `<<IMG:N>>` 的兜底在实现期核验。

### 3.5 开关

`RAG_AGENT_TOOL_CONTEXT_BUDGET`（开关，默认 off）与 `RAG_AGENT_TOOL_CONTEXT_CHARS`
（预算值，默认 6000）两个 env 并列声明防混用（R②-11.5）；开关用 `os.environ` 即时读
（对齐 RAG_AGENT_SPEC_RETRIEVAL 模式），评测/探针可干净钉臂（R③-5）。

## 4. 质量评测门（v2 按 R③ 全面重写）

### 4.1 确定性单测（commit 1 合入即跑）
打包 meta_out 正确性/预算生效/两形态诚实注记/字节等同去重 key（含无 id 回退）/
**半截与 salvage 不登记 + 翻页性质**/送达点提交（超时孤儿永不提交）/pure_text 二次检索/
artifacts 契约（chunks=打包列表、included、dedup_keys、flag-off 无 included 键回退）/
marker 对位（含 salvage 原始 N）/flag off 工具文本逐字节回旧格式/条件化提示词 off 臂原文。
**外加端到端一跑（R③-8.2）**：off 臂对现冻结基线 `make agent-eval-gate` 必须绿。

### 4.2 L7 扩展（新用例与重冻**捆绑在翻默认 commit**，R③-7）

新增 3 例（归 grounded 族，满足族≥4 断言；n_total 28→31，两处测试断言同步改，R③-2）：
- `g-long-1`：8 条长块、金事实在预算内第 2 块、**金块之前/之后为明显离题的长填充**
  （R③-1.4）→ grounded=1.0；
- `g-low-1`（R①-6）：金事实在预算内但相关度标签为「低」→ grounded=1.0（标签语义指令的守护）；
- `h-long-1`（按 R③-1 重新规格化）：金事实只在预算外尾块；验收=
  **「诚实标记 ∪ 金 token」任一命中即过**（再检索后答对与诚实说没有都是合法终局）+
  `must_not_contain=[预埋诱饵错误值]`（编造判死）。runner 增 `must_not_contain` 支持 +
  `RunFailed(max_turns…)` 改判行为性失败而非 error_count（R③-1.3）。
- 存量杠杆（R③-4.1）：g01-g04 在金块**之后**追加长离题填充（金恒在预算内）——
  4 个存量用例免费获得截断敏感性，pass 语义不变。
- mock 数据面修正（R③-3）：语料行补 `title` + 跨档真实 score（8.5/7.0/6.0/4.5）——
  否则 flag on 下所有检索用例渲染成「未知文档(低 0.00)」的畸变数据面；
  runner 在 flag on 时按 serving 同构造 `search_session` 挂 ctx；
  scripted 理想 provider 按新 case 字段脚本化（自检 CI 满分约束，R③-2）。
  去重的多检索行为在 L7 的覆盖有限（mock 数据面单一），**如实声明主覆盖在单测**（R③-3b）。
- 可选第 4 例 `r-long-1`（诚实注记闭环：换词重查命中金）——mock 需升级 query→corpus 映射，
  列为非阻塞增强（R③-4.3）。

### 4.3 staging 真数据探针（v2 按 R③-6 两层化 + R①-10 加臂）

1. **廉价打包层预筛（零生成）**：候选题池（~15 题）flag on 跑检索+打包，只入选实测
   `dropped>0` 的题（多检索候选另看 dedup>0）；触发性由 receipt 计数**测量**而非题型直觉。
2. **生成层 A/B**：入选 ≥5 题 × 臂：off / on@6000 / **on@10000**（R①-10，仅对 dropped>0
   的题跑第三臂）；每题预登记关键事实清单（数字/步骤数/文档名，事实级计数为主报告单位，
   注明同题事实聚类）；on 臂 `dropped==0∧dedup==0` 的题=未触发试验（计无回归样本、
   不计触发配额），触发试验不足则补题；多检索试验 <2 个则结论如实降级。
   预登记数值线：触发题第 2+ 轮 `tokens_prompt` 中位数降 ≥25%。
3. **验收线（含诚实的统计措辞，R③-6）**：触发题关键事实零回退 + 无编造（诱饵法抽查）+
   来源⊆included 抽查。n≈5 单检索题全过仅能在 95% 置信下排除 ~45% 以上的每题回退率——
   本探针是 gross-regression 冒烟而非无回退证明；细粒度保障=同打包器结构论证 + L7 门 +
   上线后 receipt 遥测 + kill switch。任何编造/事实丢失 = 不过门，flag 保持 off。

### 4.4 冻结臂位与提交序（R③-5/7，两个 P0 的闭合）

- **commit 1（实现，默认 off）**：代码+单测；off 臂对现基线 gate 绿（零行为变化实证）。
- **证据阶段（不动基线）**：`RAG_AGENT_TOOL_CONTEXT_BUDGET=true` 强制 on 跑全量用例 +
  §4.3 探针——产出证据工件，非基线。
- **commit 2（翻默认，唯证据全绿）**：默认 on + 3 新例 + runner/scripted/测试同步 +
  在该 commit 的**默认 env（=on）下现跑并 `--freeze-baseline`** + 引用证据工件路径。
  单 commit ⇒ `git revert` 一步回滚默认+用例+基线，臂位恒一致。
- 重冻理由如实记：分母变化 **且处理臂（工具文本格式+条件化提示词）整体切换**（R③-8.1）。

## 5. 残余风险（如实入档）

预算外长尾事实答不全（普通路径同构 + agent 多一层注记/再检索缓解）；流式增量中的
`[文档N]`/`<<IMG:N>>` 残留靠 prompt 压制非确定性清洗；审批挂起续跑段去重失效（fail-open）；
去重不折叠拼接窗内重叠（膨胀下界）。

## 6. 预期收益（v2 下调，R①-9）

22k 深思二轮 → 预估 **~12-15k tokens**（各检索 ≤6000 chars + 跨检索去重；窗内重叠不折叠故
高于 v1 估的 10-12k），42s 作答轮 → ~22-28s；light 单检索基本不变；未缓存 input 成本同步降。

## 7. 相邻议题（本设计不决定，仅记录）

`config.rag.max_context_chars` 死配置接通与 6000→10000 全局调优（budget_ab 建议）；
agent qa 归因双口径落库；`speculative_search`+`search_session` 合并 scratch 槽位；
`_format_context_ex`/`_chunk_header` 私有名的外部消费方契约注释（随 commit 1 顺带加）。


## 8. 质量评测结果（2026-07-11，过门证据）

### 8.1 L7 扩展（31 例，qwen3.7-plus light 真模型）
- 新例 g-long-1（金块在预算内第 2 位+7 条长离题填充）/ g-low-1（金块标签「低」）/
  h-long-1（金块仅在预算外尾块+断言搭配词诱饵）——**ON 臂 grounded 9/9=1.0**，
  error_count=0，其余族全 1.0；write_propose 0.5↔0.667 震荡与 off 臂同幅
  （n=6 单例翻转=0.167>δ 的既有方差，与预算无关）。
- 过程中修正两个判分器盲点（诱饵裸值误杀诚实归因→改断言搭配词；h 族词表缺
  「未在」变体）——均为 R③-1 预言的可表达性问题。
- 报告：eval_harness/reports/agent_eval_20260711T160716.json（+T161012 第二跑）。

### 8.2 staging 三臂探针（打包层预筛 + 生成层 A/B）
- 预筛：12 候选题 9 题实测 dropped>0（真实语料检索扩展 7-22 块，included 压至
  3-12）；6 题入选，每题 3-4 条事实自 included 块预登记（共 23 条）。
- 生成层（18 次 agent ask）：**on@6000 关键事实回退 = 0/18**（off 命中 18 条全保持，
  另多补 1 条）；截断最狠两题（转正 22→5、注塑 12→3）抽查与语料一致、无编造。
- **二轮 prompt tokens 中位 7224→4540（-37%，超 ≥25% 预登记线）**，均值 8306→4622，
  长尾（14077）整个削平至 ≤5350。
- **on@10000 弃**：与 off 几乎同分布（中位 6755/均值 8111，预算几乎不咬合）、
  平均墙钟更慢（34.4s vs 30.3s）、且出现 2 条方差性回退——R①-10 第三臂的价值
  即此否定答案，agent 预算维持 6000。
- 诚实降级声明（R③-6）：18 次全部单检索——**跨检索去重的生成层未被真数据行使**
  （覆盖=单测 5 例 + 预筛机制验证）；多检索触发依赖模型自主，后续以 receipt
  遥测（dedup 计数）在真实流量中观察。
- 顺带发现并修复：流式增量中 [文档N]/<think> 残留（探针 1/6 与 3/6 命中，后者
  三臂皆有=既有缺口）→ 预算模式完成时恒发 content_blocks 替换帧（无图=清洗后
  纯文本块），定稿视图确定性干净；probe 原始 JSON 在会话 scratchpad（数字已录本节）。

### 8.3 裁决
三层全绿 → `RAG_AGENT_TOOL_CONTEXT_BUDGET` 默认翻 on（本 commit），基线在
默认臂（=on，31 例）现跑重冻；`git revert` 本 commit 即一步回滚默认+用例+基线。
