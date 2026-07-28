# -*- coding: utf-8 -*-
"""B3（2026-07-25）：xlsx 步骤卡的图↔步骤绑定优先级。

缺陷：作者显式写的「如图N」被**位置计数器**的 图N→步骤N 直连抢先覆盖 —— 而同一段代码的
注释自述的是相反顺序（"(b) 命中后的绑定走的是 figure_refs 分支"）。两类确定性错绑：
  · 步骤6 写「如图3」而步骤3 恰好空着 → 图3 被绑到步骤3；
  · 任意一句「如图2」就让**全篇**从未被引用的图按序号硬绑（figure_no_meaningful 是文档级布尔）。
错绑经 step_card.extra.image_refs → chunk_meta.image_refs_json 直达钉钉卡片，一线看到错图。

三步修复：① meaningful 改 per-figure；② figure_refs 精确匹配优先；③ 显式引用可追加为
第二张图，并按图片实体身份去重（bound_nos 只防步骤重复占用，防不住同图重复追加）。

⚠️ 存量生效需**冻结分类输入 + 逐文档 count+type_mix manifest 门**的重灌（user-gated），
本批只交付代码。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import opensearch_pipeline.pipeline_nodes as pn


def _binding_src() -> str:
    return inspect.getsource(pn.node_chunk_documents)


def test_b3_figure_refs_now_matched_before_positional_shortcut():
    """精确匹配必须排在数字直连之前 —— 这正是注释一直宣称、代码却相反的那条。"""
    src = _binding_src()
    i_refs = src.index('if fno in (c.extra.get("figure_refs") or [])')
    i_num = src.index("target = step_by_no.get(int(mnum.group(1)))")
    assert i_refs < i_num, "figure_refs 精确匹配必须先于位置计数器直连"


def test_b3_meaningfulness_is_per_figure_not_document_wide():
    """文档级布尔会让任意一句「如图2」把全篇未被引用的图都按序号硬绑。"""
    src = _binding_src()
    assert "def _fno_meaningful(" in src
    assert "_fno_meaningful(fno)" in src
    # 旧的文档级布尔不得再作为判据存在
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "figure_no_meaningful" not in code


def test_b3_meaningfulness_conditions_match_the_documented_intent():
    """(a) 某 fno 被多 asset 共用（作者手工标的语义标签）或 (b) 被步骤文本显式引用。"""
    src = _binding_src()
    i = src.index("def _fno_meaningful(")
    body = src[i:i + 400]
    assert "fno_counts.get(fno, 0) > 1" in body      # (a)
    assert "fno in all_step_fig_refs" in body        # (b)


def test_b3_explicit_reference_can_append_as_second_image():
    """作者写了「如图N」就是要在这一步看到它 —— 即便该步骤已绑过图。"""
    src = _binding_src()
    assert "追加为**第二张图**" in src
    i = src.index("if target is None:\n                                    for c in step_cards:")
    seg = src[i:i + 300]
    assert "figure_refs" in seg and "bound_nos" not in seg, "第二张图分支不该再看 bound_nos"


# ═══════════════════ 图片实体去重 ═══════════════════

def test_b3_dedup_uses_xlsx_identity_contract():
    """xlsx 的载重身份是 filename + anchor_row（同 anchor 多图消歧靠它）。"""
    a = {"filename": "img2.png", "anchor_row": 12, "oss_key": "k1"}
    b = {"filename": "img2.png", "anchor_row": 12, "oss_key": "k2"}
    assert pn._same_image_ref(a, b)


def test_b3_same_filename_different_anchor_is_not_the_same_image():
    a = {"filename": "img2.png", "anchor_row": 12}
    b = {"filename": "img2.png", "anchor_row": 30}
    assert not pn._same_image_ref(a, b)


def test_b3_dedup_falls_back_to_oss_key_and_index():
    a = {"oss_key": "processing/assets/x.png", "image_index": 3}
    b = {"oss_key": "processing/assets/x.png", "image_index": 3}
    assert pn._same_image_ref(a, b)
    assert not pn._same_image_ref(a, {"oss_key": "processing/assets/x.png", "image_index": 4})


def test_b3_dedup_is_applied_at_the_append_site():
    src = _binding_src()
    assert "_same_image_ref(_entry, _e)" in src


def test_b3_unrelated_entries_are_not_merged():
    assert not pn._same_image_ref({"filename": "a.png", "anchor_row": 1},
                                  {"filename": "b.png", "anchor_row": 1})
    assert not pn._same_image_ref({}, {})
