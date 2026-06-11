# 四层诊断判定树

> 原则：自上而下逐层取证，每步有探针命令，拿到证据再进下一步。
> 每棵树末尾附"已修根因案例对照"——先比对历史案例能省一半时间。
> 生产只读入口统一：`from opensearch_pipeline.prod_access import get_prod_readonly_conn`。

## §图缺失判定树（为什么没图 / 图记得有却没出来）

按序回答八个问题，任何一步答案为"否"即定位到该层：

**Q1（L1）图存在吗？** RDS 全量盘点目标文档的绑定图：

```sql
SELECT chunk_type, step_no, image_refs_json FROM chunk_meta
WHERE doc_id=%s AND is_active=1 ORDER BY chunk_id
-- 逐条解析 image_refs_json：caption 用 visual_summary，按用户描述的关键词 LIKE 检索
```
注意 caption 截断陷阱：先看全文再下结论（"手持记录单"的全文里可能藏着
"底部有条形码及红色激光扫描线"——那就是扫码枪图）。

**Q2（L1）绑定在对的 chunk 上吗？** 看 Q1 结果里图挂的 step_no/section 与
用户预期步骤是否一致；不一致 = 摄取层绑定问题（错绑/丢绑）。

**Q3（L1）是版本回归吗？** 对照上一版本（`is_active=0 AND version_no=<v-1>`）
的 image_refs：旧版有、新版无 → 回灌回归；旧版也没有 → 非回归（可能是首次绑定
后的新期望）。

**Q4（L2）承载 chunk 被检索到了吗？** 跑 `scripts/diag_answer_chain.py "<问题>"`，
看带图 chunk 清单里有没有它、它的 rerank 分、以及**是否进了 context**
（headers 列表）。没进池 → 检索层（裸排名锚定：`scratch/ha3_query_prod.py`）；
进池没进 context → 看扩展构成与截断（chunks 总数 >30 警惕扩展洪泛，
对照 `RAG_STEP_EXPAND_FAMILY_CAP`）。

**Q5（L3）提示形态对吗？** diag_answer_chain 输出的 header 里，该 chunk 应有
`[📷 图片] <<IMG:N>>`。有标记缺 📷 标签或全无 → `_format_context` 分支问题。

**Q6（L3）LLM 引用了吗？** `scripts/probe_citation_x3.py "<问题>"` 跑 ×3：
原始 `<<IMG:N>>` 标记列表 vs 带图文档编号。0/3 或 1/3 引用 → 生成层引用倾向
（prompt 规则 10 域）；注意引用是概率行为，**单次不引用不构成结论**。

**Q7（L4）配额/轮转吃掉了吗？** probe 的 `--synthetic` 模式（合成全引用）隔离
builder：合成下图在、真实下图缺 → 引用问题；合成下也缺 → builder
（轮转配额 `RAG_MAX_ANSWER_IMAGES`、近重抑制误杀——看日志"近重图片抑制"行）。

**Q8（L4）渲染层丢了吗？** UI 网络层（签名 URL 是否 200，常见 403=签名 key
权限/过期）+ DOM `naturalWidth>0`。API blocks 有图而 UI 无图才查这层。

**已修案例对照**：
- step_card 提示缺 `[📷 图片]` 标签 → LLM 引用倾向 1/3（Q5/Q6，2026-06-11 修）
- 规则 10 被动语态 → 步骤类默认不引用（Q6，已改"默认应插入+严禁无关"）
- max_images=3 顺序整段消耗 → 后位步骤图（扫码枪）永远挤出（Q7，已改轮转+6）
- 孪生文档灌满引用 → 重复表单图占名额（见 §重复判定树）
- PDF 资产镜像/倒置 → 图"在"但内容错（Q1 全文 caption 即可识别失真描述）

## §拒答判定树（该答而未答 / no_result=True）

**Q1 检索空吗？** API 响应 `sources` 为空 → 纯检索问题（裸排名锚定 + 变体查询
试词汇 gap：用户口语 vs 文档术语，如"员工档案"vs"人员档案"）。

**Q2 sources 非空时，分数形态？** diag_answer_chain 看 rerank_score：
- 全场 < 0.8（medium）→ 低置信带 → 守卫规则注入 → LLM 严格核对后拒答
  （行为正确——问题回到"为什么相关内容分数低"= 检索/词汇层）
- 有 ≥0.8 的 chunk 仍拒答 → 看 Q3

**Q3 目标内容进 context 了吗？** headers 里找目标小节。关键病灶模式
（2026-06-11 J-r120_23 实证）：**扩展洪泛**——超大手册 mega-parent 的
step_no=0 大规模平局使兄弟扩展退化成全家族（50+ chunks ≈ 15k 字），
组内 step_no 升序让目标垫底被预算截断。看 chunks 总数、context 字符数、
目标小节是否在 headers 内。已有防洪 `RAG_STEP_EXPAND_FAMILY_CAP=12`，
若复发先确认该配置生效。

**Q4 LLM 自主拒答？** guard=False、内容也进了 context 仍拒答 → 偶发采样
（×3 复测定性；已知 ~11% 偶发尾部含 DashScope 抖动）。可复现的自主拒答才值得
动 prompt。

**guard 字段读法**：`guard = is_low_confidence_band(chunks)`（rerank 分优先，
max≥0.8 即 False）。sources 里展示的分可能是**兄弟继承分**（扩展 chunk 继承
原命中分 ×0.85），别拿它推断 band——以 diag_answer_chain 的 rerank_score 为准。

## §重复判定树（图重复 / 来源出现两次 / 正文有来源列表）

**形态 A：正文"来源依据：/参考来源：" + 结构化面板 = 双重引用**
- 检查 `strip_doc_citations`（llm_generator）覆盖：编号形态 `[文档N]` 与
  **标题式来源段**（标题行 + 《标题》/文件名列表）均应被清洗（2026-06-11 已修，
  含 bot 侧 `_strip_trailing_sources` 词表）。复发 → 收集原文形态加正则 + 测试，
  注意误杀防护（"处罚依据：《员工手册》…"是正当答案）。

**形态 B：同一张表单/界面的图出现两张**
先用 UI 取证模板的 `img_docs`（URL 中的 doc_id）判归属：
- 来自**不同 doc_id 且标题仅扩展名不同** → **孪生文档**（pdf+docx 双活）。
  全库盘点 SQL：title 去扩展名分组、组内 >1 且各自有 active chunks
  （现成清单 `scratch/title_twins_20260611.md`，42 组）。处置 = 内容级核验
  同一性（步骤文本逐条对照）→ 经用户授权退役较弱版本
  （模板 `scratch/retire_twin_wi007_docx.py`：RDS 停用 + **HA3 同步删**——
  只停 RDS 的话 HA3 会继续服务到下次 清理stage3）。
- 来自同一 doc_id → 近重抑制的值差异政策（取值不同的变体**有意保留**，如
  不同账套的两张登录图）；确属同屏真重复 → 看 builder 近重日志为何未判。

**形态 C：相似但合法**——不同文档对同一表单各有实拍（如 WI-007 与 WI-008
都拍了产品标识卡）：内容判定后通常保留；引用噪声靠 prompt"严禁引用与回答
内容无关的其他文档的图片"约束。

## 通用：偶发 vs 确定性判定

| 信号 | 定性 | 动作 |
|---|---|---|
| API 直连复测即消失 | 偶发（采样/DashScope 抖动） | 记录不修，频发再统计 |
| ×3 中 ≤1 次异常 | 倾向性问题 | prompt/提示层候选 |
| ×3 全复现 + 合成隔离同样复现 | 确定性 bug | 定位层修复 + 回归题集 |
| 只在 UI 复现、API 正常 | 渲染层或**读错气泡**（pitfalls） | 先核 n_msgs 再说 |
