# G6 抽取保真金集 — 标注指南（fidelity_v1）

## 这个金集测什么、不测什么

| 测 | 不测 |
|---|---|
| 抽取产物 vs 原文档的**逐字保真**（char-F1 / CER） | 检索召回（goldset 管） |
| 表格**单元格内容 + 行列结构**（cell-F1 / grid_exact） | 答案质量（judge 管） |
| parser / OCR 模型**换版回归** | 图文绑定（binding L4 管） |

一句话：goldset 问"系统答得对不对"，本金集问"字抽得准不准"。前者隔着检索+LLM，
局部乱码/多栏串行对它可能不可见；本金集是 G3/G11/G20 这类 parser 改动与
qwen-vl-ocr 换版的**第一道回归网**，也是 tier-3（全页 VLM 解析）投入决策的尺子。

## 规模与选材（20-30 页就够）

按语料体裁覆盖，不追数量——每类 2-4 页即可：

- [ ] 原生 PDF 单栏（SOP 正文页）
- [ ] 原生 PDF **多栏页**（G3 的守卫）
- [ ] PDF **跨页表格**（G11 的守卫；ref_grid 写"拼接后应有的完整表"）
- [ ] 坏字体/乱码风险 PDF 页（G15 的守卫）
- [ ] **扫描件** 2-3 页（`requires_real_ocr: true`；OCR 模型换版的守卫）
- [ ] DOCX 含合并单元格表格（G1 的守卫；DOCX 无页码 → `page: 0` 全文比对）
- [ ] XLSX 图文作业指导书 1 份
- [ ] 含全角字母数字的页（ＦＣＡ００７３ 类；G20 的守卫）

种子来源：binding GT 已锚定的 9 个文档优先（doc_sha256 现成、语料代表性已验证）。

## 标注流程（stub 先行，人工只做校对）

```bash
# 1. 生成底稿（抽取输出预填 ref_text/ref_grid）
python -m eval_harness.fidelity.run_fidelity \
    --stub eval_samples/docs/某文档.pdf > eval_samples/fidelity_gt/stub_某文档.json

# 2. 人工校对：对照原文档逐字改错 —— 这一步是 GT 的全部价值所在
# 3. 校对完成 → _doc_meta.degraded 改为 false，条目并入 fidelity_gt.json
# 4. 全部就绪后冻结基线
python -m eval_harness.fidelity.run_fidelity run --freeze
```

### ⚠️ 三条铁律

1. **stub ≠ GT**。底稿是抽取器自己的输出，它的错误（漏字/串行/表格错位）会原样
   躺在 ref 里——不校对就冻结 = 自己给自己打分（binding GT 循环性 G23 的同款陷阱，
   那边的教训是"strict which-image 键是抽取器自己的 image_index"）。校对前
   `degraded` 必须是 `true`（不进 hard 口径，只算趋势）。
2. **doc_sha256 锚定源文件字节**。文档换版 = 新条目，绝不复用旧转写（stub 命令
   自动算好）。哈希不符时该 doc 记 SKIP，不会拿错的 GT 打分。
3. **scope=body 的转写不含页眉页脚**（管线本来就该裁掉它们）；表格内容不写进
   ref_text（表格由 tables 条目单独比）。

## 校对时怎么写 ref

- **ref_text**：按阅读序逐字转写该页正文（标题、段落、列表）。空白不敏感
  （比对前全部剔除），全角/半角字母数字不敏感（自动折半角）——**中文标点是敏感的**，
  照原文写。
- **ref_grid**：二维数组，一行一个子数组。**空单元格写 ""**（占位保结构——
  grid_exact 靠它抓 G1 类列错位）；合并单元格：内容写在左上格，被覆盖的格写 ""。
- 扫描件看不清的字：写你能确认的部分，整句不可辨认就不要写进 ref（宁缺勿错——
  错的 ref 会把正确抽取判成回归）。

## 读分数（三指标合读定位失败类型）

| char_f1 | cer | 结论 |
|---|---|---|
| 高 | 低 | 保真良好 |
| 高 | **高** | 内容都在但**语序乱**（多栏交错/跨页错拼——查 G3/G11） |
| **低** | 高 | 内容**真丢了**（截断/跳块/OCR 漏——查页上限/乱码门） |

表格同理：`cell_f1` 高 + `grid_exact=false` = 单元格都在但**行列错位**（查 G1 占位）。

## 基线与门（与 L6 三态门同语义）

- `run --freeze` 把 corpus 指标 + hard 文档集写进 `baseline.json`（**可提交**——
  只有分数没有内容；GT 本体永不进仓库）。
- 之后每次 `run` 自动比对：char_f1/table_cell_f1/grid_exact 跌幅或 cer 涨幅
  > δ(默认 0.02) → REGRESSION；hard 文档缺测**不算过**；hard 文档集变了 →
  INCOMPARABLE（先补齐或重冻，学 regime-mismatch 不给 GO）。
- `--strict` 下 REGRESSION/INCOMPARABLE → exit 1，可直接挂进 release-gate 脚本。

## 何时必须重跑/重冻

- parser 改动（pdf_extractor / docx_extractor / chunker 上游）→ **重跑**，看 gate。
- OCR/VLM 模型换版 → **重跑 --real**，看扫描件条目。
- 归一规则变更（NORMALIZATION_VERSION bump）→ 重跑；比对归一是 metrics 内联的
  独立口径，不随管线归一器漂移，历史分数仍可比。
- 新增 GT 文档 → 跑绿后 **--freeze 重冻**（文档集指纹变了）。
