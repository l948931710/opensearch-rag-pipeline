# -*- coding: utf-8 -*-
"""徽章两份实现的**全交叉积**语义 parity（真 MySQL，2026-08-04）。

## 为什么既有 parity 测试不够

`test_kb_endpoints.py::test_kb_badge_case_sql_parity` 是**结构性**的：查徽章字面量在
CASE 里出现、查各分支首次出现的先后顺序。它守得住「漏加一支」，守不住**语义写错**——
把 `= 'QUARANTINED'` 写成 `!=`、把 `LEFT(...,7)` 写成 `LEFT(...,8)`、漏个
`UPPER()`，字面量和顺序统统不变，那条测试照绿。

本模块把两份实现放在**真 MySQL** 上跑全交叉积（6 × 129,360 = **776,160** 组），逐组比对。

## 覆盖的输入轴

**两条 status 轴独立**（2026-08-06 修）：`m.status`（文档级 →「已退役」）与
`v.status`（版本级 →「历史版本」）。此前本模块把同一张表自连（`FROM t m JOIN t v ON v.i=m.i`）
⇒ `m.status ≡ v.status` 恒成立，两轴的交叉是**实质盲区**（codex 独立指出）。现改为两张表
`t_m` / `t_v` 做 CROSS JOIN——插入仅 6 + 129,360 行（比旧方案只多 6 行），
776,160 组交叉积在 SQL 侧展开，不用逐组灌库。

m 轴：status。v 轴：publish_status × chunk_status × index_status ×
content_process_status × gate_status × status(版本级)。每轴都含 NULL / 空串 / 大小写变体。
`chunk_active` 轴已于 2026-08-06 整个移除（不再有"镜像只支持 None 形态"这一说）。

依赖本地 MySQL（conftest 串行组成员）。无本地栈则 skip。
"""
import itertools
import os

import pytest

# 库名带 pid：固定名 + `CREATE/DROP DATABASE` 在两个 pytest 进程同时跑时会互相端掉对方的库
# （对面一句 DROP 落在本侧灌 12.9 万行与自连接查询之间 ⇒ 1049/1146 硬红，且本模块没有任何
# 兜底判据，报错完全指不到真因）。pid 后缀是**单边**兜底：不依赖对面进程是否带 conftest 的
# 跨进程锁——本机 10 个老 worktree 都带着固定库名版本在跑。窗口是组内最长的 4.51s × 2 条。
_DB = f"badge_parity_test_{os.getpid()}"


def _conn():
    """真实 MySQL 连接，host-pin 本地 dev 栈（与 test_ingest_lease_db 同安全闸）。"""
    import pymysql
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        pytest.skip("非本地 dev 栈，跳过（本测试建/删库，绝不指向远端）")
    try:
        return pymysql.connect(host=cfg.rds.host, port=int(cfg.rds.port), user=cfg.rds.user,
                               password=cfg.rds.password, autocommit=True)
    except Exception as e:   # noqa: BLE001
        # host 是本地但栈没起（CI 无 MySQL 的普通 test job / 开发机未起栈）——skip 不假红。
        # 真库覆盖由 CI db-integration job（起 MySQL service）与本地栈强制执行，
        # 与 test_ingest_lease_db._db_ready 同语义（2026-08-05 CI 首跑即踩：ConnectionRefused 红 2 例）。
        pytest.skip(f"本地 MySQL 未起，跳过（{type(e).__name__}）")


# 每轴都带 NULL / 空串 / 大小写变体 —— COALESCE/UPPER/LOWER 写漏时正是这些取值翻车
_M_AXES = {
    # 文档级（document_meta.status）
    "status": [None, "active", "ACTIVE", "retired", "archived", ""],
}
_V_AXES = {
    # 版本级（document_version.status）——本次新增的独立轴，含大小写变体（镜像用 LOWER）
    "status": [None, "", "active", "superseded", "SUPERSEDED", "inactive"],
    "publish_status": [None, "", "QUARANTINED", "quarantined", "SKIPPED_PII", "SKIPPED", "PUBLISHED"],
    "chunk_status": [None, "", "EMPTY", "empty", "NEEDS_REVIEW", "needs_review", "OK"],
    "index_status": [None, "", "INDEXED", "SUCCESS", "success", "FAILED", "PROCESSING", "DELETED"],
    "content_process_status": [None, "", "NOT_STARTED", "FAILED", "REJECTED", "SKIPPED_DUPLICATE",
                               "PENDING_APPROVAL", "DONE", "LOADING", "CONTENT_MISMATCH", "content_mismatch"],
    "gate_status": [None, "", "quarantined", "QUARANTINED", "pending_clean"],
}
_MCOLS = list(_M_AXES)
_VCOLS = list(_V_AXES)


@pytest.fixture(scope="module")
def _rows():
    """(m 行, v 行) 两组，交叉积由 SQL 的 CROSS JOIN 展开。"""
    return (list(itertools.product(*(_M_AXES[c] for c in _MCOLS))),
            list(itertools.product(*(_V_AXES[c] for c in _VCOLS))))


def _seed(cur, name, cols, rows):
    cur.execute(f"DROP TABLE IF EXISTS {name}")
    coldef = ", ".join(f"{c} VARCHAR(32)" for c in cols)
    cur.execute(f"CREATE TABLE {name} (i INT PRIMARY KEY, {coldef}) DEFAULT CHARSET utf8mb4")
    ph = ",".join(["%s"] * (len(cols) + 1))
    cur.executemany(f"INSERT INTO {name} VALUES ({ph})",
                    [(i,) + r for i, r in enumerate(rows)])


def test_badge_python_sql_parity_full_cross_product(_rows):
    """★ 两份实现在全交叉积上**逐组同值**。

    任何一支的条件写错（运算符 / 大小写函数 / LEFT 长度 / 分支顺序）都会在这里翻车，
    而结构性 parity 测试看不见那些。
    """
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _kb_status_badge

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DB}")
            cur.execute(f"USE {_DB}")
            mrows, vrows = _rows
            _seed(cur, "t_m", _MCOLS, mrows)
            _seed(cur, "t_v", _VCOLS, vrows)
            # 两张表 CROSS JOIN：m.status 与 v.status 真正独立（自连同一行是本模块旧盲区）
            cur.execute(f"SELECT m.i, v.i, ({_KB_BADGE_CASE_SQL}) FROM t_m m CROSS JOIN t_v v")
            sql_out = {(a, b): c for a, b, c in cur.fetchall()}

        bad = []
        for mi, mr in enumerate(mrows):
            mv = dict(zip(_MCOLS, mr))
            for vi, vr in enumerate(vrows):
                vv = dict(zip(_VCOLS, vr))
                py = _kb_status_badge(
                    vv["content_process_status"], vv["index_status"], mv["status"],
                    version_status=vv["status"], publish_status=vv["publish_status"],
                    chunk_status=vv["chunk_status"], gate_status=vv["gate_status"])
                if py != sql_out[(mi, vi)]:
                    # 先铺 vv 再覆盖两个 status，否则 vv["status"] 会顶掉文档级 mv["status"]，
                    # 失败输出里就看不到真正的 m.status（codex 2026-08-06 点名）。
                    bad.append(({**vv, "m.status": mv["status"], "v.status": vv["status"]},
                                py, sql_out[(mi, vi)]))
        n_total = len(mrows) * len(vrows)
        assert not bad, (
            f"Python 阶梯与 SQL 镜像在 {len(bad)}/{n_total} 组上不一致，前 3 组："
            + "; ".join(f"{v} → py={p!r} sql={s!r}" for v, p, s in bad[:3]))
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_DB}")
        conn.close()


def test_sql_mirror_never_emits_outside_vocab(_rows):
    """SQL 镜像的产出必须全部落在封闭词表内（Python 侧另有封闭集测试守）。"""
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _KB_BADGE_VOCAB

    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_DB}")
            cur.execute(f"USE {_DB}")
            mrows, vrows = _rows
            _seed(cur, "t_m", _MCOLS, mrows)
            _seed(cur, "t_v", _VCOLS, vrows)
            cur.execute(f"SELECT DISTINCT ({_KB_BADGE_CASE_SQL}) FROM t_m m CROSS JOIN t_v v")
            got = {r[0] for r in cur.fetchall()}
        assert got <= set(_KB_BADGE_VOCAB), f"SQL 镜像产出了词表外的徽章：{got - set(_KB_BADGE_VOCAB)}"
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_DB}")
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
    assert _kb_status_badge("DONE", "SUCCESS", "active",
                            gate_status="quarantined") == "已隔离", "Python 阶梯缺 gate 轴"


def test_version_status_axis_is_in_both_implementations():
    """★ 版本级 status 轴必须**两侧都有**（2026-08-06）。

    升版收尾只写 `document_version.status='superseded'`、不动 `index_status` ⇒ 旧版本行
    永远停在 SUCCESS；两侧任一侧漏掉该轴，旧版本就会重新显示成「已上线」。
    ⚠️ 与第一支的 `m.status`（文档级）是**两条不同的轴**——本模块此前自连同一张表，
    二者恒等，这个交叉是测不到的。
    """
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL, _kb_status_badge
    assert "v.status" in _KB_BADGE_CASE_SQL, "SQL 镜像缺版本级 status 轴"
    assert _kb_status_badge("DONE", "SUCCESS", "active",
                            version_status="superseded") == "历史版本"
    # 文档级优先于版本级
    assert _kb_status_badge("DONE", "SUCCESS", "retired",
                            version_status="superseded") == "已退役"


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
