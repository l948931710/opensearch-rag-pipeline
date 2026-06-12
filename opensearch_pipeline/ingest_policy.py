# -*- coding: utf-8 -*-
"""
ingest_policy.py — raw/ 摄取准入策略（注册扫描与 stage-1 共用的单一来源）

依据 2026-06-10 对 OSS raw/ 全量盘点（3644 个对象）制定：
  - `_archive/` 是历史归档（438 个 .doc 原件等），活跃语料的 .docx 转换件已在部门目录；
  - `~$xxx.doc` 是 Office 编辑临时文件、`Thumbs.db` 是 Windows 缩略图缓存 ——
    两类垃圾已被注册进 document_meta（语料清理工单里的 2 条 ~$ 行即源于此）；
  - mp4/压缩包等非文档格式按用户决策不进知识库。

注意：本策略只管"扫描时是否纳入"。已注册的存量垃圾行属于语料清理
（docs/corpus_cleanup_worklist.md），不在此处处理。

2026-06-11 新增注册侧同名防重（raw_key_stem / stem_twin_action）：同一文档以
同名异扩展（.pdf/.docx 双上传）或换路径重传再注册会产生孪生 doc_id（实例：
FL-ZS-WI-005《注塑收货报检》双注册、A1员工行为管理标准 被注册 4 次）。
策略（用户拍板）：同部门同 stem 已有 active 注册 → 跳过注册 + 告警日志；
跨部门同 stem → 仅告警不拦截（拦截与否是 ACL 归属问题，防重不替它做决定）。
"""

import os
from typing import Tuple

# 策略版本（register_new_files.py 的 INGEST_POLICY_REV 必须与此一致，parity test 钉死）
INGEST_POLICY_REV = "2026-06-11"

# 编辑器/系统临时文件
IGNORED_BASENAME_PREFIXES: Tuple[str, ...] = ("~$", ".~")
IGNORED_BASENAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

# 非文档格式：视频/音频/压缩包/系统文件（用户决策：mp4 与压缩包不进知识库）
IGNORED_EXTS = {
    "db", "tmp", "lnk", "exe", "dll",
    "mp4", "avi", "mov", "wmv", "mp3", "wav",
    "zip", "rar", "7z", "tar", "gz",
}

# 遗留格式不在管线内支持（用户决策 2026-06-10）：.doc/.xls/.ppt 走一次性转换
# （docx/xlsx/pptx）后回灌 —— 活跃语料的转换件大多已存在；注册了也产不出 chunk。
# 与 stage-1 扫描 SQL 的排除清单保持一致。
UNSUPPORTED_LEGACY_EXTS = {"doc", "xls", "ppt"}

# 路径级排除：隔离区与历史归档（兼裸前缀，防止 key 不以 raw/ 开头的复用场景漏判）
EXCLUDED_PATH_SEGMENTS = ("/_quarantine/", "/_archive/")
EXCLUDED_PATH_PREFIXES = ("raw/_quarantine/", "raw/_archive/", "_quarantine/", "_archive/")

# 准入白名单 = unified_extractor 实际能处理的扩展名（contract test 钉死两表一致）。
# 纯黑名单会让未知格式（.wps/.et/.bak…）被注册后以 unsupported 空文档静默走完
# 生命周期 —— 正是 0-chunk 不可见文档问题的复发路径。
INGESTABLE_EXTS = {
    "pdf", "docx", "xlsx", "pptx",
    "txt", "md", "csv", "html", "htm",
    "png", "jpg", "jpeg", "webp", "tif", "tiff", "gif", "bmp",
}

# stage-1 扫描/排空计数 SQL 的扩展名排除清单（遗留格式 + 存量注册的垃圾行）。
# ⚠️ node_scan_raw_files 的认领 SQL 与 dataworks_orchestrator._count_pending_rows
# 的 stage-1 计数 SQL 必须用同一份 —— 两者不一致时，计数器看得到、认领挑不走，
# run_stage_drained 的"无进展"守卫会把 stage-1 永久判死（对抗评审 2026-06-10 证实）。
STAGE1_SQL_EXCLUDED_EXTS = ("doc", "xls", "ppt", "db", "mp4", "tmp", "zip", "rar")


def stage1_ext_exclusion_sql() -> str:
    """渲染 stage-1 SQL 的 NOT IN 片段（常量受信，无注入面）。"""
    return "(" + ", ".join(f"'{e}'" for e in STAGE1_SQL_EXCLUDED_EXTS) + ")"


def should_ingest_raw_key(key: str) -> Tuple[bool, str]:
    """raw/ 对象是否应纳入摄取。

    Returns:
        (True, "") 应纳入；(False, reason) 跳过原因（用于扫描日志统计）。
    """
    if not key or key.endswith("/"):
        return False, "directory"
    for seg in EXCLUDED_PATH_SEGMENTS:
        if seg in key:
            return False, f"excluded path ({seg.strip('/')})"
    for prefix in EXCLUDED_PATH_PREFIXES:
        if key.startswith(prefix):
            return False, f"excluded path ({prefix})"

    basename = os.path.basename(key)
    lower_base = basename.lower()
    if any(lower_base.startswith(p) for p in IGNORED_BASENAME_PREFIXES):
        return False, "temp file (~$)"
    if lower_base in IGNORED_BASENAMES:
        return False, f"junk file ({basename})"

    ext = os.path.splitext(basename)[1].lower().lstrip(".")
    if not ext:
        return False, "no extension"
    if ext in IGNORED_EXTS:
        return False, f"ignored ext (.{ext})"
    if ext in UNSUPPORTED_LEGACY_EXTS:
        return False, f"unsupported legacy ext (.{ext})"
    if ext not in INGESTABLE_EXTS:
        return False, f"unknown ext (.{ext})"
    return True, ""


# ⚠️ 下面两个函数在 dataworks_nodes/register_new_files.py 的内联 fallback 区有
# 逐字符一致的副本（PyODPS 节点运行时无本包）——修改时两处同步，parity test 对拍。

def raw_key_stem(key: str) -> str:
    """basename 去掉最后一层扩展名（只去一层："a.b.docx" → "a.b"；无扩展名原样返回）。"""
    base = os.path.basename(key or "")
    return os.path.splitext(base)[0].strip()


def stem_twin_action(dept: str, stem: str, existing: dict) -> tuple:
    """同名（同 stem）注册防重裁决。existing 形如 {stem: set(已注册部门小写)}。

    Returns:
        ("skip", reason)  同部门（大小写不敏感）已有 active 同名注册 → 跳过（防孪生 doc_id）；
        ("warn", reason)  仅异部门已有同名 → 告警不拦截（归属是 ACL 问题，防重不替它决定）；
        ("ok", "")        无同名注册。reason 为中文，列出已注册部门。
    """
    if not stem:
        return "ok", ""
    depts = existing.get(stem) or set()
    if not depts:
        return "ok", ""
    dept_l = (dept or "").strip().lower()
    depts_l = sorted({str(d).strip().lower() for d in depts})
    listed = ", ".join(depts_l)
    if dept_l in depts_l:
        return "skip", f"已有: {listed}"
    return "warn", f"已有: {listed}"
