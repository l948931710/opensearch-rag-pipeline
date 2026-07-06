# -*- coding: utf-8 -*-
"""text_normalize.py — canonical 文本版本化归一（G20，纯 Python 零依赖）。

背景（工业级审计 G20）：canonicalization 此前直接哈希并持久化原始抽取文本——
金集中实际存在全角 token（ＦＣＡ００７３、５秒钟）已进入 embedding/BM25，与用户
半角查询失配；OCR 混排还会引入零宽字符与连片空行。工业管线（Docling/
Unstructured/deepdoc）均在切块前归一。

保守归一三步（刻意不做全量 NFKC——NFKC 会把 ㈠/㈜/① 类枚举符拆解，毁掉
圈号标注绑定与条款编号）：
  1. 去零宽字符（ZWSP/ZWNJ/ZWJ/WJ/BOM）；
  2. 全角【字母/数字】→ 半角（ＦＦ１０-１９/２１-３Ａ/４１-５Ａ 区间）+ 全角空格
     → 半角空格。全角标点（，。（）：）一律不动——中文排版语义，转了伤可读性；
  3. 折叠 3+ 连续换行为 2（连片空行来自页眉裁剪/OCR 空页）。

版本纪律：canonical JSON 落 normalization_version 标记；改动归一规则必须 bump
NORMALIZATION_VERSION——canonical_sha256 建立在归一后文本上，规则漂移若无版本
标记，skip-unchanged/跨文档去重会把"同文档不同规则"误判为内容变更。
开关：RAG_TEXT_NORMALIZE（默认 ON；置 false 回到原始文本直通）。
"""

import os
import re

NORMALIZATION_VERSION = "norm-v1"

# 零宽字符：ZWSP / ZWNJ / ZWJ / Word-Joiner / BOM(FEFF 出现在文中时)
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")
_BLANK_RUN_RE = re.compile(r"\n[ \t　]*\n(?:[ \t　]*\n)+")

# 全角→半角映射：仅字母/数字/空格（标点刻意排除，见模块 docstring）
_WIDTH_MAP = {}
for _cp in range(0xFF10, 0xFF1A):          # ０-９
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
for _cp in range(0xFF21, 0xFF3B):          # Ａ-Ｚ
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
for _cp in range(0xFF41, 0xFF5B):          # ａ-ｚ
    _WIDTH_MAP[_cp] = _cp - 0xFEE0
_WIDTH_MAP[0x3000] = 0x20                  # 全角空格


def normalization_enabled() -> bool:
    return os.environ.get("RAG_TEXT_NORMALIZE", "true").lower() not in ("0", "false", "no")


def normalize_text(text: str) -> str:
    """保守归一（幂等）：去零宽 → 全角字母数字折半角 → 折叠连片空行。"""
    if not text:
        return text
    out = _ZERO_WIDTH_RE.sub("", text)
    out = out.translate(_WIDTH_MAP)
    out = _BLANK_RUN_RE.sub("\n\n", out)
    return out
