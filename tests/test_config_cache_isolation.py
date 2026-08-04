# -*- coding: utf-8 -*-
"""test_config_cache_isolation.py — 进程级 config 缓存必须逐测还原（2026-08-04）。

## 治的是什么

`opensearch_pipeline.config._config` 是**进程级全局**，惰性加载后**永不失效**
（`config.py:1280-1288`）。7 个测试模块用「`monkeypatch.setenv(...)` + `CONF._config = None`」
强制按新 env 重建配置；`monkeypatch` 会在测试结束**还原 env**，但**不会**还原这个缓存
⇒ 缓存里留着**按已被还原的那份 env 建出来的 config**，后续所有测试都读它。

## 它就是 xdist flake 家族的成因

conftest 按**模块**分组（`group = mod`）⇒ 同模块内顺序确定、不互踩；但一个 worker 会
**顺序跑很多模块**，谁先谁后随分发变化 ⇒ 表现为「单跑必绿、全量偶红、干净树亦复现」。
**不是 xdist 的问题，是进程级状态泄漏。**

## 定位时用的确定性复现（2026-08-04）

    pytest tests/test_sensitive_guard_eval.py \
           tests/test_stream_gate.py::TestStreamGateE2E::test_flag_off_refusal_passthrough_unchanged

前者设 `RAG_GENERAL_ABILITY_MODE=office` / `RAG_SENSITIVE_QUERY_GUARD` 并清缓存；
后者本应看到「flag 全关」的纯拒答，实得带 `"source": "guard"` + `"suggest_titles"` 的改道流
⇒ **必红**；单跑必绿。加上 conftest 的 `_restore_global_config_cache` 后即绿。

⚠️ **已知未覆盖**：flake 家族的另一员
`test_miniapp_serving::test_token_tamper_and_garbage` 的泄漏源**未定位** ——
用上面 7 个候选前导模块**都复现不出**（已在摘掉 fixture 的条件下逐个验过）。
本 fixture 是通用的（还原任何测试对该缓存的替换），**可能**覆盖它，但**未经证明**。
若它再次偶红，请从「其它进程级全局」方向查，别默认已修。
"""
import pytest

# 模块内测试按定义顺序执行（pytest 保证），故可用「前一条制造泄漏、后一条验证还原」的配对。
_BASELINE = {}


def test_a_replaces_the_process_wide_config_cache(monkeypatch):
    """前提条件：本测试**确实**替换了缓存对象（否则下一条的断言是空转）。"""
    import opensearch_pipeline.config as C
    _BASELINE["obj"] = C._config
    monkeypatch.setenv("RAG_GENERAL_ABILITY_MODE", "office")
    C._config = None
    C.get_config()                       # 按被改的 env 重建 ⇒ 缓存里换了一个对象
    assert C._config is not _BASELINE["obj"], (
        "没能造出泄漏——用例前提不成立，下一条测试会变成空转")


def test_b_cache_is_restored_for_the_next_test():
    """★ 上一条替换的缓存必须已被还原。

    没有 conftest 的 `_restore_global_config_cache`，这里拿到的是**按
    `RAG_GENERAL_ABILITY_MODE=office` 建出来的 config**，而那个 env 早已被 monkeypatch 还原
    ⇒ 本进程后续每一个读配置的测试都在用一份与当前 env 不符的配置。
    """
    import opensearch_pipeline.config as C
    assert "obj" in _BASELINE, "配对的前一条没跑——本测试不能单独执行"
    assert C._config is _BASELINE["obj"], (
        "进程级 config 缓存未被还原 ⇒ 后续测试会读到按已还原 env 建的配置（xdist flake 家族根因）")


def test_conftest_has_the_autouse_restore_fixture():
    """守卫：这条 fixture 必须是 autouse 的——不是 autouse 就等于没有。"""
    import pathlib
    src = pathlib.Path("tests/conftest.py").read_text(encoding="utf-8")
    i = src.find("def _restore_global_config_cache")
    assert i > 0, "conftest 缺 _restore_global_config_cache"
    head = src[max(0, i - 200):i]
    assert "autouse=True" in head, "该 fixture 不是 autouse —— 不会自动生效"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
