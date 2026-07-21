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
