# -*- coding: utf-8 -*-
"""eval_harness/fidelity — G6 抽取保真金集（raw extraction fidelity）。

与既有评测的分工（互补，不替代）：
  · goldset (golden_full*)     — 端到端：query → 检索 → 生成 → 判官。隔着检索+LLM
                                 看抽取，局部乱码/串行/丢字对它可能不可见。
  · binding (L4 Jaccard)       — 图→chunk 绑定精度。
  · eval_extraction_coverage   — 结构不变量（"能产出 chunk 吗"），自述不测质量。
  · fidelity (本目录)          — 抽取产物 vs 人工参考转写的【字符/单元格级】保真：
                                 char-F1 / CER / 表格 cell-F1。parser/OCR 模型任何
                                 换版的第一道回归网（tier-3 全页 VLM 解析的度量前提）。

纯 Python 打分、零 API（默认 simulate 抽取；扫描件标 requires_real_ocr 用 --real）。
GT 文件含全文转写 → 放本地 eval_samples/fidelity_gt/（与 binding GT 同规矩，不进仓库）。
"""
