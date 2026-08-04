# -*- coding: utf-8 -*-
"""徽章两份实现的**全交叉积**语义 parity（真 MySQL，2026-08-04）。

## 为什么既有 parity 测试不够

`test_kb_endpoints.py::test_kb_badge_case_sql_parity` 是**结构性**的：查徽章字面量在
CASE 里出现、查各分支首次出现的先后顺序。它守得住「漏加一支」，守不住**语义写错**——
把 `= 'QUARANTINED'` 写成 `!=`、把 `LEFT(...,7)` 写成 `LEFT(...,8)`、漏个
`UPPER()`，字面量和顺序统统不变，那条测试照绿。

本模块把两份实现放在**真 MySQL** 上跑全交叉积（约 12 万组），逐组比对。

## 覆盖的输入轴

doc_status × publish_status × chunk_status × index_status × content_process_status × gate_status，
每轴都含 NULL / 空串 / 大小写变体 / 真实取值。`chunk_active` 恒 None —— 那正是列表路径的
调用形态，也是 SQL 镜像唯一声明支持的形态（镜像的 docstring 写明不含 chunk_active 分支）。

依赖本地 MySQL（conftest 串行组成员）。无本地栈则 skip。
"""
import itertools

import pytest


def _conn():
    """真实 MySQL 连接，host-pin 本地 dev 栈（与 test_ingest_lease_db 同安全闸）。"""
    import pymysql
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        pytest.skip("非本地 dev 栈，跳过（本测试建/删库，绝不指向远端）")
    return pymysql.connect(host=cfg.rds.host, port=int(cfg.rds.port), user=cfg.rds.user,
                           password=cfg.rds.password, autocommit=True)


# 每轴都带 NULL / 空串 / 大小写变体 —— COALESCE/UPPER/LOWER 写漏时正是这些取值翻车
_AXES = {
    "status": [None, "active", "ACTIVE", "retired", "archived", ""],
    "publish_status": [None, "", "QUARANTINED", "quarantined", "SKIPPED_PII", "SKIPPED", "PUBLISHED"],
    "chunk_status": [None, "", "EMPTY", "empty", "NEEDS_REVIEW", "needs_review", "OK"],
    "index_status": [None, "", "INDEXED", "SUCCESS", "success", "FAILED", "PROCESSING", "DELETED"],
    "content_process_status": [None, "", "NOT_STARTED", "FAILED", "REJECTED", "SKIPPED_DUPLICATE",
                               "PENDING_APPROVAL", "DONE", "LOADING", "CONTENT_MISMATCH", "content_mismatch"],
    "gate_status": [None, "", "quarantined", "QUARANTINED", "pending_clean"],
}
_COLS = list(_AXES)


@pytest.fixture(scope="module")
def _rows():
    return list(itertools.product(*(_AXES[c] for c in _COLS)))


def test_badge_python_sql_parity_full_cross_product(_rows):
    """★ 两份实现在全交叉积上**逐组同值**。

    任何一支的条件写错（运算符 / 大小写函数 / LEFT 长度 / 分支顺序）都会在这里翻车，
    而结构性 parity 测试看不见那些。
    """
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _kb_status_badge

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS badge_parity_test")
            cur.execute("USE badge_parity_test")
            cur.execute("DROP TABLE IF EXISTS t")
            cols = ", ".join(f"{c} VARCHAR(32)" for c in _COLS)
            cur.execute(f"CREATE TABLE t (i INT PRIMARY KEY, {cols}) DEFAULT CHARSET utf8mb4")
            ph = ",".join(["%s"] * (len(_COLS) + 1))
            cur.executemany(f"INSERT INTO t VALUES ({ph})",
                            [(i,) + r for i, r in enumerate(_rows)])
            # 镜像引用 m./v. 两个别名 → 同一张表自连，两侧取同一行
            cur.execute(f"SELECT m.i, ({_KB_BADGE_CASE_SQL}) FROM t m JOIN t v ON v.i=m.i")
            sql_out = dict(cur.fetchall())

        bad = []
        for i, r in enumerate(_rows):
            v = dict(zip(_COLS, r))
            py = _kb_status_badge(v["content_process_status"], v["index_status"], v["status"],
                                  chunk_active=None, publish_status=v["publish_status"],
                                  chunk_status=v["chunk_status"], gate_status=v["gate_status"])
            if py != sql_out[i]:
                bad.append((v, py, sql_out[i]))
        assert not bad, (
            f"Python 阶梯与 SQL 镜像在 {len(bad)}/{len(_rows)} 组上不一致，前 3 组："
            + "; ".join(f"{v} → py={p!r} sql={s!r}" for v, p, s in bad[:3]))
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS badge_parity_test")
        conn.close()


def test_sql_mirror_never_emits_outside_vocab(_rows):
    """SQL 镜像的产出必须全部落在封闭词表内（Python 侧另有封闭集测试守）。"""
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _KB_BADGE_VOCAB

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS badge_parity_test")
            cur.execute("USE badge_parity_test")
            cur.execute("DROP TABLE IF EXISTS t2")
            cols = ", ".join(f"{c} VARCHAR(32)" for c in _COLS)
            cur.execute(f"CREATE TABLE t2 (i INT PRIMARY KEY, {cols}) DEFAULT CHARSET utf8mb4")
            ph = ",".join(["%s"] * (len(_COLS) + 1))
            cur.executemany(f"INSERT INTO t2 VALUES ({ph})",
                            [(i,) + r for i, r in enumerate(_rows)])
            cur.execute(f"SELECT DISTINCT ({_KB_BADGE_CASE_SQL}) FROM t2 m JOIN t2 v ON v.i=m.i")
            got = {r[0] for r in cur.fetchall()}
        assert got <= set(_KB_BADGE_VOCAB), f"SQL 镜像产出了词表外的徽章：{got - set(_KB_BADGE_VOCAB)}"
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS badge_parity_test")
        conn.close()


def test_gate_axis_is_in_both_implementations():
    """🔴 gate 轴必须**两侧都有**。

    背景（2026-08-04 B2 复核）：`_kb_version_quarantined` 的隔离判定是
    `publish='QUARANTINED' OR gate='quarantined'`，但此前 gate 轴只活在 doc-status /
    版本历史两处的 `_is_q` 外挂里；`_KB_BADGE_CASE_SQL`（my-docs / browse / 徽章服务端
    筛选 / stats 聚合共用）**只看 publish** ⇒ gate-only 隔离在列表侧会显示成「已上线」。

    ⚠️ 该状态**当前不可达**：全仓 gate_status 只有三个写方（pipeline_nodes 的
    'pending_clean' 初值、cost_breaker、spot_checker），后两者都与 publish_status
    写在同一条 UPDATE 里。这条守卫是**防止将来冒出 gate-only 写方**时两侧再度分叉。
    """
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _kb_status_badge
    assert "gate_status" in _KB_BADGE_CASE_SQL, "SQL 镜像缺 gate 轴"
    assert _kb_status_badge("DONE", "SUCCESS", "active", None, None, None,
                            gate_status="quarantined") == "已隔离", "Python 阶梯缺 gate 轴"


def test_gate_only_quarantine_is_unreachable_today():
    """守卫上一条的**前提**：不存在只写 gate_status 而不写 publish_status 的写方。

    这条一旦红，说明有人新增了 gate-only 写方 —— 那时上面那条 parity 就从"拆隐雷"
    变成"修在线 bug"，必须一并复核 console 的异常聚合与待办条口径。
    """
    import pathlib
    import re
    # 2026-08-04 独立核验拓宽：旧版只扫 3 个硬编码文件 + 单引号字面量——参数化写法、
    # 新文件、双引号全逃逸（且当时就漏数了 dataworks_nodes/register_new_files.py 的写点）。
    # 现改全仓 *.py 扫描 + 引号两态 + 参数化形态（gate_status 出现在 SET/VALUES 且值经
    # 绑定参数传 'quarantined' 的启发式：捕 "gate_status" 与 quarantined 同语句共现）。
    # 仍是文本级启发式（AST 对 SQL 字符串无能为力），但覆盖面从「碰巧选中的 3 文件」
    # 变成「全仓任何 .py」——新增 gate-only 写方想逃逸得同时绕开两种形态。
    offenders = []
    root = pathlib.Path("opensearch_pipeline")
    extra = [pathlib.Path("dataworks_nodes"), pathlib.Path("scripts"), pathlib.Path("deploy")]
    files = [p for base in [root, *extra] if base.exists()
             for p in base.rglob("*.py")]
    pat = re.compile(r"gate_status\s*=\s*(?:'quarantined'|\"quarantined\"|%s|%\(\w+\)s)")
    for p in files:
        src = p.read_text(encoding="utf-8", errors="ignore")
        for m in pat.finditer(src):
            stmt = src[max(0, m.start() - 800):m.start() + 400]
            # 参数化形态只有当 quarantined 字面量在同窗口出现时才算命中（排除读侧比较）
            if m.group(0).endswith(("'quarantined'", '"quarantined"')) or "quarantined" in stmt.lower():
                if "publish_status" not in stmt and "SELECT" not in stmt[:200].upper():
                    offenders.append(f"{p}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "出现了只写 gate_status='quarantined' 而不写 publish_status 的写方：" + str(offenders)
        + " —— 徽章 gate 轴从此不再是拆隐雷，请复核列表/聚合口径")
