# -*- coding: utf-8 -*-
"""
normalize.py — 别名归一 normalize(namespace, raw) -> norm_value（确定性纯函数，可穷举单测）。

norm_value 进 ontology_identifier 的 uk_ns_norm_active 唯一键，是"同一编号只有一个现行
指向"的物理承载——归一规则即身份判定的第一道闸，三条铁律（可行性红队 critical）：

1. **任何命名空间都不剥改模后缀**：'ABC123' 与 'ABC123-M' 必须归一为不同值——剥了就在
   唯一键上把基础品与改模变体撞成一行（事实合并）。后缀剥离只在候选生成阶段作 rule
   提示（resolve.py），绝不进归一键。
2. **customer:* 保留中文与括号**：'A(高钙)' 与 'A(低钙)' 的括号内容承载区分语义，只统一
   括号全半角；大小写也保留（客户命名的大小写语义归 steward 判断，不由归一抹平）。
3. **lab_sample 保内容原样**：'黑色注塑叉子' 这类描述名只去空白；材质词（PP/PS/PET）
   特征提取在候选阶段做，不进归一键。

分派键 = namespace 冒号前缀（'customer:KFC' → 'customer'）——业务 scope 唯一性由
namespace 自身编码（外评 S4），归一函数不感知具体客户/供应商。未登记命名空间走保守
默认（去首尾空白 + 全角→半角，不改大小写），宁可少归并、不可误合并。
"""
from __future__ import annotations

__all__ = ["normalize", "dispatch_key"]


def _to_halfwidth(s: str) -> str:
    """全角→半角：U+FF01–FF5E → ASCII（含全角括号/连字符/字母数字），全角空格 U+3000 → 空格。
    刻意不用 NFKC——其附带的兼容分解（㎡→m2 等）超出"全半角统一"的承诺面，行为不可穷举。"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def dispatch_key(namespace: str) -> str:
    """'customer:KFC' → 'customer'；'u8' → 'u8'。"""
    if not namespace or not namespace.strip():
        raise ValueError("namespace 不能为空")
    return namespace.split(":", 1)[0].strip().lower()


def normalize(namespace: str, raw: str) -> str:
    """按命名空间分派的确定性归一。空值/纯空白直接 ValueError——空 norm 进唯一键是事故。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"raw 不能为空（namespace={namespace!r}）")
    key = dispatch_key(namespace)
    s = _to_halfwidth(raw).strip()

    if key in ("u8", "internal", "lot_code"):
        # 去首尾空白、全半角统一、大写化；保留全部后缀（铁律 1）
        return s.upper()
    if key in ("supplier", "material_grade"):
        # 牌号/料号内部空白皆噪声：'pp 1100' 与 'pp1100' 同物
        return "".join(s.split()).upper()
    if key == "customer":
        # 铁律 2：保中文/括号/大小写，仅全半角统一 + 去首尾空白
        return s
    if key == "lab_sample":
        # 铁律 3：去全部空白，内容原样（不改大小写；中文自然保留）
        return "".join(s.split())
    # 未登记命名空间（hr/dingtalk_uid/us_wh/…）：保守默认，不改大小写
    return s


def title_key(title: str) -> str:
    """标题聚类键（C2 复核批次6，唯一权威实现）：借 lab_sample 归一——去全部空白 +
    全半角统一，内容/大小写原样。store 落 `ontology_object.normalized_title`（038）、
    seeding 同名聚类召回、backfill 脚本三处共用；改这里=改聚类语义，须重跑 backfill。"""
    return normalize("lab_sample", title)
