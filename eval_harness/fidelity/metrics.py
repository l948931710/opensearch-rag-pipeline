# -*- coding: utf-8 -*-
"""fidelity/metrics.py — 保真度量纯函数（零 I/O，可离线单测）。

三个互补指标，合起来能区分四类抽取失败：

  cer(ref, hyp)         归一化编辑距离（字符错误率）。对【语序】敏感——
                        多栏交错（G3 类 bug）、跨页错拼会重罚 CER。
  char_f1(ref, hyp)     字符多重集 F1。对语序【不】敏感——只看内容有没有。
  两者合读：char_f1 高 + CER 高 = 内容都在但被打乱（阅读序 bug）；
            char_f1 低           = 内容真丢了（截断/跳块/OCR 漏）。
  table_scores(ref_grid, hyp_md)
                        表格 cell 多重集 F1（内容层）+ grid_exact（行列结构层）。
                        cell_f1 高 + grid_exact=False = 单元格都在但行列错位
                        （G1 空格丢失类 bug）。

比对前双方过同一保守归一（折叠空白 + 全角字母数字→半角，与
opensearch_pipeline.text_normalize 同口径）——分数不被 RAG_TEXT_NORMALIZE
开关本身干扰，度量的是内容保真而非空白风格。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List

# 与 text_normalize._WIDTH_MAP 同口径（此处独立内联：metrics 必须零依赖可单测，
# 且比对归一的口径一旦定下就不应随管线归一器演进而漂移——那会让历史分数不可比）
_WIDTH_MAP = {}
for _cp in range(0xFF10, 0xFF1A):
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
for _cp in range(0xFF21, 0xFF3B):
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
for _cp in range(0xFF41, 0xFF5B):
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
_WIDTH_MAP[0x3000] = 0x20

_MAX_CER_CHARS = 20000  # 单页转写远小于此；防御性上限保 O(n*m) DP 可控


def norm_for_compare(s: str) -> str:
    """比对归一：全角字母数字折半角 → 全部空白剔除（空白不参与保真判定）。"""
    s = (s or "").translate(_WIDTH_MAP)
    return "".join(s.split())


def cer(ref: str, hyp: str) -> float:
    """字符错误率 = Levenshtein(ref, hyp) / len(ref)，双方先过 norm_for_compare。

    ref 为空：hyp 也空 → 0.0；hyp 非空 → 1.0（全是插入噪声）。可 >1.0（截到 1.0
    没有信息量，保留原值供排查——hyp 比 ref 长很多说明混入了别页/页眉内容）。
    """
    r, h = norm_for_compare(ref), norm_for_compare(hyp)
    if not r:
        return 0.0 if not h else 1.0
    r, h = r[:_MAX_CER_CHARS], h[:_MAX_CER_CHARS]
    # 两行 DP
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1,                      # 删除
                         cur[j - 1] + 1,                   # 插入
                         prev[j - 1] + (rc != hc))         # 替换
        prev = cur
    return round(prev[-1] / len(r), 4)


def char_f1(ref: str, hyp: str) -> float:
    """字符多重集 F1（语序无关）：衡量"内容在不在"，与 CER 合读定位失败类型。"""
    r, h = Counter(norm_for_compare(ref)), Counter(norm_for_compare(hyp))
    if not r and not h:
        return 1.0
    if not r or not h:
        return 0.0
    overlap = sum((r & h).values())
    p = overlap / sum(h.values())
    rec = overlap / sum(r.values())
    return round(2 * p * rec / (p + rec), 4) if (p + rec) else 0.0


def parse_md_table(md: str) -> List[List[str]]:
    """markdown pipe 表 → 栅格（保留空单元格）。跳过 |---| 分隔行。"""
    grid: List[List[str]] = []
    for line in (md or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        parts = line.split("|")[1:-1]
        cells = [p.strip() for p in parts]
        if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c):
            if any(c for c in cells):  # 全 --- 的分隔行
                continue
        grid.append(cells)
    return grid


def table_scores(ref_grid: List[List[str]], hyp_md: str) -> Dict[str, object]:
    """表格保真：cell_f1（内容多重集）+ grid_exact（形状与逐格全等）。

    cell 归一后为空的格不计入内容集（占位空格是结构信息，由 grid_exact 负责）。
    """
    hyp_grid = parse_md_table(hyp_md)
    ref_cells = Counter(norm_for_compare(c) for row in ref_grid for c in row
                        if norm_for_compare(c))
    hyp_cells = Counter(norm_for_compare(c) for row in hyp_grid for c in row
                        if norm_for_compare(c))
    if not ref_cells and not hyp_cells:
        f1 = 1.0
    elif not ref_cells or not hyp_cells:
        f1 = 0.0
    else:
        overlap = sum((ref_cells & hyp_cells).values())
        p = overlap / sum(hyp_cells.values())
        rec = overlap / sum(ref_cells.values())
        f1 = round(2 * p * rec / (p + rec), 4) if (p + rec) else 0.0

    shape_ok = (len(hyp_grid) == len(ref_grid)
                and all(len(hr) == len(rr) for hr, rr in zip(hyp_grid, ref_grid)))
    grid_exact = bool(shape_ok and all(
        norm_for_compare(hc) == norm_for_compare(rc)
        for hr, rr in zip(hyp_grid, ref_grid) for hc, rc in zip(hr, rr)))
    return {"cell_f1": f1, "grid_exact": grid_exact,
            "ref_shape": [len(ref_grid), max((len(r) for r in ref_grid), default=0)],
            "hyp_shape": [len(hyp_grid), max((len(r) for r in hyp_grid), default=0)]}
