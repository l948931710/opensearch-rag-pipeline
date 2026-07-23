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


class _PlainSock:
    """无 cipher() 的裸 socket——客户端腿明文。"""


class _TlsSock:
    def cipher(self):
        return ("AES256-GCM-SHA384", "TLSv1.2", 256)


class _Conn:
    def __init__(self, row, sock=None):
        self._row = row
        if sock is not None:
            self._sock = sock

    def cursor(self):
        return _Cur(self._row)

    def close(self):
        pass


def _wire(monkeypatch, *, ssl_ca, simulate, row, sock=None):
    from opensearch_pipeline import readiness
    readiness._reset_cache()
    fake_cfg = SimpleNamespace(simulate_db=simulate,
                               rds=SimpleNamespace(ssl_ca=ssl_ca))
    monkeypatch.setattr("opensearch_pipeline.config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _Conn(row, sock=sock))
    return readiness


def test_cipher_probe_states(monkeypatch):
    # socket 不可达 → 回退 SHOW STATUS(非空=verified;空=代理语义不可裁决,放行可见)
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False,
              row=("Ssl_cipher", "ECDHE-RSA-AES128-GCM-SHA256"))
    assert r.rds_tls_cipher_status() == "tls_verified(ECDHE-RSA-AES128-GCM-SHA256)"
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False, row=("Ssl_cipher", ""))
    assert r.rds_tls_cipher_status() == "tls_unverifiable(proxy_status_empty)"
    r = _wire(monkeypatch, ssl_ca="", simulate=False, row=None)
    assert r.rds_tls_cipher_status() == "plaintext"
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=True, row=None)
    assert r.rds_tls_cipher_status() == "skipped"
    r._reset_cache()


def test_cipher_probe_client_socket_is_the_verdict(monkeypatch):
    """2026-07-21 rwlb 实弹坑锁死:客户端腿 TLS 实证时 SHOW STATUS 为空(代理报后端腿)
    【不得】判明文;socket 可达且非 TLS 才是真接线缺陷。"""
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False,
              row=("Ssl_cipher", ""), sock=_TlsSock())
    assert r.rds_tls_cipher_status() == "tls_verified(client-leg AES256-GCM-SHA384/TLSv1.2)"
    r = _wire(monkeypatch, ssl_ca="/x/ca.pem", simulate=False,
              row=("Ssl_cipher", "whatever"), sock=_PlainSock())
    assert r.rds_tls_cipher_status() == "ca_configured_but_plaintext"
    r._reset_cache()


def test_pool_wrapper_chain_is_penetrable():
    """锁包装链可穿透性:_rds_socket_of 必须能从 Guarded/DBUtils 包装挖到 _sock——
    链条变动时此测试先红,防探针静默退化成 fallback 分支。"""
    from opensearch_pipeline.readiness import _rds_socket_of

    class _Raw:
        _sock = _TlsSock()

    class _Steady:
        _con = _Raw()

    class _Guarded:
        _con = _Steady()

    assert isinstance(_rds_socket_of(_Guarded()), _TlsSock)


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
    with pytest.raises(RuntimeError, match="非 TLS"):
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


class TestRequireTlsFailClosed:
    """γ5（M8，Majors 批次 γ，codex 共识 2026-07-21）：RAG_RDS_REQUIRE_TLS=on 时
    探针对「无法实证客户端腿 TLS」fail-closed（分支形态：readiness 状态词裁决）；
    off（默认）维持上方告警放行契约。"""

    def test_unverifiable_status_raises(self, monkeypatch):
        monkeypatch.setenv("RAG_RDS_REQUIRE_TLS", "true")
        api = _wire_startup(monkeypatch, env="production",
                            status="tls_unverifiable(proxy_status_empty)")
        with pytest.raises(RuntimeError, match="RAG_RDS_REQUIRE_TLS"):
            api._rds_tls_startup_check()

    def test_probe_error_status_raises(self, monkeypatch):
        monkeypatch.setenv("RAG_RDS_REQUIRE_TLS", "true")
        api = _wire_startup(monkeypatch, env="staging", status="error")
        with pytest.raises(RuntimeError, match="RAG_RDS_REQUIRE_TLS"):
            api._rds_tls_startup_check()

    def test_verified_still_passes(self, monkeypatch):
        monkeypatch.setenv("RAG_RDS_REQUIRE_TLS", "true")
        api = _wire_startup(monkeypatch, env="production",
                            status="tls_verified(client-leg AES256-GCM-SHA384/TLSv1.2)")
        api._rds_tls_startup_check()                  # 实证通过，不抛

    def test_flag_off_unverifiable_keeps_pass(self, monkeypatch):
        monkeypatch.delenv("RAG_RDS_REQUIRE_TLS", raising=False)
        api = _wire_startup(monkeypatch, env="production",
                            status="tls_unverifiable(proxy_status_empty)")
        api._rds_tls_startup_check()                  # off：维持放行（既有拍板）


# ── posture 字段 ─────────────────────────────────────────────────────────────


def test_posture_upload_signing_key_field(monkeypatch):
    from opensearch_pipeline import readiness
    readiness._reset_cache()
    monkeypatch.delenv("RAG_UPLOAD_SIGNING_KEY", raising=False)
    assert readiness.security_posture_report()["upload_signing_key"] == "fallback_session_key"
    monkeypatch.setenv("RAG_UPLOAD_SIGNING_KEY", "k" * 32)
    assert readiness.security_posture_report()["upload_signing_key"] == "dedicated"


class TestRdsSslCiphersKnob:
    """RAG_RDS_SSL_CIPHERS（2026-07-22 镜像切换窗）：rwlb 代理仅支持静态 RSA 密钥交换 ×
    CPython≥3.10 create_default_context 的 PFS-only 套件清单 → 交集为空恒握手失败（镜像
    py3.11 中招、buildpack py3.9 幸免）。旋钮只对 RDS 这条连接注入套件白名单。
    锁四点（codex 共识 2026-07-22）：默认零行为差 / 空白串不触发 / 无 CA 仍显式明文 /
    字典单一形态绝不混传顶层 ssl_*。"""

    def _cfg(self, **kw):
        from opensearch_pipeline.config import RDSConfig
        return RDSConfig(**kw)

    def test_default_branch_byte_equal(self):
        """CA 有、ciphers 空 → 与旋钮引入前逐键相等的顶层形态（完整相等断言守零行为差）。"""
        args = self._cfg(ssl_ca="/x/ca.pem").pymysql_ssl_args()
        assert args == {"ssl_ca": "/x/ca.pem", "ssl_verify_cert": True,
                        "ssl_verify_identity": True}

    def test_blank_ciphers_stays_toplevel(self):
        """空白串（strip 后空）不触发字典形态。"""
        args = self._cfg(ssl_ca="/x/ca.pem", ssl_ciphers="   ").pymysql_ssl_args()
        assert "ssl" not in args
        assert args["ssl_ca"] == "/x/ca.pem"

    def test_ciphers_without_ca_still_plaintext(self):
        """无 CA 时 ciphers 不改变显式明文语义（TLS 的开关唯 RAG_RDS_SSL_CA）。"""
        args = self._cfg(ssl_ciphers="AES256-GCM-SHA384").pymysql_ssl_args()
        assert args == {"ssl_disabled": True}

    def test_ciphers_branch_dict_only_no_toplevel_mix(self):
        """字典单一形态：返回值只许 {"ssl": {...}}——任何顶层 ssl_* 键混入都会让
        pymysql 整体重建 ssl 配置丢弃字典（丢 CA + 关 hostname 校验的既知雷）。"""
        args = self._cfg(ssl_ca="/x/ca.pem",
                         ssl_ciphers="ECDHE+AESGCM:AES256-GCM-SHA384").pymysql_ssl_args()
        assert set(args) == {"ssl"}
        assert args["ssl"] == {"ca": "/x/ca.pem", "check_hostname": True,
                               "verify_mode": True,
                               "cipher": "ECDHE+AESGCM:AES256-GCM-SHA384"}

    def test_ciphers_branch_verify_off_maps_both_false(self):
        """ssl_verify_cert=False → 字典两键同为 False（与顶层形态语义等价）。"""
        args = self._cfg(ssl_ca="/x/ca.pem", ssl_verify_cert=False,
                         ssl_ciphers="AES256-GCM-SHA384").pymysql_ssl_args()
        assert args["ssl"]["check_hostname"] is False
        assert args["ssl"]["verify_mode"] is False

    def test_load_config_env_wiring(self, monkeypatch):
        """RAG_RDS_SSL_CIPHERS 从环境变量真接线（不只测直构 dataclass）。"""
        monkeypatch.setenv("RAG_RDS_SSL_CIPHERS", "AES256-GCM-SHA384:AES128-GCM-SHA256")
        from opensearch_pipeline.config import load_config
        cfg = load_config()
        assert cfg.rds.ssl_ciphers == "AES256-GCM-SHA384:AES128-GCM-SHA256"

