# -*- coding: utf-8 -*-
"""方案 F 的**功能级**验收：生产姿态下 skip-gate 路径仍能看见图集漂移。

为什么单独一个文件、为什么必须是功能级：

生产 `RAG_SKIP_UNCHANGED_REINGEST=true`（`dataworks_nodes/stage1_node.py:36-41`）。
正文 hash 与上一版本一致时，`node_build_canonical` 在 skip-gate 里 `continue`，该篇被
排除出 `ctx["canonicals"]`，**永远到不了** `node_write_chunk_meta`。而"同一份文件 +
正文一字未改 + VLM 判定漂移导致存活图集变少"正是选项 A 实测到的那类事件
（6.5% 图翻面，全部单向 ROUTE_TO_VECTOR → DISCARD，4 张是 GT 期望的步骤配图）。
只挂 DAG 2 会把要抓的核心场景整个漏掉；源码级 grep 断言证明不了这条链真的通。

本文件因此驱动真实的 `node_build_canonical`，断言：**该篇被 skip 且不进 canonicals
的同时，ASSET_SET_DIFF 审计行确实写出**。
"""
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock

import opensearch_pipeline.audit_log as audit_log
import opensearch_pipeline.pipeline_nodes as pn

_BODY = "工序说明：扫码报检操作步骤，正文一字未改（≥32 字符以通过空 canonical 守卫）。"


class _AnyHash(str):
    """与任何 canonical_sha256 相等的哨兵。

    这样测试就不必复刻 `normalize_text` + sha256 的实现细节 —— 本文件要钉的是
    「skip 发生时有没有留痕」，不是哈希怎么算的。
    """

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = str.__hash__


class _FakeConn:
    """按 SQL 形状应答的最小 DB 替身；记录所有 execute 供断言。"""

    def __init__(self, baseline_row=(1, "processing/canonical/D/v1/content.json", "rawhash")):
        self.calls = []
        self.baseline_row = baseline_row
        self._last = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = sql

    def fetchone(self):
        s = self._last or ""
        if "canonical_sha256 IS NOT NULL" in s:          # skip-gate 的 prior SELECT
            return (1, _AnyHash("prior"))
        if "index_status='SUCCESS'" in s:                 # asset_set_diff 的基线 SELECT
            return self.baseline_row
        return None

    def fetchall(self):
        return []

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _asset(idx, status="ROUTE_TO_VECTOR"):
    return {"image_index": idx, "status": status, "oss_key": f"img/{idx}.png"}


@pytest.fixture
def harness(monkeypatch):
    """生产姿态：skip 开、非 simulate、真实 raw checksum 相同。"""
    monkeypatch.setenv("RAG_SKIP_UNCHANGED_REINGEST", "true")
    monkeypatch.delenv("RAG_DEDUP_CROSS_DOC", raising=False)
    fake = _FakeConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: fake)
    monkeypatch.setattr(pn, "_get_oss_bucket", lambda *a, **kw: (MagicMock(), False))

    audits = []
    monkeypatch.setattr(audit_log, "write_audit",
                        lambda **kw: audits.append(kw))

    _MISSING = object()

    def _run(cur_assets, prev_assets=_MISSING, cur_checksum="rawhash",
             prev_canonical=_MISSING):
        # 上一版本的 canonical。prev_canonical 可整体指定（用于"缺 assets 键"这种老格式）；
        # 否则由 prev_assets 构造，None 表示读取失败。
        if prev_canonical is _MISSING:
            prev_canonical = ({"doc_id": "D", "version_no": 1, "assets": prev_assets}
                              if prev_assets is not None else None)
        monkeypatch.setattr(pn, "_fetch_canonical_json", lambda key: prev_canonical)
        result = SimpleNamespace(
            doc_id="D", version_no=2, source_key="raw/dept/sop.pdf", file_ext=".pdf",
            extract_method="native", title="扫码报检作业指导书", text=_BODY,
            text_length=len(_BODY), blocks=[], page_count=3, ocr_required=False,
            ocr_status="NOT_REQUIRED", warnings=[], assets=cur_assets)
        ctx = {"extractions": [result], "simulate_db": False,
               "_raw_checksum": {("D", 2): cur_checksum}}
        pn.node_build_canonical(ctx)
        return ctx, audits, fake

    return _run


def _diff_audits(audits):
    return [a for a in audits if a.get("action_type") == "ASSET_SET_DIFF"]


# ── 核心验收 ────────────────────────────────────────────────────────────────

def test_skip_gate_still_emits_asset_set_diff(harness):
    """正文没变 + 图少了 12/23/29 ⇒ 该篇被 skip，但**仍写出一次 ASSET_SET_DIFF**。"""
    import json
    ctx, audits, fake = harness(
        cur_assets=[_asset(8)],
        prev_assets=[_asset(8), _asset(12), _asset(23), _asset(29)])

    # ① skip 行为本身不受影响（本篇不进 canonicals、状态写了 SKIPPED_DUPLICATE）
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", [])), \
        "skip 决策被改动了 —— 方案 F 必须是纯旁路"
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)

    # ② 漂移可见
    diffs = _diff_audits(audits)
    assert len(diffs) == 1, f"skip 路径没写出 ASSET_SET_DIFF（实得 {len(diffs)} 条）"
    ev = json.loads(diffs[0]["message"])
    assert ev["kind"] == "same_source_drift"          # 同源 + 完整 + 有 removal ⇒ 高置信
    assert ev["removed"] == [12, 23, 29] and ev["n_removed"] == 3
    assert ev["observed_stage"] == "SKIP_GATE"
    assert diffs[0]["doc_id"] == "D" and diffs[0]["version_no"] == 2


def test_skip_gate_logs_a_visible_line_on_high_confidence(harness, capsys):
    harness(cur_assets=[_asset(8)],
            prev_assets=[_asset(8), _asset(12), _asset(23), _asset(29)])
    out = capsys.readouterr().out
    assert "[asset-drift]" in out and "少了 3 张" in out


def test_unchanged_asset_set_writes_nothing(harness):
    """图集没变 ⇒ 不写审计噪声（每天全量重扫都会走这条路径）。"""
    _ctx, audits, _fake = harness(cur_assets=[_asset(8), _asset(12)],
                                  prev_assets=[_asset(12), _asset(8)])
    assert _diff_audits(audits) == []


def test_changed_raw_file_is_not_high_confidence(harness):
    """raw checksum 不同 ⇒ 只能称 source_changed，不得声称"图被漂移吃掉了"。"""
    import json
    _ctx, audits, _fake = harness(cur_assets=[_asset(8)],
                                  prev_assets=[_asset(8), _asset(12)],
                                  cur_checksum="different-raw-hash")
    ev = json.loads(_diff_audits(audits)[0]["message"])
    assert ev["kind"] == "source_changed"


def test_unreadable_prev_canonical_yields_no_event(harness):
    """上一版 canonical 读不到 ⇒ unknown ⇒ **绝不**伪造成"整篇图没了"。"""
    _ctx, audits, _fake = harness(cur_assets=[_asset(8)], prev_assets=None)
    assert _diff_audits(audits) == []


def test_missing_assets_key_in_prev_yields_no_event(harness):
    """老版本 canonical 根本没有 assets 键（早期格式）⇒ 同样按 unknown 处理。

    注意与上一条的区别：那条是**读不到文件**，这条是**读到了但没有该键** —— 两者都
    必须落到 unknown，不能因为"读成功了"就把它当成空集从而伪造全量 removal。
    """
    _ctx, audits, _fake = harness(cur_assets=[_asset(8)],
                                  prev_canonical={"doc_id": "D", "version_no": 1})
    assert _diff_audits(audits) == []


def test_prev_with_explicit_empty_assets_reports_additions_not_removals(harness):
    """显式 `assets: []` 是**真空集**（不是 unknown）⇒ 本版新增的图应报为 added。"""
    import json
    _ctx, audits, _fake = harness(cur_assets=[_asset(8)],
                                  prev_canonical={"doc_id": "D", "version_no": 1,
                                                  "assets": []})
    ev = json.loads(_diff_audits(audits)[0]["message"])
    assert ev["added"] == [8] and ev["n_removed"] == 0
    assert ev["kind"] != "same_source_drift", "只增不减不该报高置信丢图"


def test_audit_failure_does_not_break_ingest(harness, monkeypatch):
    """留痕失败必须 fail-open：skip 照常发生，运行不炸。"""
    monkeypatch.setattr(audit_log, "write_audit",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("audit down")))
    ctx, _audits, fake = harness(cur_assets=[_asset(8)],
                                 prev_assets=[_asset(8), _asset(12)])
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", []))
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


# ── P1-5（审查 2026-07-26）：资产集新增时撤销 skip ──────────────────────────
#
# `_canonical_sha256` 只哈希正文，而 `ROUTE_TO_VECTOR` 不产任何 block ⇒ 选项 C 与
# strip-stitch 救回来的图**不改变正文哈希** ⇒ 生产默认开着的 skip-gate 判
# SKIPPED_DUPLICATE 并 continue，救回的图永远到不了 chunk_meta/HA3。也就是说这两项改动
# 在「文件没变、只是想让图回来」这个**它们的目标场景**上完全无效。

def test_p15_additions_cancel_the_skip(harness):
    """新增 ⇒ 撤销 skip，该篇照常进 canonicals；且**不得**写 SKIPPED_DUPLICATE。"""
    ctx, audits, fake = harness(
        cur_assets=[_asset(8), _asset(12), _asset(31)],       # 比基线多两张
        prev_assets=[_asset(8)])
    assert any(c["doc_id"] == "D" for c in ctx.get("canonicals", [])), \
        "资产集新增时该篇必须照常入库，否则 C / strip-stitch 的收益永远拿不到"
    assert not any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls), \
        "撤销 skip 时不得留下 SKIPPED_DUPLICATE 状态行（判定必须在任何写之前）"
    assert _diff_audits(audits), "撤销 skip 不影响留痕"


def test_p15_removals_never_cancel_the_skip(harness):
    """**非对称判据的核心**：减少绝不撤销 skip。

    减少既可能是真漂移，也可能是环境缺依赖（py3.7 节点缺 PyMuPDF 时 `import fitz` 失败
    只 print、返回空 assets）。若据此放行，DAG 3 收尾会拿一个零图的新版本去停用正在服务的
    旧版本 —— 把观测变成事故。新增则不同：破损的抽取只会产出**更少**的图，永远不会更多。
    """
    ctx, _audits, fake = harness(cur_assets=[_asset(8)],
                                 prev_assets=[_asset(8), _asset(12), _asset(23)])
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", [])), "减少不得撤销 skip"
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


def test_p15_extraction_collapse_does_not_cancel_skip(harness):
    """极端形态：本轮抽取整段塌成空集（缺依赖）—— 必须仍然 skip，旧版本继续服务。"""
    ctx, _audits, fake = harness(cur_assets=[], prev_assets=[_asset(i) for i in range(1, 11)])
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", []))
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


def test_p15_no_change_still_skips(harness):
    """图集没变 ⇒ 行为与历史逐条一致（这是绝大多数日常重扫的路径）。"""
    ctx, _audits, fake = harness(cur_assets=[_asset(8), _asset(12)],
                                 prev_assets=[_asset(12), _asset(8)])
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", []))
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


def test_p15_rollback_flag_restores_legacy_behaviour(harness, monkeypatch):
    monkeypatch.setenv("RAG_SKIP_GATE_HONORS_ASSETS", "0")
    ctx, _audits, fake = harness(cur_assets=[_asset(8), _asset(12), _asset(31)],
                                 prev_assets=[_asset(8)])
    assert all(c["doc_id"] != "D" for c in ctx.get("canonicals", [])), "显式关掉后回到只看正文"
    assert any("SKIPPED_DUPLICATE" in c[0] for c in fake.calls)


def test_p15_predicate_is_asymmetric_by_construction():
    """判据函数本身：只认 n_added，n_removed 一律不放行。"""
    import opensearch_pipeline.pipeline_nodes as pn
    assert pn._asset_additions_block_skip({"n_added": 2, "n_removed": 0, "added": [1, 2]}) is True
    assert pn._asset_additions_block_skip({"n_added": 0, "n_removed": 9, "added": []}) is False
    assert pn._asset_additions_block_skip(None) is False
    assert pn._asset_additions_block_skip({}) is False
