# -*- coding: utf-8 -*-
"""
test_image_ocr_pii_gate.py — Stage-2 image-OCR PII gate (RAG_IMAGE_OCR_PII, default OFF).

Covers: detection over asset['ocr_text'] only (not visual_summary); flag-OFF no-op; high-sev
image hit → whole-doc QUARANTINE; material-code FP allow-list; graceful degradation on a bad
asset; the redaction gap-fix mutating the SAME in-memory asset object (with filename/anchor_row
preserved); and the DAG-2 redact-before-chunk ordering the same-object fix relies on.
"""

import pytest

import opensearch_pipeline.pipeline_nodes as pn

_PHONE = "13800138000"
_IDCARD = "110101199003078515"  # matches cn_id_card pattern
# ⚠️ 第二个身份证常量必须是**完全合成**的。曾经这里放过一个由生产调查片段
# （6104****2124）补全首尾而来的值——首尾是真的，等于把真 PII 提交进公开仓。
_IDCARD2 = "220101199512120011"  # 合成：吉林前缀 + 1995-12-12，无真实来源


def _doc(ocr_text="", visual_summary="", text="正文内容", status="ROUTE_TO_TEXT",
         filename="img1.png", anchor_row=None, llm_risk="low"):
    asset = {"status": status, "ocr_text": ocr_text, "visual_summary": visual_summary,
             "filename": filename}
    if anchor_row is not None:
        asset["anchor_row"] = anchor_row
    return {"doc_id": "D", "version_no": 1, "text": text, "assets": [asset],
            "llm_risk_level": llm_risk}


def _ctx(doc):
    return {"canonicals": [doc], "simulate_db": True}


def _img_hits(doc):
    return [h for h in doc.get("risk_hits", []) if h.get("source") == "image_ocr"]


# ── detection ────────────────────────────────────────────────────────────────
def test_flag_on_detects_image_phone(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"联系电话 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    hits = _img_hits(doc)
    assert any(h["finding_type"] == "image_ocr:cn_mobile" for h in hits)
    assert doc["risk_level"] == "medium"


def test_flag_off_noop(monkeypatch):
    monkeypatch.delenv("RAG_IMAGE_OCR_PII", raising=False)
    doc = _doc(ocr_text=f"联系电话 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []
    assert doc["risk_level"] == "low"


def test_visual_summary_not_scanned(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text="干净文本", visual_summary=f"按钮上显示 {_PHONE}")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []
    assert doc["risk_level"] == "low"


def test_discarded_asset_skipped(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话 {_PHONE}", status="DISCARD_LOW_VALUE")
    pn.node_detect_sensitive(_ctx(doc))
    assert _img_hits(doc) == []


def test_high_sev_image_hit_quarantines_whole_doc(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"身份证 {_IDCARD}")
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    assert doc["risk_level"] == "high"
    pn.node_redact_or_quarantine(ctx)
    assert doc["redaction_action"] == "QUARANTINE"


def test_material_code_fp_ignored(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"物料编码 {_IDCARD}")          # id-card pattern but a material code
    pn.node_detect_sensitive(_ctx(doc))
    assert not any(h["finding_type"] == "image_ocr:cn_id_card" for h in _img_hits(doc))
    assert doc["risk_level"] == "low"
    # same number WITHOUT the anchor → real high-sev hit
    doc2 = _doc(ocr_text=f"客户身份证 {_IDCARD}")
    pn.node_detect_sensitive(_ctx(doc2))
    assert doc2["risk_level"] == "high"


def test_graceful_degradation_on_bad_asset(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    # base text carries a phone; the bad asset's ocr_text is a non-str → re.search raises → caught
    doc = _doc(ocr_text=f"电话 {_PHONE}", text=f"正文电话 {_PHONE}")
    doc["assets"].append({"status": "ROUTE_TO_TEXT", "ocr_text": 12345, "filename": "bad.png"})
    pn.node_detect_sensitive(_ctx(doc))            # must not raise
    assert doc["risk_level"] == "medium"           # base-text + good-asset PII still detected


# ── redaction gap-fix (same in-memory object) ────────────────────────────────
def test_redaction_masks_ocr_text_same_object(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话{_PHONE}")
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)                  # medium
    pn.node_redact_or_quarantine(ctx)              # same ctx/object
    assert _PHONE not in doc["assets"][0]["ocr_text"]
    assert "138****8000" in doc["assets"][0]["ocr_text"]


def test_flag_off_redaction_leaves_ocr_untouched(monkeypatch):
    monkeypatch.delenv("RAG_IMAGE_OCR_PII", raising=False)
    doc = _doc(ocr_text=f"电话{_PHONE}", text=f"正文{_PHONE}")  # base text → medium → redact runs
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    pn.node_redact_or_quarantine(ctx)
    assert doc["assets"][0]["ocr_text"] == f"电话{_PHONE}"      # ocr untouched when flag off


def test_filename_and_anchor_row_survive_redaction(monkeypatch):
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    doc = _doc(ocr_text=f"电话{_PHONE}", filename="proc_step3.png", anchor_row=7)
    ctx = _ctx(doc)
    pn.node_detect_sensitive(ctx)
    pn.node_redact_or_quarantine(ctx)
    a = doc["assets"][0]
    assert a["filename"] == "proc_step3.png"
    assert a["anchor_row"] == 7
    assert _PHONE not in a["ocr_text"]


# ── structural: redact (node 03) runs before chunk (node 05) in DAG-2 ─────────
def test_dag2_redact_before_chunk():
    from opensearch_pipeline.dag_definitions import build_dag2_canonical_to_chunk
    dag = build_dag2_canonical_to_chunk()
    order = dag._topological_sort()
    funcs = [dag.nodes[nid].func for nid in order]
    assert funcs.index(pn.node_redact_or_quarantine) < funcs.index(pn.node_chunk_documents)


# ── 零命中重跑清理陈旧 finding 行（全维度复审 摄取#6）─────────────────────────
def test_zero_hits_rerun_still_deletes_stale_findings(monkeypatch):
    """源文件已修/白名单更新后的重跑：零命中也必须 DELETE 上一轮 finding 行，
    否则审计表残留 QUARANTINED 与文档实际 CLEAN 处置矛盾。"""
    from unittest.mock import MagicMock, patch

    doc = _doc(ocr_text="", text="干净正文，无任何敏感实体")
    ctx = {"canonicals": [doc], "simulate_db": False}

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    with patch.object(pn, "_get_db_conn", return_value=conn):
        pn.node_detect_sensitive(ctx)

    assert doc["sensitive_detected"] is False
    executed_sql = " ".join(str(c.args[0]) for c in cursor.execute.call_args_list)
    assert "DELETE FROM document_sensitive_finding" in executed_sql
    cursor.executemany.assert_not_called()   # 零命中：只清不插
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


# ── D1/D2 修复（2026-07-26）──────────────────────────────────────────────────
#
# 依据：`docs/image_ocr_pii_exposure_investigation_2026-07-26.md`（只读生产调查）。
# D1 是 🔴：`_MATERIAL_CODE_ANCHORS` 里的**裸 `编码`** 让抑制在现网吃掉了 **3/3 = 100%**
# 的真身份证命中 —— U8 人事界面普遍带「员工编码/部门编码/存货编码」，而旧判定是 **text
# 级**：整段 OCR 里出现一次 `编码`，该图的 cn_id_card 检测就整体失效。而 cn_id_card 是
# severity=high（整篇隔离），是这条链上**唯一承重的检测**。现网当时没泄露纯属巧合
# （bank_card 顺带命中同一串数字且未被抑制）。

from opensearch_pipeline.pii_patterns import (  # noqa: E402
    _id_card_ctx_is_fp,
    _image_ocr_fp_ignore,
    scrub_image_text,
)

# 报告里那三条真命中的上下文（号码已换成测试用的合法格式，语境逐字保留）
_HR_SCREEN = f"员工编码 A001 部门编码 SC01 证件类型 证件号码 {_IDCARD} 出生日期 1983-04-26"
_LEAVE_SCREEN = f"存货编码 CC7W 离职日期 证件号码 {_IDCARD} 证件类型 (0)身份证"


@pytest.mark.parametrize("anchor", ["员工编码", "部门编码", "客户编码", "编码"])
def test_d1_bare_bianma_alone_does_not_suppress(anchor):
    """**P0-3（审查 2026-07-26）**：D1 的核心就是把裸「编码」从锚点表删掉，而此前
    **零覆盖** —— 变异实测：把 `"编码"` 加回 `_MATERIAL_CODE_ANCHORS`，全量 3464 仍全绿。

    根因是原有 D1 用例的语料都含正向锚点（「证件号码」），`_id_card_ctx_is_fp` 在
    `_ID_CARD_POSITIVE_ANCHORS` 分支就 return False，**根本走不到** `_MATERIAL_CODE_ANCHORS`。
    所以这条用例的语料**刻意不含任何正向锚点**，直接压在被删的那个锚点上。
    """
    t = f"{anchor} {_IDCARD} 数量 500"
    assert _image_ocr_fp_ignore("cn_id_card", t) is False, \
        f"裸「{anchor}」不得抑制 cn_id_card —— 现网实测抑制率曾达 3/3 = 100%"
    assert _IDCARD not in scrub_image_text(t)


def test_d1_bare_bianma_no_longer_suppresses_real_id():
    """🔴 D1 回归钉子：ERP 人事界面里带「员工编码」不得再吃掉真身份证检测。"""
    assert _image_ocr_fp_ignore("cn_id_card", _HR_SCREEN) is False
    assert _IDCARD not in scrub_image_text(_HR_SCREEN)
    assert "110101****8515" in scrub_image_text(_HR_SCREEN)


def test_d1_cunhuo_bianma_with_real_id_not_suppressed():
    """「存货编码」是保留的锚点，但它与真身份证共存时不得整体抑制（match 级）。"""
    assert _image_ocr_fp_ignore("cn_id_card", _LEAVE_SCREEN) is False
    assert _IDCARD not in scrub_image_text(_LEAVE_SCREEN)


def test_d1_genuine_material_code_still_suppressed():
    """反向不能过头：真·物料编码 FP 仍须抑制，否则 ERP 手册整篇被推 high。"""
    t = f"物料编码 {_IDCARD} 数量 500 单位 只"
    assert _image_ocr_fp_ignore("cn_id_card", t) is True
    assert _IDCARD in scrub_image_text(t)          # 原样保留，未被掩码


def test_d1_is_match_level_not_text_level():
    """同一张图里既有物料编码又有真身份证时，**各判各的** —— 旧 text 级判据两个一起放过。"""
    t = f"物料编码 {_IDCARD} 。另见 证件号码 {_IDCARD2} 姓名 张三"
    assert _image_ocr_fp_ignore("cn_id_card", t) is False
    out = scrub_image_text(t)
    assert "220101****0011" in out, "真身份证必须被掩码"


def test_d1_positive_anchor_beats_material_anchor():
    """正向锚点优先（同 cn_mobile 的 _PHONE_POSITIVE_ANCHORS 设计）。"""
    import re
    from opensearch_pipeline.pii_patterns import ENTITY_PATTERNS
    m = re.search(ENTITY_PATTERNS["cn_id_card"], f"物料编码 身份证 {_IDCARD}")
    assert _id_card_ctx_is_fp(m) is False


def test_d1_body_path_untouched():
    """正文 PII 走 `_body_entity_fp_ignore`，本次未动 —— 不得被图像侧改动波及。"""
    from opensearch_pipeline.pii_patterns import _body_entity_fp_ignore
    assert _body_entity_fp_ignore("cn_id_card", _HR_SCREEN) is False


def test_d1_quarantine_still_flag_gated(monkeypatch):
    """风险边界：图像 cn_id_card→high→整篇隔离仍在 `RAG_IMAGE_OCR_PII` 门内（默认 OFF）。
    ⇒ D1 修复在默认姿态下**不会让任何文档下架**，只让 G21 由「巧合掩码」变成「按设计掩码」。

    ⚠️ 审查修正（2026-07-26）：初版只断言 flag-OFF 时 `_img_hits(...) == []`，而该断言
    **恒真** —— 把 flag 设成 true（与测试意图完全相反）它照样成立。现在两侧都断言，
    ON 侧必须真的产出 high 命中，这条边界才立得住。
    """
    # ⚠️ `_img_hits` 读的是 `doc["risk_hits"]`，**必须先跑 node** 才有内容 ——
    # 初版那条恒真断言的根因正是漏了这一步（我修的时候又踩了一次，故在此写明）。
    monkeypatch.setenv("RAG_IMAGE_OCR_PII", "true")
    on_doc = _doc(ocr_text=_HR_SCREEN)
    pn.node_detect_sensitive(_ctx(on_doc))
    assert any(h["finding_type"] == "image_ocr:cn_id_card" and h["severity"] == "high"
               for h in _img_hits(on_doc)), \
        "flag 开启时 D1 修复后应能检出 high（否则本用例无对照意义）"

    monkeypatch.delenv("RAG_IMAGE_OCR_PII", raising=False)
    off_doc = _doc(ocr_text=_HR_SCREEN)
    pn.node_detect_sensitive(_ctx(off_doc))
    assert _img_hits(off_doc) == [], "默认姿态下不得产出任何图像命中"


# ── D2：scrub_image_text 幂等（文档一直这么承诺，实测此前不成立）──────────────

@pytest.mark.parametrize("raw", [
    "联系电话 13900139000",
    f"证件号码 {_IDCARD} 身份证",
    "地址:浙江省温岭市金塘南路88号",
    "已掩码 139****9000 原样",
    "混合 13900139000 与 139****9000",
])
def test_d2_scrub_is_idempotent(raw):
    """旧实现下 `139****9000` 第二遍会被 masked_id 整词换成 `[标识已脱敏]`，
    把仍保留后 4 位的可用掩码降级成完全不可读。触发路径 = node03 就地改写后
    node05 的 G21 在**同一轮里**再扫一次。"""
    once = scrub_image_text(raw)
    assert scrub_image_text(once) == once


def test_d2_identity_is_scoped_to_g21_only():
    """masked_id 在 **G21 这条路径上**的职责是占位保护，不是再脱敏一次 —— 故走恒等。

    ⚠️ 审查修正 P1-4（2026-07-26）：初版把**全局** `REDACTION_MAP["masked_id"]` 改成了恒等，
    理由写的是"该路径只有 RAG_IMAGE_OCR_PII 打开才会走到"——**不成立**：
    `node_redact_or_quarantine` 的正文 + blocks 脱敏循环同样遍历 `REDACTION_MAP` 且不读任何
    flag。改全局表等于顺手关掉了正文侧对已掩码标识的处理。故恒等收窄到 G21 专用覆盖表。
    """
    import re
    from opensearch_pipeline.pii_patterns import (
        ENTITY_PATTERNS, REDACTION_MAP, _IMAGE_SCRUB_REDACTION_OVERRIDE)
    m = re.search(ENTITY_PATTERNS["masked_id"], "卡号 6222****5678 尾号")
    assert _IMAGE_SCRUB_REDACTION_OVERRIDE["masked_id"](m) == m.group(), "G21 侧恒等"
    assert REDACTION_MAP["masked_id"](m) == "[标识已脱敏]", \
        "全局表必须保持整词替换 —— node03 的正文路径靠它"


def test_d2_masked_values_survive_a_full_scrub():
    """占位保护仍然有效：已掩码值经全表扫描后原样保留，不被任何模式二次处理。"""
    assert scrub_image_text("6222****5678") == "6222****5678"
    assert "[标识已脱敏]" not in scrub_image_text("卡号 6222****5678")


# ── 方案 A（2026-07-26）：G21 消费点补留痕 ──────────────────────────────────
#
# 背景：图像侧 PII 在审计表上此前完全不可见（`image_ocr:*` 恒 0 行）。根因是那条留痕挂在
# node02 的 `RAG_IMAGE_OCR_PII` 门内，而该 flag 只在 DataWorks 节点 setdefault、**覆盖不了
# 笔记本重跑**；G21 默认 ON 且不读任何 flag，两条执行路径都生效 —— 所以留痕该挂它这里。
# 选 A 而不是"默认开 flag"的依据：实测开 flag 的唯一增量是把 2 篇在用的 public 手册从
# 「掩码后继续服务」降级成「下架」（high 0→2 篇），是净损失。

def test_a_findings_hook_records_only_what_was_actually_redacted():
    """判据是「值变了」而非「模式匹配上了」—— bank_card 的 FP 由 `_guarded_bank_card_redact`
    **在内部**就地放过，在调用侧另抄一遍判据必然漂移，所以回收口开在 scrub 里。"""
    f = []
    scrub_image_text(f"证件号码 {_IDCARD} 联系电话 {_PHONE}", findings=f)
    assert sorted(n for n, _ in f) == ["cn_id_card", "cn_mobile"]

    f = []
    scrub_image_text("订单号 1234567890123456 数量 5", findings=f)
    assert f == [], "bank_card 的订单号 FP 不得留痕"

    f = []
    scrub_image_text("银行卡号 6222021234567890123", findings=f)
    assert [n for n, _ in f] == ["bank_card"]

    f = []
    scrub_image_text("注册号 13889211462 证书", findings=f)
    assert f == [], "cn_mobile 的凭证号 FP 不得留痕（106E77 教训）"

    f = []
    scrub_image_text(f"物料编码 {_IDCARD}", findings=f)
    assert f == [], "整体抑制的物料编码不得留痕"


def test_a_masked_id_produces_no_finding():
    """D2 之后 masked_id 的脱敏器是恒等 —— G21 对它什么也没做，审计行不该声称做了。"""
    f = []
    assert scrub_image_text("卡号 6222****5678", findings=f) == "卡号 6222****5678"
    assert f == []


def test_a_hook_is_opt_in():
    """不传 findings 时行为与旧签名逐字节一致（G21 之外的调用方不受影响）。"""
    t = f"证件号码 {_IDCARD}"
    assert scrub_image_text(t) == scrub_image_text(t, findings=[])


# ── 落库侧 ──────────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, raise_on=None):
        self.raise_on, self.executed, self.many, self.commits = raise_on, [], [], 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.raise_on == "delete":
            raise RuntimeError("boom")
        self.executed.append((sql, params))

    def executemany(self, sql, rows):
        if self.raise_on == "insert":
            raise RuntimeError("boom")
        self.many.append((sql, rows))

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


_DOC = {"doc_id": "D", "version_no": 2}


def test_a_persist_row_shape(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    pn._persist_image_scrub_findings({"simulate_db": False}, _DOC,
                                     [("cn_id_card", _IDCARD), ("cn_mobile", _PHONE)])
    sql, rows = fake.many[0]
    types = [r[2] for r in rows]
    assert types == ["image_scrub:cn_id_card", "image_scrub:cn_mobile"]
    assert all(r[8] == "REDACTED" for r in rows), "G21 只掩码，绝不写 QUARANTINED"
    assert [r[3] for r in rows] == ["high", "medium"]          # 取 ENTITY_SEVERITY
    blob = str(rows)
    assert _IDCARD not in blob and _PHONE not in blob, "原文绝不入库（只存指纹 + 掩码预览）"
    assert rows[0][6] is None, "未配 RAG_PII_FINGERPRINT_KEY 时指纹列必须为 NULL"
    assert fake.commits == 1


def test_a_fingerprint_is_hmac_not_bare_sha256(monkeypatch):
    """审查 P2-12：无盐 SHA-256 对 11 位手机号可**秒级枚举**回推，叠加同行的首尾各 2 位
    等于把明文放进了一张有看板查询面、默认留存 24 个月的表。node02 哈希的是实体名/词表词，
    从来不是 PII 值 —— 所以「只存 SHA-256 + 掩码预览」这句在那边成立、在这边不成立。"""
    import hashlib
    monkeypatch.delenv("RAG_PII_FINGERPRINT_KEY", raising=False)
    assert pn._pii_fingerprint(_PHONE) is None, "拿不到密钥宁可不写指纹，也不退回明文哈希"
    monkeypatch.setenv("RAG_PII_FINGERPRINT_KEY", "k")
    fp = pn._pii_fingerprint(_PHONE)
    assert fp and len(fp) == 64
    assert fp != hashlib.sha256(_PHONE.encode()).hexdigest(), "不得等于裸 SHA-256"
    assert fp == pn._pii_fingerprint(_PHONE), "同值同指纹（去重能力保留）"


def test_a_preview_is_length_capped():
    """审查 P1-10：列是 VARCHAR(255)，而 secret_like/access_key/email 的匹配长度无上界。
    一条超长命中会让 executemany 整体 1406 失败 ⇒ 该文档本轮**全部** image_scrub 行
    （含同篇合法的手机号/身份证留痕）一起被 fail-open 吞掉，只剩一行 print。"""
    import re
    from opensearch_pipeline.pii_patterns import ENTITY_PATTERNS
    long_secret = "token: " + "A" * 400
    assert re.search(ENTITY_PATTERNS["secret_like"], long_secret)
    fake = _FakeConn()
    import opensearch_pipeline.pipeline_nodes as _pn
    _pn._get_db_conn = lambda **kw: fake          # 局部替身，随测试结束丢弃
    _pn._persist_image_scrub_findings({"simulate_db": False}, _DOC,
                                      [("secret_like", long_secret)])
    assert len(fake.many[0][1][0][7]) <= 255, "preview 必须封顶到列宽"


def test_a_prefix_does_not_collide_with_node02(monkeypatch):
    """`image_ocr:` 是判断「node02 那条 flag 门内路径跑没跑过」的既有判据（只读调查正是
    这么用的）。复用会毁掉它 —— 本层必须用 `image_scrub:`。"""
    fake = _FakeConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    pn._persist_image_scrub_findings({"simulate_db": False}, _DOC, [("cn_mobile", _PHONE)])
    assert all(not r[2].startswith("image_ocr:") for r in fake.many[0][1])
    del_sql = fake.executed[0][0]
    assert "image_scrub:%" in del_sql, "只清本层自己的行，不得动 node02 的"


def test_a_dedup_within_doc(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    pn._persist_image_scrub_findings({"simulate_db": False}, _DOC,
                                     [("cn_mobile", _PHONE)] * 3)
    assert len(fake.many[0][1]) == 1


@pytest.mark.parametrize("stage", ["delete", "insert"])
def test_a_persist_is_fail_open(monkeypatch, capsys, stage):
    """与 node02 的 `raise` 刻意不同：那里留痕失败=隔离决策没落库；这里只是少一行审计，
    绝不让审计写把分块炸掉。"""
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _FakeConn(raise_on=stage))
    pn._persist_image_scrub_findings({"simulate_db": False}, _DOC, [("cn_mobile", _PHONE)])
    assert "留痕写入失败" in capsys.readouterr().out


def test_a_noop_cases(monkeypatch):
    called = []
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: called.append(1))
    pn._persist_image_scrub_findings({"simulate_db": False}, _DOC, [])     # 无命中
    pn._persist_image_scrub_findings({"simulate_db": True}, _DOC,
                                     [("cn_mobile", _PHONE)])              # simulate
    assert called == []


def test_a_wired_into_g21_call_site(monkeypatch):
    """G21 必须把回收口传下去**并真的调用**落库。

    ⚠️ 审查修正（2026-07-26）：初版用源码子串断言，把那行改成
    `pass  # _persist_image_scrub_findings(ctx, doc, _g21_findings)` 照样绿。
    改成 monkeypatch 真函数 + 跑真 node，断言它被调用且拿到了非空 findings。
    """
    called = []
    monkeypatch.setattr(pn, "_persist_image_scrub_findings",
                        lambda ctx, doc, findings: called.append((doc["doc_id"], list(findings))))
    monkeypatch.delenv("RAG_IMAGE_OCR_PII_FAILCLOSED", raising=False)   # 默认 ON
    doc = _doc(ocr_text=f"联系电话 {_PHONE}")
    pn.node_chunk_documents({"canonicals": [doc], "simulate_db": True})
    assert called, "G21 未调用 _persist_image_scrub_findings"
    assert called[0][0] == "D"
    assert [n for n, _ in called[0][1]] == ["cn_mobile"], "回收口未把命中传下去"


def test_a_does_not_touch_any_decision():
    """纯留痕：不参与 final_risk、不回写 redaction_action、不影响 chunk 产出。

    只查**函数体**（剥掉 docstring）—— docstring 里正当地讨论了这些概念。
    """
    import ast
    import inspect
    fn = ast.parse(inspect.getsource(pn._persist_image_scrub_findings)).body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.dump(n) for n in body)
    for banned in ("final_risk", "redaction_action", "entity_risk", "QUARANTINED"):
        assert banned not in code, f"留痕函数不得触碰 {banned}"
    assert "REDACTED" in code, "action 恒为 REDACTED"
