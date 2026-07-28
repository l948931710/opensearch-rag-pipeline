# -*- coding: utf-8 -*-
"""ingest_flags.py — 摄取侧运行开关的**单一来源**（纯函数，零副作用）。

刻意做成独立轻量模块：`versions.build_run_provenance()` 需要把开关的**有效值**写进
provenance，而它自称 pure/read-only；若从 `pipeline_nodes`（~8000 行摄取核心）反向
import 一个 env parser，会把整个摄取模块拖进 provenance 链路。依赖方向固定为：

    pipeline_nodes → ingest_flags ← versions

新增开关请一并放这里，并同时提供"有效布尔值"给 provenance —— 存原始字符串会让
`""` / `"1"` / `"TRUE "` 在审计里长得不一样却含义相同。
"""
import os

# 图片内容覆写（Path A/B/C/D）的**关闭**值。默认 ON，故这里列的是 off-list：
# 默认 ON 的开关若把"未知值"也当关，配错一个字就会静默退回旧行为且无人察觉。
_IMAGE_CONTENT_OVERRIDE_OFF = ("0", "false", "no", "off")


def image_content_override_enabled() -> bool:
    """图片内容覆写总闸（`RAG_IMAGE_CONTENT_OVERRIDE`）。**2026-07-25 起默认 ON**。

    gate 住 `_insert_image_refs_heuristic` 的四条覆写路径：
      · Path A 内容匹配覆写（几何锚落到非步骤起始块时，按 caption/OCR 的 n-gram 改锚）
      · Path B 圈号集合 Jaccard 覆写
      · Path C 跨页图号区间引用覆写
      · Path D 同页相邻图的簇传播（seed 只来自 Path A 强覆写）

    翻默认的依据（L4-ing pdf 支柱，6 篇 GT / 48 strong 行，同缓存快照）：
    暖缓存 OFF 0.89236 → ON 0.96528；冷缓存（caption 全部重掷）OFF 0.84722 →
    ON 0.89931 —— **两种 caption 制度下增益都为正**，0 条退化，docx/xlsx/img_dup_p95
    两侧均不动，三轮 warm-ON 逐题签名 byte-identical。这也正是 D8 战役当年留的 chip
    （"扩 3 doc 多轮稳定后切默认 ON"，现已 6 篇）。

    回滚：显式设 `0/false/no/off`（大小写与首尾空白不敏感）。**注意回滚只影响之后的
    Stage2**；已入库 chunk 要真回滚需对受影响 doc 集做冻结分类的 OFF re-chunk +
    Stage3 重索引 + count/type_mix manifest 门。
    """
    return os.environ.get("RAG_IMAGE_CONTENT_OVERRIDE", "").strip().lower() \
        not in _IMAGE_CONTENT_OVERRIDE_OFF


# 选项 C 的**关闭**值。2026-07-26 起默认 ON，故这里列 off-list（同
# RAG_IMAGE_CONTENT_OVERRIDE）：默认 ON 的开关若把"未知值"也当关，配错一个字就会静默
# 退回旧判据、把步骤图重新弃掉且无人察觉。
_FUNNEL_DISCARD_REQUIRES_CATEGORY_OFF = ("0", "false", "no", "off")

# 漏斗策略标签。空串 = 历史判据，"c1" = 选项 C（**现默认**）。它进缓存键/判决记录主键/
# chunk provenance/L4 regime —— 改判据必须换标签，否则两套判据的判决会在同一命名空间里混住。
FUNNEL_POLICY_LEGACY = ""
FUNNEL_POLICY_C1 = "c1"


def funnel_discard_requires_category_enabled() -> bool:
    """Funnel 3 弃图需 VLM 明确表态（`RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY`）。
    **2026-07-26 起默认 ON**（Sam 拍板：先重写 GT，再翻默认值）。

    改的是 `LOW_RELEVANCE + 非 text_heavy` 这一条分支：今天直接 `DISCARD_DECORATIVE`，
    开启后只有当 VLM **自己把类别判成** `decorative`/`logo_header` 时才弃，其余（含
    `unknown`）降级保留为 `ROUTE_TO_VECTOR`。

    依据（2026-07-26 实测，`docs/evidence/funnel_c_category_probe_20260726/`，
    6 篇 GT PDF 过 Funnel 1 的 77 张逐张真调 VLM）：32 张判 `LOW_RELEVANCE`，其中
    **26 张 `image_category="unknown"`** —— 而它们的 caption 全部准确且实质（"一只手正
    在将多色线缆连接至电脑主板上的CPU散热器风扇接口"）。prompt 对 `LOW_RELEVANCE` 的
    定义只有「装饰图、封面Logo、页边留白、空白排版线条」，手在主板上操作一条都不沾：
    模型是在**拒绝归类**，不是在断言"这是装饰"。按 §3 的成本不对称（错弃 = 该步骤永久
    缺图且无信号），在一个**非断言**上弃图站不住。

    翻默认值的依据 —— **必须先解掉两层循环性，否则闸门方向是反的**（方案 §12-13）：

    1. PDF GT 原本按漏斗**之后**的 manifest 标注（`_note` 自陈"funnel 弃 page 11-13
       截图"）；独立重标 38/38 张（逐条引源 PDF 正文原句）后发现 **36 张是步骤配图**，
       原 GT 只记 11 张、对独立标注召回仅 30.6%；
    2. `scratch/eval_manifest/*.json` 的 preflight 也只装漏斗**存活**集，25 个新 GT ref
       落在被弃图上 ⇒ 整篇被判 degraded 踢出统计（两个 arm 分数一度逐位相同）。改为装
       漏斗**前**候选集后才可比。

    两层都解掉后的实测（同一 warm-cache 快照，pdf strong 行 48/48、0 degraded/0 error）：
    **OFF 0.84919 → ON 0.91320（+0.06401）**，其余 5 篇逐位不变，xlsx/img_dup 不动。
    对独立标注的召回 33.3% → 91.7%，绑定纯度 31/33，**零非内容图误留**。
    （同一套数据在旧循环 GT 下读出的是 −0.13345 —— 方向完全相反。）

    回滚：显式设 `0/false/no/off`。**注意回滚只影响之后的 Stage1**；已入库文档要真回滚
    需重新摄取。另注意翻默认值 = 缓存键默认带 `:c1` 后缀 ⇒ 下次摄取会对**曾被 Funnel 3
    弃**的图重判（跨策略条件回退只放行 C 不影响的条目），这是有意的有界重付。
    """
    return os.environ.get("RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY", "").strip().lower() \
        not in _FUNNEL_DISCARD_REQUIRES_CATEGORY_OFF


def effective_funnel_policy_version() -> str:
    """当前生效的漏斗策略标签（**单一来源**）。

    必须同时喂给：VLM 缓存键后缀、RDS 判决记录主键、chunk `_provenance`、L4 regime 键。
    少喂任何一处都会串线 —— 最典型的是缓存：`_process_embedded_images` 的命中判定发生在
    `process_image` **之前**（`unified_extractor.py:1992`），键里不带策略的话，开了 C 也
    会被旧的 `DISCARD` 条目直接短路，A/B 变成假 ON。
    """
    return FUNNEL_POLICY_C1 if funnel_discard_requires_category_enabled() else FUNNEL_POLICY_LEGACY


# PDF 条带缝合的**关闭**值。2026-07-26 起默认 ON，故列 off-list。
_PDF_STRIP_STITCH_OFF = ("0", "false", "no", "off")


def pdf_strip_stitch_enabled() -> bool:
    """PDF 条带切片缝合（`RAG_PDF_STRIP_STITCH`，**2026-07-26 起默认 ON**）。

    审查遗漏项（2026-07-26）：该开关此前**不在本模块**，其有效值也就进不了 provenance。
    后果：显式设 `RAG_PDF_STRIP_STITCH=0` 的 run 与默认 ON 的 run 产出的 chunk **版本戳
    完全相同**（`EXTRACTOR_VERSION=1.3.0` 只反映默认），事后无法归因。本模块 docstring
    早就写着「新增开关请一并放这里，并同时提供有效布尔值给 provenance」—— 同批三个默认
    翻转用了三种不同的处理方式，这条是补齐最后一个。
    """
    return os.environ.get("RAG_PDF_STRIP_STITCH", "").strip().lower() \
        not in _PDF_STRIP_STITCH_OFF
