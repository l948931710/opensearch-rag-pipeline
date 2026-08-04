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


# ── #3 隔离顺序不变量：先删 HA3 再 commit RDS is_active=FALSE ──
# 项目既有 inspect.getsource wiring-test 模式（见 test_cs5_outbox）；HA3/DB 失败注入路径难以
# 端到端 mock，此处在结构层锁定「HA3 delete 在 is_active=FALSE 之前 + 删失败即 PENDING_DELETE 后
# continue」，防止有人回退到「先 commit RDS 隔离态再删 HA3」（删失败窗口越权泄漏）。

def test_quarantine_deletes_ha3_before_committing_rds_is_active():
    import inspect
    from opensearch_pipeline import spot_checker
    src = inspect.getsource(spot_checker.run_spot_check_pipeline)
    i_delete = src.index("_delete_chunks_from_index(doc_id, version_no, conn, config")
    i_inactive = src.index("SET is_active = FALSE")
    assert i_delete < i_inactive, "HA3 delete 必须在 chunk_meta is_active=FALSE 之前"
    # 删除失败分支：标 PENDING_DELETE 后 continue，绝不 fall through 到翻隔离态
    fail_seg = src[i_delete:i_inactive]
    assert "PENDING_DELETE" in fail_seg and "continue" in fail_seg
