# Tier-3（全页 VLM 解析/TEDS）投入决策——NO-GO（2026-07-06）

> 结论：**毙掉 tier-3**。按预定判据（G6 扫描件批次 char_f1 ≥ 0.9 → 不立项）：
> 纯扫描件上 qwen-vl-ocr 的字符保真 **0.9923**，已在天花板，全页 VLM 解析无提升空间。
> OCR-fallback 人群（278 份 PDF）的真实保真损失全部来自**管线集成 bug**（免费可修），
> 不是解析器能力——预算改投两个集成修复。

## 度量方法（G6 保真金集，铁律合规）

- 样本：从 278 份 OCR-fallback PDF（占全部 PDF 44%）选 3 份代表：
  `scan_rd_training`（纯图扫描，pypdf+ocr 路径）/ `scan_fin_invoice`（原生+截图混排）/
  `scan_mkt_bizdev`（原生正文页 + 附件表单整页图）。
- GT：raw-first 人工视觉转写（非 stub 校对），5 页；`--real` 走真管线真 qwen-vl-ocr。
- GT 位于 data repo `eval_samples/fidelity_gt/`（含 scanned_docs/ 源文件），不进本仓库。

## 证据

| 度量对象 | char_f1 | cer | 解读 |
|---|---|---|---|
| **纯 OCR 输出 vs 人工转写**（doc1 整页扫描） | **0.9923** | 0.0077 | 引擎在天花板；残差≈全半角标点 |
| 表单整页图（doc3-p2，TEDS 目标场景） | 0.9717 | 0.054 | 表单文字近乎全对 |
| 原生混排正文页（doc3-p1） | 0.998 | 0.004 | 原生路径本就不需要 tier-3 |
| **管线集成后**（doc1，同一页） | 0.6628 | 1.0058 | ⚠️ 集成 bug 拉爆（见下） |

## G6 顺带抓获的两个真 bug（预算的正确去向）

1. **纯扫描 PDF 双重 OCR 重复**（NEW）：整页扫描 → ①pypdf 0 字符触发 per-page OCR 兜底
   转写一次 ②同一页作为嵌入图片被 image funnel `ROUTE_TO_TEXT` 又 OCR 一次 → canonical
   内容翻倍。**生产已中招**：`DOC_RD_20260611201420_6B37DC`（1 页 ~600 字）生产索引
   11 chunk/1501 字，"建立和完善人才培训机制"×2、"第七条"×4。波及≈88 份 pypdf+ocr 文档
   （190 份 pdfplumber+ocr 中低文本页也可能）。修复方向：page-level OCR 兜底已触发的页，
   funnel 对该页主内容图跳过 ROUTE_TO_TEXT（或产物做近重复合并）。
2. **OCR 路径无页眉裁剪**（G12 的 OCR 侧空位）：页眉表（公司名/文件编号/版本/生效日期）
   被整页 OCR 逐字转进 canonical（doc2-p2 实测：页眉字符 ≈ 5 倍于正文）。原生 pdfplumber
   路径有页眉处理，OCR 兜底路径没有。

## 后续

- 双重 OCR 修复后：把 `scan_rd_training` 从 degraded 提升为 hard（预期 ~0.99，
  永久守卫该修复）；受影响扫描件纳入重灌清单（本就要为 B2-1/docx 表重灌）。
- TEDS 表格结构评测：当前语料的表单图以标签为主、字符保真已 0.97，结构化收益弱——
  随 tier-3 一并搁置；若未来出现数据密集型表格语料再议。
