# -*- coding: utf-8 -*-
"""PR-D（P0-08/09）：迁移工具守卫 + CI loader 清单一致性。

- apply_migration.classify_target：**物理指纹优先**——生产 host + 自报 dev 标签
  仍判 prod（旧洞：environment∈(development,test) 即判 local）。
- ci_load_schema.sh MANIFEST 解析：每个 schema/*.sql 都有登记、目标可被
  apply_migration 表达。
（分支版另有 ontology 家族清单/backfill 节点/apply_ontology_dbs 三组用例，
 依赖 ontology-p0 独有文件，随大合并回归——main 侧此处只保守卫与 splitter。）
"""
import os
import re

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


def _load_script(fname, modname):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(_REPO, "scripts", fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_split_statements_comment_semicolon_safe():
    """回归（staging 首灌实测 1064）：整行注释里的分号（011 头注「USE …; 再 …;」）
    不得把注释后半截切成"SQL"——先剥注释再切分。"""
    mod = _load_script("apply_migration.py", "apply_migration2")
    sql = ("-- 头注：USE fuling_knowledge; 后执行一遍，再 USE fuling_operation; 执行一遍。\n"
           "CREATE TABLE IF NOT EXISTS t (id INT); -- 内联注释保留\n"
           "INSERT INTO t VALUES (1);\n")
    stmts = mod._split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE")
    assert "后执行一遍" not in " ".join(stmts)


# ── P1-14：splitter 引号感知 + DELIMITER fail-closed ─────────────────────────


@pytest.fixture(scope="module")
def am():
    return _load_script("apply_migration.py", "apply_migration_p114")


class TestSplitterQuoteAware:
    """引号内分号绝不切分；三种 MySQL 引号 × 反斜杠转义 × 双写转义。"""

    @pytest.mark.parametrize("sql,expected", [
        # 三种引号内的分号
        ("INSERT INTO t VALUES ('a;b');", ["INSERT INTO t VALUES ('a;b')"]),
        ('INSERT INTO t VALUES ("a;b");', ['INSERT INTO t VALUES ("a;b")']),
        ("SELECT `col;on` FROM t;", ["SELECT `col;on` FROM t"]),
        # 反斜杠转义的引号不提前闭合（其后的 ; 仍在字符串内）
        ("INSERT INTO t VALUES ('a\\';b');", ["INSERT INTO t VALUES ('a\\';b')"]),
        ('INSERT INTO t VALUES ("a\\";b");', ['INSERT INTO t VALUES ("a\\";b")']),
        # 双写转义（'' / "" / ``；032 实战形态：COMMENT ''…'' 内含中文）
        ("INSERT INTO t VALUES ('it''s; fine');",
         ["INSERT INTO t VALUES ('it''s; fine')"]),
        ('SELECT "a""b;c";', ['SELECT "a""b;c"']),
        ("SELECT `we``ird;id`;", ["SELECT `we``ird;id`"]),
        # 反引号内反斜杠是字面量（MySQL 语义：不吞下一个字符）
        ("SELECT `a\\` FROM t;", ["SELECT `a\\` FROM t"]),
        # 引号内的注释起始符是字面量（数据不得被当注释剥掉）
        ("INSERT INTO t VALUES ('x -- not; comment');",
         ["INSERT INTO t VALUES ('x -- not; comment')"]),
        ("INSERT INTO t VALUES ('x /* no; */ y');",
         ["INSERT INTO t VALUES ('x /* no; */ y')"]),
        ("INSERT INTO t VALUES ('#not; comment');",
         ["INSERT INTO t VALUES ('#not; comment')"]),
        # 多语句 + 字符串分号混排
        ("INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c;d');",
         ["INSERT INTO t VALUES ('a;b')", "INSERT INTO t VALUES ('c;d')"]),
    ])
    def test_quoted_semicolons(self, am, sql, expected):
        assert am._split_statements(sql) == expected

    def test_multiline_string_line_start_comment_kept(self, am):
        """多行字符串里以 -- 开头的行是数据（旧 splitter 会整行剥掉——暗雷）。"""
        sql = "INSERT INTO t VALUES ('l1\n-- keep; me\nl3');"
        stmts = am._split_statements(sql)
        assert stmts == ["INSERT INTO t VALUES ('l1\n-- keep; me\nl3')"]

    def test_doubled_quote_comment_sample(self, am):
        """schema/032 活体同款（文件随 ontology-p0 合并落 main）：COMMENT ''…'' 双写
        转义 + @ddl 动态 PREPARE 必须干净切分——引号内分号/双写引号不切。"""
        text = (
            "SET @has := 0;\n"
            "SET @tbl := 'schema_migrations';\n"
            "SET @ddl := IF(@has = 0,\n"
            "  'ALTER TABLE schema_migrations ADD COLUMN checksum CHAR(64) NULL "
            "COMMENT ''迁移文件 SHA-256; 同名异 checksum 中止''',\n"
            "  'SELECT 1');\n"
            "PREPARE s FROM @ddl;\n"
            "EXECUTE s;\n"
            "DEALLOCATE PREPARE s;\n"
        )
        stmts = am._split_statements(text)
        assert len(stmts) == 6   # SET/SET/SET(@ddl)/PREPARE/EXECUTE/DEALLOCATE
        ddl = [s for s in stmts if "ADD COLUMN checksum" in s]
        assert len(ddl) == 1 and "''迁移文件 SHA-256" in ddl[0]

    def test_unterminated_quote_fail_closed(self, am):
        with pytest.raises(am.MigrationSqlError, match="未闭合"):
            am._split_statements("SELECT 'abc;")

    def test_unterminated_block_comment_fail_closed(self, am):
        with pytest.raises(am.MigrationSqlError, match="未闭合"):
            am._split_statements("SELECT 1 /* oops")


class TestSplitterComments:
    """注释内分号/引号不参与切分；/*! 保留；-- 需后跟空白（MySQL 规则）。"""

    @pytest.mark.parametrize("sql,expected", [
        # 行内注释里的分号（旧 splitter 会把 "-- note" 切成独立"语句"）
        ("SELECT 1; -- note; trailing\nSELECT 2;", ["SELECT 1", "SELECT 2"]),
        ("SELECT 1; # note; x\nSELECT 2;", ["SELECT 1", "SELECT 2"]),
        # 注释内引号不开字符串（否则后续全被吞成"字符串"）
        ("-- don't; won't\nSELECT 1;", ["SELECT 1"]),
        ("/* it's; a comment */ SELECT 1;", ["SELECT 1"]),
        # /*! versioned */ 与 /*+ hint */ 是可执行内容，必须保留
        ("/*!40101 SET NAMES utf8mb4 */;", ["/*!40101 SET NAMES utf8mb4 */"]),
        ("SELECT /*+ MAX_EXECUTION_TIME(1000) */ 1;",
         ["SELECT /*+ MAX_EXECUTION_TIME(1000) */ 1"]),
        # "--" 后无空白不是注释（5--3 = 5-(-3)）
        ("SELECT 5--3;", ["SELECT 5--3"]),
    ])
    def test_comment_semantics(self, am, sql, expected):
        assert am._split_statements(sql) == expected


class TestSplitterDelimiterFailClosed:
    """DELIMITER 指令 fail-closed：响亮抛错，绝不静默错切；字符串/语句中缝不误伤。"""

    @pytest.mark.parametrize("sql", [
        "DELIMITER $$\nCREATE PROCEDURE p() BEGIN SELECT 1; END$$\nDELIMITER ;\n",
        "delimiter ;;\nselect 1;;\ndelimiter ;\n",          # 小写
        "  DELIMITER $$\n",                                  # 缩进
        "SELECT 1;\nDELIMITER $$\n",                         # 语句之后
    ])
    def test_delimiter_raises(self, am, sql):
        with pytest.raises(am.MigrationSqlError, match="DELIMITER"):
            am._split_statements(sql)

    @pytest.mark.parametrize("sql,expected", [
        # 字符串里的 DELIMITER 是数据
        ("INSERT INTO t VALUES ('DELIMITER $$');",
         ["INSERT INTO t VALUES ('DELIMITER $$')"]),
        # 多行字符串行首 DELIMITER 仍是数据
        ("INSERT INTO t VALUES ('a\nDELIMITER $$\nb');",
         ["INSERT INTO t VALUES ('a\nDELIMITER $$\nb')"]),
        # 语句中缝的 delimiter 标识符（如列名）不误伤
        ("CREATE TABLE t (\n  delimiter VARCHAR(10)\n);",
         ["CREATE TABLE t (\n  delimiter VARCHAR(10)\n)"]),
    ])
    def test_delimiter_lookalikes_pass(self, am, sql, expected):
        assert am._split_statements(sql) == expected

    def test_all_schema_files_sweep(self, am):
        """全仓 schema/*.sql：存储过程文件（002/003/006/016/017/018 族）必须响亮
        fail-closed；其余必须干净切分出非空语句。防的是「新迁移悄悄用了 DELIMITER
        而 apply 工具静默错切」。"""
        delim_re = re.compile(r"(?mi)^\s*DELIMITER\b")
        schema_dir = os.path.join(_REPO, "schema")
        delim_files, clean_files = [], []
        for f in sorted(os.listdir(schema_dir)):
            if not f.endswith(".sql"):
                continue
            text = open(os.path.join(schema_dir, f), encoding="utf-8").read()
            if delim_re.search(text):
                delim_files.append(f)
                with pytest.raises(am.MigrationSqlError):
                    am._split_statements(text)
            else:
                clean_files.append(f)
                stmts = am._split_statements(text)
                assert stmts, f"{f} 切分出空语句列表"
                assert all(s.strip() for s in stmts), f
        assert "002_feedback_system.sql" in delim_files   # 正则没瞎匹配的哨兵
        assert "013_qa_retrieved_doc_fact.sql" in clean_files
