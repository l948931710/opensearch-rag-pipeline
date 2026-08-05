# -*- coding: utf-8 -*-
"""附录B — OFFSET 分页的 ORDER BY 必须是**全序**（2026-08-03）。

`document_meta.updated_at` / `kb_contribution.created_at` 都是 `DATETIME`（**秒**精度），
而批量上传、批量重灌、org_sync 刷新会让几十篇文档落在同一秒。ORDER BY 不含唯一列时，
同值行之间的相对次序由 MySQL 自行决定、**逐次查询可变**，于是 `LIMIT/OFFSET` 翻页
既漏行又重行 —— 用户侧就是"我明明传了，翻遍列表也找不到"。

本地真库实测（MySQL 8.0.46，12 篇同一秒的文档，每页 4 条翻 3 页 = 12 个位置）：
    无 tiebreaker：并集只有 **8/12**，DOC_a~DOC_d **一页都没出现过**，另 4 篇各出现两次
    有 tiebreaker：并集 **12/12**
下面 `test_offset_pagination_covers_every_row_exactly_once` 就是这段实测的固化。
"""
import os
import pathlib
import re

import pytest

# 各表的唯一键列（ORDER BY 里出现任一即构成全序）。
# ⚠️ message_id 已移除（2026-08-04 独立核验 B3）：qa_session_log 只有普通 idx_message_id，
# 唯一键全是复合键——把它当全序列会让未来的分页点静默漏保。
UNIQUE_COLS = {
    "id",                # AUTO_INCREMENT PK
    "doc_id",            # document_meta.uk_doc_id
    "contribution_id",   # kb_contribution.uk_contribution_id
    "conversation_id",   # qa_conversation PK 的后半（查询已按 user_id 收窄）
    "chunk_id", "feedback_id", "task_id", "job_id",
}


def _split_top_level(clause: str):
    """按顶层逗号切 ORDER BY 项（括号内的逗号不算，如 COALESCE(a,b)）。"""
    out, depth, cur = [], 0, ""
    for ch in clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [t.strip() for t in out if t.strip()]


def _bare_col(term: str) -> str:
    tok = term.strip().split()[0] if term.strip() else ""
    return tok.split(".")[-1].strip("`()\"' ")


def _paginated_order_bys():
    """扫出所有 `OFFSET %s` 分页点，回其窗口内**每一个** ORDER BY 子句。

    2026-08-04 独立核验（B3 变异 C）：旧版只 `rfind` 最近一个 ORDER BY —— 分支构造的
    `order = "ORDER BY …DESC…" if x else "ORDER BY …ASC…"` 只有靠后那臂被查，前一臂的
    方向/全序可整体改坏而全绿。现改为窗口内全量枚举、逐臂检查（宁可保守多查：窗口内
    偶发混入邻近查询的 ORDER BY 时守卫会红并点名，作者看一眼即可，好过静默漏保）。"""
    found = []
    for path in sorted(pathlib.Path("opensearch_pipeline").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"OFFSET\s+%s", src):
            window = src[max(0, m.start() - 900):m.start()]
            starts = [mm.start() for mm in re.finditer(r"ORDER BY", window, flags=re.I)]
            site = f"{path}:{src[:m.start()].count(chr(10)) + 1}"
            for j, i in enumerate(starts):
                end = starts[j + 1] if j + 1 < len(starts) else len(window)
                clause = window[i + len("ORDER BY"):end]
                # 截到 LIMIT 为止；去掉 Python 字符串拼接的引号/换行噪音
                clause = re.split(r"\bLIMIT\b", clause, flags=re.I)[0]
                clause = clause.replace('"', " ").replace("'", " ").replace("\n", " ")
                found.append((site, clause.strip()))
    return found


def test_every_offset_pagination_has_a_unique_tiebreaker():
    """类级守卫：**将来**新增的分页端点漏 tiebreaker 也会在这里红。

    单点断言只能锁住今天这 5 处；这条锁的是"凡 OFFSET 分页必须全序"这条规则本身。
    """
    sites = _paginated_order_bys()
    assert sites, "没扫到任何 OFFSET 分页点——扫描逻辑失效了（防守本测试自身变空转）"
    bad = []
    for where, clause in sites:
        cols = {_bare_col(t) for t in _split_top_level(clause)}
        if not (cols & UNIQUE_COLS):
            bad.append(f"{where}  ORDER BY {clause}")
    assert not bad, (
        "以下 OFFSET 分页的 ORDER BY 不是全序（同值行次序逐次可变 ⇒ 翻页漏行/重行）：\n  "
        + "\n  ".join(bad))


def test_scanner_actually_sees_the_known_sites():
    """防守上一条：扫描器必须真的覆盖到已知的 5 个分页点（否则它可能什么都没查）。"""
    sites = dict(_paginated_order_bys())
    files = [w.split(":")[0] for w in sites]
    for f in ("opensearch_pipeline/routes/kb_console.py",
              "opensearch_pipeline/routes/contribution.py",
              "opensearch_pipeline/api.py"):
        assert any(x == f for x in files), f"扫描器漏了 {f}"
    assert len(sites) >= 5, f"已知 5 处分页，只扫到 {len(sites)}"


# ── 真库行为测试（本地 MySQL 才跑；make test 默认无凭据 → skip）────────────────

def _local_db_ok():
    """本地 MySQL 可用性 + host-pin（与 test_msg_dedup_rds_integration 同款安全闸）。"""
    try:
        import pymysql
        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
        cfg = get_config()
        if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
            return False
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password, connect_timeout=3)
        conn.close()
        return True
    except Exception:   # noqa: BLE001
        return False


@pytest.mark.skipif(not _local_db_ok(), reason="Local MySQL not available")
def test_offset_pagination_covers_every_row_exactly_once():
    """同一秒 12 篇 → 每页 4 条翻 3 页：无 tiebreaker 会漏 4 篇，有 tiebreaker 恰好全覆盖。"""
    import pymysql
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, connect_timeout=5)
    # 库名带 pid：固定名 + `DROP DATABASE` 在两个 pytest 进程同时跑时会互相端掉对方的库
    # （A 建表灌数 / B 一句 DROP ⇒ A 得 1049/1146 硬红）。pid 后缀是**单边**兜底，
    # 不依赖对面进程是否带 conftest 的跨进程锁。
    db = f"pg_probe_pytest_{os.getpid()}"
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
            cur.execute(f"CREATE DATABASE {db}")
            cur.execute(f"""CREATE TABLE {db}.document_meta (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                doc_id VARCHAR(100) NOT NULL,
                status VARCHAR(20) DEFAULT 'active',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_doc_id (doc_id)) ENGINE=InnoDB""")
            cur.executemany(
                f"INSERT INTO {db}.document_meta (doc_id, status, updated_at)"
                " VALUES (%s,'active','2026-08-03 10:00:00')",
                [(f"DOC_{c}",) for c in "abcdefghijkl"])
            conn.commit()

            def pages(order):
                seen = []
                for off in (0, 4, 8):
                    cur.execute(f"SELECT doc_id FROM {db}.document_meta ORDER BY {order}"
                                f" LIMIT 4 OFFSET {off}")
                    seen += [r[0] for r in cur.fetchall()]
                return seen

            base = "(status='active') DESC, updated_at DESC"
            without = pages(base)
            withtb = pages(base + ", doc_id DESC")

        assert len(set(withtb)) == 12 and len(withtb) == 12, (
            f"加 tiebreaker 后必须不重不漏，实得 {sorted(withtb)}")
        # 现状必然漏行——若哪天 MySQL 恰好稳定了，本断言会提醒该重新评估（而不是静默失效）
        assert len(set(without)) < 12, (
            "无 tiebreaker 竟然全覆盖了：本机 MySQL 优化器恰好稳定，"
            "该缺陷仍在（无序保证），请勿据此撤销 tiebreaker")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
        conn.commit()
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── tiebreaker 方向必须跟随前一排序键（2026-08-04 B3 复核，EXPLAIN 实测）──────────

def test_tiebreaker_direction_follows_the_preceding_key():
    """🔴 tiebreaker 的 ASC/DESC 必须与它前一个排序键一致 —— 这是**性能**约束，不是风格。

    真库 EXPLAIN 实测（MySQL 8.0.46，qa_conversation 单用户 8000 行，
    索引 `idx_user_visible_recent (user_id, hidden_at, last_message_at)`）：

        ORDER BY last_message_at DESC, conversation_id DESC
          → type=range key=idx_user_visible_recent  Backward index scan; Using index   ← 无 filesort
        ORDER BY last_message_at DESC, conversation_id ASC
          → type=ref   key=PRIMARY                  **Using filesort**（4000 行）

    机制：InnoDB 二级索引物理上就是 `(索引列…, 主键列…)`，`(user_id, hidden_at,
    last_message_at, conversation_id)` 恰好覆盖同向的复合排序；方向一混就用不上，
    退化成主键扫 + 全量 filesort。

    ⚠️ 顺带纠一处我自己的判断：当初写 `d2c8e12` 时的理由是「这些查询本就走 filesort，
    故加列可忽略」。5 处里 4 处如此，**`/api/conversations` 那处不是** —— 它改前就没有
    filesort，改后**依然**没有（正因为方向跟随）。结论对，理由曾经是错的。
    """
    bad = []
    for site, clause in _paginated_order_bys():
        terms = _split_top_level(clause)
        if len(terms) < 2:
            continue

        def _dir(t: str) -> str:
            u = t.upper().split()
            return "DESC" if "DESC" in u else "ASC"

        # 只看最后一项（tiebreaker）与其紧邻的前一项
        if _dir(terms[-1]) != _dir(terms[-2]):
            bad.append(f"{site}: …{terms[-2].strip()} , {terms[-1].strip()}")
    assert not bad, (
        "tiebreaker 方向与前一排序键不一致 —— 会让 InnoDB 用不上二级索引的隐含主键序、"
        "退化成 filesort（真库 EXPLAIN 实测）：\n  " + "\n  ".join(bad))


def test_direction_scanner_is_not_vacuous():
    """守卫上一条不是空转：扫描器确实看到了多项 ORDER BY 的分页点。"""
    multi = [(s, c) for s, c in _paginated_order_bys() if len(_split_top_level(c)) >= 2]
    assert len(multi) >= 3, (
        f"只扫到 {len(multi)} 个多项 ORDER BY 分页点——方向守卫基本没覆盖到东西，"
        "检查 _paginated_order_bys 的窗口大小/正则是否失效")
