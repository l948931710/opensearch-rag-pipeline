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
        simulate=False,
        rag=types.SimpleNamespace(main_hit_revalidate=revalidate, allowed_depts_acl=acl),
    )


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)

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


def _run(monkeypatch, chunks, imgs, rows, *, cfg=None, db_raises=False):
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: cfg or _cfg())
    monkeypatch.setattr(retriever, "_fetch_cosurface_images",
                        lambda *a, **k: list(imgs))

    def _conn():
        if db_raises:
            raise RuntimeError("RDS unreachable")
        return _Conn(rows)

    monkeypatch.setattr(_db, "_get_db_conn", _conn)
    return retriever.cosurface_doc_images("q", chunks)


def _images_of(out):
    return [c["visual_summary"] for c in out if c.get("chunk_type") == "image"]


# ── 版本轴（稳态缺陷，可达性最高）────────────────────────────────────────────

def test_old_version_image_is_dropped(monkeypatch):
    """当前版本 v3 的正文，绝不配 v2 的图。RDS 侧刻意判"有效"，所以只有版本轴能解释这次丢弃。"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 2, cid="c_old", pk=11)],
               [_row("c_old", 1, 11)])
    assert _images_of(out) == [], "旧版本图必须被丢弃"


def test_same_version_image_is_kept(monkeypatch):
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 3, cid="c_now", pk=11)],
               [_row("c_now", 1, 11)])
    assert _images_of(out) == ["c_now"]


def test_unknown_version_dropped_by_version_axis_even_when_authority_active(monkeypatch):
    """f9e59ea（fail-closed 改回）后：version_no 缺失（parse 缺省 0）由**版本轴**丢弃。

    RDS 行刻意置 is_active=1（权威侧判活），故这次丢弃只能由版本轴解释——与
    test_image_cosurface 的 fail-closed 钉子互为犄角。
    （订正 2026-08-04 独立核验点名的过期测试：旧名字/docstring 仍在宣传被 f9e59ea
    推翻的 fail-open 论证「版本轴放行、4c 拦下」，且靠 is_active=0 的巧合续绿。）"""
    out = _run(monkeypatch, [_text("A", 3)], [_img("A", 0, cid="c_unk", pk=11)],
               [_row("c_unk", 1, 11)])          # is_active=1：权威侧放行，版本轴独自承重
    assert _images_of(out) == []


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
