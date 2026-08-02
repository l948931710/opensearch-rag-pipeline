# -*- coding: utf-8 -*-
"""T12 守卫 —— 【检索投影轴】的写入口必须始终是可枚举的一小撮(node-ACL 设计稿 §9.1)。

投影轴 = `chunk_meta.owner_dept` / `chunk_meta.allowed_depts` + HA3 记录。
`acl_policy.project_doc_acl` 是唯一注入点，它保证【模式互斥】:
    legacy ⇒ (真实 owner, 组码集)   ·   node ⇒ (哨兵, 仅 d:/dx:)
绕过它的入口一旦出现，后果不是报错而是**静默权限重开** —— node 文档被写回真实 owner，
retriever 的 legacy owner 分支照样放行，哨兵白设; 反向漏 `allowed_depts` 则让 node 文档
谁也搜不到。两个方向都不会有任何异常抛出。

**两类入口都要抓**（2026-07-31 codex 评审：只做词法扫描会漏掉语义绕过）:
  · 词法 —— 直接写 `chunk_meta` 的投影列、手工构造 HA3 `cmd:add` 记录;
  · 语义（AST）—— 对 `build_dag3_chunk_to_opensearch` / `_push_chunks_to_ha3` 的任何**引用**。
    三个 `scratch/*_stage3.py` 正是这种形态:本地没有任何 `cmd:add` 字面量，却自建 `Chunk`
    后直跑 DAG3，把陈旧 owner 推进生产 HA3。
    ⚠️ 刻意标"引用"而非"调用"——见 `_semantic_push_refs`：追调用点会被别名/walrus/getattr
    等一长串静态形态逐个打穿，补一种冒一种。

本守卫**不判断对错**，只钉死"数量与位置"——新增/移动一个写点就红，逼作者显式说明理由
并更新 allowlist。已按 T12 停用的脚本豁免，**但 tombstone 必须真的拦得住**（见
`_is_effective_tombstone`）——只加一行注释不算。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# tracked 面 = 会进镜像 / 随仓库分发的代码。tests/ 不扫（测试里出现这些字面量是正常的）。
_TRACKED_DIRS = ("opensearch_pipeline", "scripts", "dataworks_nodes", "eval_harness", "deploy")

_RE_UPDATE = re.compile(r"chunk_meta\s+SET\b", re.I)
_RE_INSERT = re.compile(r"INSERT\s+INTO\s+[\w`.{}()]*chunk_meta\b", re.I)
# 大小写不敏感 + 兼容 `cmd="add"` 形态（codex：`CMD:add` 与默认参数都曾漏报）
_RE_HA3_ADD = re.compile(r"""["']cmd["']\s*:\s*["']add["']|\bcmd\s*=\s*["']add["']""", re.I)
_RE_PROJ_COL = re.compile(r"owner_dept|allowed_depts", re.I)
_RE_TOMBSTONE = re.compile(r"T12-TOMBSTONE")

# 语义绕过：这两个入口会把调用方自建的 Chunk 直接推进 HA3，投影值完全由调用方决定。
_PUSH_CALLS = frozenset({"build_dag3_chunk_to_opensearch", "_push_chunks_to_ha3"})

# path → (写点数, 为什么它是受控的)
_ALLOWLIST = {
    "opensearch_pipeline/access_grants.py": (
        2, "materialize_doc_allowed_depts —— ACL 变更的唯一写实现（decide 端点 + reconcile 共用）;"
           "node 分支的 (owner, allowed_depts) 二元组由 project_doc_acl 产出"),
    "opensearch_pipeline/pipeline_nodes.py": (
        5, "摄取/推送的受控实现本体：write_chunk_meta 的 INSERT（node 投影由上游 project_doc_acl "
           "产出，失败即抛、绝不退回真实 owner）+ stage-3 的 cmd:add（经 Chunk.to_ha3_doc）+ "
           "_push_chunks_to_ha3 的模块内调用"),
    "opensearch_pipeline/dag_definitions.py": (
        1, "DAG 定义处组装 DAG3 —— 不构造任何 Chunk，投影仍由 DAG3 内部节点按权威产出"),
    "opensearch_pipeline/dataworks_orchestrator.py": (
        2, "生产 stage-3 官方编排入口：reload 时按权威重解析投影后才跑 DAG3（import + 调用各一处）"),
    "opensearch_pipeline/run_simulation.py": (
        2, "本地 simulate 运行器：跑的是真实 DAG3（投影仍由节点按权威产出），且 sim 下不连生产"),
}

# 运维机（scratch/）面的保留项：不 tombstone，但必须钉死其安全不变量。
#
# **当前为空 —— 刻意的**。曾登记 staging T1 探针，复审后改为 tombstone：字符串存在性的
# allowlist 守不住（断言被挪进注释/死代码、delete 移出 finally 都照样绿），而把它升级成
# AST 级不变量校验、只为保住一个实验已完成的一次性脚本，不划算（codex 2026-07-31）。
# 保留这套机制是为将来真有"必须可执行"的运维件时用；届时 invariants 请写成 AST 断言而非子串。
_SCRATCH_ALLOWLIST: dict = {}


# ── 扫描 ─────────────────────────────────────────────────────────────────────
def _semantic_push_refs(src: str):
    """AST：对 push 入口的任何**引用**（不是定义）。

    ⚠️ 刻意**不去证明"这个引用最终会被调用"的数据流** —— 那条路走不通。追调用点时，下列
    静态可解析形态逐个把扫描打穿（2026-07-31 codex 两轮实测，SCAN 均为空）:

        push = _push_chunks_to_ha3; push(...)            push: object = _push...   (AnnAssign)
        from x import _push_chunks_to_ha3 as push        (push := _push...)()      (NamedExpr)
        getattr(mod, "_push_chunks_to_ha3")()            obj.push = _push...; obj.push()
        name = "_push_chunks_to_ha3"; getattr(m, name)() def f(push=_push...): push()

    每补一种就冒出下一种。故改为**保守标记引用**:凡这些危险入口的名字出现在 `Name` 载入 /
    属性名 / 字符串常量（含两个字面量相加）里，一律计入写入口。宁可多报（多报只需在
    allowlist 里说明理由），不可漏放。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []                          # 语法错的文件根本跑不起来，词法规则仍会覆盖它
    out = []

    def _folded_str(node):
        """字面量字符串（含 `"a" + "b"` 这种拼接）→ 值；否则 None。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            lhs, rhs = _folded_str(node.left), _folded_str(node.right)
            return (lhs + rhs) if (lhs is not None and rhs is not None) else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for al in node.names:
                if al.name in _PUSH_CALLS:
                    out.append((f"REF:{al.name}", node.lineno))
            continue
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id                 # 读取该名字：调用、赋给别名、当默认参数传…
        elif isinstance(node, ast.Attribute):
            name = node.attr               # mod._push_chunks_to_ha3 / obj.push = ...
        elif isinstance(node, (ast.Constant, ast.BinOp)):
            name = _folded_str(node)       # getattr 的常量串 / 先存进变量的名字
        if name in _PUSH_CALLS:
            out.append((f"REF:{name}", node.lineno))
    return out


def _scan(src: str):
    """→ [(kind, line)]（按行号排序）。该文件里所有【投影轴写点】。"""
    hits = []
    for m in _RE_UPDATE.finditer(src):
        if _RE_PROJ_COL.search(src[m.end():m.end() + 300]):
            hits.append(("UPDATE", src[:m.start()].count("\n") + 1))
    for m in _RE_INSERT.finditer(src):
        if _RE_PROJ_COL.search(src[m.end():m.end() + 900]):
            hits.append(("INSERT", src[:m.start()].count("\n") + 1))
    for m in _RE_HA3_ADD.finditer(src):
        hits.append(("HA3_ADD", src[:m.start()].count("\n") + 1))
    hits += _semantic_push_refs(src)
    return sorted(hits, key=lambda h: h[1])


def _is_effective_tombstone(src: str) -> bool:
    """tombstone 是否**真的拦得住** —— 首个语句（docstring 除外）必须是无条件
    `raise SystemExit`。

    ⚠️ 只认 `T12-TOMBSTONE` 注释是可以被绕过的：给危险脚本加一行注释就能骗过扫描。
    ⚠️ **import 也不能放行**（2026-07-31 codex 实测：`import dangerous_side_effect` +
    `raise SystemExit()` 曾被判为有效）—— Python 的 import 会执行任意模块级副作用，
    而这些脚本恰恰习惯在 import 期读 `.env.production`、建连接。唯一例外是
    `from __future__ import ...`（语法要求必须在最前，且无副作用）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue                                          # 模块 docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue                                          # 语法强制前置，无副作用
        # 第一个真正的语句
        return isinstance(node, ast.Raise) and _raises_system_exit(node)
    return False


def _raises_system_exit(node: ast.Raise) -> bool:
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "SystemExit"


def _collect(dirs):
    found, tombstoned = {}, {}
    for d in dirs:
        base = _ROOT / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            hits = _scan(src)
            if not hits:
                continue
            rel = f.relative_to(_ROOT).as_posix()
            if _RE_TOMBSTONE.search(src):
                tombstoned[rel] = _is_effective_tombstone(src)
            else:
                found[rel] = hits
    return found, tombstoned


# ── tracked 面（CI 防线）─────────────────────────────────────────────────────
def test_no_unlisted_projection_writers():
    """未登记的投影轴写入口 = 直接红。新增请先想清楚为什么不能走 project_doc_acl。"""
    found, _ = _collect(_TRACKED_DIRS)
    unlisted = {p: h for p, h in found.items() if p not in _ALLOWLIST}
    assert not unlisted, (
        "发现未登记的【检索投影轴】写入口（绕过 project_doc_acl ⇒ 静默权限重开/node 文档失联）:\n"
        + "\n".join(f"  {p}: {h}" for p, h in sorted(unlisted.items()))
        + "\n处置（T12，设计稿 §9.1）:改调 acl_policy.project_doc_acl、或加 T12-TOMBSTONE 停用、"
          "或在 tests/test_acl_projection_writers.py 的 _ALLOWLIST 里登记理由。")


def test_allowlisted_writer_count_pinned():
    """已登记文件里【新增】写点同样要红 —— 否则 allowlist 会变成整文件豁免。"""
    found, _ = _collect(_TRACKED_DIRS)
    for path, (expected, reason) in _ALLOWLIST.items():
        hits = found.get(path, [])
        assert len(hits) == expected, (
            f"{path} 的投影轴写点数 {len(hits)} ≠ 登记的 {expected}（登记理由:{reason}）\n"
            f"  实际:{hits}\n"
            "  新增写点请确认它经 project_doc_acl / Chunk.to_ha3_doc 产出投影值，再更新 _ALLOWLIST。")


def test_tracked_tombstones_are_effective():
    """tracked 面的 tombstone 必须真的拒绝执行（只加注释不算）。"""
    _, tombstoned = _collect(_TRACKED_DIRS)
    assert tombstoned, "tracked 面一个 tombstone 都没有 —— 标记被摘掉了？"
    broken = [p for p, ok in tombstoned.items() if not ok]
    assert not broken, (
        f"这些文件带 T12-TOMBSTONE 标记但拦不住执行（首个可执行语句不是无条件 "
        f"raise SystemExit）:{broken}")


def test_tombstoned_exporter_blocks_before_side_effects():
    """具体钉死离线导出器：拦截必须早于 import 期读 .env.production 并连生产 RDS。"""
    src = (_ROOT / "scripts" / "export_full_to_oss_for_v2.py").read_text(encoding="utf-8")
    assert _RE_TOMBSTONE.search(src) and _is_effective_tombstone(src)
    assert src.index("raise SystemExit") < src.index('_load(".env")')


# ── 运维机本地审计（scratch/ 被 .gitignore；CI 上不存在 ⇒ skip）──────────────
#
# ⚠️ 这**不是** CI 防线。scratch/ 不入仓、不入镜像，所以本节只在运维机本地跑得到，
# 且 tombstone 无法随仓库分发到其他机器 —— T12 因此标 PARTIAL（设计稿 §9.1）。
def test_scratch_writers_are_tombstoned_or_allowlisted():
    """运维机本地审计：scratch/ 里能写生产投影的脚本必须已停用，或在 allowlist 里。"""
    if not (_ROOT / "scratch").exists():
        pytest.skip("scratch/ 不存在（CI 环境）—— 本审计只在运维机本地有意义")
    found, tombstoned = _collect(("scratch",))
    broken = [p for p, ok in tombstoned.items() if not ok]
    assert not broken, f"scratch 里这些 tombstone 拦不住执行:{broken}"
    unlisted = {p: h for p, h in found.items() if p not in _SCRATCH_ALLOWLIST}
    assert not unlisted, (
        "scratch/ 里存在未停用、未登记的生产投影写入口（运维机持 PROD-RW 即可直接跑）:\n"
        + "\n".join(f"  {p}: {h}" for p, h in sorted(unlisted.items()))
        + "\n处置:加 T12-TOMBSTONE + 无条件 raise SystemExit，或登记进 _SCRATCH_ALLOWLIST。")


def test_scratch_allowlist_is_empty():
    """**零例外**策略：scratch 面当前不允许任何 allowlist 项。

    空字典配空循环等于没保护（`test_scratch_allowlist_invariants_still_present` 恒绿）,
    所以这条显式断言。将来确需保留某个运维件时,请**同时**引入该文件专用的 AST 不变量
    校验器 —— 现有的子串存在性机制不够强（断言挪进注释/死代码照样绿）。
    """
    assert not _SCRATCH_ALLOWLIST, (
        "新增了 scratch allowlist 项，但 substring 形式的 invariants 守不住。"
        "请改为 AST 断言校验器，或直接 tombstone 该脚本。")


def test_scratch_allowlist_invariants_still_present():
    """allowlist 的前提是那些安全断言还在 —— 守卫被删掉时 allowlist 必须失效。"""
    if not (_ROOT / "scratch").exists():
        pytest.skip("scratch/ 不存在（CI 环境）")
    for rel, (reason, invariants) in _SCRATCH_ALLOWLIST.items():
        p = _ROOT / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        for inv in invariants:
            assert inv in src, (
                f"{rel} 的安全不变量 {inv!r} 不见了，但它仍在 _SCRATCH_ALLOWLIST 里"
                f"（登记理由:{reason}）。要么恢复该断言，要么改为 tombstone。")


# ── 扫描器自身的 fixture 测试（防止规则退化后静默放行）──────────────────────
@pytest.mark.parametrize("snippet,kind", [
    ('body = [{"CMD": "ADD", "fields": f}]', "HA3_ADD"),          # 大小写变体
    ('def push(cmd="add"): pass', "HA3_ADD"),                     # 默认参数形态
    ('run(build_dag3_chunk_to_opensearch())', "REF:build_dag3_chunk_to_opensearch"),
    ('_push_chunks_to_ha3(chunks, cfg)', "REF:_push_chunks_to_ha3"),
    ('cur.execute("UPDATE kb.chunk_meta SET owner_dept=%s", (o,))', "UPDATE"),
    ('cur.execute("INSERT INTO chunk_meta (chunk_id, owner_dept) VALUES (%s,%s)")', "INSERT"),
    # ↓ 以下全部是 2026-07-31 codex 两轮实测能整个绕过的形态（当时 SCAN 均为空）
    ('push = _push_chunks_to_ha3\npush(chunks, cfg)\n', "REF:_push_chunks_to_ha3"),
    ('push: object = _push_chunks_to_ha3\npush(c)\n', "REF:_push_chunks_to_ha3"),      # AnnAssign
    ('(push := _push_chunks_to_ha3)()\n', "REF:_push_chunks_to_ha3"),                  # walrus
    ('obj.push = _push_chunks_to_ha3\nobj.push()\n', "REF:_push_chunks_to_ha3"),       # 属性赋值
    ('from x import _push_chunks_to_ha3 as push\npush(chunks, cfg)\n', "REF:_push_chunks_to_ha3"),
    ('getattr(mod, "_push_chunks_to_ha3")(chunks, cfg)\n', "REF:_push_chunks_to_ha3"),
    ('name = "_push_chunks_to_ha3"\ngetattr(m, name)()\n', "REF:_push_chunks_to_ha3"), # 变量存名字
    ('getattr(m, "_push_chunks" + "_to_ha3")()\n', "REF:_push_chunks_to_ha3"),         # 字面量拼接
    ('globals()["_push_chunks_to_ha3"]()\n', "REF:_push_chunks_to_ha3"),
    ('def f(push=_push_chunks_to_ha3): push()\n', "REF:_push_chunks_to_ha3"),          # 默认参数
    ('a = build_dag3_chunk_to_opensearch\nb = a\nc = b\nd = c\ne = d\nf = e\nf()\n',
     "REF:build_dag3_chunk_to_opensearch"),                                            # 六级别名链
])
def test_scanner_catches_bypass_shapes(snippet, kind):
    assert kind in {k for k, _ in _scan(snippet)}, f"扫描器漏掉了这种绕过形态:{snippet!r}"


@pytest.mark.parametrize("snippet", [
    'globals()["".join(["_push_chunks_to_", "ha3"])]()',
    'getattr(mod, "_push_chunks_to_{}".format("ha3"))()',
])
def test_scanner_known_static_gaps_are_documented(snippet):
    """**已知漏报**（非回归）—— 常量折叠只做 str 字面量与 `+`，不做 `join`/`format`。

    钉死它们是为了让这条边界**可见**：若哪天扩展了折叠规则，本测试会红，提示同步更新
    设计稿 §9.1 的残留风险条目。T12 本就是 PARTIAL，此处不假装覆盖完全。
    """
    assert not _scan(snippet), "折叠规则扩展了 —— 请同步更新设计稿 §9.1 的残留风险"


def test_scanner_ignores_push_function_definition():
    """不抓定义（`def` 本身） —— 否则受控实现自己会把 allowlist 计数撑爆。"""
    assert not _scan("def _push_chunks_to_ha3(chunks, cfg):\n    return None\n")


@pytest.mark.parametrize("src,ok", [
    ('"""doc"""\n# T12-TOMBSTONE\nraise SystemExit("no")\n', True),
    ('from __future__ import annotations\nraise SystemExit("no")\n', True),   # 语法强制前置，无副作用
    # ★ 2026-07-31 codex 实测：这条曾被判 True —— import 会执行任意模块级副作用
    ('"""doc"""\nimport dangerous_side_effect\nraise SystemExit("no")\n', False),
    ('"""doc"""\n# T12-TOMBSTONE\nprint("side effect")\nraise SystemExit()\n', False),
    ('"""doc"""\n# T12-TOMBSTONE\nif x:\n    raise SystemExit()\n', False),   # 有条件 ⇒ 不算
    ('"""doc"""\n# T12-TOMBSTONE 只有注释\nrun()\n', False),
])
def test_tombstone_effectiveness_rules(src, ok):
    assert _is_effective_tombstone(src) is ok
