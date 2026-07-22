# -*- coding: utf-8 -*-
"""
test_posture_guard.py — 生产安全姿态断言（批次5 P0-07d；2026-07-21 迁移批B1
自 claude/ontology-p0 移植，用例与分支 test_unknown_unknowns_batch5.py 同文）。

production/staging 启动必须显式表态：RAG_REQUIRE_AUTH + RAG_ACL_FAIL_CLOSED 双开，
或当日日期绑定的 RAG_ALLOW_LEGACY_OPEN_PROD=ack:<YYYY-MM-DD> 逃生口（P1-14，
午夜过期）。development 不受影响；RDS TLS 刻意不在本断言范围（P0-02 记录在案）。
"""
import pytest

from tests.test_config_loading import _fresh_load


class TestProdPostureAssertion:
    def test_missing_posture_raises(self):
        with pytest.raises(ValueError, match="RAG_REQUIRE_AUTH"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="")

    def test_partial_posture_names_the_missing_one(self):
        with pytest.raises(ValueError, match="RAG_ACL_FAIL_CLOSED"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="",
                        RAG_REQUIRE_AUTH="true")

    def test_posture_flags_on_passes(self):
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                          RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="",
                          RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")
        assert cfg.environment == "production"

    def test_legacy_ack_is_transitional_escape(self):
        """P1-14（外审核查 2026-07-16）：ack 绑当日日期（ack:<YYYY-MM-DD>，午夜过期）。"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        cfg = _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                          RAG_DASHSCOPE_API_KEY="x",
                          RAG_ALLOW_LEGACY_OPEN_PROD=f"ack:{today}")
        assert cfg.environment == "production"

    def test_legacy_bare_ack_rejected(self):
        """裸 `ack`（旧格式，无期限逃生口）→ 拒绝启动，报错给出当日格式。"""
        with pytest.raises(ValueError, match="ack:"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="ack")

    def test_legacy_stale_dated_ack_rejected(self):
        """过期日期的 ack → 拒绝（每次重启/重部署要求当日重签，缺口不被遗忘）。"""
        with pytest.raises(ValueError, match="ack:"):
            _fresh_load(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x",
                        RAG_ALLOW_LEGACY_OPEN_PROD="ack:2020-01-01")

    def test_development_unaffected(self):
        cfg = _fresh_load(RAG_SIMULATE="true", RAG_ALLOW_LEGACY_OPEN_PROD="")
        assert cfg.environment not in ("production", "staging")

    def test_staging_label_also_guarded(self):
        """staging 标签同受断言（本地 RAG_ENV=prod_ro/staging overlay 因此需补两 flag——
        docs/environment_design.md 部署清单）。"""
        with pytest.raises(ValueError, match="RAG_REQUIRE_AUTH"):
            _fresh_load(RAG_ENVIRONMENT="staging", RAG_SIMULATE="true",
                        RAG_DASHSCOPE_API_KEY="x", RAG_ALLOW_LEGACY_OPEN_PROD="")


class TestRdsTlsPosture:
    """γ5（M8，Majors 批次 γ，codex 共识 2026-07-21）：RAG_RDS_REQUIRE_TLS 姿态断言。
    默认 off=维持 P0-02 告警不阻断；on 且 prod/staging 真连 RDS 时 CA 非空+文件在+
    verify 开，缺一拒启动，**无 ack 逃生口**（回滚=关 flag）。"""

    _PROD_BASE = dict(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                      RAG_SIMULATE_DB="false",
                      RAG_RDS_HOST="rm-fake-nonprod.mysql.rds.aliyuncs.com",
                      RAG_ALLOW_REMOTE_DB="read_only_ack",
                      RAG_DASHSCOPE_API_KEY="x",
                      RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")

    def test_flag_off_no_ca_warns_only(self):
        """flag 未设（默认）：生产无 CA 维持 P0-02 告警不阻断——既有拍板 byte-identical。"""
        cfg = _fresh_load(**self._PROD_BASE)
        assert cfg.environment == "production" and not cfg.rds.ssl_ca

    def test_flag_on_no_ca_raises(self):
        from opensearch_pipeline.config import EnvironmentMismatchError
        with pytest.raises(EnvironmentMismatchError, match="RAG_RDS_REQUIRE_TLS"):
            _fresh_load(RAG_RDS_REQUIRE_TLS="true", **self._PROD_BASE)

    def test_flag_on_ca_file_missing_raises(self):
        from opensearch_pipeline.config import EnvironmentMismatchError
        with pytest.raises(EnvironmentMismatchError, match="CA 文件不存在"):
            _fresh_load(RAG_RDS_REQUIRE_TLS="true",
                        RAG_RDS_SSL_CA="/nonexistent/ca.pem", **self._PROD_BASE)

    def test_flag_on_verify_off_raises(self, tmp_path):
        from opensearch_pipeline.config import EnvironmentMismatchError
        ca = tmp_path / "ca.pem"
        ca.write_text("dummy")
        with pytest.raises(EnvironmentMismatchError, match="VERIFY_CERT"):
            _fresh_load(RAG_RDS_REQUIRE_TLS="true", RAG_RDS_SSL_CA=str(ca),
                        RAG_RDS_SSL_VERIFY_CERT="false", **self._PROD_BASE)

    def test_flag_on_full_posture_passes(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("dummy")
        cfg = _fresh_load(RAG_RDS_REQUIRE_TLS="true", RAG_RDS_SSL_CA=str(ca),
                          **self._PROD_BASE)
        assert cfg.rds.ssl_ca == str(ca) and cfg.rds.ssl_verify_cert is True

    def test_flag_on_simulate_db_skips(self):
        """simulate_db（make sim/单测形态）不评估——与 R1-R5 同前置。"""
        cfg = _fresh_load(RAG_RDS_REQUIRE_TLS="true", RAG_ENVIRONMENT="production",
                          RAG_SIMULATE="true", RAG_DASHSCOPE_API_KEY="x",
                          RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")
        assert cfg.environment == "production"

    def test_flag_on_development_unaffected(self):
        cfg = _fresh_load(RAG_RDS_REQUIRE_TLS="true", RAG_SIMULATE="true")
        assert cfg.environment not in ("production", "staging")

    def test_staging_no_ca_warns_p002(self, caplog):
        """γ5 前半：staging/test 分支补 P0-02 同文告警（无条件 warning-only）。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.config"):
            _fresh_load(RAG_ENVIRONMENT="staging", RAG_SIMULATE="true",
                        RAG_SIMULATE_DB="false",
                        RAG_RDS_HOST="rm-fake-nonprod.mysql.rds.aliyuncs.com",
                        RAG_ALLOW_REMOTE_DB="read_only_ack",
                        RAG_DASHSCOPE_API_KEY="x",
                        RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")
        assert any("[P0-02]" in r.message for r in caplog.records)


class TestOpsAlertWebhookPosture:
    """γ7（M10，Majors 批次 γ，codex 共识 2026-07-21）：RAG_OPS_ALERT_REQUIRE 姿态断言。
    on 且 prod/staging → webhook 非空且过 _webhook_allowed 域校验；配置存在≠送达证明。"""

    _BASE = dict(RAG_ENVIRONMENT="production", RAG_SIMULATE="true",
                 RAG_DASHSCOPE_API_KEY="x",
                 RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")

    def test_flag_off_no_webhook_passes(self):
        cfg = _fresh_load(**self._BASE)
        assert cfg.environment == "production"

    def test_flag_on_missing_webhook_raises(self):
        with pytest.raises(ValueError, match="RAG_OPS_ALERT_WEBHOOK 未配置"):
            _fresh_load(RAG_OPS_ALERT_REQUIRE="true", **self._BASE)

    def test_flag_on_bad_domain_raises(self):
        with pytest.raises(ValueError, match="未过域校验"):
            _fresh_load(RAG_OPS_ALERT_REQUIRE="true",
                        RAG_OPS_ALERT_WEBHOOK="https://evil.example.com/robot/send?access_token=x",
                        **self._BASE)

    def test_flag_on_http_scheme_raises(self):
        with pytest.raises(ValueError, match="未过域校验"):
            _fresh_load(RAG_OPS_ALERT_REQUIRE="true",
                        RAG_OPS_ALERT_WEBHOOK="http://oapi.dingtalk.com/robot/send?access_token=x",
                        **self._BASE)

    def test_flag_on_dingtalk_webhook_passes(self):
        cfg = _fresh_load(
            RAG_OPS_ALERT_REQUIRE="true",
            RAG_OPS_ALERT_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=x",
            **self._BASE)
        assert cfg.environment == "production"

    def test_flag_on_allowlisted_gateway_passes(self):
        cfg = _fresh_load(
            RAG_OPS_ALERT_REQUIRE="true",
            RAG_OPS_ALERT_WEBHOOK="https://alerts.corp.internal/hook",
            RAG_OPS_ALERT_WEBHOOK_ALLOW="alerts.corp.internal",
            **self._BASE)
        assert cfg.environment == "production"

    def test_flag_on_development_unaffected(self):
        cfg = _fresh_load(RAG_OPS_ALERT_REQUIRE="true", RAG_SIMULATE="true")
        assert cfg.environment not in ("production", "staging")

    def test_flag_on_staging_also_guarded(self):
        with pytest.raises(ValueError, match="RAG_OPS_ALERT_WEBHOOK"):
            _fresh_load(RAG_OPS_ALERT_REQUIRE="true", RAG_ENVIRONMENT="staging",
                        RAG_SIMULATE="true", RAG_DASHSCOPE_API_KEY="x",
                        RAG_REQUIRE_AUTH="true", RAG_ACL_FAIL_CLOSED="true")
