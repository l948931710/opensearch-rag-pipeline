# 隔离区（_quarantine）摄取就绪清单 — 等待权限系统设计完成后执行

生成: 2026-06-10 · 数据来源: OSS 全量盘点（read-only）+ RDS 校验 · 状态: **仅文档，未做任何处理**

## 语料语义（用户确认）

- `raw/`（排除 `_archive`/`_quarantine`）= 当前**公开**语料（serving 现用）
- `raw/**/_archive/` = 旧格式归档，**永不处理**（其 docx/xlsx 转换件已在公开语料）
- `raw/**/_quarantine/` = **权限管理设计完成后**待处理的文档（本清单的对象）

## 待处理量（除 mp4 与遗留格式）

总计 **2,996** 个隔离区对象，其中**可处理 2,742 个 / 约 3.4 GB**：

| 部门前缀 | 文件数 | 体积 | 主要构成 |
|---|---|---|---|
| marketing | 1,609 | 11.4 GB* | **1,394 张产品摄影 jpg**（DSC_*/LXM_* 相机文件、按产品类目分目录）+ 61 docx + 106 pdf + 10 pptx + 17 xlsx |
| production | 709 | 965 MB | 391 pdf + 267 xlsx + 45 docx（规格书/控制卡为主） |
| rd | 187 | 116 MB | 95 docx + 77 pdf + 零星图片 |
| pmc | 99 | 37 MB | 58 docx + 40 pdf |
| quality | 55 | 31 MB | 47 docx + 7 xlsx |
| finance | 46 | 11 MB | 29 docx + 17 pdf |
| supply | 38 | 9 MB | 26 docx + 11 pdf |
| admin / hr | 13 | 3 MB | xlsx/docx/pdf |

\* marketing 体积含 5 个 mp4（9.2 GB，排除）；可处理部分约 2.2 GB。

**排除项（维持现状）**: 5×mp4（用户决策不进知识库）、203×.doc + 37×.xls + 1×.ppt（遗留格式，
归档/转换路线）、Thumbs.db 等垃圾（ingest_policy 已拦截）。
`raw/_archive/**/_quarantine/` 嵌套 240 个全部为遗留格式 → 随归档不处理。

**关于 jpg×1404 的结论**（盘点核实）: 全部为**用户上传的产品摄影/参考图**（相机命名、
产品类目目录结构），与管线资产命名（`*_pN_imgNNNN`、`processing/assets/`）零匹配 ——
**不是管线倾倒的冗余图**，是有价值的待入库内容。

## 已就绪的基础设施

- RDS 零污染：`document_version` 中 **0 条** `_quarantine` 注册（干净起点）；
  `quarantine_key` 列已存在备用
- 权限钩子：`document_meta.owner_dept` + `permission_level` 列在用；
  `_dept_from_raw_key()` 路径启发已覆盖全部部门前缀；HA3 检索侧 `dept_internal`
  过滤 + 注入白名单已上线（见 retriever）
- 摄取准入：`ingest_policy.should_ingest_raw_key` 当前排除 `_quarantine/` ——
  权限系统就绪后**只需移除该路径排除**（或按部门白名单渐进放开），junk/遗留过滤继续生效
- 预检工具：`scripts/eval_extraction_coverage.py` 可对隔离区样本先跑结构不变量
  （建议放开前按部门抽样 12/类验证）

## 放开前必做（按依赖顺序）

1. **ACL 设计定稿**：dept→permission_level 映射、跨部门共享策略
   （现状全 public；隔离区文档默认应为 `dept_internal`）
2. **产品摄影专路**：1,394 张 marketing 产品图全部过三段漏斗 ≈ 一次性
   Qwen-VL 成本（约 1.5s/张、8 并发、MD5 缓存去重后实际更低）。建议为
   `image_category=product_photo` 类目录开**快速路由**（跳过 OCR 密度段，
   直接 VLM 描述 → visual_knowledge chunk），并按产品类目目录回填 category_l2
3. **分批放开**：先小部门（hr/admin/supply，~55 个文档）验证权限过滤端到端，
   再放 production/rd/pmc，最后 marketing（量最大）
4. 每批跑 `eval_extraction_coverage.py --ext ...` 预检 + 批后 L0/L1 验证
