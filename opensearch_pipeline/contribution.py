# -*- coding: utf-8 -*-
"""
contribution.py — 员工知识贡献的【纯函数】层（无 DB / 无 OSS 副作用，便于单测）。

职责：状态词表、ID 生成、问题归一化 + hash（缺口去重对齐）、采纳后合成 .md 文档正文、
缺口提问 query_text 的员工端 PII 脱敏。所有【DB 状态机 + OSS 物化 + 登记】留在 api.py
（依赖 _get_db_conn/_kb_db/_op_db 等服务端 helper），本模块只做可纯测的转换。

⚠️ 合成 .md 正文【绝不含提交人姓名】（会进 embedding/检索→泄漏）；审计（author）只留在
   kb_contribution 表。见 schema/010 头注与方案 v2「正文洁净」。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger(__name__)

# ── 状态词表（与 schema/010 一致；前端徽章据此分流）─────────────────────────
REVIEW_PENDING = "pending"
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"

INGEST_NONE = "none"
INGEST_REGISTERING = "registering"
INGEST_REGISTERED = "registered"
INGEST_SEARCHABLE = "searchable"
INGEST_FAILED = "failed"

# document_version.index_status 中「已成功索引」的取值（生产实际写 SUCCESS；历史/schema 注释另有
# INDEXED）。reconcile 据此把 registered→searchable。见 [[h5-dept-upload-app]] 徽章词表坑。
INDEX_OK_STATUSES = ("SUCCESS", "INDEXED")
INDEX_FAIL_STATUSES = ("INDEXING_ERROR", "ERROR", "FAILED")

# 文本长度护栏（防刷 / 防超长；与 schema VARCHAR(512) 对齐）。
MAX_QUESTION_LEN = 500
MAX_CONTENT_LEN = 20000


def new_contribution_id() -> str:
    """贡献 ID = CONTRIB_<ULID>（复用 kb_upload 的 ULID：时间可排序、无碰撞）。"""
    from opensearch_pipeline.kb_upload import new_ulid

    return "CONTRIB_" + new_ulid()


def normalize_question(s: Optional[str]) -> str:
    """问题归一化（缺口去重 / 贡献匹配的单一口径）。

    NFKC（全角→半角、兼容字符规整）→ 去首尾空白 → 小写 → 去掉所有空白与标点（仅留
    字母数字 + 下划线 + CJK，`\\W` 在 Python str 下含 CJK 为 word 字符故保留）。
    使「如何 申请 密钥?」「如何申请密钥」「如何申请密钥！」归一为同一串。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    # \W 含空白与标点；Python str 的 \w 含 CJK，故中文/字母数字/下划线保留，其余剥离。
    return re.sub(r"\W+", "", s, flags=re.UNICODE)


def question_hash(s: Optional[str]) -> str:
    """sha256(normalize_question(s))。空问题→空串的 hash（稳定）。"""
    return hashlib.sha256(normalize_question(s).encode("utf-8")).hexdigest()


def synthesize_markdown(question: Optional[str], content: Optional[str]) -> str:
    """把采纳的问答合成为可检索 .md 正文。

    正文 = 「# 问题」+「答案」，【不含提交人姓名/审计信息】（那些只留 kb_contribution 表，
    doc_id↔contribution_id 即溯源链）。不加 YAML front matter——避免 .md 提取器把它当正文索引。
    """
    q = (question or "").strip()
    c = (content or "").strip()
    return f"# {q}\n\n{c}\n"


def contribution_state(review_status: Optional[str], ingestion_status: Optional[str]) -> str:
    """把两条生命周期折叠成一个【前端徽章码】（4 态 + 驳回）。

    pending     待审核
    rejected    已驳回
    registering 已采纳·待入库（registering/registered/none 但已 accepted）
    searchable  已入库
    failed      入库失败（可重试）
    """
    rs = (review_status or REVIEW_PENDING).strip().lower()
    if rs == REVIEW_REJECTED:
        return "rejected"
    if rs == REVIEW_PENDING:
        return "pending"
    # accepted：看物化进度
    ing = (ingestion_status or INGEST_NONE).strip().lower()
    if ing == INGEST_SEARCHABLE:
        return "searchable"
    if ing == INGEST_FAILED:
        return "failed"
    # none / registering / registered → 已采纳·待入库
    return "registering"


def redact_query_text(text: Optional[str]) -> str:
    """员工端展示缺口 query_text 前的【不可逆 PII 脱敏】。

    复用入库侧 redaction.redact_text（纯本地正则，无 LLM/网络）：身份证/手机号/邮箱/银行卡/
    地址/密钥及标注式姓名→占位符。⚠️ 与 qa_logger._redact_for_log 不同——此处面向【跨用户展示】，
    故【无条件】脱敏（不受 RAG_QA_LOG_PII_REDACT 开关影响），且脱敏失败时返回安全占位符
    「[内容已隐藏]」而非原文（绝不向员工泄露未脱敏的他人提问）。
    """
    if not text:
        return ""
    try:
        from opensearch_pipeline.redaction import redact_text

        masked, _counts = redact_text(text)
        return masked
    except Exception as e:  # pragma: no cover - 纯本地正则极少失败
        logger.warning("缺口 query_text 脱敏失败，回退安全占位符 (non-fatal): %s", e)
        return "[内容已隐藏]"


def validate_contribution_text(question: Optional[str], content: Optional[str]) -> Optional[str]:
    """校验提交内容；通过→None，否则→中文错误原因（端点转 400）。"""
    q = (question or "").strip()
    c = (content or "").strip()
    if not q:
        return "问题不能为空"
    if not c:
        return "答案/知识内容不能为空"
    if len(q) > MAX_QUESTION_LEN:
        return f"问题过长（上限 {MAX_QUESTION_LEN} 字）"
    if len(c) > MAX_CONTENT_LEN:
        return f"内容过长（上限 {MAX_CONTENT_LEN} 字）"
    return None
