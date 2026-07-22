# -*- coding: utf-8 -*-
"""B4（生产级外审 2026-07-17 P1-02/P2-11/P2-13）：DataWorks 供应链守护。

① 九个解压点全部走 _safe_extractall + _verify_zip_integrity，不留裸 extractall；
② 九份内联拷贝 AST 同构（粘贴脚本无法共享 import，测试锁同文——与 B3 _rds_ssl_kwargs 同法）；
③ _safe_extractall 预算行为（zip-slip/成员数/单成员/总量/压缩比/加密项）；
④ _verify_zip_integrity sidecar 语义（缺失过渡放行/匹配过/不匹配拒）；
⑤ 全部节点 py3.7 分支依赖锁版（AST 定位 sys.version_info 分支）；
⑥ workflows 全部 uses 按 40-hex SHA 钉死（与 trivy/Dockerfile 同纪律）。
"""
import ast
import hashlib
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent

# 下载 zip 的九个解压点（自包含粘贴脚本）
_EXTRACT_FILES = [
    "dataworks_nodes/stage1_node.py",
    "dataworks_nodes/stage2_node.py",
    "dataworks_nodes/stage3_node.py",
    "dataworks_nodes/ops_health_monitor_node.py",
    "dataworks_nodes/retention_node.py",
    "dataworks_nodes/gap_groups_node.py",
    "dataworks_nodes/ontology_backfill_node.py",
    "dataworks_nodes/ontology_invariants_node.py",
    "scripts/dataworks_stage3_with_cleanup.py",
]
# 全部装依赖的节点（钉版检查面）
_DEP_FILES = _EXTRACT_FILES + [
    "dataworks_nodes/register_new_files.py",
    "dataworks_nodes/scan_oss_sync_keys.py",
]
_WORKFLOWS = [".github/workflows/ci.yml", ".github/workflows/frontend.yml",
              ".github/workflows/stress.yml", ".github/workflows/image.yml"]


def test_no_bare_extractall_and_verify_wired():
    for rel in _EXTRACT_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert ".extractall('.')" not in text, f"{rel}: 仍有裸 extractall"
        assert "_safe_extractall" in text, f"{rel}: 缺 _safe_extractall"
        assert "_verify_zip_integrity" in text, f"{rel}: 缺 sidecar 完整性校验"


def _fn(rel, name):
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{rel}: 未找到 {name}")


def _logic_dump(node):
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return "\n".join(ast.dump(s) for s in body)


@pytest.mark.parametrize("helper", ["_safe_extractall", "_verify_zip_integrity"])
def test_helper_copies_structurally_identical(helper):
    ref = _logic_dump(_fn(_EXTRACT_FILES[0], helper))
    for rel in _EXTRACT_FILES[1:]:
        assert _logic_dump(_fn(rel, helper)) == ref, f"{rel} 的 {helper} 与参考拷贝不同文"


@pytest.fixture()
def safe_extractall():
    node = _fn(_EXTRACT_FILES[0], "_safe_extractall")
    ns = {"os": os}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<helper>", "exec"), ns)  # noqa: S102
    return ns["_safe_extractall"]


class _FakeZF:
    """预算路径专用假 ZipFile（不真解压）。"""

    def __init__(self, infos):
        self._infos = infos
        self.extracted = False

    def infolist(self):
        return self._infos

    def extractall(self, dest):
        self.extracted = True


def _info(name, size, csize=None, flag=0):
    return SimpleNamespace(filename=name, file_size=size,
                           compress_size=(size if csize is None else csize),
                           flag_bits=flag)


class TestSafeExtractallBudgets:
    def test_zip_slip_rejected(self, safe_extractall, tmp_path):
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escape.txt", "x")
        with zipfile.ZipFile(evil) as zf, pytest.raises(RuntimeError, match="Zip-Slip"):
            safe_extractall(zf, str(tmp_path / "out"))

    def test_member_flood_rejected(self, safe_extractall, tmp_path):
        z = _FakeZF([_info(f"f{i}.txt", 10) for i in range(4001)])
        with pytest.raises(RuntimeError, match="成员数超预算"):
            safe_extractall(z, str(tmp_path))
        assert not z.extracted

    def test_oversize_member_rejected(self, safe_extractall, tmp_path):
        z = _FakeZF([_info("big.bin", 201 * 1024 * 1024)])
        with pytest.raises(RuntimeError, match="单成员超预算"):
            safe_extractall(z, str(tmp_path))

    def test_ratio_bomb_rejected(self, safe_extractall, tmp_path):
        z = _FakeZF([_info("bomb.bin", 100 * 1024 * 1024, csize=1024)])
        with pytest.raises(RuntimeError, match="压缩比异常"):
            safe_extractall(z, str(tmp_path))

    def test_total_budget_rejected(self, safe_extractall, tmp_path):
        z = _FakeZF([_info(f"p{i}.bin", 150 * 1024 * 1024,
                           csize=100 * 1024 * 1024) for i in range(4)])
        with pytest.raises(RuntimeError, match="总解压量超预算"):
            safe_extractall(z, str(tmp_path))

    def test_encrypted_member_rejected(self, safe_extractall, tmp_path):
        z = _FakeZF([_info("sec.txt", 10, flag=0x1)])
        with pytest.raises(RuntimeError, match="加密成员"):
            safe_extractall(z, str(tmp_path))

    def test_normal_package_extracts(self, safe_extractall, tmp_path):
        ok = tmp_path / "ok.zip"
        with zipfile.ZipFile(ok, "w") as zf:
            zf.writestr("pkg/mod.py", "x = 1")
            zf.writestr("build_info.json", "{}")
        out = tmp_path / "out"
        out.mkdir()
        with zipfile.ZipFile(ok) as zf:
            safe_extractall(zf, str(out))
        assert (out / "pkg" / "mod.py").exists()


class _FakeResource:
    def __init__(self, content):
        self._c = content

    def open(self, mode="r"):
        import io
        return io.StringIO(self._c)


class _FakeOdps:
    def __init__(self, sidecars):
        self._s = sidecars

    def get_resource(self, name):
        if name not in self._s:
            raise KeyError(name)
        return _FakeResource(self._s[name])


@pytest.fixture()
def verify_zip(tmp_path):
    node = _fn(_EXTRACT_FILES[0], "_verify_zip_integrity")

    def _make(sidecars):
        ns = {"os": os, "odps": _FakeOdps(sidecars)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<helper>", "exec"), ns)  # noqa: S102
        return ns["_verify_zip_integrity"]

    zip_path = tmp_path / "opensearch_pipeline_production.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("pkg/mod.py", "x = 1")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return _make, str(zip_path), digest


class TestVerifyZipIntegrity:
    def test_sidecar_missing_transitional_pass(self, verify_zip):
        make, zip_path, digest = verify_zip
        assert make({})(zip_path) == digest            # 过渡期放行，返回 digest 留痕

    def test_sidecar_match_passes(self, verify_zip, monkeypatch):
        make, zip_path, digest = verify_zip
        fn = make({os.path.basename(zip_path) + ".sha256": digest + "\n"})
        # 节点内 sidecar 资源名 = zip 文件名 + .sha256（节点工作目录即 '.'）；
        # monkeypatch.chdir 自动还原——裸 os.chdir 会污染同 worker 后续用例的相对路径
        # （main 摘取时 g6 模板 open 实撞，2026-07-18 双分支同修）
        monkeypatch.chdir(os.path.dirname(zip_path))
        assert fn(os.path.basename(zip_path)) == digest

    def test_sidecar_mismatch_rejects(self, verify_zip, monkeypatch):
        make, zip_path, digest = verify_zip
        fn = make({os.path.basename(zip_path) + ".sha256": "0" * 64})
        monkeypatch.chdir(os.path.dirname(zip_path))
        with pytest.raises(RuntimeError, match="制品完整性校验失败"):
            fn(os.path.basename(zip_path))


def _py37_dep_lists(rel):
    """AST 定位 sys.version_info 分支，返回 py3.7 侧的字符串列表们。"""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        src = ast.dump(node.test)
        if "version_info" not in src:
            continue
        # `sys.version_info >= (3, 8)` → orelse=py3.7；`< (3, 8)` → body=py3.7
        branch = node.orelse if ("GtE" in src or "Gt" in src) else node.body
        for stmt in branch:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.List):
                    vals = [c.value for c in sub.elts
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                    if vals:
                        out.append(vals)
    return out


def test_all_nodes_py37_deps_fully_pinned():
    """P2-13 残留收口：每个节点 py3.7 分支的依赖清单必须全员 `==` 锁版
    （此前只有 ontology 一个节点有钉版回归测试）。"""
    unpinned = []
    for rel in _DEP_FILES:
        for deps in _py37_dep_lists(rel):
            for d in deps:
                if "==" not in d:
                    unpinned.append(f"{rel}: {d}")
    assert not unpinned, f"py3.7 分支存在未锁版依赖: {unpinned}"
    # 至少要真的扫到了清单（防 AST 定位失效假绿）
    assert sum(len(_py37_dep_lists(rel)) for rel in _DEP_FILES) >= 10


def test_workflows_all_actions_sha_pinned():
    """P2-11：uses 一律 40-hex commit SHA（mutable tag 漂移=构建执行代码被上游改写）。"""
    import re
    offenders = []
    for wf in _WORKFLOWS:
        for line in (REPO / wf).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- uses:") or s.startswith("uses:"):
                ref = s.split("@", 1)[1].split()[0] if "@" in s else ""
                if not re.fullmatch(r"[0-9a-f]{40}", ref):
                    offenders.append(f"{wf}: {s}")
    assert not offenders, f"workflows 存在未按 SHA 钉死的 action: {offenders}"


# ── δ3（M13，Majors 批次 δ，codex 共识 2026-07-21）：STRICT + HMAC 信任边界 ──────


@pytest.fixture(autouse=True)
def _dw_env_clean(monkeypatch):
    monkeypatch.delenv("RAG_DW_SIDECAR_STRICT", raising=False)
    monkeypatch.delenv("RAG_DW_ZIP_HMAC_KEY", raising=False)


def _hmac_of(key: str, digest: str) -> str:
    import hmac
    return hmac.new(key.encode("utf-8"), digest.encode("ascii"),
                    hashlib.sha256).hexdigest()


class TestSidecarStrictMode:
    def test_strict_without_key_rejects(self, verify_zip, monkeypatch):
        """STRICT on 而调度 env 无 key → 无从硬验，直接拒（缺 sidecar 与否无关）。"""
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_SIDECAR_STRICT", "true")
        monkeypatch.chdir(os.path.dirname(zip_path))
        with pytest.raises(RuntimeError, match="RAG_DW_ZIP_HMAC_KEY"):
            make({})(os.path.basename(zip_path))

    def test_strict_missing_sidecar_rejects(self, verify_zip, monkeypatch):
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_SIDECAR_STRICT", "on")
        monkeypatch.setenv("RAG_DW_ZIP_HMAC_KEY", "k1")
        monkeypatch.chdir(os.path.dirname(zip_path))
        with pytest.raises(RuntimeError, match="STRICT"):
            make({})(os.path.basename(zip_path))

    def test_strict_full_chain_passes(self, verify_zip, monkeypatch):
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_SIDECAR_STRICT", "1")
        monkeypatch.setenv("RAG_DW_ZIP_HMAC_KEY", "k1")
        monkeypatch.chdir(os.path.dirname(zip_path))
        base = os.path.basename(zip_path)
        fn = make({base + ".sha256": digest + "\n",
                   base + ".sha256.hmac": _hmac_of("k1", digest) + "\n"})
        assert fn(base) == digest


class TestHmacTrustBoundary:
    def test_key_with_valid_hmac_passes(self, verify_zip, monkeypatch):
        """默认（非 STRICT）+ key：sha256 对 + 签名对 → 放行。"""
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_ZIP_HMAC_KEY", "prod-key")
        monkeypatch.chdir(os.path.dirname(zip_path))
        base = os.path.basename(zip_path)
        fn = make({base + ".sha256": digest,
                   base + ".sha256.hmac": _hmac_of("prod-key", digest)})
        assert fn(base) == digest

    def test_key_with_forged_sidecar_rejects(self, verify_zip, monkeypatch):
        """核心威胁：攻击者换 zip+同步伪造 .sha256（sha256 面自洽）——签名对不上
        调度 env key → 拒。"""
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_ZIP_HMAC_KEY", "prod-key")
        monkeypatch.chdir(os.path.dirname(zip_path))
        base = os.path.basename(zip_path)
        fn = make({base + ".sha256": digest,
                   base + ".sha256.hmac": _hmac_of("attacker-key", digest)})
        with pytest.raises(RuntimeError, match="HMAC 校验失败"):
            fn(base)

    def test_key_missing_hmac_sidecar_rejects(self, verify_zip, monkeypatch):
        """key 已配而 .hmac 资源缺失（只传了两份旧资源）→ 硬拒（不静默降级回 sha256-only）。"""
        make, zip_path, digest = verify_zip
        monkeypatch.setenv("RAG_DW_ZIP_HMAC_KEY", "prod-key")
        monkeypatch.chdir(os.path.dirname(zip_path))
        base = os.path.basename(zip_path)
        fn = make({base + ".sha256": digest})
        with pytest.raises(RuntimeError, match="sha256.hmac"):
            fn(base)

    def test_no_key_keeps_legacy_behavior(self, verify_zip, monkeypatch):
        """无 key（默认）：hmac 面零接触——与 B4 现状 byte-identical（off 契约）。"""
        make, zip_path, digest = verify_zip
        monkeypatch.chdir(os.path.dirname(zip_path))
        base = os.path.basename(zip_path)
        assert make({base + ".sha256": digest})(base) == digest    # 不需要 .hmac 资源


def test_py37_requirements_mirror_parity():
    """δ3：requirements-dataworks-py37.txt 必须 == 节点 py3.7 分支钉版并集
    （改任一侧不改另一侧即红；文件是 ci pip-audit 的审计面）。"""
    union = set()
    for rel in _DEP_FILES:
        for deps in _py37_dep_lists(rel):
            union.update(d for d in deps if "==" in d)
    req = REPO / "requirements-dataworks-py37.txt"
    lines = {ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")}
    assert lines == union, (
        f"钉版镜像与节点 DEPS 漂移：文件多 {sorted(lines - union)}，缺 {sorted(union - lines)}")


def test_audit_baseline_file_shape():
    """基线文件可被 ci.yml 的 grep|sed 拼接消费：非注释行全部形如漏洞 ID。"""
    import re
    base = REPO / "deploy" / "dataworks_py37_audit_baseline.txt"
    ids = [ln.strip() for ln in base.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
    assert len(ids) >= 50                      # 快照基线量级 sanity
    bad = [i for i in ids if not re.fullmatch(r"[A-Z]+-[A-Za-z0-9-]+", i)]
    assert not bad, f"基线含非 ID 行（会拼进 --ignore-vuln 弄坏命令）: {bad}"
