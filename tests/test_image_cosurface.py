# -*- coding: utf-8 -*-
"""
test_image_cosurface.py — 图片召回增强（lever A）单元测试

验证 retriever.cosurface_doc_images：
  - 把同文档最相关 image chunk **插入到首个正文 chunk 之后**（不是追加末尾，
    否则 <<IMG:N>> 提示会被 _format_context 的 max_context_chars 截断）
  - 每个文档只补一次、总量受 max_images 限制
  - 结果已含 image chunk 时短路返回（不打扰可视化查询）
  - 任意异常 fail-open 返回原 chunks
  - retrieve_and_enrich 仅在 cosurface_images=True 且全局开关开启时调用

HA3 / embedding 层全部 mock，无需真实服务。
"""

from unittest.mock import patch, MagicMock

from opensearch_pipeline import retriever


# ⚠️ 桩里必须带 `version_no`（2026-08-04 补）：补图的版本轴是 **fail-closed** —— 两边
# 版本都已知且相等才补。此前桩完全没建模该字段，于是这些用例是在一个**现实中不存在**
# 的形态上跑（真实 HA3 表有 version_no INT64，见 docs/ha3_stg_table_spec.md 的生产表实时
# 导出；`HA3_DEFAULT_OUTPUT_FIELDS` 也一直请求它）。补齐后它们验的才是真实形态。
def _chunks(ver=3):
    return [
        {"doc_id": "A", "version_no": ver, "chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "a0", "title": "DocA"},
        {"doc_id": "B", "version_no": ver, "chunk_index": 0, "chunk_type": "text_chunk", "chunk_text": "b0", "title": "DocB"},
        {"doc_id": "A", "version_no": ver, "chunk_index": 1, "chunk_type": "text_chunk", "chunk_text": "a1", "title": "DocA"},
    ]


_IMGS = [
    {"doc_id": "A", "version_no": 3, "chunk_index": 9, "chunk_type": "image", "source_image": "oss/a.png", "visual_summary": "imgA"},
    {"doc_id": "B", "version_no": 3, "chunk_index": 9, "chunk_type": "image", "source_image": "oss/b.png", "visual_summary": "imgB"},
]


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_inserts_after_first_sibling(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = list(_IMGS)
    out = retriever.cosurface_doc_images("q", _chunks())
    seq = [(c["doc_id"], c["chunk_type"]) for c in out]
    # image inserted right after the FIRST text chunk of each doc; A's 2nd chunk gets none
    assert seq == [
        ("A", "text_chunk"), ("A", "image"),
        ("B", "text_chunk"), ("B", "image"),
        ("A", "text_chunk"),
    ]
    assert sum(1 for c in out if c["chunk_type"] == "image") == 2


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_caps_max_images(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = list(_IMGS)
    out = retriever.cosurface_doc_images("q", _chunks(), max_images=1)
    assert sum(1 for c in out if c["chunk_type"] == "image") == 1


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_skips_image_without_source(mock_emb, mock_client, mock_parse):
    mock_client.return_value = MagicMock()
    mock_parse.return_value = [{"doc_id": "A", "chunk_index": 9, "chunk_type": "image", "source_image": ""}]
    out = retriever.cosurface_doc_images("q", _chunks())
    assert all(c["chunk_type"] != "image" for c in out)  # empty source_image → not inserted


@patch("opensearch_pipeline.retriever.get_query_embedding")
def test_short_circuits_when_image_present(mock_emb):
    chunks = _chunks() + [{"doc_id": "A", "chunk_index": 5, "chunk_type": "image", "source_image": "x"}]
    out = retriever.cosurface_doc_images("q", chunks)
    assert out is chunks               # unchanged object
    mock_emb.assert_not_called()       # no HA3 query issued


@patch("opensearch_pipeline.retriever.get_query_embedding", side_effect=RuntimeError("boom"))
def test_fail_open_on_error(mock_emb):
    chunks = _chunks()
    out = retriever.cosurface_doc_images("q", chunks)
    assert out == chunks               # error swallowed, original returned


@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
@patch("opensearch_pipeline.retriever.cosurface_doc_images", side_effect=lambda q, c, **k: c)
@patch("opensearch_pipeline.retriever.expand_step_context", side_effect=lambda c, q: c)
@patch("opensearch_pipeline.retriever.stitch_neighbor_chunks", side_effect=lambda c, window=1: c)
@patch("opensearch_pipeline.retriever.search_chunks")
def test_retrieve_and_enrich_opt_in_gating(mock_search, mock_stitch, mock_expand, mock_cosurf, mock_emb):
    mock_search.return_value = _chunks()
    # default (False) → cosurface NOT called
    retriever.retrieve_and_enrich("q")
    mock_cosurf.assert_not_called()
    # opt-in True → cosurface called, and the embedding is computed once + threaded through
    retriever.retrieve_and_enrich("q", cosurface_images=True)
    assert mock_cosurf.called
    # search_chunks receives the pre-computed embedding (no re-embed inside)
    assert mock_search.call_args.kwargs.get("query_embedding") is not None


# ── 版本轴 fail-closed（2026-08-04，复核 codex 对 e5e29ce 的建议后改回）─────────

def _gate_env(monkeypatch, *, vrows, srows, imgs, simulate_db=False, simulate_os=False):
    """把版本门接到一个可控的权威表上（默认真 DB 档）。就地打桩，返回 None。

    ⚠️ `_fetch_cosurface_images` **必须一并打桩**：本函数替换的 cfg 是个
    SimpleNamespace，没有 `alibaba_vector` —— 取图会抛异常走 cosurface 的 fail-open
    出口，于是"没有图"恒成立，「图被丢弃」类断言**全部变成空转**（本文件 2026-08-06
    首版就踩了这个，`test_stale_version_image_is_dropped` 假绿了一轮）。
    """
    import types
    from opensearch_pipeline import db as _db

    monkeypatch.setattr(retriever, "_fetch_cosurface_images",
                        lambda *a, **k: [dict(i) for i in imgs])

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            self.k = ("serving" if "active_version_count" in sql
                      else "identity" if "cm.chunk_type" in sql else "acl")

        def fetchall(self):
            return {"serving": srows, "identity": vrows}.get(getattr(self, "k", ""), [])

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(retriever, "get_config", lambda: types.SimpleNamespace(
        simulate_db=simulate_db, simulate_opensearch=simulate_os, simulate=False,
        rag=types.SimpleNamespace(main_hit_revalidate=False, allowed_depts_acl=False)))
    monkeypatch.setattr(_db, "_get_db_conn", lambda: _Conn())


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_stale_version_image_is_dropped(mock_emb, mock_client, mock_parse, monkeypatch):
    """★ 旧版本的图**不得**配到当前版本的正文上。

    双活版本窗口（新版已 INDEXED、旧版尚未 deactivate）里旧版本 chunk 仍 `is_active=1`
    ⇒ 它能干净通过 4c（`_revalidate_main_hits` 只比 chunk_id/is_active/permission/owner/id，
    **不看版本**）。版本门是唯一能挡住它的一道。

    2026-08-06：权威值改为**该 doc 最高的「完整 INDEXED」active 版本**
    （`_resolve_serving_versions`）；HA3 自报的 version_no 不再参与裁决（它正是被复核的
    对象）。故本条由权威表桩建模"该 chunk 是 v2、serving 是 v3"，并刻意让 HA3 侧**谎报**
    v3 —— 若权威取错边，本条即红。
    """
    mock_client.return_value = MagicMock()
    img = dict(_IMGS[0], version_no=3, chunk_id="cA", id="11")   # HA3 谎报 v3
    # 先立反证锚：权威说"当前版本"时这张图**确实出得来**，否则下面的断言是空转的。
    _gate_env(monkeypatch, vrows=[("cA", "11", "image", 3, "A")],
              srows=[("A", 3, 3, 1)], imgs=[img])
    ok = retriever.cosurface_doc_images("q", _chunks(ver=3))
    assert [c for c in ok if c["chunk_type"] == "image"], "反证锚失败：当前版本的图本就出不来"

    _gate_env(monkeypatch, vrows=[("cA", "11", "image", 2, "A")],
              srows=[("A", 3, 3, 1)], imgs=[img])  # RDS: 本行 v2、serving 是 v3
    out = retriever.cosurface_doc_images("q", _chunks(ver=3))
    assert not [c for c in out if c["chunk_type"] == "image"], "旧版本的图被配进了当前版本正文"


@patch("opensearch_pipeline.retriever._parse_ha3_response")
@patch("opensearch_pipeline.retriever._get_ha3_client")
@patch("opensearch_pipeline.retriever.get_query_embedding", return_value=([0.1] * 8, [], []))
def test_version_gate_sim_tiers(mock_emb, mock_client, mock_parse, monkeypatch):
    """★ SIM **两档不可合并**（codex 第二轮点名）。

    · 全模拟（假 DB + 假 HA3）⇒ **no-op**：没有真数据面，门无意义，强行 fail-closed
      只会让 `make sim-all` 与所有演示路径一张图都不出；
    · **半模拟**（假 DB + **真 HA3**）⇒ **fail-closed**：这一档
      `_validate_environment_target_consistency` 会**跳过 RDS 目标校验**，等于完全没有
      守卫 —— 不能 warning 后放行。

    两档合并成任意一边，本条即红。
    """
    mock_client.return_value = MagicMock()
    img = dict(_IMGS[0], version_no=3, chunk_id="cA", id="11")

    # 两档喂**同样的**权威表（空集 = 查不到任何行），差别只在 simulate_opensearch。
    _gate_env(monkeypatch, vrows=[], srows=[], imgs=[img], simulate_db=True, simulate_os=True)
    out = retriever.cosurface_doc_images("q", _chunks(ver=3))
    assert [c for c in out if c["chunk_type"] == "image"], "全模拟档应 no-op（图不该被拦）"

    _gate_env(monkeypatch, vrows=[], srows=[], imgs=[img], simulate_db=True, simulate_os=False)
    out = retriever.cosurface_doc_images("q", _chunks(ver=3))
    assert not [c for c in out if c["chunk_type"] == "image"], "半模拟档必须 fail-closed"


def test_local_os_fallback_selects_version_no():
    """🔴 本地 OpenSearch 回退路径必须取 `version_no`。

    写侧 `Chunk.to_opensearch_doc`（chunker.py:205）一直在写它，是**读侧**漏了 ⇒ 下游恒得 0，
    而 `stitch_neighbor_chunks` 用它建 `WHERE version_no=%s`
    ⇒ 该路径的邻居拼接自 2026-06-28 起一直**静默全空**（fail-open 风格让它不报错）。
    """
    import pathlib
    src = pathlib.Path("opensearch_pipeline/retriever.py").read_text(encoding="utf-8")
    i = src.index('"_source": ["chunk_id"')
    block = src[i:i + 400]
    assert '"version_no"' in block, "本地 OS 回退的 _source 漏了 version_no（邻居拼接会静默全空）"
    assert '"version_no": src.get("version_no")' in src, "本地 OS 回退的结果字典没回填 version_no"
