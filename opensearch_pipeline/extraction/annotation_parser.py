# -*- coding: utf-8 -*-
"""
annotation_parser.py — SOP 标注解析器

解析文档步骤文本与 OCR 文本中的标注标记（①②③ / 圈数字 / 序号），
将标注映射转化为自然语言描述，并清洗 OCR 文本为 BM25 可用的关键词。

典型场景：
  - 步骤文本中出现 "图中①为业务导航，②为供应链" → 提取 {①: 业务导航, ②: 供应链}
  - OCR 识别的截图文字含 "①业务导航 ②供应链" → 同上
  - 步骤文本中出现 "如下图①-⑤步骤" + OCR 有编号项 → 合并两侧信号

依赖：仅标准库 re
"""

import re
from typing import Dict, List

# ────────────────────────────────────────────────────────────────
# 圈数字常量
# ────────────────────────────────────────────────────────────────

# 支持的圈数字字符（①-⑩）
CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩"
# 圈数字 → 整数映射
CIRCLED_TO_INT: Dict[str, int] = {ch: i + 1 for i, ch in enumerate(CIRCLED_DIGITS)}

# ────────────────────────────────────────────────────────────────
# 正则模式
# ────────────────────────────────────────────────────────────────

# 匹配单个圈数字后紧跟描述：①业务导航  /  ②供应链管理
#   - 圈数字后可跟可选的 "为" / "是" / ":" / "：" / 空格
#   - 描述部分：至少 1 个非空白非标点字符开头，延伸到下一个圈数字或行尾
_CIRCLED_ITEM_RE = re.compile(
    r"([" + CIRCLED_DIGITS + r"])"             # group(1): 圈数字
    r"[为是:：\s]*"                             # 可选连接词
    r"([\u4e00-\u9fffA-Za-z0-9]"               # group(2): 描述首字符（中文/英文/数字）
    r"[^①②③④⑤⑥⑦⑧⑨⑩]*?)"                    # 描述后续（非贪婪，到下一个圈数字前停止）
    r"(?=[①②③④⑤⑥⑦⑧⑨⑩,，;；。\n]|$)"         # 前瞻：下一个圈数字或分隔符或行尾
)

# "图中①为XX" 格式：明确的 "图中" / "如图" 前缀 + 圈数字 + "为/是" + 描述
_EXPLICIT_LABEL_RE = re.compile(
    r"(?:图中|如图|图片中|截图中)"               # 前缀
    r"([" + CIRCLED_DIGITS + r"])"             # group(1): 圈数字
    r"[为是:：]"                                # 连接词
    r"([\u4e00-\u9fffA-Za-z0-9]"               # group(2): 描述
    r"[^①②③④⑤⑥⑦⑧⑨⑩,，;；。\n]*?)"
    r"(?=[①②③④⑤⑥⑦⑧⑨⑩,，;；。\n]|$)"
)

# 步骤文本中的范围引用：如下图①-⑤步骤 / ①~③
_RANGE_REF_RE = re.compile(
    r"[如下图中截]*"
    r"([" + CIRCLED_DIGITS + r"])"             # group(1): 起始圈数字
    r"[-—~～至到]"
    r"([" + CIRCLED_DIGITS + r"])"             # group(2): 结束圈数字
)

# 上下文引用：（如下图①）/ （如图②）/ （如上图⑦）
# 从前后文提取上下文描述
_CONTEXT_REF_RE = re.compile(
    r"([^，。；;\n]{2,20}?)"                    # group(1): 前文上下文
    r"[（(]?如[下上]?图"
    r"([" + CIRCLED_DIGITS + r"])"             # group(2): 圈数字
    r"[）)]?"
)

# OCR 中的阿拉伯数字编号项：1.业务导航 / 1、业务导航 / 1)业务导航 / (1)业务导航
_ARABIC_NUM_ITEM_RE = re.compile(
    r"(?:^|\n)\s*"
    r"(?:\()?(\d{1,2})(?:\))?"                 # group(1): 数字
    r"[.、)）:：\s]+"
    r"([\u4e00-\u9fffA-Za-z]"                  # group(2): 描述
    r"[^\n]*?)"
    r"(?=\n|$)"
)


# ────────────────────────────────────────────────────────────────
# 公共函数
# ────────────────────────────────────────────────────────────────

def parse_annotation_map(step_text: str, ocr_text: str) -> Dict[str, str]:
    """
    从步骤文本和 OCR 文本中解析标注映射。

    解析策略（按优先级）：
      1. 在 step_text 和 ocr_text 中查找 "图中①为XX" 格式的显式标注
      2. 在两段文本中查找 "①XX ②YY" 格式的圈数字项
      3. 若 step_text 包含圈数字范围引用（如 ①-⑤），且 OCR 含阿拉伯编号项，
         则将阿拉伯编号项映射为对应圈数字

    Args:
        step_text: 步骤文本（文档正文中描述操作步骤的文字）
        ocr_text:  OCR 识别文本（截图中识别出的文字）

    Returns:
        标注映射 {圈数字: 描述}，如 {"①": "业务导航", "②": "供应链"}。
        未找到标注时返回空 dict。
    """
    result: Dict[str, str] = {}

    # 预扫描：收集 step_text 中范围引用涉及的圈数字（如 ①-③ 中的 ①②③），
    # 这些圈数字不应在阶段 2 中被当作独立标注项提取。
    range_digits: set = set()
    if step_text:
        for rm in _RANGE_REF_RE.finditer(step_text):
            s = CIRCLED_TO_INT.get(rm.group(1), 0)
            e = CIRCLED_TO_INT.get(rm.group(2), 0)
            if s and e and s <= e:
                for i in range(s, e + 1):
                    if 1 <= i <= len(CIRCLED_DIGITS):
                        range_digits.add(CIRCLED_DIGITS[i - 1])

    # ── 阶段 1：显式 "图中①为XX" 标注（优先级最高） ──
    for source in (step_text, ocr_text):
        if not source:
            continue
        for m in _EXPLICIT_LABEL_RE.finditer(source):
            key = m.group(1)
            val = m.group(2).strip().rstrip("，,。.；;、")
            if key not in result and val:
                result[key] = val

    # ── 阶段 2：圈数字项 "①XX ②YY" ──
    for source in (step_text, ocr_text):
        if not source:
            continue
        for m in _CIRCLED_ITEM_RE.finditer(source):
            key = m.group(1)
            # 跳过范围引用中的圈数字（由阶段 3 处理）
            if key in range_digits and source is step_text:
                continue
            val = m.group(2).strip().rstrip("，,。.；;、")
            if key not in result and val:
                result[key] = val

    # ── 阶段 2.5：上下文引用 "如下图①" → 从前文推断描述 ──
    if step_text:
        for m in _CONTEXT_REF_RE.finditer(step_text):
            ctx = m.group(1).strip().lstrip("，,;；、")
            key = m.group(2)
            # 清理步骤前缀（如"步骤2："、"步骤三："）和前导"的"
            ctx = re.sub(r'^步骤\s*[\d一二三四五六七八九十]+\s*[：:]\s*', '', ctx)
            ctx = ctx.lstrip("的")
            if key not in result and ctx and len(ctx) >= 2:
                result[key] = ctx

    # ── 阶段 3：范围引用 + 阿拉伯编号回退 ──
    if not result:
        result = _try_range_with_arabic_fallback(step_text, ocr_text)

    return result


def expand_annotation_map(annotation_map: Dict[str, str]) -> str:
    """
    将标注映射转化为自然语言描述。

    Args:
        annotation_map: 标注映射，如 {"①": "业务导航", "②": "供应链"}

    Returns:
        自然语言字符串，如 "图中①为业务导航，②为供应链。"。
        映射为空时返回空字符串。
    """
    if not annotation_map:
        return ""

    # 按圈数字数值排序
    sorted_items = sorted(
        annotation_map.items(),
        key=lambda kv: CIRCLED_TO_INT.get(kv[0], 99),
    )

    parts: List[str] = []
    for key, val in sorted_items:
        parts.append(f"{key}为{val}")

    return "图中" + "，".join(parts) + "。"


def extract_circled_refs(step_text: str) -> List[str]:
    """
    提取步骤文本中引用的所有圈数字（不含描述映射，仅列出引用了哪些）。

    用途：即使无法建立 annotation_map，也能在 step_card.extra 中
    记录该步骤引用了 ①②③ 等标注，便于检索层匹配。

    Args:
        step_text: 步骤文本

    Returns:
        去重排序的圈数字列表，如 ["①", "②", "⑤"]
    """
    if not step_text:
        return []
    found = set()
    for ch in step_text:
        if ch in CIRCLED_TO_INT:
            found.add(ch)
    return sorted(found, key=lambda c: CIRCLED_TO_INT.get(c, 99))


def clean_ocr_keywords(ocr_text: str, min_keyword_len: int = 2) -> str:
    """
    清洗 OCR 文本，提取适合 BM25 索引的关键词。

    清洗规则：
      - 去除纯标点行和单字符 token
      - 合并连续空白为单个空格
      - 关键词去重（保持首次出现顺序）
      - 过滤长度小于 min_keyword_len 的 token

    Args:
        ocr_text:        原始 OCR 识别文本
        min_keyword_len: 最小关键词长度，默认 2

    Returns:
        清洗后的关键词字符串，用空格连接，适合追加到 chunk_text。
        输入为空时返回空字符串。
    """
    if not ocr_text or not ocr_text.strip():
        return ""

    # 按行处理
    lines = ocr_text.split("\n")
    tokens: List[str] = []
    seen: set = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 跳过纯标点行
        if _is_pure_punctuation(line):
            continue

        # 将行拆分为 token（按空白和常见分隔符）
        parts = re.split(r"[\s,，;；|｜/／\\\\]+", line)
        for part in parts:
            # 去除首尾标点
            cleaned = part.strip("。.!！?？、,，;；:：()（）[]【】{}「」《》<>""''\"'")
            if not cleaned:
                continue
            if len(cleaned) < min_keyword_len:
                continue
            if _is_pure_punctuation(cleaned):
                continue

            # 去重
            lower_key = cleaned.lower()
            if lower_key not in seen:
                seen.add(lower_key)
                tokens.append(cleaned)

    return " ".join(tokens)


# ────────────────────────────────────────────────────────────────
# 内部辅助函数
# ────────────────────────────────────────────────────────────────

# 纯标点检测正则
_PUNCT_ONLY_RE = re.compile(
    r"^[\s\W\d"
    r"。.!！?？、,，;；:：()（）\[\]【】{}「」《》<>""''\"'\-—~～·…●○■□▲△☆★※→←↑↓"
    r"]+$"
)


def _is_pure_punctuation(text: str) -> bool:
    """判断文本是否仅包含标点、空白和数字（无实际语义内容）。"""
    return bool(_PUNCT_ONLY_RE.match(text))


def _try_range_with_arabic_fallback(
    step_text: str, ocr_text: str
) -> Dict[str, str]:
    """
    处理范围引用场景：step_text 含 "①-⑤" 而 OCR 含阿拉伯编号项。

    当步骤文本只给出范围引用（如 "如下图①-⑤步骤"）而未给出具体描述时，
    尝试从 OCR 文本中的阿拉伯编号列表（如 "1.提交申请 2.审批通过"）
    推断每个圈数字的含义。
    """
    if not step_text or not ocr_text:
        return {}

    # 查找范围引用
    range_match = _RANGE_REF_RE.search(step_text)
    if not range_match:
        return {}

    start_idx = CIRCLED_TO_INT.get(range_match.group(1), 0)
    end_idx = CIRCLED_TO_INT.get(range_match.group(2), 0)
    if start_idx == 0 or end_idx == 0 or start_idx > end_idx:
        return {}

    # 从 OCR 中提取阿拉伯编号项
    arabic_items: Dict[int, str] = {}
    for m in _ARABIC_NUM_ITEM_RE.finditer("\n" + ocr_text):
        num = int(m.group(1))
        desc = m.group(2).strip().rstrip("，,。.；;、")
        if desc and start_idx <= num <= end_idx:
            arabic_items[num] = desc

    if not arabic_items:
        return {}

    # 将阿拉伯编号映射为圈数字
    result: Dict[str, str] = {}
    for num, desc in sorted(arabic_items.items()):
        if 1 <= num <= len(CIRCLED_DIGITS):
            circled = CIRCLED_DIGITS[num - 1]
            result[circled] = desc

    return result
