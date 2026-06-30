# -*- coding: utf-8 -*-
"""spot_checker._suggests_tightening — 安全复审权限比对的 fail-closed 行为。

回归：未知/异常 suggested 权限此前 .get(...,0) 映射为 public(0)，导致"收紧建议"被静默放过
（不隔离）。现按最严处理，宁可触发隔离复审。"""
from opensearch_pipeline.spot_checker import _suggests_tightening


def test_tightening_detected():
    assert _suggests_tightening("restricted", "public") is True
    assert _suggests_tightening("internal", "public") is True
    assert _suggests_tightening("restricted", "dept_internal") is True


def test_no_tightening_when_same_or_looser():
    assert _suggests_tightening("public", "restricted") is False
    assert _suggests_tightening("dept_internal", "internal") is False   # 同级（归一化）
    assert _suggests_tightening("public", "public") is False


def test_unknown_suggested_fails_closed():
    assert _suggests_tightening("Restricted", "public") is True     # 大小写归一
    assert _suggests_tightening("confidential", "public") is True   # 生造词 → 按最严，触发复审
    assert _suggests_tightening("机密", "dept_internal") is True
