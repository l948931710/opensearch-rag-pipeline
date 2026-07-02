# -*- coding: utf-8 -*-
"""
pii_patterns.py — PII/敏感信息词表与正则（单一权威）

从 pipeline_nodes.py 机械搬移（F-A1 结构债拆分，2026-07-01）：serving 侧
（qa_logger → redaction）此前为了拿 ENTITY_PATTERNS 要 import 整个 7000+ 行
摄取模块（连带 chunker），现在两侧都从这里取。pipeline_nodes 仍 re-export
全部名字，既有导入点与 tests 的 monkeypatch 目标不受影响。

⚠️ 本表是入库脱敏（node_detect_sensitive / node_redact_or_quarantine）与
问答日志脱敏（redaction.redact_text）的共同事实来源——改动即同时影响两侧。
"""

SEMANTIC_KEYWORDS = {
    "pii": [
        "身份证", "身份证号", "手机号", "电话号码", "家庭住址",
        "银行卡", "银行卡号", "社保", "社保号", "护照",
        "邮箱地址", "紧急联系人", "出生日期", "员工编号",
        "薪资", "工资", "绩效", "花名册",
    ],
    "business": [
        "客户报价", "供应商价格", "研发配方", "合同机密",
        "银行流水", "财务报表", "利润表", "资产负债",
    ],
    "security": [
        "账号密码", "数据库密码", "服务器地址", "VPN",
        "AK/SK", "AccessKey", "SecretKey", "API密钥",
    ],
}

# ⚠️ 号码/密钥类实体用显式 lookaround 定界，不用 \b：Python 正则里 CJK 属 \w，\b 只在
# word/非word 交界成立，故「身份证号110101…」「密钥LTAI…」这类号码紧贴中文时 \b 不成立、
# 整体漏检（cn_mobile 早已改用 (?<!\d)…(?!\d) 规避）。lookaround 只断言前后不是同类字符，
# 与中文/空格/标点邻接均能命中，同时仍防止匹配更长数字/标识串的子串。redaction.py 直接
# 复用本表（_FULL_ID/_ACCESS_KEY = ENTITY_PATTERNS[...]），改这里即同步修复入库脱敏侧。
ENTITY_PATTERNS = {
    "cn_id_card": r"(?<![0-9Xx])[1-9]\d{5}(18|19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9Xx])",
    "cn_mobile": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "access_key": r"(?<![A-Za-z0-9])(LTAI|AKIA)[A-Za-z0-9]{12,}(?![A-Za-z0-9])",
    "secret_like": r"(?i)\b(secret|password|passwd|pwd|token|api[_-]?key)(\s*[:=]\s*)[A-Za-z0-9_\-]{8,}",
}

REDACTION_MAP = {
    "cn_id_card": lambda m: m.group()[:6] + "****" + m.group()[-4:],
    "cn_mobile": lambda m: m.group()[:3] + "****" + m.group()[-4:],
    "email": lambda m: m.group().split("@")[0][:2] + "***@" + m.group().split("@")[1],
    "access_key": lambda m: m.group()[:8] + "****",
    "secret_like": lambda m: m.group(1) + m.group(2) + "****",
}

# Per-entity severity. high → document QUARANTINE (dropped from index);
# medium → REDACT (masked in-place via REDACTION_MAP, doc kept + indexed).
# Internal contact numbers/emails in SOPs are masked (medium), not dropped; true
# national identifiers and secrets remain high → quarantine.
ENTITY_SEVERITY = {
    "cn_id_card": "high",
    "cn_mobile": "medium",
    "email": "medium",
    "access_key": "high",
    "secret_like": "high",
}
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# 图像 OCR PII 的已知误报锚点：18 位物料编码会命中 cn_id_card 正则。仅在这一类已确证的
# FP 上抑制（锚点共现时），绝不抑制密钥/手机号等。⚠️ 启用 RAG_IMAGE_OCR_PII 前须用真实
# CE38C5 等 OCR 样本验证此 allow-list，避免过度抑制真实身份证号。
_MATERIAL_CODE_ANCHORS = ("物料编码", "物料号", "料号", "编码", "material code", "material no")


def _image_ocr_fp_ignore(entity_name: str, ocr_text: str) -> bool:
    """图像 OCR 命中是否属已知误报（当前仅：cn_id_card 命中且物料编码锚点共现）。"""
    if entity_name == "cn_id_card":
        low = ocr_text.lower()
        return any(a.lower() in low for a in _MATERIAL_CODE_ANCHORS)
    return False
