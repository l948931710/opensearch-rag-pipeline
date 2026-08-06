# -*- coding: utf-8 -*-
"""2026-08-06 codex 补评审 · 批次 B —— Sam 拍板的三项（不含图片版本不变量那批）。

三项各自独立、机械、无设计风险，故与图片那批分开落地：
  ① `POST /api/search` **彻底删除**（形状守卫在 tests/test_rag_api.py::TestSearchEndpointRemoved）；
  ② feedback / review-task 处置加**前态谓词** + 409 冲突告知；
  ③ HA3 方向二（stale/zombie）按子类**分级告警**。

②③ 的共同点：修的都是「宣称与实现不符」——
  · `decision_endpoint_shapes_2026-08-04.md` 宣称这两个端点有前态谓词，实际没有；
  · `compute_parity` 的 docstring 称 HA3 stale "harmless to recall"，**只论了召回、没论机密性**。
"""
import inspect

import pytest

from opensearch_pipeline import reconcile
from opensearch_pipeline.routes import kb_console


# ────────────────── ② 前态谓词 + 冲突分流 ──────────────────

@pytest.mark.parametrize("fn,table,open_pred", [
    (kb_console.kb_feedback_resolve, "user_feedback", "handled_status"),
    (kb_console.kb_review_task_resolve, "review_task", "review_status"),
])
def test_处置写入带前态谓词(fn, table, open_pred):
    """原实现只按主键更新 ⇒ 两个管理员同时处置同一条 = **静默 last-writer-wins**，
    后手悄悄覆盖前手的判断，双方都以为自己生效了。

    ⚠️ 断言必须锚到 **WHERE 子句**。首版写成「函数源码里出现 handled_status 即可」，
    而 `SET handled_status=%s` 天然满足它 ⇒ 把谓词整个删掉测试照样绿
    （变异验证 M5 未变红，实测踩中）。"""
    src = inspect.getsource(fn)
    i = src.index("UPDATE {_")                       # UPDATE 语句起点（表名走 f-string 变量）
    stmt = src[i:src.index("cur.fetchone", i)]       # 到冲突分流之前
    w = stmt.index(" WHERE ")
    where = stmt[w:]
    assert "_pre" in where, (
        f"{table} 的 UPDATE WHERE 子句里没有前态谓词 ⇒ 并发处置静默覆盖")
    # 谓词本身必须由状态列构成（挂个恒真的 _pre 等于没挂）
    assert open_pred in src[:i], f"_pre 不是由 {open_pred} 构成"


@pytest.mark.parametrize("fn", [kb_console.kb_feedback_resolve,
                                kb_console.kb_review_task_resolve])
def test_零影响行时区分_404_与_409(fn):
    """0 行有两种成因，必须分开告诉操作人：
    「这条不存在」和「已被别人处置了」是完全不同的后续动作（前者查错了，后者该刷新）。
    只回 404 会让并发冲突看起来像数据丢失。"""
    src = inspect.getsource(fn)
    assert "status_code=409" in src, "缺 409 冲突分支 ⇒ 并发冲突被误报成 404"
    assert "status_code=404" in src, "缺 404 分支"
    assert "请刷新" in src, "409 文案没给出下一步动作"


@pytest.mark.parametrize("fn,reopen_marker,normal_marker", [
    (kb_console.kb_feedback_resolve, "handled_status IN %s", "handled_status NOT IN %s"),
    (kb_console.kb_review_task_resolve, "review_status <> %s", "review_status = %s"),
])
def test_reopen_的前态与处置相反(fn, reopen_marker, normal_marker):
    """reopen 的前提是「已处置」，resolve/dismiss 的前提是「仍未处置」。
    两者共用一个谓词会让 reopen 永远 409（或 resolve 永远放行）。

    ⚠️ 断言必须锚到**分叉特有的谓词串**。首版只查 `req.action == "reopen"` 是否出现，
    而该函数另有一处用它决定 `reviewed_at=NULL` ⇒ 把谓词分叉整个删掉测试照样绿
    （变异验证 M8 未变红，实测踩中）。"""
    src = inspect.getsource(fn)
    assert reopen_marker in src, f"缺 reopen 侧前态谓词 {reopen_marker!r}"
    assert normal_marker in src, f"缺 resolve/dismiss 侧前态谓词 {normal_marker!r}"


# ────────────────── ③ HA3 方向二分级 ──────────────────

def test_残留子类判据_只含机密性两类():
    """`dup` **不计入**：它是同文档重切后的旧 PK，内容在另一个 active PK 下仍正当在服，
    且服务端 4c 的 B7 物理 PK 轴已把它拦掉 —— 那才是真正的 purge 滞后。
    把 dup 算进去会让每次重切都报警，把真残留淹掉。"""
    assert set(reconcile._STALE_RESIDUE_SUBTYPES) == {"rds_inactive", "orphan_chunkid"}


@pytest.mark.parametrize("subtypes,want", [
    ({}, 0),
    ({"dup": 7}, 0),                                        # 纯滞后 ⇒ 不算残留
    ({"rds_inactive": 3}, 3),                               # 文档退役/旧版停用，HA3 还在
    ({"orphan_chunkid": 2}, 2),                             # RDS 已无此行，HA3 还在
    ({"dup": 5, "rds_inactive": 3, "orphan_chunkid": 2}, 5),
    ({"rds_inactive": None}, 0),                            # 脏值不炸
])
def test_残留计数(subtypes, want):
    assert reconcile._residue_count(subtypes) == want


def test_两条产出路径同构():
    """compute_parity 与桶扫描器 finalize 必须都回 ha3_stale_residue ——
    只补一条的话，告警在另一条执行路径上恒不触发（本仓 flag 传播的老教训）。"""
    assert '"ha3_stale_residue"' in inspect.getsource(reconcile.compute_parity)
    src_all = reconcile.__file__ and open(reconcile.__file__, encoding="utf-8").read()
    assert src_all.count('"ha3_stale_residue": _residue_count(') == 2, \
        "两条路径没有都补上残留计数"


def test_残留触发告警且不占_drift_去重槽(monkeypatch):
    """残留与 drift 是**不同的故障**：drift 是「该在的不在」（召回），残留是「该没的还在」
    （机密性）。顶同一个标题会误导排障方向，占同一个去重槽会掩盖真 drift。"""
    sent = []
    import opensearch_pipeline.alerting as _al
    monkeypatch.setattr(_al, "send_ops_alert",
                        lambda title, text, **kw: sent.append((title, kw.get("dedup_key"), text)))
    reconcile._alert_on_drift({
        "ok": True, "complete": True, "enum_health": "healthy",
        "counts": {"ha3_stale": 9, "ha3_stale_residue": 5},
        "stale_subtypes": {"dup": 4, "rds_inactive": 5},
    })
    assert sent, "残留未发出任何告警"
    title, dk, text = sent[0]
    assert "残留" in title and "drift" not in title.lower(), f"顶了 drift 标题: {title}"
    assert dk == "reconcile:rds-ha3-parity:residue", f"占了别人的去重槽: {dk}"
    assert "5" in text, "正文没给出残留条数"


def test_退出码与_ok_有意不变():
    """⚠️ 这条是**有意的克制**，不是遗漏：`ok`/退出码的语义是 recall-loss，改了会让
    已部署的 launchd 作业开始变红 —— 属需 Sam 先知情的部署面变化（B6 同族教训）。
    残留只加告警，不改退出码。若将来要改，请连同这条断言一起改并通知运维。"""
    assert reconcile._exit_code(
        {"ok": True, "complete": True,
         "counts": {"ha3_stale_residue": 99}}) == 0, "残留改变了退出码 —— 需先知会运维"


def test_残留进入告警触发条件():
    """接线守卫：判据函数对了 ≠ 触发条件真的读了它（两者曾各自正确、中间没接起来）。"""
    src = inspect.getsource(reconcile.run_parity_check)
    assert "ha3_stale_residue" in src and "_residue > 0" in src, \
        "残留没有进入 _alert_on_drift 的触发条件"
