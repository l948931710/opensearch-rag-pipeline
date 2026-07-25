# -*- coding: utf-8 -*-
"""clients.ha3_enumerate_bucket / ha3_fetch_by_pks 的协议单测（2026-07-22）。

背景铁律：零向量 + id filter 的 QueryRequest **不是全集枚举**（内积恒 0 ⇒ 得分全并列 ⇒
返回任意但确定的子集，随 build/merge 漂移）。枚举唯倒排、存在性唯 fetch。本文件钉死
两条原语的请求形状与健康判据——它们是 parity/orphan-删除/L6 门三条链路的共同地基，
一旦悄悄放宽，破枚举会被当成"这批就是空的"，进而误报缺失甚至误删。
"""
import json

import pytest

from opensearch_pipeline.clients import (
    HA3_ENUM_ANCHOR, HA3_ENUM_BUCKET, HA3_ENUM_SIZE,
    ha3_enumerate_bucket, ha3_fetch_by_pks,
)


def _row(pk, active=1, **kw):
    f = {"chunk_id": f"c{pk}", "doc_id": "d1", "chunk_type": ["text_chunk"],
         "version_no": 1, "is_active": active}
    f.update(kw)
    return {"id": pk, "score": 0.0, "fields": f}


def _resp(rows, covered=1.0, **extra):
    body = {"totalCount": len(rows), "coveredPercent": covered, "result": rows}
    body.update(extra)
    return type("R", (), {"body": json.dumps(body)})()


class _Cli:
    def __init__(self, resp):
        self._resp, self.reqs = resp, []

    def search(self, req):
        self.reqs.append(req)
        return self._resp() if callable(self._resp) else self._resp


# ── 请求形状 ──

def test_enumerate_request_shape_is_pure_inverted_single_page():
    cli = _Cli(_resp([_row(1)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert out["healthy"] and set(out["rows"]) == {1}
    req = cli.reqs[0]
    assert getattr(req, "knn", None) is None, "枚举必须纯倒排，不带向量/knn"
    assert req.size == HA3_ENUM_SIZE
    assert getattr(req, "from_", None) in (None, 0), "绝不深分页（score 全并列，翻页会重复）"
    assert req.text.query_string == HA3_ENUM_ANCHOR
    assert req.text.filter == "id>=0 AND id<500"
    assert "id" in req.output_fields and "is_active" in req.output_fields


def test_enumerate_forces_id_and_is_active_even_if_caller_omits():
    cli = _Cli(_resp([_row(1)]))
    ha3_enumerate_bucket(cli, "tbl", 0, 500, ["chunk_id"])
    assert set(["chunk_id", "id", "is_active"]) <= set(cli.reqs[0].output_fields)


def test_enumerate_preserves_is_active_joined_by_pk():
    """is_active 必须按 PK 关联回写（parse_ha3_response 是固定字典、不透传它）。"""
    cli = _Cli(_resp([_row(1, active=1), _row(2, active=0)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert out["rows"][1]["is_active"] == "1" and out["rows"][2]["is_active"] == "0"


def test_enumerate_normalizes_multi_string_chunk_type():
    cli = _Cli(_resp([_row(1, chunk_type=["step_card"])]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert out["rows"][1]["chunk_type"] == "step_card"


# ── 健康判据（每条都会让"破枚举"伪装成"空结果"，必须逐条钉死）──

@pytest.mark.parametrize("kw,frag", [
    (dict(rows=[_row(1)], covered=0.5), "coveredPercent"),
    (dict(rows=[_row(1)], covered=None), "coveredPercent"),
    (dict(rows=[_row(1)], covered="x"), "coveredPercent"),
])
def test_enumerate_unhealthy_on_covered_percent(kw, frag):
    cli = _Cli(_resp(kw["rows"], covered=kw["covered"]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and frag in out["reason"]
    assert out["rows"] == {}, "不健康桶绝不返回半截结果冒充完整"


def test_enumerate_unhealthy_on_error_code():
    cli = _Cli(_resp([], errorCode=2000, errorMsg="boom"))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "errorCode=2000" in out["reason"]


def test_enumerate_zero_error_code_is_not_an_error():
    cli = _Cli(_resp([_row(1)], errorCode=0))
    assert ha3_enumerate_bucket(cli, "tbl", 0, 500)["healthy"]


def test_enumerate_unhealthy_on_saturation():
    cli = _Cli(_resp([_row(i) for i in range(HA3_ENUM_SIZE)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "饱和" in out["reason"]


def test_enumerate_unhealthy_on_pk_outside_bucket():
    """PK 越桶 ⇒ filter 没生效 ⇒ 结果不可信（此时"缺失"判定完全失真）。"""
    cli = _Cli(_resp([_row(999)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "越出请求桶" in out["reason"]


def test_enumerate_unhealthy_on_duplicate_pk():
    cli = _Cli(_resp([_row(1), _row(1)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "重复 PK" in out["reason"]


@pytest.mark.parametrize("active", [None, 2, "x"])
def test_enumerate_unhealthy_on_broken_is_active_anchor(active):
    """锚点契约破裂（is_active 缺失/非法）⇒ 该行本就可能被锚点漏掉 ⇒ 判不健康。
    这是 is_active 未经阿里确认为官方 match-all 原语时的自检兜底。"""
    cli = _Cli(_resp([_row(1, active=active)]))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "is_active" in out["reason"]


def test_enumerate_unhealthy_on_non_list_result():
    cli = _Cli(_resp({"hits": []}))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert not out["healthy"] and "result 非 list" in out["reason"]


@pytest.mark.parametrize("lo,hi", [(0, 0), (500, 0), (0, HA3_ENUM_BUCKET + 1)])
def test_enumerate_rejects_illegal_bucket_width(lo, hi):
    """宽桶会重新引入翻页/饱和 ⇒ 直接拒绝，不给调用方悄悄放宽的机会。"""
    out = ha3_enumerate_bucket(_Cli(_resp([])), "tbl", lo, hi)
    assert not out["healthy"] and "桶宽非法" in out["reason"]


def test_enumerate_never_raises_on_sdk_exception():
    class _Boom:
        def search(self, req):
            raise RuntimeError("network down")

    out = ha3_enumerate_bucket(_Boom(), "tbl", 0, 500)
    assert not out["healthy"] and "network down" in out["reason"]


def test_enumerate_ignores_total_count():
    """totalCount 被 size 截顶（实测 size=100→34、1000→34），绝不能拿来计数/判完整。"""
    cli = _Cli(_resp([_row(1), _row(2)], totalCount=99999))
    out = ha3_enumerate_bucket(cli, "tbl", 0, 500)
    assert out["healthy"] and len(out["rows"]) == 2


# ── ha3_fetch_by_pks ──

class _FetchCli:
    def __init__(self, present, fail_batches=(), bad=None):
        self.present, self.fail_batches, self.bad = set(present), set(fail_batches), bad
        self.calls = []

    def fetch(self, req):
        ids = [int(x) for x in req.ids]
        self.calls.append(ids)
        if len(self.calls) - 1 in self.fail_batches:
            raise RuntimeError("batch boom")
        rows = self.bad if self.bad is not None else [
            {"id": p, "fields": {"chunk_id": f"c{p}"}} for p in ids if p in self.present]
        return type("R", (), {"body": json.dumps({"result": rows})})()


def test_fetch_splits_present_and_missing():
    cli = _FetchCli(present=[1, 3])
    out = ha3_fetch_by_pks(cli, "tbl", [1, 2, 3])
    assert set(out["rows_by_pk"]) == {1, 3}
    assert out["missing_pks"] == [2] and out["unknown_pks"] == []


def test_fetch_batches_at_100():
    cli = _FetchCli(present=range(250))
    ha3_fetch_by_pks(cli, "tbl", list(range(250)))
    assert [len(c) for c in cli.calls] == [100, 100, 50]


def test_fetch_batch_error_is_unknown_never_missing():
    """批异常 ⇒ unknown（不可判定），**绝不误判 missing**——否则会触发对在场行的补推/DROP。"""
    cli = _FetchCli(present=[], fail_batches=[0])
    out = ha3_fetch_by_pks(cli, "tbl", [1, 2])
    assert out["unknown_pks"] == [1, 2] and out["missing_pks"] == []
    assert out["errors"] and "batch boom" in out["errors"][0]


def test_fetch_rejects_pk_outside_request_set():
    cli = _FetchCli(present=[], bad=[{"id": 999, "fields": {}}])
    out = ha3_fetch_by_pks(cli, "tbl", [1])
    assert out["unknown_pks"] == [1] and "不在请求集合内" in out["errors"][0]


def test_fetch_rejects_duplicate_pk_in_batch():
    cli = _FetchCli(present=[], bad=[{"id": 1, "fields": {}}, {"id": 1, "fields": {}}])
    out = ha3_fetch_by_pks(cli, "tbl", [1])
    assert out["unknown_pks"] == [1] and "同批重复 PK" in out["errors"][0]


def test_fetch_error_code_body_is_batch_error():
    class _C:
        def fetch(self, req):
            return type("R", (), {"body": json.dumps({"errorCode": 500, "errorMsg": "x"})})()

    out = ha3_fetch_by_pks(_C(), "tbl", [1])
    assert out["unknown_pks"] == [1] and "errorCode=500" in out["errors"][0]


def test_fetch_handles_dict_and_to_map_bodies():
    class _Dict:
        def fetch(self, req):
            return type("R", (), {"body": {"result": [{"id": 1, "fields": {}}]}})()

    class _ToMap:
        def fetch(self, req):
            body = type("B", (), {"to_map": lambda self: {"result": [{"id": 1, "fields": {}}]}})()
            return type("R", (), {"body": body})()

    for cli in (_Dict(), _ToMap()):
        assert set(ha3_fetch_by_pks(cli, "tbl", [1])["rows_by_pk"]) == {1}


def test_fetch_never_raises():
    class _Boom:
        def fetch(self, req):
            raise RuntimeError("down")

    out = ha3_fetch_by_pks(_Boom(), "tbl", [1, 2])
    assert out["unknown_pks"] == [1, 2] and out["missing_pks"] == []
