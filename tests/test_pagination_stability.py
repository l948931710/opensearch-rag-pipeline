# -*- coding: utf-8 -*-
"""附录B — OFFSET 分页的 ORDER BY 必须是**全序**（2026-08-03）。

`document_meta.updated_at` / `kb_contribution.created_at` 都是 `DATETIME`（**秒**精度），
而批量上传、批量重灌、org_sync 刷新会让几十篇文档落在同一秒。ORDER BY 不含唯一列时，
同值行之间的相对次序由 MySQL 自行决定、**逐次查询可变**，于是 `LIMIT/OFFSET` 翻页
既漏行又重行 —— 用户侧就是"我明明传了，翻遍列表也找不到"。

本地真库实测（MySQL 8.0.46，12 篇同一秒的文档，每页 4 条翻 3 页 = 12 个位置）：
    无 tiebreaker：2026-08-03 本机实测并集只有 8/12（DOC_a~DOC_d 一页都没出现过、
    另 4 篇各出现两次）。⚠️ 这是**一次观测**，不是契约 —— SQL 只保证"不保证次序"，
    并不保证某个计划**必然**显形为漏行，故对应的反证断言已于 2026-08-06 删除
    有 tiebreaker：并集 **12/12**
下面 `test_offset_pagination_covers_every_row_exactly_once` 就是这段实测的固化。
"""
import os
import pathlib
import re

import pytest

# ── 分页站点契约（2026-08-06 codex 补评审：全局裸列名白名单不够用）──────────────
#
# 为什么不再用 `UNIQUE_COLS = {"id","doc_id",...}` 这种**全局裸列名**集合：
#   · 唯一性是**站点性质**，不是列名性质。`conversation_id` 只在 WHERE 固定 user_id 后唯一
#     （DDL 是复合 PK `(user_id, conversation_id)`）；`chunk_meta.doc_id` 在 chunk 表里
#     大量重复；同名 `id` 在不同表语义不同。裸列名白名单对这三种全部误判为"安全"。
#   · JOIN 会改变唯一性：document_meta.doc_id 在当前一对零/一 JOIN 后仍唯一，
#     换成一对多 JOIN 就不再唯一，而列名毫无变化。
# 故改为**显式站点契约**：每个 OFFSET 分页点登记 (端点, 臂, 唯一项, 唯一性依据, 是否方向敏感)。
#
# `direction_sensitive`（方向敏感）**当前一个站点都没有**，规则处于 dormant。
#   ⚠️ 2026-08-06 修正：这段原本写的是「`qa_conversation` 的索引
#   `(user_id, hidden_at, last_message_at)` + PK `(user_id, conversation_id)` **恰好覆盖**
#   同向复合排序 —— InnoDB 二级索引隐含主键后缀……混向才退化 filesort」。
#   **那是推理，不是实测，而且实测把它推翻了**（同文件
#   `test_tiebreaker_plan_cost_is_governed_by_index_coverage_not_direction`，
#   MySQL 8.0.46 / FORCE INDEX / 2000 行 / ANALYZE）：在该查询形态下 MySQL **没有**用隐含
#   主键后缀去满足 ORDER BY —— 加了 tiebreaker 之后**同向也 filesort**，只有索引**显式**
#   含 conversation_id 时才恢复 backward index scan。
#   ⇒ 「方向必须跟随」只在索引显式含 tiebreaker 列时才有适用对象；现状下无对象。
#   （措辞边界：这只支持「该形态下不消除排序」，**不**支持「MySQL 普遍不用隐含 PK 后缀」，
#    也**不**断言生产实际选定计划已切换 —— 探针用了 FORCE INDEX，不测优化器自主选路。）
#   ⚠️ 这**也不是全仓普适规则**：document_meta / kb_contribution 没有覆盖完整排序键的复合索引，
#   本来就要排序；browse 的 `owner_dept ASC, updated_at DESC, doc_id DESC` 与 gaps 的
#   `days_ago ASC, rid DESC` 都是**合法**混向 —— 把"方向必须跟随"当全仓规则会误伤它们
#   （codex 2026-08-06 指出，我原提案正是这个错）。
SITES = {
    ("opensearch_pipeline/api.py", "list_conversations", 1): dict(
        unique="conversation_id",
        basis="qa_conversation PK (user_id, conversation_id)；查询固定 user_id=%s",
        # 2026-08-06 实测置 False：生产索引 `(user_id, hidden_at, last_message_at)` 只到
        # last_message_at，MySQL **不会**用隐含主键后缀满足 ORDER BY ⇒ 加了 tiebreaker
        # 无论方向都 filesort，方向规则在现状下**无适用对象**。
        # 待 `idx_user_visible_recent` 扩到含 conversation_id 后再翻回 True
        # （schema 变更，user-gated；见 test_tiebreaker_plan_cost_is_governed_by_index_coverage_not_direction）。
        direction_sensitive=False, lexical=True),
    ("opensearch_pipeline/routes/contribution.py", "kb_contributions_mine", 1): dict(
        unique="contribution_id",
        basis="kb_contribution.uk_contribution_id（全局唯一）", direction_sensitive=False, lexical=True),
    ("opensearch_pipeline/routes/contribution.py", "kb_contributions_pending", 1): dict(
        unique="contribution_id",
        basis="kb_contribution.uk_contribution_id（全局唯一）；本臂 scope_clause 动态但不引入 JOIN",
        direction_sensitive=False, lexical=True),
    ("opensearch_pipeline/routes/kb_console.py", "kb_my_docs", 1): dict(
        unique="m.doc_id",
        basis="document_meta.uk_doc_id；LEFT JOIN 由 uk_doc_version 保证不扇出",
        direction_sensitive=False, lexical=True),
    ("opensearch_pipeline/routes/kb_console.py", "kb_browse", 1): dict(
        unique="m.doc_id",
        basis="document_meta.uk_doc_id；LEFT JOIN 由 uk_doc_version 保证不扇出",
        direction_sensitive=False, lexical=True),
    # lexical=False：ORDER BY 在动态变量 `order` 里（两条分支），词法扫描解析不出，
    # 强行扫会把变量后面的整段代码当成子句。改由「捕获最终 cursor.execute SQL」的行为测试覆盖。
    # ⚠️ `behavior_test` 不是注释而是**契约**：`test_lexical_false_sites_name_a_real_backstop`
    # 会去被点名的模块里查这个函数真的存在。2026-08-06 收尾复核发现原注释点名的
    # `test_review_tasks_both_arms_end_with_task_id` **全仓根本不存在** —— 逃生口指着一个
    # 幽灵兜底，等于无兜底。真实覆盖者是下面这个。
    ("opensearch_pipeline/routes/kb_console.py", "kb_review_tasks", 1): dict(
        unique="t.task_id",
        basis="review_task.task_id NOT NULL UNIQUE（schema/001:305 uk_task_id）；"
              "LEFT JOIN document_meta ON m.doc_id=t.doc_id，doc_id 唯一 ⇒ 一对零/一不扇出",
        direction_sensitive=False, lexical=False,
        behavior_test=("tests.test_kb_approval", "test_review_tasks_order_by_has_unique_tiebreaker")),
    ("opensearch_pipeline/api.py", "history", 1): dict(
        unique="id",
        basis="qa_session_log.id AUTO_INCREMENT PK", direction_sensitive=False, lexical=True),
}

# 严格的 `[alias.]column` —— 函数调用 / 算术 / 取模 / 括号表达式**一律不认**为唯一项。
# `_bare_col("id % 2 DESC")` 旧实现会取到 "id" 并判为唯一，而 `id % 2` 显然非单射。
_STRICT_TERM = re.compile(r"^(?:(?P<alias>[A-Za-z_]\w*)\.)?(?P<col>[A-Za-z_]\w*)"
                          r"(?:\s+(?P<dir>ASC|DESC))?$", re.I)


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


def _parse_term(term: str):
    """→ (列名, 方向) 或 None（非严格单列表达式，不得充当唯一项）。"""
    m = _STRICT_TERM.match(term.strip().strip("`"))
    if not m:
        return None
    # **保留 alias**：只回裸列名的话，`m.doc_id`（一对一 JOIN，唯一）与
    # `x.doc_id`（一对多 JOIN，不唯一）无法区分 —— 那就没真正做到"表/作用域语义"
    # （codex 2026-08-06 指出）。契约里登记的也是完整 `alias.column`。
    alias = (m.group("alias") or "").lower()
    return (f"{alias}.{m.group('col')}" if alias else m.group("col"),
            (m.group("dir") or "ASC").upper())


def _enclosing_def(src: str, pos: int) -> str:
    """OFFSET 所在的最近一个 `def name(`——站点身份用函数名，不用行号
    （行号会因无关插行整体漂移，制造纯噪音的红）。"""
    # `async def` 必须认（FastAPI 仓库的现实演进面，codex 2026-08-06 点名）
    names = [(m.start(), m.group(1))
             for m in re.finditer(r"^(?:async\s+)?def (\w+)\(", src, re.M)]
    names += [(m.start(), m.group(1))
              for m in re.finditer(r"^    (?:async\s+)?def (\w+)\(", src, re.M)]
    prev = [n for st, n in sorted(names) if st < pos]
    return prev[-1] if prev else "<module>"


def _paginated_order_bys():
    """扫出所有 OFFSET 分页点，回 (函数名, 文件, 该点窗口内每一个 ORDER BY 臂)。

    2026-08-04（B3 变异 C）：旧版只 `rfind` 最近一个 ORDER BY —— 分支构造的
    `order = "…DESC…" if x else "…ASC…"` 只有靠后那臂被查。现为窗口内全量枚举、逐臂检查。
    2026-08-06（codex 补评审）：
      · `OFFSET` 匹配加 `re.I` —— SQL 关键字大小写不敏感，小写 `offset %s` 是**现实**写法，
        而不是刻意绕法（改成不敏感几乎零成本）；
      · 窗口下界由「固定 900 字符」改为「上一个 `.execute(`」—— 固定窗口两头不是人：
        放窄会随 SQL 变长越窗漏扫（本批给 my-docs/browse 各加了一列，正在逼近），
        放宽则跨查询污染（实测 2000 字符时 pending 抓到了 mine 的 ORDER BY）。

    ⚠️ **本守卫的词法边界**（抓删除、不抓等价改写；下列写法它看不见）：
      · `LIMIT %s, %s` 两参形式、`OFFSET %(offset)s` 具名占位符 —— 2026-08-06 收尾复核前
        这里只写「本仓风格禁止，未专门封堵」。**「禁止」当时没有任何东西在执行**，
        等于给新分页查询留了一个合法 MySQL 语法的静默逃生口（codex 指出）。
        现由 `test_no_alternate_offset_spellings_bypass_the_scanner` **强制**；
      · 刻意拆词 `"OFF" + "SET"`、任意元编程拼 SQL；
      · 窗口内注释/无关字符串里的假 `ORDER BY`（会被当成真子句 ⇒ 守卫**偏严**，红了看一眼即可）。
    动态拼接的两个端点（pending 的 scope_clause、review-tasks 的 order 分支）不靠词法覆盖，
    另由「捕获最终 cursor.execute SQL」的行为测试兜底。
    """
    found = []
    for path in sorted(pathlib.Path("opensearch_pipeline").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        ordinal = {}
        for m in re.finditer(r"OFFSET\s+%s", src, flags=re.I):
            # 窗口下界取**上一个 `cur.execute(`**——固定字符窗口会跨查询污染
            # （2000 字符时 pending 抓到了 mine 的 ORDER BY，900 字符又会随 SQL 变长越窗）。
            _ex = [mm.end() for mm in re.finditer(r"\.execute\(", src[:m.start()])]
            lo = _ex[-1] if _ex else max(0, m.start() - 2000)
            window = src[lo:m.start()]
            starts = [mm.start() for mm in re.finditer(r"ORDER BY", window, flags=re.I)]
            fn = _enclosing_def(src, m.start())
            ordinal[fn] = ordinal.get(fn, 0) + 1
            key = (str(path), fn, ordinal[fn])
            # 🔴 每个 OFFSET **恒产出至少一条记录**（臂可为空字符串）。
            # 旧版只在找到 ORDER BY 后才 append ⇒ 新增一个**没有 ORDER BY** 的
            # `LIMIT %s OFFSET %s` 会产出零条记录，于是"每个站点都登记""唯一项必须收尾"
            # 这些守卫**全部照绿** —— 恰恰漏掉了最危险的那种新增（codex 2026-08-06 实测复现）。
            if not starts:
                found.append((key, ""))
                continue
            for j, i in enumerate(starts):
                end = starts[j + 1] if j + 1 < len(starts) else len(window)
                clause = re.split(r"\bLIMIT\b", window[i + len("ORDER BY"):end], flags=re.I)[0]
                clause = clause.replace('"', " ").replace("'", " ").replace("\n", " ")
                found.append((key, clause.strip()))
    return found


def test_every_offset_pagination_is_a_registered_site():
    """★ 每个 OFFSET 分页点都必须**在 SITES 里登记**。

    新增一个分页端点却不登记 ⇒ 这里红，倒逼作者写清"唯一项是谁、依据什么 DDL、方向敏不敏感"。
    旧版只查"裸列名落在全局白名单里"，等于允许作者不写依据就过关。
    """
    seen = {key for key, _c in _paginated_order_bys()}
    assert seen, "没扫到任何 OFFSET 分页点——扫描逻辑失效了（防守本测试自身变空转）"
    unregistered = seen - set(SITES)
    assert not unregistered, (
        f"这些 OFFSET 分页点未登记进 SITES（请写明唯一项与 DDL 依据）：{sorted(unregistered)}")
    missing = {k for k, v in SITES.items() if v["lexical"]} - seen
    assert not missing, f"SITES 登记了但扫不到（站点没了？改名了？）：{sorted(missing)}"


def test_unique_tiebreaker_is_present_and_last():
    """★ 唯一项必须存在，且**位于 ORDER BY 末位**。

    "末位"这条不是洁癖：唯一项后面还挂东西时（例如
    `last_message_at DESC, conversation_id ASC, 0 ASC`），只比较"最后两项方向"的守卫会被
    整体绕过 —— 而全序其实早在唯一项那里就成立了，后面那项纯属噪音/伪装。
    要求收尾同时堵死这类构造（codex 2026-08-06 给的绕法）。
    """
    bad = []
    for key, clause in _paginated_order_bys():
        if not SITES[key]["lexical"]:
            continue   # 动态构造，另由行为测试覆盖
        want = SITES[key]["unique"]
        terms = [_parse_term(t) for t in _split_top_level(clause)]
        if terms and terms[-1] and terms[-1][0] == want:
            continue
        cols = [t[0] if t else f"<非单列表达式:{raw.strip()}>"
                for t, raw in zip(terms, _split_top_level(clause))]
        bad.append(f"{key}  ORDER BY {clause}  ⇒ 末位应为唯一项 {want!r}，实得 {cols}")
    assert not bad, (
        "以下 OFFSET 分页的唯一 tiebreaker 缺失或不在末位"
        "（同值行次序逐次可变 ⇒ 翻页漏行/重行）：\n  " + "\n  ".join(bad))


def test_direction_rule_applies_only_to_direction_sensitive_sites():
    """★ tiebreaker 方向跟随前项 —— **只对登记为方向敏感的站点**生效。

    ⚠️ 当前 `direction_sensitive` 一个站点都没有 ⇒ 本测试是 **dormant** 的（空转）。
    原先这里写「仅 conversations 有覆盖完整排序键的复合索引（PK 后缀 conversation_id），
    混向才用不上单次索引扫描」—— **实测推翻**：现状 3 列索引下同向也 filesort，
    只有索引**显式**含 conversation_id 才恢复 backward index scan
    （见 `test_tiebreaker_plan_cost_is_governed_by_index_coverage_not_direction`）。
    等 `idx_user_visible_recent` 扩列（user-gated，F-35）后把 conversations 翻回 True，
    本测试才重新有对象。
    ⚠️ 绝不能推广成全仓规则：browse 的 `owner_dept ASC, updated_at DESC, doc_id DESC`
    与 gaps 的 `days_ago ASC, rid DESC` 都是**合法**混向，全仓化会把它们误判成缺陷
    （codex 2026-08-06 拦下的正是这个提案）。
    """
    bad = []
    for key, clause in _paginated_order_bys():
        if not SITES[key]["lexical"] or not SITES[key]["direction_sensitive"]:
            continue
        terms = [_parse_term(t) for t in _split_top_level(clause)]
        if len(terms) < 2 or not all(terms[-2:]):
            bad.append(f"{key}  ORDER BY {clause}  ⇒ 末两项无法解析为单列")
            continue
        if terms[-2][1] != terms[-1][1]:
            bad.append(f"{key}  ORDER BY {clause}  ⇒ {terms[-2]} 与 {terms[-1]} 方向不一致")
    assert not bad, (
        "方向敏感站点的 tiebreaker 方向与前项不一致 —— 会用不上二级索引的隐含主键序、"
        "退化成 filesort：\n  " + "\n  ".join(bad))


def test_no_alternate_offset_spellings_bypass_the_scanner():
    """★ 收尾复核（2026-08-06，codex）：扫描器只认 `OFFSET %s`，而 MySQL 还有别的合法写法。

    `LIMIT %s, %s`（逗号两参）和 `OFFSET %(offset)s`（具名占位符）都是**标准语法**，
    写出来的新分页查询完全不会进 `seen` ⇒ 「未登记站点」「唯一项收尾」「有 OFFSET 无
    ORDER BY」三道检查**全部跳过**、照样全绿。原文件把这条记成「本仓风格禁止，未专门封堵」，
    但**没有任何东西在执行那条「禁止」** —— 一句约定不是守卫。

    这里把约定变成硬门：这两种写法在 `opensearch_pipeline/` 里一出现就红，
    作者要么改成 `OFFSET %s` 并去 SITES 登记，要么显式扩本守卫。

    ⚠️ 第一版只列了四条正则，被 codex 一句话破了：`LIMIT /* offset */ %s, %s` 仍是合法分页，
    但 `LIMIT\\s+%s` 匹配不到 —— **黑名单遇上可插注释的语法就是筛子**。现改为两步：
      ① 先剥掉 `/*…*/` 与 `-- …` 注释（用等长空格替换，行号不漂）；
      ② 在剥净的源码上，不再枚举"坏写法"，而是**正向要求**：
         每个 `OFFSET` 必须恰好是主扫描器认得的 `OFFSET %s`，且任何 `LIMIT <参数>,` 逗号
         两参形式一律禁止。这样约束的是"主扫描器看得见"这个性质本身，而不是某几种拼法。
    """
    def _blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))

    def _strip_sql_comments(s: str) -> str:
        # 等长替换（保留换行）⇒ 行号与原文一一对应，报错能直接点到人。
        # 三种都要剥：`/*…*/`、`--…`、以及 MySQL 的 `#…`（codex 第 3 轮补的第三种 ——
        # `LIMIT %s OFFSET # offset\n %s` 同时躲过主扫描器和只剥前两种的这里）。
        # ⚠️ 剥 `#` 会**误伤字符串字面量里的 `#`**（`LIKE '%#%' … OFFSET %(off)s` 同行时，
        # 整行剩下的部分连同 OFFSET 一起被抹掉）。我原本写「剥除只会少扫、不会误红」——
        # **那句话是错的，少扫在这里就是漏检**（codex 第 4 轮）。补法见 `_starts`：两边都扫。
        s = re.sub(r"/\*.*?\*/", _blank, s, flags=re.S)
        s = re.sub(r"--[^\n]*", _blank, s)
        return re.sub(r"#[^\n]*", _blank, s)

    def _starts(pattern: str, raw: str, src: str) -> set:
        """在**原文和剥净文本上都扫**，取并集（等长替换 ⇒ 偏移一一对应，可直接合）。

        两边都扫，缺一不可：
          · 只扫原文 ⇒ 注释隔断的（`OFFSET /*x*/ %s`、`OFFSET # 跳过\\n %s`）躲掉；
          · 只扫剥净 ⇒ 字符串字面量里的 `#` 造成的误剥把真 OFFSET 一起吃掉。
        """
        return ({m.start() for m in re.finditer(pattern, raw, flags=re.I)}
                | {m.start() for m in re.finditer(pattern, src, flags=re.I)})

    hits = []
    for path in sorted(pathlib.Path("opensearch_pipeline").rglob("*.py")):
        raw = path.read_text(encoding="utf-8")
        src = _strip_sql_comments(raw)
        # ① 正向要求：每个**像 SQL 子句的** OFFSET 都必须是主扫描器认得的形态。
        # 候选条件必须带「后面跟参数」这个前瞻——否则 `\bOFFSET\b`+re.I 会命中
        # Python 变量名 `offset`（`offset = max(0, ...)`、`int(offset or 0)`），
        # 基线直接全红（第一版就是这么翻的车）。
        # ⚠️ 候选取原文 ∪ 剥净（见 `_starts`），但「主扫描器看不看得见」一律拿**原文**判 ——
        # 拿剥净文本判等于替扫描器把注释也剥了，`OFFSET /*x*/ %s` 就成了"合规"。
        for pos in sorted(_starts(r"\bOFFSET\b(?=\s*[%\d])", raw, src)):
            if not re.match(r"OFFSET\s+%s", raw[pos:], flags=re.I):
                line = raw[:pos].count("\n") + 1
                hits.append(f"{path}:{line}  OFFSET 不是 `OFFSET %s` 形态 "
                            f"⇒ `_paginated_order_bys` 扫不到：{raw[pos:pos + 40]!r}")
        # ② LIMIT 的逗号两参形式一律禁（它整个绕开 OFFSET 关键字）
        for pos in sorted(_starts(r"\bLIMIT\s*(?:%s|%\(\w+\)s|\d+)\s*,", raw, src)):
            line = raw[:pos].count("\n") + 1
            hits.append(f"{path}:{line}  `LIMIT <a>, <b>` 两参形式 —— 无 OFFSET 关键字，"
                        f"分页守卫会静默放行：{raw[pos:pos + 40]!r}")
    assert not hits, (
        "检出主扫描器识别不了的分页写法（改用 `LIMIT %s OFFSET %s` 并在 SITES 登记，"
        "或显式扩 `_paginated_order_bys` 的匹配）：\n  " + "\n  ".join(hits))


def test_lexical_false_sites_name_a_real_backstop():
    """★ 收尾复核（2026-08-06，codex）：`lexical=False` 是逃生口，必须有人兜底。

    登记 `lexical=False` 会让三道词法检查全部跳过该站点。原实现里这个口子**完全不受约束**：
    新加一个「有 OFFSET、无 ORDER BY」的查询再标 `lexical=False`，就永久隐身。

    这里要求每个 `lexical=False` 站点点名一个**真实存在、会被 pytest 收集、且确实针对该站点**
    的行为测试 —— 三个条件缺一不可：
      ① 模块在 `tests.` 下且函数名 `test_` 开头 ⇒ pytest 会收集它
         （只验 `callable` 不够：`_review_tasks` 那种 helper 也 callable，
          填进去照样全绿 —— codex 破的正是这一版）；
      ② 函数**代码里**（`ast.unparse` 后，docstring 与注释均已剥除）必须出现该站点登记的
         唯一项（如 `t.task_id`）⇒ 兜底测试与站点真的挂钩，而不是随便点一个存在的测试凑数。
         剥 docstring 是必要的：codex 第 3 轮给的伪兜底就是「函数体只有一个把唯一项
         原样写进去的 docstring + `assert True`」—— 四项检查全过、零覆盖。

    ⚠️ 这条不是形式主义：写它的时候发现原注释点名的
    `test_review_tasks_both_arms_end_with_task_id` **全仓根本不存在** ——
    逃生口指着一个幽灵兜底，等于无兜底（真实覆盖者是 test_kb_approval 里那个）。

    ⚠️ **能力边界（明说，不假装）**：这是**纪律守卫，不是对抗守卫**。
    静态检查无法证明一个测试真的执行并断言了那条 SQL；铁了心的作者永远能写
    `x = "t.task_id"` 这种形式合规的假兜底。它防的是**漂移与疏忽**——正是实际发生过的
    那种（点名了一个不存在的测试，没人发现）。真要证明覆盖，得让兜底测试捕获
    `cursor.execute` 的最终 SQL（review-tasks 现在的那条就是这么做的）。
    """
    import ast
    # ⚠️ **不 import 被点名的测试模块**：`importlib.import_module("tests.test_kb_approval")`
    # 会在当前 xdist worker 里执行那个模块的导入副作用，而本仓已知存在跨测试模块互踩的
    # flake 家族（backlog §C.3；实测本守卫用 importlib 那一版跑 make test 时，
    # 另一模块的 `get_config.cache_clear()` 报 AttributeError）。纯 AST 读文件即可，
    # 零副作用、也不依赖被点名模块能否在本 worker 里成功导入。
    missing = []
    for key, meta in SITES.items():
        if meta["lexical"]:
            continue
        ref = meta.get("behavior_test")
        if not ref:
            missing.append(f"{key}  lexical=False 但未点名 behavior_test")
            continue
        mod_name, fn_name = ref
        # 收集性判据必须对齐 pytest 的**真实**规则：`pyproject.toml:65` 只设了
        # `testpaths=["tests"]`，没设 `python_files` ⇒ 用默认的 `test_*.py` / `*_test.py`。
        # 只查包前缀 `tests.` 是不够的：`tests/pagination_backstop.py` 里放一个
        # `def test_x()` 能过前缀检查，但**永远不会被收集**（codex 第 5 轮的命名漂移构造）。
        stem = mod_name.rsplit(".", 1)[-1]
        collectible = (mod_name.startswith("tests.")
                       and (stem.startswith("test_") or stem.endswith("_test"))
                       and fn_name.startswith("test_"))
        if not collectible:
            missing.append(f"{key}  {mod_name}.{fn_name} 不会被 pytest 收集"
                           f"（模块须在 tests. 下且文件名匹配 test_*.py / *_test.py、"
                           f"函数名须 test_ 开头）")
            continue
        mod_path = pathlib.Path(mod_name.replace(".", "/") + ".py")
        if not mod_path.is_file():
            missing.append(f"{key}  behavior_test 模块文件 {mod_path} 不存在")
            continue
        try:
            tree = ast.parse(mod_path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            missing.append(f"{key}  {mod_path} 解析失败，无法核对覆盖面：{e}")
            continue
        node = next((n for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == fn_name), None)
        if node is None:
            missing.append(f"{key}  点名的 {mod_name}.{fn_name} **不存在** —— 幽灵兜底")
            continue
        want = meta["unique"]
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body = node.body[1:] or [ast.Pass()]   # 去 docstring
        body = ast.unparse(node)                        # 注释天然不进 AST
        if want not in body:
            missing.append(f"{key}  {fn_name} 源码里没有出现唯一项 {want!r} "
                           f"⇒ 它没在替本站点兜底")
    assert not missing, (
        "`lexical=False` 站点必须点名真实存在、会被收集、且针对本站点的行为测试：\n  "
        + "\n  ".join(missing))


def test_strict_term_parser_rejects_non_injective_expressions():
    """★ 严格列解析的反证：非单射表达式**不得**被当成唯一项。

    旧的 `_bare_col("id % 2 DESC")` 会取到 "id" 并判唯一 —— 而 `id % 2` 只有两个取值。
    """
    for bogus in ("id % 2 DESC", "COALESCE(a,b) DESC", "LEFT(doc_id,3) ASC",
                  "id+1", "(status='active') DESC", "RAND()"):
        assert _parse_term(bogus) is None, f"非单射表达式被当成了单列：{bogus!r}"
    assert _parse_term("m.doc_id DESC") == ("m.doc_id", "DESC")   # alias 必须保留
    assert _parse_term("conversation_id") == ("conversation_id", "ASC")


def test_scanner_sees_every_registered_arm():
    """防守以上三条：扫描器必须真的产出足够多的排序臂（否则它可能什么都没查）。

    2026-08-06：旧版用 `dict(_paginated_order_bys())` 去重，会把 review-tasks
    同一位置的**两条动态分支臂折叠成一条**，覆盖数被静默低估（codex 指出）。
    现按 (函数, 臂序) 计数，不折叠。
    """
    arms = _paginated_order_bys()
    n_lex = sum(1 for v in SITES.values() if v["lexical"])
    assert len(arms) >= n_lex, f"登记 {n_lex} 个词法站点，只扫到 {len(arms)} 条排序臂"
    for key, meta in SITES.items():
        if not meta["lexical"]:
            continue
        got = [c for k, c in arms if k == key]
        assert got, f"{key} 扫不到任何排序臂"
        assert any(c.strip() for c in got), (
            f"{key} 有 OFFSET 但**没有 ORDER BY** —— 这正是 2026-08-06 补上的漏检形态")


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
    """同一秒 12 篇 → 每页 4 条翻 3 页：**有 tiebreaker 必须恰好全覆盖**。

    只断言正向契约。"无 tiebreaker 会漏 4 篇"是 2026-08-03 的一次观测，不作断言
    （理由见下方注释与模块 docstring）。
    """
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
            f"加 tiebreaker 后必须不重不漏，实得 {sorted(withtb)}"
            f"（对照：同参数无 tiebreaker 实得 {sorted(without)}）")
        # ⚠️ 2026-08-06（codex 补评审）**删掉了**原来那条反证断言
        # `assert len(set(without)) < 12`（"无 tiebreaker 必须实际漏行"）：
        # SQL 只是**不保证**同值行次序，并不保证某个具体计划**必然**表现为漏行。
        # 那条断言把"缺陷存在"错写成"缺陷必然显形"，会随优化器/版本变化随机变红，
        # 而它红了并不说明 tiebreaker 有问题。正向断言（有 tiebreaker ⇒ 不重不漏）才是契约。
        # `without` 只进失败信息作对照，不作断言（此前写"保留用于失败信息"却没真的输出，
        # codex 2026-08-06 指出）。
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
        conn.commit()
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── 动态构造站点：靠捕获最终 SQL，不靠词法 ──────────────────────────────────

# review-tasks 的两条动态分支由 `tests/test_kb_approval.py::
# test_review_tasks_order_by_has_unique_tiebreaker` 覆盖 —— 那里已有捕获最终
# cursor.execute SQL 的 harness（`_review_tasks`），不在本文件重复造一套驱动。


def test_gaps_upstream_limits_are_totally_ordered():
    """★ gaps 的**上游两个 LIMIT** 也必须全序（codex 2026-08-06 BLOCKER）。

    下游 hash 终排序只决定"已选出的候选怎么排"，补不回**上游边界处漏选**的行：
      · NO_RESULT 侧 `created_at` 是秒精度 DATETIME；
      · REFUSAL 侧 `days_ago` 只有整天粒度。
    同值撞在 LIMIT 边界时，静态库两次重算都可能选出不同候选集。
    tiebreaker 依据：`q.id` 是 qa_session_log 的 PK（`message_id` 只有普通 idx_message_id，
    DDL 注释「消息唯一ID」**不是**约束）；`t.rid` = 子查询的 `MAX(q.id)`，
    而 `GROUP BY q.message_id` 的分组彼此不相交 ⇒ 各组 MAX 必不相同。
    """
    from opensearch_pipeline.routes import contribution

    seen = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.append(" ".join(sql.split()))

        def fetchall(self):
            return []

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def close(self):
            pass

    import unittest.mock as _m
    with _m.patch("opensearch_pipeline.db._get_db_conn", lambda: _Conn()):
        try:
            contribution._compute_open_gaps(["hr"], "t")
        except Exception:      # noqa: BLE001 — 桩不完整无妨，SQL 已捕获
            pass
    # ★ 捕获**最终执行的 SQL**，不是 inspect.getsource 搜字符串 —— 后者对死代码里的
    # 同款字符串照样绿（codex 2026-08-06 指出）。
    no_result = [q for q in seen if "ORDER BY q.created_at" in q]
    refusal = [q for q in seen if "ORDER BY t.days_ago" in q]
    assert no_result, f"没捕获到 NO_RESULT 上游查询：{seen}"
    assert refusal, f"没捕获到 REFUSAL 上游查询：{seen}"
    for tag, qs, want in (("NO_RESULT", no_result, "q.id"), ("REFUSAL", refusal, "t.rid")):
        order = qs[0].split("ORDER BY")[1].split("LIMIT")[0]
        terms = [_parse_term(t) for t in _split_top_level(order)]
        assert terms and terms[-1] and terms[-1][0] == want, (
            f"{tag} 上游 LIMIT 的唯一 tiebreaker 不在末位：{order}")
    # rid 的唯一性依据是 `MAX(q.id)` + 互斥分组 —— 换成非唯一表达式就不成立，钉住它
    assert "MAX(q.id) rid" in refusal[0], f"rid 不再是 MAX(q.id)：{refusal[0][:300]}"
    # ⚠️ 补全序只稳定「选哪 2000 条」，**不解决 2000 硬 cap 本身的不完整**
    #（第 2001 条起永不可见）——那是设计题，见 backlog §F。




# ── 可执行 EXPLAIN（2026-08-06 codex 补评审：此前 EXPLAIN 只写在 docstring 里）──────

@pytest.mark.skipif(not _local_db_ok(), reason="Local MySQL not available")
def test_tiebreaker_plan_cost_is_governed_by_index_coverage_not_direction():
    """★ 把「方向跟随」从**口头结论**变成**可复现实测** —— 结果推翻了原结论。

    账本 §C-bis 记「EXPLAIN 实测 5/5 零计划影响」「逆向 ASC 会从 Backward index scan
    掉成 PRIMARY + filesort」。仓里此前**没有任何执行 EXPLAIN 的测试**（代码里的断言纯词法，
    codex 2026-08-06 指出）。本条把它跑出来，实测（MySQL 8.0.46）结论是：

      索引 = 生产现状 3 列 `(user_id, hidden_at, last_message_at)`（tiebreaker 只靠**隐含主键后缀**）
        · `ORDER BY last_message_at DESC`（仅索引前缀）      → 无 filesort，backward index scan
        · `… DESC, conversation_id DESC`（同向）             → **filesort**
        · `… DESC, conversation_id ASC`（混向）              → **filesort**
      索引 = 显式 4 列 `(…, last_message_at, conversation_id)`
        · 同向 → **无 filesort，backward index scan**
        · 混向 → filesort

    ⇒ 两条订正：
      1. **在该查询形态 + 该索引 + FORCE INDEX 下，MySQL 8.0.46 未利用隐含的
         `conversation_id` 后缀消除排序。**（措辞刻意收窄：这**不**支持"MySQL 普遍不使用
         隐含 PK 后缀"，隐含后缀在其他访问/覆盖场景仍可能被用到；本条也**不**断言
         生产实际选定计划已经切换——探针用了 FORCE INDEX，不测优化器自主选路。）
         在生产现状的 DDL 下，
         `d2c8e12` 加 tiebreaker **本身**就让 `/api/conversations` 丢掉了 backward index scan
         —— 账本的「零计划影响」对该端点**失实**。（正确性仍需要 tiebreaker，不能撤。）
      2. 「方向必须跟随」**只在索引显式含 tiebreaker 列时才有意义**；生产现状下方向根本
         不影响计划。故 SITES 里 conversations 的 `direction_sensitive` 已置 False，
         并把"扩索引"记为 user-gated 的独立项（schema 变更走 schema/ + 台账，F-35 纪律）。

    本条测的是**索引覆盖度 ⇒ 排序能力**这条确定性质（用 FORCE INDEX 固定），
    不测优化器在特定行数/统计下的自主选路（那受版本与统计影响，不适合当阻塞断言）。
    """
    import json

    import pymysql
    from opensearch_pipeline.config import get_config

    def _walk(o, key):
        """EXPLAIN JSON 嵌套层数不固定；**必须解析 JSON**，不能对原文做子串判断
        —— `"using_filesort": false` 里也含 "filesort"（本测试初版即踩）。"""
        if isinstance(o, dict):
            if o.get(key) is True:
                return True
            return any(_walk(v, key) for v in o.values())
        if isinstance(o, list):
            return any(_walk(v, key) for v in o)
        return False

    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, autocommit=True)
    db = f"pg_explain_{os.getpid()}"
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
            cur.execute(f"USE {db}")

            def _plan(idx_cols, order, idx_name):
                cur.execute("DROP TABLE IF EXISTS qc")
                cur.execute(f"""CREATE TABLE qc (
                    user_id VARCHAR(64) NOT NULL, conversation_id VARCHAR(64) NOT NULL,
                    hidden_at DATETIME DEFAULT NULL, last_message_at DATETIME DEFAULT NULL,
                    PRIMARY KEY (user_id, conversation_id),
                    INDEX {idx_name} ({idx_cols})) DEFAULT CHARSET utf8mb4""")
                cur.executemany(
                    "INSERT INTO qc VALUES (%s,%s,NULL,%s)",
                    [("u1", f"c{i:05d}", f"2026-08-01 00:{i % 60:02d}:00") for i in range(2000)])
                cur.execute("ANALYZE TABLE qc")
                cur.execute(
                    f"EXPLAIN FORMAT=JSON SELECT conversation_id FROM qc FORCE INDEX ({idx_name})"
                    f" WHERE user_id='u1' AND hidden_at IS NULL ORDER BY {order} LIMIT 20 OFFSET 40")
                raw = cur.fetchone()[0]
                j = json.loads(raw) if isinstance(raw, str) else raw
                return _walk(j, "using_filesort"), _walk(j, "backward_index_scan")

            prod_idx = "user_id, hidden_at, last_message_at"
            full_idx = "user_id, hidden_at, last_message_at, conversation_id"
            # ① 生产现状：仅前缀排序可走索引
            fs, bw = _plan(prod_idx, "last_message_at DESC", "ix")
            assert not fs and bw, (
                "仅按索引前缀排序应当无 filesort 且走 backward index scan —— "
                f"实得 filesort={fs} backward={bw}，本机 MySQL 与实测基线不符，先查环境")
            # ② 生产现状：加 tiebreaker 后**无论方向**都 filesort（隐含主键后缀用不上）
            fs, bw = _plan(prod_idx, "last_message_at DESC, conversation_id DESC", "ix")
            assert fs and not bw, (
                "隐含主键后缀竟然满足了 ORDER BY —— 本 MySQL 行为已变，"
                f"「扩索引」那条待办可重估（filesort={fs} backward={bw}）")
            assert _plan(prod_idx, "last_message_at DESC, conversation_id ASC", "ix")[0]
            # ③ 显式含 tiebreaker 列：同向可走索引、混向不行 —— 方向规则的**真正**适用条件
            fs, bw = _plan(full_idx, "last_message_at DESC, conversation_id DESC", "ix")
            assert not fs and bw, (
                "显式含列的索引在同向下应免排序 + backward scan —— 「扩索引」这条补救不成立，"
                f"勿据此改 schema（filesort={fs} backward={bw}）")
            assert _plan(full_idx, "last_message_at DESC, conversation_id ASC", "ix")[0]
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
        conn.close()
