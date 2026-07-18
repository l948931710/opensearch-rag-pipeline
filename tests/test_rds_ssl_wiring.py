# -*- coding: utf-8 -*-
"""B3（生产级外审 2026-07-17 RB-02 残留）：RDS TLS 接线守护。

① 全仓 pymysql.connect 位点必须带显式 SSL 语义（pymysql_ssl_args / ssl_disabled）——
   堵「未配 ssl 的连接随 pymysql 2.x PREFERRED + 客户端 OpenSSL 版本漂移」类缺口；
② 八份内联 `_rds_ssl_kwargs` 拷贝结构一致（自包含脚本无法共享 import，靠测试锁同文）；
③ 内联 helper 行为（未配 CA 显式明文 / 配 CA 验证 TLS / verify 可关）；
④ readiness Ssl_cipher 探针状态词；⑤ api 启动自检 fail-fast 语义；⑥ posture 字段。
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOTS = ("opensearch_pipeline", "dataworks_nodes", "scripts", "deploy",
              "stress_harness", "eval_harness")
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
    """扫描六根：含 pymysql.connect 的文件必须含显式 SSL 语义标记。
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
        assert out["ssl"]["ca"] == "/x/aliyun-rds-ca.pem"
        assert out["ssl"]["check_hostname"] is True and out["ssl_verify_cert"] is True

    def test_verify_off_relaxes_hostname(self, inline_helper, monkeypatch):
        monkeypatch.setenv("RAG_RDS_SSL_CA", "/x/ca.pem")
        monkeypatch.setenv("RAG_RDS_SSL_VERIFY_CERT", "false")
        out = inline_helper()
        assert out["ssl"]["check_hostname"] is False and out["ssl_verify_cert"] is False


# ── readiness Ssl_cipher 探针 ─────────────────────────────────────────────────


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


def _wire(monkeypatch, *, ssl_ca, simulate, row):
    from opensearch_pipeline import readiness
    readiness._reset_cache()
    fake_cfg = SimpleNamespace(simulate_db=simulate,
                               rds=SimpleNamespace(ssl_ca=ssl_ca))
    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _Conn(row))
    return readiness


def test_cipher_probe_states(monkeypatch):
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False,
              row=("Ssl_cipher", "ECDHE-RSA-AES128-GCM-SHA256"))
    assert r.rds_tls_cipher_status() == "tls_verified(ECDHE-RSA-AES128-GCM-SHA256)"
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False, row=("Ssl_cipher", ""))
    assert r.rds_tls_cipher_status() == "ca_configured_but_plaintext"
    r = _wire(monkeypatch, ssl_ca="", simulate=False, row=None)
    assert r.rds_tls_cipher_status() == "plaintext"
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=True, row=None)
    assert r.rds_tls_cipher_status() == "skipped"
    r._reset_cache()


# ── api 启动自检 ──────────────────────────────────────────────────────────────


def _wire_startup(monkeypatch, *, env, status, ssl_ca="/x/ca.pem"):
    from opensearch_pipeline import api, readiness
    fake_cfg = SimpleNamespace(environment=env, simulate_db=False,
                               rds=SimpleNamespace(ssl_ca=ssl_ca))
    monkeypatch.setattr(api, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(readiness, "rds_tls_cipher_status", lambda: status)
    return api


def test_startup_check_fails_fast_on_configured_plaintext(monkeypatch):
    api = _wire_startup(monkeypatch, env="production", status="ca_configured_but_plaintext")
    with pytest.raises(RuntimeError, match="Ssl_cipher"):
        api._rds_tls_startup_check()


def test_startup_check_passes_on_verified_and_tolerates_probe_error(monkeypatch):
    api = _wire_startup(monkeypatch, env="production", status="tls_verified(X)")
    api._rds_tls_startup_check()                      # 不抛
    api = _wire_startup(monkeypatch, env="staging", status="error")
    api._rds_tls_startup_check()                      # 探针 error 不 brick

    api = _wire_startup(monkeypatch, env="development",
                        status="ca_configured_but_plaintext")
    api._rds_tls_startup_check()                      # 非 prod/staging 不硬断

    api = _wire_startup(monkeypatch, env="production",
                        status="ca_configured_but_plaintext", ssl_ca="")
    api._rds_tls_startup_check()                      # 未配 CA 归 P0-02 告警拍板，不在此断


# ── posture 字段 ─────────────────────────────────────────────────────────────


def test_posture_upload_signing_key_field(monkeypatch):
    from opensearch_pipeline import readiness
    readiness._reset_cache()
    monkeypatch.delenv("RAG_UPLOAD_SIGNING_KEY", raising=False)
    assert readiness.security_posture_report()["upload_signing_key"] == "fallback_session_key"
    monkeypatch.setenv("RAG_UPLOAD_SIGNING_KEY", "k" * 32)
    assert readiness.security_posture_report()["upload_signing_key"] == "dedicated"
