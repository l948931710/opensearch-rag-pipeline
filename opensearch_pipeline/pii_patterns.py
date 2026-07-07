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
    # ⚠️ 顺序 load-bearing（node 03 / G21 兜底按 dict 序逐模式 re.sub）：masked_id 必须
    # 排最前——它只应捕获【上游文档里预先掩码】的标识（如 "3301****1234"）；若排在
    # cn_mobile/bank_card 之后，会把本轮刚产出的 "138****5678"/"6222****5678" 掩码
    # 二次吞成 [标识已脱敏]，丢失前后段可读性。
    "masked_id": r"(?<![\dA-Za-z])\d{3,4}[\*＊✱•·∗●]{2,}[\dXx]{3,4}(?![\dA-Za-z])",
    "cn_id_card": r"(?<![0-9Xx])[1-9]\d{5}(18|19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9Xx])",
    "cn_mobile": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "access_key": r"(?<![A-Za-z0-9])(LTAI|AKIA)[A-Za-z0-9]{12,}(?![A-Za-z0-9])",
    # 与本表其余号码/密钥类一致用 lookaround 定界（见上方 ⚠️）：旧实现遗留 \b，而 Python 正则里
    # CJK 属 \w → 中文紧贴英文关键字（"数据库password=…"）时 \b 不成立 → 明文密钥既写入
    # qa_session_log 又逃过入库隔离闸门（node_detect_sensitive 的 re.search）。lookaround 同步修复两侧。
    "secret_like": r"(?i)(?<![A-Za-z0-9])(secret|password|passwd|pwd|token|api[_-]?key)(\s*[:=]\s*)[A-Za-z0-9_\-]{8,}",
    # ── G5 扩展实体（2026-07-06：与 redaction.py 离线脱敏派生合并为同一权威表）──
    # 此前这四类只存在于离线脱敏模块：摄取门检测不到 → 银行卡/信用代码/地址原样入
    # 索引；更糟的是语义词"银行卡"命中走 REDACT 路径时，REDACTION_MAP 无 bank_card
    # 条目 → 卡号原样保留且文档标 CLEAN。redaction.py 直接复用本表（勿再双处定义）。
    # 银行卡：16-19 位数字、非字母数字邻接（USCC 尾校验字母绝不半匹配——redact-v2 教训）
    "bank_card": r"(?<![\dA-Za-z])\d{16,19}(?![\dA-Za-z])",
    # 统一社会信用代码：18 位大写字母数字、数字开头、且至少含一个字母（纯数字 18 位
    # 归身份证/物料编码/卡号模式管辖——否则 18 位物料编码会被误判 uscc）。整 token
    # 脱敏防尾校验字符泄露。
    "uscc": (r"(?<![\dA-Za-z])\d(?=[0-9A-HJ-NP-Z]{17}(?![\dA-Za-z]))"
             r"(?=[0-9]*[A-HJ-NP-Z])[0-9A-HJ-NP-Z]{17}(?![\dA-Za-z])"),
    # 住址/通讯地址：市/区/县 + 路名成分 + 门牌号
    "address": (r"[一-龥]{2,8}(?:市|区|县|自治州|地区)"
                r"[一-龥0-9（）()]{0,22}?"
                r"(?:路|街|大道|大街|巷|弄|村|镇|乡|新村|小区|段)"
                r"[一-龥0-9（）()]{0,15}?\d{1,5}号"),
}

# G5：bank_card 的已知 FP 面——ERP/生产语料里 16-19 位订单号/物料号/工单号/条码，
# 外加资质证书/注册号（2026-07-07，106E77：证书编号 17 位数字命中 bank_card）。
# 正向锚点（卡号/账号/银行）优先：共现即视为真卡号；否则业务/凭证锚点共现 → 抑制。
_BANK_CARD_POSITIVE_ANCHORS = ("卡号", "账号", "账户", "银行", "bank")
_BANK_CARD_FP_ANCHORS = ("订单", "单号", "物料", "料号", "工单", "编号", "编码",
                         "流水号", "条码", "追溯码", "batch", "order", "sku", "barcode",
                         "证书", "证号", "注册号", "许可证", "备案", "certificate",
                         "license", "licence", "registration")


def _bank_card_ctx_is_fp(m) -> bool:
    """bank_card 单个命中的上下文 FP 判定（±24 字符窗口）。"""
    s = m.string
    ctx = s[max(0, m.start() - 24): m.end() + 8].lower()
    if any(p in ctx for p in _BANK_CARD_POSITIVE_ANCHORS):
        return False
    return any(a in ctx for a in _BANK_CARD_FP_ANCHORS)


def _guarded_bank_card_redact(m):
    """FP 上下文（订单号/物料号锚点）保留原文；真卡号 前4****后4。"""
    if _bank_card_ctx_is_fp(m):
        return m.group()
    return m.group()[:4] + "****" + m.group()[-4:]


REDACTION_MAP = {
    "cn_id_card": lambda m: m.group()[:6] + "****" + m.group()[-4:],
    "cn_mobile": lambda m: m.group()[:3] + "****" + m.group()[-4:],
    "email": lambda m: m.group().split("@")[0][:2] + "***@" + m.group().split("@")[1],
    "access_key": lambda m: m.group()[:8] + "****",
    "secret_like": lambda m: m.group(1) + m.group(2) + "****",
    # G5 扩展（修复"银行卡命中却无脱敏条目"缺陷）
    "bank_card": _guarded_bank_card_redact,
    "uscc": lambda m: m.group()[:4] + "****" + m.group()[-2:],
    "address": lambda m: "[地址已脱敏]",
    "masked_id": lambda m: "[标识已脱敏]",
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
    # G5 扩展类型统一 medium（就地脱敏、文档保留可检索）。与 redaction.HIGH_TYPES 的
    # 分级刻意不同：那边 high 门的是"脱敏派生可否发布"（零残留要求）；摄取门 high=
    # 整文档隔离（下架），而 bank_card 在本语料的主要命中面是 ERP 订单号/物料号 FP，
    # 隔离的可用性代价不成比例——就地掩码 + 上下文 FP 抑制是均衡点。
    "bank_card": "medium",
    "uscc": "medium",
    "address": "medium",
    "masked_id": "medium",
}
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# 图像 OCR PII 的已知误报锚点：18 位物料编码会命中 cn_id_card 正则。仅在这一类已确证的
# FP 上抑制（锚点共现时），绝不抑制密钥/手机号等。⚠️ 启用 RAG_IMAGE_OCR_PII 前须用真实
# CE38C5 等 OCR 样本验证此 allow-list，避免过度抑制真实身份证号。
_MATERIAL_CODE_ANCHORS = ("物料编码", "物料号", "料号", "编码", "material code", "material no")


def _image_ocr_fp_ignore(entity_name: str, ocr_text: str) -> bool:
    """图像 OCR 命中是否属已知误报。

    - cn_id_card：18 位物料编码 + 物料锚点共现（既有）。
    - bank_card（G5）：所有命中都落在订单号/物料号业务锚点上下文（正向锚点优先）。
    """
    if entity_name == "cn_id_card":
        low = ocr_text.lower()
        return any(a.lower() in low for a in _MATERIAL_CODE_ANCHORS)
    if entity_name == "bank_card":
        return _body_entity_fp_ignore("bank_card", ocr_text)
    return False


def _body_entity_fp_ignore(entity_name: str, text: str) -> bool:
    """正文实体命中的 FP 抑制（G5，当前仅 bank_card）。

    所有命中都在业务锚点（订单/物料/工单…）上下文且无正向锚点（卡号/账号/银行）
    时判 FP——ERP 手册整篇订单号示例不再把文档推成 medium/REDACTED。任一命中
    不在锚点上下文 → 不抑制（fail-closed 方向）。
    """
    if entity_name != "bank_card":
        return False
    import re as _re
    ms = list(_re.finditer(ENTITY_PATTERNS["bank_card"], text))
    return bool(ms) and all(_bank_card_ctx_is_fp(m) for m in ms)


# ── 图片文本（OCR/VLM caption）的凭证号 FP 面（2026-07-07，106E77 事后）──
# 资质证书类图片的编号常撞号码正则：FDA 注册号 "13889211462" 命中 cn_mobile、
# 证书编号 17 位数字段命中 bank_card。VLM caption 会照抄这些号码
# （"FDA注册确认函，显示注册号13889211462"），盲掩会毁掉"我们的 FDA 注册号是
# 多少"这类合法业务答案。与 bank_card 同款设计：正向锚点（电话/联系/手机）优先
# ——共现即视为真手机号照掩；否则凭证锚点共现 → 保留原文。
_CREDENTIAL_ANCHORS = ("注册号", "证书", "证号", "编号", "备案", "许可证", "文号",
                       "批号", "registration", "certificate", "license", "licence")
_PHONE_POSITIVE_ANCHORS = ("电话", "手机", "联系", "致电", "微信", "传真",
                           "tel", "phone", "mobile", "contact", "call")


def _mobile_ctx_is_credential(m) -> bool:
    """cn_mobile 单个命中的凭证号 FP 判定（±24 字符窗口，正向锚点优先）。"""
    ctx = m.string[max(0, m.start() - 24): m.end() + 8].lower()
    if any(p in ctx for p in _PHONE_POSITIVE_ANCHORS):
        return False
    return any(a in ctx for a in _CREDENTIAL_ANCHORS)


def scrub_image_text(text: str) -> str:
    """图片伴生文本（asset ocr_text / visual_summary）的消费点兜底掩码。

    G21 fail-closed 的共享实现：ENTITY_PATTERNS 全表就地掩码（dict 序 load-bearing，
    masked_id 必须先跑），带三层 FP 护栏——
      * cn_id_card：物料编码锚点整文本抑制（_image_ocr_fp_ignore，既有语义）；
        ⚠️ 绝不加凭证锚点护栏——"身份证号"本身含"证号"，加了会漏真身份证。
      * bank_card：REDACTION_MAP 内建 _guarded_bank_card_redact 逐命中判定。
      * cn_mobile：凭证号上下文（注册号/证书/…）逐命中保留，正向锚点（电话/联系）
        共现则照掩（106E77 FDA 注册号教训）。
    幂等：已掩码文本（138****5678）被 masked_id 模式先行捕获，不再二次吞。
    """
    if not text:
        return text
    import re as _re
    out = text
    for name, pat in ENTITY_PATTERNS.items():
        rep = REDACTION_MAP.get(name)
        if rep is None or _image_ocr_fp_ignore(name, out):
            continue
        if name == "cn_mobile":
            out = _re.sub(pat, lambda m: m.group()
                          if _mobile_ctx_is_credential(m) else rep(m), out)
        else:
            out = _re.sub(pat, rep, out)
    return out
