# -*- coding: utf-8 -*-
"""PR-D（P0-08/09）：迁移工具守卫 + CI loader 清单一致性 + 节点供应链防护。

- apply_migration.classify_target：**物理指纹优先**——生产 host + 自报 dev 标签
  仍判 prod（旧洞：environment∈(development,test) 即判 local）。
- ci_load_schema.sh MANIFEST 解析：每个 schema/*.sql 都有登记、目标可被
  apply_migration 表达、本体表族确实指向 fuling_ontology（PR-B 落库断言的源头）。
- ontology_backfill_node 的 Zip-Slip 防护与锁版本依赖。
"""
import os
import re
import zipfile

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── classify_target：物理指纹优先（P0-09）─────────────────────────────────────


@pytest.mark.parametrize("host,dbname,expect", [
    # 生产指纹 host + 生产库名 → prod（自报 environment 无发言权——函数根本不收它）
    ("rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com", "fuling_operation",
     (False, False, True)),
    # 生产指纹 host + _stg 库 → staging
    ("rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com", "fuling_operation_stg",
     (False, True, False)),
    # 本地 host → local
    ("127.0.0.1", "fuling_operation", (True, False, False)),
    ("localhost", "fuling_ontology", (True, False, False)),
    # 未知远端（非本地非指纹）→ 宁严不松按 prod
    ("some-unknown-remote.example.com", "fuling_operation", (False, False, True)),
])
def test_classify_target_fingerprint_first(host, dbname, expect):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "apply_migration", os.path.join(_REPO, "scripts", "apply_migration.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.classify_target(host, dbname) == expect


# ── ci_load_schema.sh MANIFEST 一致性（P0-08 配套）────────────────────────────


def _parse_manifest():
    text = open(os.path.join(_REPO, "scripts", "ci_load_schema.sh"), encoding="utf-8").read()
    m = re.search(r'MANIFEST="\n(.*?)"\n', text, re.S)
    assert m, "ci_load_schema.sh 里找不到 MANIFEST 块"
    entries = {}
    for line in m.group(1).strip().splitlines():
        fn, target = line.split()
        entries[fn] = target
    return entries


def test_manifest_covers_every_schema_file():
    entries = _parse_manifest()
    schema_dir = os.path.join(_REPO, "schema")
    files = sorted(f for f in os.listdir(schema_dir) if re.match(r"\d", f) and f.endswith(".sql"))
    missing = [f for f in files if f not in entries]
    assert not missing, f"schema 文件缺 MANIFEST 登记（loader 会 die）: {missing}"
    stale = [f for f in entries if f not in files]
    assert not stale, f"MANIFEST 登记了不存在的文件: {stale}"


def test_manifest_targets_expressible():
    """每个目标都能被 loader 与 apply_migration 表达（split=004/018 双库混排特例）。"""
    legal = {"knowledge", "operation", "ontology", "both", "split"}
    entries = _parse_manifest()
    bad = {f: t for f, t in entries.items() if t not in legal}
    assert not bad, f"非法目标: {bad}"


def test_manifest_ontology_family_in_ontology_db():
    """PR-B：027-030 必须整体落 fuling_ontology（独立库隔离的清单源头）。"""
    entries = _parse_manifest()
    for f in ("027_ontology_core.sql", "028_ontology_identity.sql",
              "029_ontology_link.sql", "030_sem_views.sql"):
        assert entries[f] == "ontology", (f, entries[f])
    assert entries["032_schema_migrations_checksum.sql"] == "both"


# ── ontology_backfill_node：供应链防护（P0-09）────────────────────────────────


def _node_source():
    return open(os.path.join(_REPO, "dataworks_nodes", "ontology_backfill_node.py"),
                encoding="utf-8").read()


def test_node_deps_pinned():
    src = _node_source()
    m = re.search(r"DEPS = \[(.*?)\]", src, re.S)
    assert m
    for dep in re.findall(r'"([^"]+)"', m.group(1)):
        assert "==" in dep, f"依赖未锁版本: {dep}"


def test_node_no_plaintext_credential_scaffold():
    src = _node_source()
    assert 'os.environ["RAG_RDS_PASSWORD"] = ' not in src
    assert "原样复制" not in src           # 旧「粘贴凭据」指引已移除
    assert "平台注入" in src


def test_node_zip_slip_guard(tmp_path):
    src = _node_source()
    assert "_safe_extractall" in src
    # 抽出守卫函数单测（不执行整个节点脚本——它需要 PyODPS 运行时）
    ns = {"os": os}
    func_src = re.search(r"def _safe_extractall\(.*?zf\.extractall\(dest_root\)", src, re.S)
    assert func_src
    exec(func_src.group(0), ns)   # noqa: S102 — 受控源码片段
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with zipfile.ZipFile(evil) as zf, pytest.raises(RuntimeError, match="Zip-Slip"):
        ns["_safe_extractall"](zf, str(tmp_path / "out"))
    ok = tmp_path / "ok.zip"
    with zipfile.ZipFile(ok, "w") as zf:
        zf.writestr("pkg/mod.py", "x = 1")
    out = tmp_path / "out2"
    out.mkdir()
    with zipfile.ZipFile(ok) as zf:
        ns["_safe_extractall"](zf, str(out))
    assert (out / "pkg" / "mod.py").exists()


def test_split_statements_comment_semicolon_safe():
    """回归（staging 首灌实测 1064）：整行注释里的分号（011 头注「USE …; 再 …;」）
    不得把注释后半截切成"SQL"——先剥注释再切分。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "apply_migration2", os.path.join(_REPO, "scripts", "apply_migration.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sql = ("-- 头注：USE fuling_knowledge; 后执行一遍，再 USE fuling_operation; 执行一遍。\n"
           "CREATE TABLE IF NOT EXISTS t (id INT); -- 内联注释保留\n"
           "INSERT INTO t VALUES (1);\n")
    stmts = mod._split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
    assert "后执行一遍" not in " ".join(stmts)
