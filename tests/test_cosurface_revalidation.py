# -*- coding: utf-8 -*-
"""附录B — cosurface 补图必须走与主命中同一条权威复核链（2026-08-03）。

修复前：`_fetch_cosurface_images` 的结果从 HA3 **直接**进最终 chunks —— cosurface 是
`retrieve_and_enrich` 的最后一个检索步骤（其后只有 `_attach_doc_dates` 这种纯元数据），
全程没有任何下游复核。"doc 已授权"不能替代"这条 image 行有效"：

  · **旧版本图**（可达性最高，且是稳态而非竞态）：补图 filter 不带 `version_no`，
    `_cosurface_top_doc_ids` 又把版本轴折叠掉 ⇒ 当前版本正文能配上旧版本的图；
  · **孤儿 PK**：同版本重切给同一 chunk_id 重分配 `chunk_meta.id`，旧 PK 既不在
    deactivate 的 `version_no < N` 覆盖面、也不在按 version 删的 PENDING_DELETE 里；
  · **真 TOCTOU**：主命中复核完成【之后】才发生的 retire / visibility 收紧 / spot 隔离。

图片是本系统 PII 风险最高的模态（漏斗有专门的 QUARANTINE_SENSITIVE 判决），
"已撤下的图仍被投放"正是该漏斗要防的失败。
"""
import types
from unittest.mock import MagicMock, patch

import pytest

from opensearch_pipeline import retriever


def _cfg(*, revalidate=True, acl=False):
    return types.SimpleNamespace(
        simulate_db=False,
        simulate_opensearch=False,
        simulate=False,
        rag=types.SimpleNamespace(main_hit_revalidate=revalidate, allowed_depts_acl=acl),
    )


class _Cur:
    """SQL 感知的游标桩（2026-08-06）：cosurface 链上现在有**三种**权威查询 ——
    4c 的 chunk_meta 复核、版本门的 chunk 身份查询、以及 serving 版本的聚合解析。
    此前桩对任何 SQL 都回同一批行，行形态一混就是假绿/假红。"""

    def __init__(self, rows, vrows, srows):
        self.rows, self.vrows, self.srows = rows, vrows, srows
        self._kind = "acl"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "active_version_count" in sql:
            self._kind = "serving"
        elif "cm.chunk_type" in sql:
            self._kind = "identity"
        else:
            self._kind = "acl"

    def fetchall(self):
        return {"serving": self.srows, "identity": self.vrows}.get(self._kind, self.rows)


class _Conn:
    def __init__(self, rows, vrows, srows):
        self.rows, self.vrows, self.srows = rows, vrows, srows

    def cursor(self, *a, **k):
        return _Cur(self.rows, self.vrows, self.srows)

    def close(self):
        pass


def _text(doc_id, ver):
    return {"doc_id": doc_id, "version_no": ver, "chunk_index": 0,
            "chunk_type": "text_chunk", "chunk_text": "t", "title": "T"}


def _img(doc_id, ver, *, cid, pk, perm="public", owner=""):
    return {"doc_id": doc_id, "version_no": ver, "chunk_index": 9, "chunk_type": "image",
            "source_image": f"oss/{cid}.png", "visual_summary": cid,
            "chunk_id": cid, "id": str(pk), "permission_level": perm, "owner_dept": owner}


def _row(cid, active, pk, perm="public", owner=""):
    """chunk_meta 权威行：(chunk_id, is_active, permission_level, owner_dept, id)"""
    return (cid, active, perm, owner, pk)


def _vrow(cid, pk, ver, ctype="image", doc="A"):
    """版本门的 chunk 身份行：(chunk_id, id, chunk_type, version_no, doc_id)"""
    return (cid, str(pk), ctype, ver, doc)


def _srow(doc, complete_max, active_max, n_ver):
    """serving 版本解析行：(doc_id, complete_max, active_max, active_version_count)

    `complete_max=None` + `n_ver>1` ⇒ 多 active 版本且都不完整 ⇒ 歧义 ⇒ fail-closed。
    """
    return (doc, complete_max, active_max, n_ver)


def _run(monkeypatch, chunks, imgs, rows, *, cfg=None, db_raises=False,
         vrows=None, srows=None):
    """vrows/srows=None ⇒ 自动按"每张图都是 serving 版本"生成，让**其它轴**独自承重。
    版本轴自己的用例显式传。"""
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: cfg or _cfg())
    monkeypatch.setattr(retriever, "_fetch_cosurface_images",
                        lambda *a, **k: list(imgs))
    if vrows is None:
        vrows = [_vrow(i.get("chunk_id"), i.get("id"), i.get("version_no"),
                       i.get("chunk_type", "image"), i.get("doc_id", "A"))
                 for i in imgs if i.get("chunk_id") or i.get("id")]
    if srows is None:
        _vers = {(i.get("doc_id", "A"), i.get("version_no")) for i in imgs}
        srows = [_srow(d, v, v, 1) for d, v in _vers]

    def _conn():
        if db_raises:
            raise RuntimeError("RDS unreachable")
        return _Conn(rows, vrows, srows)

    monkeypatch.setattr(_db, "_get_db_conn", _conn)
    return retriever.cosurface_doc_images("q", chunks)


def _images_of(out):
    return [c["visual_summary"] for c in out if c.get("chunk_type") == "image"]


# ── 版本轴（稳态缺陷，可达性最高）────────────────────────────────────────────

def test_old_version_image_is_dropped(monkeypatch):
    """当前版本 v3 的正文，绝不配 v2 的图。RDS 侧刻意判"有效"，所以只有版本轴能解释这次丢弃。

    2026-08-06：权威值改为**该 doc 最高的「完整 INDEXED」active 版本**（原先取"主结果里
    首个非零 version_no"——既非权威、胜出者还取决于排序；中途提过的
    `document_meta.current_version_no` 与 `MAX(active)` 都已被证伪，理由见
    `retriever._resolve_serving_versions` 的 docstring）。故本条现由 vrows/srows 显式建模
    "该 chunk 是 v2、而该 doc 的 serving 版本是 v3"。"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 2, cid="c_old", pk=11)],
               [_row("c_old", 1, 11)], vrows=[_vrow("c_old", 11, 2)],
               srows=[_srow("A", 3, 3, 1)])
    assert _images_of(out) == [], "旧版本图必须被丢弃"


def test_same_version_image_is_kept(monkeypatch):
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c_now", pk=11)],
               [_row("c_now", 1, 11)])
    assert _images_of(out) == ["c_now"]


def test_unknown_version_dropped_by_version_axis_even_when_authority_active(monkeypatch):
    """权威侧版本无从复核 ⇒ 由**版本轴**丢弃（fail-closed）。

    RDS 行刻意置 is_active=1（权威侧放行），故这次丢弃只能由版本轴解释。
    2026-08-06：`srows=(None, 4, 2)` 建模**真歧义态** —— 该 doc 有两个 active 版本、
    且都不是「完整 INDEXED」（vN 被 ACL/标题投影标脏 + vN+1 在 DAG3 部分推送成功）。
    此时 serving 版本无从裁决，只能 fail-closed；擅自取 MAX(active) 会选中那个
    **系统刻意没有提升**的 vN+1，属错版本投放。"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c_unk", pk=11)],
               [_row("c_unk", 1, 11)],          # is_active=1：权威侧放行，版本轴独自承重
               vrows=[_vrow("c_unk", 11, 3)],
               srows=[_srow("A", None, 4, 2)])   # 多 active 版本且都不完整 ⇒ 歧义
    assert _images_of(out) == []


def test_version_gate_ignores_ha3_returned_version(monkeypatch):
    """★ 版本轴取 **RDS 的 version_no**，绝不取 HA3 返回的那个。

    HA3 投影正是被复核的对象；拿它当依据等于让投影自证。本条把两者刻意对立：
    HA3 说"我是 v3、和正文一样"，RDS 说"这条其实是 v2，当前是 v3" ⇒ 必须丢。
    （删掉版本门、或把权威换回 `r.get("version_no")`，本条即红。）"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c_lie", pk=11)],
               [_row("c_lie", 1, 11)], vrows=[_vrow("c_lie", 11, 2)],
               srows=[_srow("A", 3, 3, 1)])
    assert _images_of(out) == [], "HA3 自报版本被当成了权威"


# ── 4c 权威轴 ────────────────────────────────────────────────────────────────

def test_inactive_image_row_is_dropped(monkeypatch):
    """retire / visibility 收紧 / spot 隔离都是把 chunk 置 is_active=0。"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c1", pk=11)],
               [_row("c1", 0, 11)])
    assert _images_of(out) == []


def test_orphan_pk_image_is_dropped(monkeypatch):
    """B7 物理 PK 轴：HA3 返回的 id 与 RDS 现行 chunk_meta.id 不一致 = 同版本重切的孤儿行。"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c1", pk=999)],
               [_row("c1", 1, 111)])
    assert _images_of(out) == []


def test_recheck_runs_before_best_by_doc_so_next_image_promotes(monkeypatch):
    """复核必须在 best_by_doc **之前**。

    排第一的图失效、排第二的有效 ⇒ 必须递补出第二张。若复核放在选图之后，
    该文档会从"配了张坏图"直接变成"没有图"，本来能出图的文档白白丢掉配图。
    """
    out = _run(monkeypatch, [_text("A", 3)],
               [_img("A", 3, cid="bad", pk=11), _img("A", 3, cid="good", pk=22)],
               [_row("bad", 0, 11), _row("good", 1, 22)])
    assert _images_of(out) == ["good"], "失效图被丢后，同文档次优图必须递补"


# ── strict：补图与主命中的失败语义**有意不同** ────────────────────────────────

def test_authority_unavailable_drops_all_images_but_keeps_text(monkeypatch):
    """权威不可达 ⇒ 补图全丢，正文答案原样返回。

    补图是可选增强：失败的代价只是没有配图，没有任何理由拿未复核的图去赌。
    """
    chunks = [_text("A", 3)]
    out = _run(monkeypatch, chunks, [_img("A", 3, cid="c1", pk=11)], [], db_raises=True)
    assert _images_of(out) == []
    assert out == chunks, "正文必须逐条保留"


def test_main_hit_path_remains_fail_open(monkeypatch):
    """不传 strict 的主命中路径**行为不变**——RDS 不可达时保留结果，绝不放大成全站无答案。"""
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg())

    def _boom():
        raise RuntimeError("RDS unreachable")

    monkeypatch.setattr(_db, "_get_db_conn", _boom)
    hits = [_img("A", 3, cid="c1", pk=11)]
    assert retriever._revalidate_main_hits(hits) == hits
    assert retriever._revalidate_main_hits(hits, strict_on_unavailable=True) == []


def test_strict_does_not_fire_when_revalidate_is_configured_off(monkeypatch):
    """strict 只挡"复核**没跑成**"，不挡"复核**按配置不跑**"。

    运维把 main_hit_revalidate 关掉是显式选择，补图不该因此比主命中更严（那会让
    一个性能/兼容开关意外变成"图全没了"的开关）。
    """
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg(revalidate=False))
    hits = [_img("A", 3, cid="c1", pk=11)]
    assert retriever._revalidate_main_hits(hits, strict_on_unavailable=True) == hits


# ── 有界过取 ────────────────────────────────────────────────────────────────

def test_cosurface_overfetches_to_leave_room_for_drops():
    """复核会丢候选；×2 的余量丢完常常一张不剩 ⇒ 过取到 ×4 给递补留空间。"""
    captured = {}

    class _C:
        def query(self, req):
            captured["top_k"] = req.top_k
            return MagicMock()

    with patch.object(retriever, "_get_ha3_client", return_value=_C()), \
         patch.object(retriever, "_parse_ha3_response", return_value=[]), \
         patch.object(retriever, "get_query_embedding", return_value=([0.1] * 8, [], [])):
        retriever._fetch_cosurface_images("q", ["A"], None, max_images=3)
    assert captured["top_k"] == 12, f"应过取到 max_images*4，实际 {captured['top_k']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── 守卫缺失补钉（2026-08-04 独立核验 B1：M6 整删 4b、M3b/M3c strict 出口全绿）──────

def test_cosurface_4b_revocation_hook_is_load_bearing(monkeypatch):
    """4b 调用点钉扎：整删 `_deny_revoked_cross_dept` 调用曾全量 4181 绿（零测试钉住）。

    真实撤销语义由 ACL 套件覆盖；本条只钉「cosurface 链上 4b 必须在场且其输出承重」
    ——哨兵过滤标记行，删掉调用点即红。"""
    calls = []

    def _sentinel(rows, *a, **k):
        calls.append(True)
        return [r for r in rows if r.get("chunk_id") != "c_revoked"]

    monkeypatch.setattr(retriever, "_deny_revoked_cross_dept", _sentinel)
    out = _run(monkeypatch, [_text("A", 3)],
               [_img("A", 3, cid="c_revoked", pk=11), _img("A", 3, cid="c_ok", pk=12)],
               [_row("c_revoked", 1, 11), _row("c_ok", 1, 12)])
    assert calls, "4b(_deny_revoked_cross_dept) 不在 cosurface 链上"
    assert _images_of(out) == ["c_ok"], "4b 的输出必须承重（丢弃行不得复活）"


def test_strict_empty_authority_rowset_drops_all(monkeypatch):
    """strict 出口②（:831 `if not rows`）：复核返回空集=权威不可用 ⇒ 补图全弃。
    （M3b 曾把该出口改回 fail-open 而全套件绿——本条即其钉子。）"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c1", pk=11)],
               rows=[])
    assert _images_of(out) == []


def test_strict_keyless_rows_drop_all(monkeypatch):
    """strict 出口③（:801 两轴无键）：chunk_id 与 id 双缺 ⇒ 无从复核，strict 全弃。
    （M3c 曾把该出口改回 fail-open 而 143 绿——本条即其钉子。）"""
    ghost = {"doc_id": "A", "version_no": 3, "chunk_index": 9, "chunk_type": "image",
             "source_image": "oss/ghost.png", "visual_summary": "ghost",
             "chunk_id": "", "id": "", "permission_level": "public", "owner_dept": ""}
    out = _run(monkeypatch, [_text("A", 3)], [ghost], [_row("cX", 1, 99)])
    assert _images_of(out) == []


# ── strict 的**逐行**收紧（2026-08-06；此前 strict 只在"整批"层面生效）────────────

def test_strict_keyless_row_inside_mixed_batch_is_dropped(monkeypatch):
    """★ 缺陷2：混合批里**单条**无键的行此前走 `kept.append(r)` 无条件保留。

    出口③只在"整批两轴都没有键"时才触发；只要同批里有一条带键的，整批就不走那个出口，
    无键行便原样穿过。实测 `[KEYLESS(A), KEYED(B)]` → `['A','B']` —— strict 对它完全失效。
    """
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg())
    monkeypatch.setattr(_db, "_get_db_conn", lambda: _Conn([_row("c_ok", 1, 12)], [], []))
    keyless = dict(_img("A", 3, cid="", pk=""), chunk_id="", id="")
    keyed = _img("A", 3, cid="c_ok", pk=12)
    out = retriever._revalidate_main_hits([keyless, keyed], strict_on_unavailable=True)
    assert [c.get("chunk_id") for c in out] == ["c_ok"], "无键行在混合批里穿过了 strict"
    # 非 strict 主命中路径行为不变（fail-open 保留）——收紧不得外溢到主命中
    out2 = retriever._revalidate_main_hits([keyless, keyed])
    assert len(out2) == 2, "主命中路径不该跟着变严"


def test_strict_row_missing_ha3_id_skips_no_longer(monkeypatch):
    """★ 缺陷3：strict 档下缺 HA3 `id` 此前**跳过**物理 PK 轴（`_ha3_pk not in (None,"")`
    短路）—— 等于对"同版本重切的孤儿 PK"这条轴完全失明，而 strict 的语义正是
    「补图失败只是没配图，绝不拿未复核的图去赌」，缺字段就是"未复核"的一种。"""
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg())
    monkeypatch.setattr(_db, "_get_db_conn", lambda: _Conn([_row("c1", 1, 11)], [], []))
    no_pk = dict(_img("A", 3, cid="c1", pk=11), id="")
    assert retriever._revalidate_main_hits([no_pk], strict_on_unavailable=True) == []
    # 反证锚：带上 id 且与权威一致 ⇒ 照常放行（否则上一行是空转的）
    ok = _img("A", 3, cid="c1", pk=11)
    assert retriever._revalidate_main_hits([ok], strict_on_unavailable=True) == [ok]
    # 主命中（非 strict）不受影响：缺 id 仍按"只跳过这一项比对"放行
    assert retriever._revalidate_main_hits([no_pk]) == [no_pk]
