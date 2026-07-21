# -*- coding: utf-8 -*-
"""B3（生产级外审 2026-07-17 RB-02 残留，摘自 ontology-p0）：RDS TLS 接线守护。

① 全仓 pymysql.connect 位点必须带显式 SSL 语义（pymysql_ssl_args / ssl_disabled）——
   堵「未配 ssl 的连接随 pymysql 2.x PREFERRED + 客户端 OpenSSL 版本漂移」类缺口；
② 八份内联 `_rds_ssl_kwargs` 拷贝结构一致（自包含脚本无法共享 import，靠测试锁同文）；
③ 内联 helper 行为（未配 CA 显式明文 / 配 CA 验证 TLS / verify 可关）；
④ api 启动自检 fail-fast 语义（main 无 readiness 层——探针内联在
   api._rds_tls_startup_check，测试直接打它）。
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
# 分支版另含 stress_harness（压测线只落 ontology-p0，main 无此目录）
SCAN_ROOTS = ("opensearch_pipeline", "dataworks_nodes", "scripts", "deploy",
              "eval_harness")
_SSL_MARKERS = ("pymysql_ssl_args", "ssl_disabled")

# 内联 helper 的八份拷贝（自包含脚本；corpus_cleanup 走 cfg.pymysql_ssl_args 不在此列）
_INLINE_COPIES = [
    "dataworks_nodes/register_new_files.py",
    "dataworks_nodes/scan_oss_sync_keys.py",
    "scripts/dataworks_stage3_with_cleanup.py",
    "scripts/feedback_miner.py",
    "scripts/cleanup_ha3_old_chunks.py",
    "scripts/export_full_to_oss_for_v2.py",
    "scripts/validate_v2.py",
    "eval_harness/ha3live.py",
]


def test_every_pymysql_connect_site_has_explicit_ssl_semantics():
    """扫描五根：含 pymysql.connect 的文件必须含显式 SSL 语义标记。
    新增连接位点忘接线 ⇒ 本测试红（B3 sweep 曾抓出 ha3live/dbprobe 两个台账外漏网）。"""
    offenders = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if "pymysql.connect(" not in text:
                continue
            if not any(m in text for m in _SSL_MARKERS):
                offenders.append(str(p.relative_to(REPO)))
    assert not offenders, (
        f"pymysql.connect 位点缺显式 SSL 语义（接 **cfg.rds.pymysql_ssl_args() 或内联 "
        f"_rds_ssl_kwargs()，至少显式 ssl_disabled）：{offenders}")


def _helper_node(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_rds_ssl_kwargs":
            return node
    raise AssertionError(f"{path}: 未找到 _rds_ssl_kwargs")


def _logic_dump(node: ast.FunctionDef) -> str:
    """去 docstring 后的结构 dump（各拷贝注释措辞可异、逻辑必须逐字同构）。"""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return "\n".join(ast.dump(stmt) for stmt in body)


def test_inline_helper_copies_are_structurally_identical():
    ref = _logic_dump(_helper_node(REPO / _INLINE_COPIES[0]))
    for rel in _INLINE_COPIES[1:]:
        assert _logic_dump(_helper_node(REPO / rel)) == ref, (
            f"{rel} 的 _rds_ssl_kwargs 与参考拷贝逻辑不一致——八份内联必须同文")


@pytest.fixture()
def inline_helper():
    """AST 抽出 register_new_files 的 helper 执行（不 import 节点脚本——顶层会下载 zip）。"""
    node = _helper_node(REPO / _INLINE_COPIES[0])
    mod = ast.Module(body=[node], type_ignores=[])
    ns = {"os": os}
    exec(compile(mod, "<_rds_ssl_kwargs>", "exec"), ns)   # noqa: S102 — 测试专用
    return ns["_rds_ssl_kwargs"]


class TestInlineHelperBehavior:
    def test_no_ca_explicit_plaintext(self, inline_helper, monkeypatch):
        monkeypatch.delenv("RAG_RDS_SSL_CA", raising=False)
        assert inline_helper() == {"ssl_disabled": True}   # 显式明文，绝不给空 kwargs

    def test_ca_enables_verified_tls(self, inline_helper, monkeypatch):
        monkeypatch.setenv("RAG_RDS_SSL_CA", "/x/aliyun-rds-ca.pem")
        monkeypatch.delenv("RAG_RDS_SSL_VERIFY_CERT", raising=False)
        out = inline_helper()
        assert out["ssl_ca"] == "/x/aliyun-rds-ca.pem"
        assert out["ssl_verify_cert"] is True and out["ssl_verify_identity"] is True

    def test_verify_off_relaxes_hostname(self, inline_helper, monkeypatch):
        monkeypatch.setenv("RAG_RDS_SSL_CA", "/x/ca.pem")
        monkeypatch.setenv("RAG_RDS_SSL_VERIFY_CERT", "false")
        out = inline_helper()
        assert out["ssl_verify_cert"] is False and out["ssl_verify_identity"] is False

    def test_never_mixes_dict_and_toplevel_ssl_kwargs(self, inline_helper, monkeypatch):
        """2026-07-21 生产实弹坑:pymysql 见任一顶层 ssl_* 参数为真即重建 ssl 配置并丢弃
        ssl={...} 字典(ca→None 退系统信任库,check_hostname 被关)。锁死:带 CA 时输出
        只允许顶层参数形态,永不携带 'ssl' 字典键。"""
        monkeypatch.setenv("RAG_RDS_SSL_CA", "/x/ca.pem")
        monkeypatch.delenv("RAG_RDS_SSL_VERIFY_CERT", raising=False)
        assert "ssl" not in inline_helper()


# ── api 启动自检（自包含探针） ────────────────────────────────────────────────


class _Cur:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        assert "Ssl_cipher" in sql

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cur(self._row)

    def close(self):
        pass


def _wire_startup(monkeypatch, *, env, row, ssl_ca="/x/ca.pem", simulate=False,
                  conn_raises=False):
    from opensearch_pipeline import api
    fake_cfg = SimpleNamespace(environment=env, simulate_db=simulate,
                               rds=SimpleNamespace(ssl_ca=ssl_ca))
    monkeypatch.setattr(api, "get_config", lambda: fake_cfg)

    def _mk_conn(*a, **k):
        if conn_raises:
            raise OSError("db down")
        return _Conn(row)

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _mk_conn)
    return api


def test_startup_check_fails_fast_on_configured_plaintext(monkeypatch):
    api = _wire_startup(monkeypatch, env="production", row=("Ssl_cipher", ""))
    with pytest.raises(RuntimeError, match="Ssl_cipher"):
        api._rds_tls_startup_check()


def test_startup_check_passes_on_verified_cipher(monkeypatch):
    api = _wire_startup(monkeypatch, env="production",
                        row=("Ssl_cipher", "ECDHE-RSA-AES128-GCM-SHA256"))
    api._rds_tls_startup_check()                      # 不抛


def test_startup_check_scope_and_probe_error_tolerance(monkeypatch):
    api = _wire_startup(monkeypatch, env="development", row=("Ssl_cipher", ""))
    api._rds_tls_startup_check()                      # 非 prod/staging 不硬断

    api = _wire_startup(monkeypatch, env="production", row=None, ssl_ca="")
    api._rds_tls_startup_check()                      # 未配 CA 归 P0-02 告警拍板，不在此断

    api = _wire_startup(monkeypatch, env="production", row=None, simulate=True)
    api._rds_tls_startup_check()                      # simulate 跳过

    api = _wire_startup(monkeypatch, env="staging", row=None, conn_raises=True)
    api._rds_tls_startup_check()                      # 探针 error 不 brick
