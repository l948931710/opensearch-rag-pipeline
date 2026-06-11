# -*- coding: utf-8 -*-
"""跨文档真重复图片抑制 + 来源 title 去重 —— 2026-06-10 live 实测问题的回归测试。

实测背景（miniapp LIVE 桥 + curl，RAG_ENV=test）：
  1. 「U8系统怎么登录」把两份不同文档里的 U8 登录截图背靠背渲染；
  2. /api/ask sources 出现同一文档两行（A1员工行为管理标准 4 次注册 → 2 个 active doc_id）。

抑制目标是【真重复】（重复注册 / docx+pdf 双格式 / 同一截图被多份 SOP 原样内嵌 ——
VLM cache 按 MD5 命中，caption 逐字相同）。对抗评审（2026-06-10，13 agents，真实语料
逐对验证）证实的三类误杀面由否决项保护，本文件逐类回归：
  - 同 doc_id 多图（SOP 各步骤截图）绝不判重；
  - 值发散的同屏变体（LOGIN_A×LOGIN_B：账套 FL063/(888)PLS vs FL0062/188@FLSJ）保留 ——
    抑制其一会让读者照着错误账套操作；
  - 引号目标发散的公式化 caption（‘采购发票审核’ vs ‘人事管理’）保留；
  - OCR 兜底文本（界面公共文案）与低熵/降级 caption 不参与判重。

caption 常量是真实语料的 VLM visual_summary 原文（阈值标定样本，勿改写）：
  MENU_A × MENU_B = 阈值粗筛下最近的必留对（jac≈0.342/lcs≈12，门限 0.35/16）
"""

import time

import opensearch_pipeline.content_blocks_builder as cb
from opensearch_pipeline.content_blocks_builder import (
    build_content_blocks,
    build_mini_program_blocks,
    _is_near_dup_caption,
)
from opensearch_pipeline.llm_generator import _extract_sources

LOGIN_A = (
    "用友U8+系统登录界面截图，显示服务器地址192.168.0.144、账套编号FL063、账号密码输入框、"
    "账套名称（888）PLS-富岭、日期2022-08-18及蓝色登录按钮，界面含红色标注提示"
    "‘输入你自己的账号密码’‘账套’‘时间不用管默认’。"
)
LOGIN_B = (
    "用友U8+系统登录界面截图，显示服务器地址192.168.0.144、用户名FL0062、密码输入框、"
    "下拉选项‘188@FLSJ富岭’、日期2022-08-05及蓝色‘登录’按钮，左侧为红色节日风格装饰图。"
)
MENU_A = (
    "U8/ERP系统界面截图，左侧为业务导航栏（含业务导航、常用功能、消息任务、报表中心等图标），"
    "右侧为经典树形菜单，展开‘库存管理’下的‘材料出库’节点，红色箭头指向‘领料申请单列表’选项。"
)
MENU_B = (
    "U8系统桌面界面截图，左侧为业务导航、常用功能等图标菜单，右侧经典树形结构展开至"
    "‘凭证处理’节点，红色箭头指向‘生成凭证’选项，该选项被红框高亮标出。"
)
# 公式化框架 + 不同引号目标：jac 很高但内容不同（对抗评审在 vlm_cache 真实语料发现的误杀类）
FRAME_X = (
    "U8系统桌面界面截图，左侧为业务导航、常用功能等图标菜单，右侧经典树形结构展开，"
    "红色箭头指向‘采购发票审核’选项，该选项被红框高亮标出。"
)
FRAME_Y = (
    "U8系统桌面界面截图，左侧为业务导航、常用功能等图标菜单，右侧经典树形结构展开，"
    "红色箭头指向‘人事管理’选项，该选项被红框高亮标出。"
)
OCR_CHROME = "业务导航 常用功能 消息任务 报表中心"   # 两张不同截图共有的界面公共文案


def _img_chunk(doc_id, oss_key, summary, title="某作业指导书.pdf"):
    return {
        "chunk_type": "image",
        "doc_id": doc_id,
        "source_image": oss_key,
        "visual_summary": summary,
        "title": title,
    }


def _fake_sign(monkeypatch):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "https://oss/" + key)


def _image_keys(blocks):
    return [b["oss_key"] for b in blocks if b["type"] == "image"]


# ═══════════════ 近重判定本体 ═══════════════

def test_true_dup_identical_caption_detected():
    """真重复签名：VLM cache 按 MD5 命中 → 逐字相同的 caption。"""
    assert _is_near_dup_caption(LOGIN_A, LOGIN_A)
    assert _is_near_dup_caption(MENU_A, MENU_A)


def test_value_divergent_variants_not_dup():
    """同屏不同部门变体：取值 token（FL063/FL0062、(888)PLS/188@FLSJ）发散 → 必留。"""
    assert not _is_near_dup_caption(LOGIN_A, LOGIN_B)


def test_threshold_calibration_keep_pair():
    assert not _is_near_dup_caption(MENU_A, MENU_B)    # jac≈0.342/lcs≈12，门限粗筛即放行
    assert not _is_near_dup_caption(LOGIN_A, MENU_A)   # 不同内容


def test_quoted_target_disjoint_not_dup():
    """公式化框架 + 不同引号目标（jac 很高）：引号目标发散否决。"""
    assert not _is_near_dup_caption(FRAME_X, FRAME_Y)


def test_arrow_target_divergent_not_dup():
    """菜单路径引号重合但箭头目标不同（vlm_cache 真实误杀对）：箭头主语发散否决。"""
    arrow_x = (
        "U8/ERP系统桌面界面截图，左侧为业务导航、常用功能等模块图标，"
        "右侧经典树形菜单展开至‘存货核算’→‘记账’，红色箭头指向‘暂估成本录入’菜单项。"
    )
    arrow_y = (
        "U8/ERP系统界面截图，左侧为业务导航、常用功能等模块图标栏，右侧为经典树形菜单结构，"
        "展开至‘存货核算’下的‘记账’子项，并用红色箭头指向‘特殊单据记账’选项。"
    )
    assert not _is_near_dup_caption(arrow_x, arrow_y)


def test_mid_band_without_quotes_not_dup():
    """中带相似度且双方都没有引号主语（同框架不同界面，vlm_cache 真实误杀对）：保留。"""
    pos = (
        "ERP系统职位信息维护界面截图，左侧为组织架构树（含总经办、财务部、人力资源部等），"
        "右侧为职位信息表单，含职位编码、名称、所属部门、成立日期等字段；界面顶部有增加、保存等操作按钮。"
    )
    dept = (
        "ERP系统部门信息维护界面截图，左侧为部门树形列表，右侧为部门信息录入表单，"
        "含部门编码、名称、负责人、成立日期等字段；界面顶部有增加、保存等操作按钮。"
    )
    assert not _is_near_dup_caption(pos, dept)


def test_low_entropy_and_degraded_never_dup():
    assert not _is_near_dup_caption("短文本", "短文本")                      # 低于最小长度
    assert not _is_near_dup_caption("！！！！！！！！！！", "！！！！！！！！！！")   # 低熵（bigram 集合过小）
    assert not _is_near_dup_caption("操作界面如下图所示。", "操作界面如下图所示。")  # 低熵样板句
    assert not _is_near_dup_caption(
        "[VLM Fallback] 图片或示意图内容。", "[VLM Fallback] 图片或示意图内容。"
    )                                                                      # 降级 caption 是常量文本
    assert not _is_near_dup_caption(LOGIN_A, "")


def test_lcs_input_clamped_fast():
    """OCR 长文不再喂给 O(n·m) DP：4k 字符对的判定在毫秒级完成。"""
    long_a = ("界面公共文案区域标题栏工具栏" * 300) + "甲"
    long_b = ("界面公共文案区域标题栏工具栏" * 300) + "乙"
    t0 = time.monotonic()
    _is_near_dup_caption(long_a, long_b)
    assert time.monotonic() - t0 < 0.5


# ═══════════════ build_content_blocks 集成 ═══════════════

def test_cross_doc_true_dup_dropped(monkeypatch):
    """同一张截图被两份文档内嵌（caption 逐字相同）：只渲染排名靠前的一张。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/menu.png", MENU_A),
        _img_chunk("DOC_B", "b/menu.png", MENU_A),
    ]
    blocks = build_content_blocks("路径如下<<IMG:1>>另一份文档<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/menu.png"]


def test_cross_doc_value_divergent_both_kept(monkeypatch):
    """两份文档各自截的登录窗带不同部门取值：都保留（抑制其一会误导读者）。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/login.jpeg", LOGIN_A),
        _img_chunk("DOC_B", "b/login.jpeg", LOGIN_B),
    ]
    blocks = build_content_blocks("登录步骤<<IMG:1>>另一部门<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/login.jpeg", "b/login.jpeg"]


def test_same_doc_similar_captions_both_kept(monkeypatch):
    """同一文档的多图绝不判近重 —— SOP 各步骤截图天然相似但各属其步。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/step2.jpeg", MENU_A),
        _img_chunk("DOC_A", "a/step7.jpeg", MENU_A),   # caption 逐字相同也保留
    ]
    blocks = build_content_blocks("第2步<<IMG:1>>第7步<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/step2.jpeg", "a/step7.jpeg"]


def test_exact_same_oss_key_rendered_once(monkeypatch):
    """同一文件（oss_key 相同）不论来自哪个 chunk，一条回答里只渲染一次。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/shared.jpeg", "某界面截图，含登录按钮与输入框"),
        _img_chunk("DOC_A", "a/shared.jpeg", "同一界面截图的另一段描述文本"),
    ]
    blocks = build_content_blocks("先看<<IMG:1>>再看<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/shared.jpeg"]


def test_cross_doc_different_content_kept(monkeypatch):
    """跨文档但内容不同（两个不同菜单路径）：都保留。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/menu1.png", MENU_A),
        _img_chunk("DOC_B", "b/menu2.png", MENU_B),
    ]
    blocks = build_content_blocks("路径一<<IMG:1>>路径二<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/menu1.png", "b/menu2.png"]


def test_ocr_fallback_text_never_compared(monkeypatch):
    """step_card/text 路径 caption 由 OCR 兜底（界面公共文案）：不参与判重，两图都保留。"""
    _fake_sign(monkeypatch)
    chunks = [
        {
            "chunk_type": "step_card", "doc_id": "DOC_A", "title": "SOP甲",
            "image_refs": [{"oss_key": "a/s1.png", "caption": OCR_CHROME}],
        },
        {
            "chunk_type": "step_card", "doc_id": "DOC_B", "title": "SOP乙",
            "image_refs": [{"oss_key": "b/s2.png", "caption": OCR_CHROME}],
        },
    ]
    blocks = build_content_blocks("第一步<<IMG:1>>第二步<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/s1.png", "b/s2.png"]

    chunks_text = [
        {
            "chunk_type": "text_chunk", "doc_id": "DOC_A", "title": "SOP甲",
            "image_refs": [{"oss_key": "a/t1.png", "ocr_text": OCR_CHROME}],
        },
        {
            "chunk_type": "text_chunk", "doc_id": "DOC_B", "title": "SOP乙",
            "image_refs": [{"oss_key": "b/t2.png", "ocr_text": OCR_CHROME}],
        },
    ]
    blocks = build_content_blocks("一<<IMG:1>>二<<IMG:2>>完", chunks_text)
    assert _image_keys(blocks) == ["a/t1.png", "b/t2.png"]


def test_dropped_dup_does_not_consume_quota(monkeypatch):
    """被抑制的真重复图不占 max_images 配额：后续图片仍能渲染。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/menu.png", MENU_A),
        _img_chunk("DOC_B", "b/menu.png", MENU_A),   # 真重复 → 被抑制
        _img_chunk("DOC_C", "c/other.png", MENU_B),
    ]
    blocks = build_content_blocks("一<<IMG:1>>二<<IMG:2>>三<<IMG:3>>完", chunks, max_images=2)
    assert _image_keys(blocks) == ["a/menu.png", "c/other.png"]


def test_missing_doc_id_skips_near_dup(monkeypatch):
    """doc_id 缺失时不做近重判定（保守保留），仅 oss_key 精确去重仍生效。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("", "a/menu.png", MENU_A),
        _img_chunk("", "b/menu.png", MENU_A),
    ]
    blocks = build_content_blocks("一<<IMG:1>>二<<IMG:2>>完", chunks)
    assert _image_keys(blocks) == ["a/menu.png", "b/menu.png"]


def test_mini_program_blocks_inherit_dedup(monkeypatch):
    """小程序 blocks 复用同一核心：真重复抑制同样生效。"""
    _fake_sign(monkeypatch)
    chunks = [
        _img_chunk("DOC_A", "a/menu.png", MENU_A),
        _img_chunk("DOC_B", "b/menu.png", MENU_A),
    ]
    blocks = build_mini_program_blocks("路径<<IMG:1>>又见<<IMG:2>>完", chunks)
    imgs = [b for b in blocks if b["type"] == "image"]
    assert [b["oss_key"] for b in imgs] == ["a/menu.png"]


# ═══════════════ _extract_sources 去重 ═══════════════

def _text_chunk(doc_id, title, score, section="", page_num=0):
    return {
        "chunk_type": "text_chunk",
        "doc_id": doc_id,
        "title": title,
        "score": score,
        "section_title": section,
        "page_num": page_num,
    }


def test_sources_same_title_multiple_doc_ids_collapse():
    """同一文件多次注册（不同 doc_id）：只保留排名最高的一行。"""
    chunks = [
        _text_chunk("DOC_HR_1", "A1员工行为管理标准.docx", 0.626, "2.3 C"),
        _text_chunk("DOC_ADMIN_1", "A1员工行为管理标准.docx", 0.626, "2.3 C"),
    ]
    sources = _extract_sources(chunks)
    assert len(sources) == 1
    assert sources[0]["doc_id"] == "DOC_HR_1"   # 首次出现（chunks 已按检索排序）


def test_sources_docx_pdf_pair_collapse():
    """docx+pdf 双格式 double-ingest：视为同一来源，保留排名靠前者。"""
    chunks = [
        _text_chunk("DOC_1", "FL-XS-WI-007《吸塑扫码报检》作业指导书.docx", 0.948),
        _text_chunk("DOC_2", "FL-XS-WI-007《吸塑扫码报检》作业指导书.pdf", 0.928),
    ]
    sources = _extract_sources(chunks)
    assert len(sources) == 1
    assert sources[0]["title"].endswith(".docx")


def test_sources_collapse_backfills_page_locator():
    """docx 首位无页码、被折叠的 pdf 孪生有 → 定位信息回填，不因折叠丢失。"""
    chunks = [
        _text_chunk("DOC_1", "某作业指导书.docx", 0.95),                  # docx 无原生页码
        _text_chunk("DOC_2", "某作业指导书.pdf", 0.93, page_num=5),
    ]
    sources = _extract_sources(chunks)
    assert len(sources) == 1
    assert sources[0]["section"] == "第5页"


def test_sources_kept_section_not_overwritten():
    """保留行已有定位时不被折叠行覆盖。"""
    chunks = [
        _text_chunk("DOC_1", "某制度.docx", 0.9, section="第4条"),
        _text_chunk("DOC_2", "某制度.pdf", 0.8, page_num=2),
    ]
    sources = _extract_sources(chunks)
    assert sources[0]["section"] == "第4条"


def test_sources_different_titles_kept():
    chunks = [
        _text_chunk("DOC_1", "A7考勤管理标准.docx", 0.846),
        _text_chunk("DOC_2", "员工手册202108月.docx", 0.750),
    ]
    assert len(_extract_sources(chunks)) == 2


def test_sources_empty_titles_not_collapsed():
    """无标题文档退回 doc_id 区分，互不相关的空标题文档不被折叠。"""
    chunks = [
        _text_chunk("DOC_1", "", 0.8),
        _text_chunk("DOC_2", "", 0.7),
    ]
    assert len(_extract_sources(chunks)) == 2
