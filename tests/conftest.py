# -*- coding: utf-8 -*-
"""全局测试装置。

在收集任何测试模块之前接线本地 dev 栈（见 tests/local_stack.py）：
凭证/地址修正必须先于一切存储集成测试（含各模块 import 期的可用性探测）发生。

生产安全总闸（防 2026-06-13 整表误清重演）：
  1. 收集阶段硬闸 `_assert_no_prod_targets_at_collection()`：一旦在 simulate 关闭的情况下
     解析到生产指纹/非本地的 RDS 或生产 HA3 目标，直接 raise 让整个 pytest 收集失败——
     禁止在 `RAG_ENV=prod_ro/test/staging/production` 下跑测试套件（套件含真实 DML 与夹具）。
  2. 每个测试前的 autouse 守卫 `_refuse_prod_targets`：重读 live config 再判一次（兜住会话中
     config 单例被改写的情形），并 `_reset_db_pool()` 关闭"跨测试复用陈旧生产连接池"窗口。
默认 `make test`（无 RAG_ENV → simulate_db=True）下两道闸都短路，不影响既有绿测；
`RAG_ENV=local`（localhost + simulate off）的本地真实库集成测试也照常放行。
"""

import pytest

from tests.local_stack import ensure_local_db_wired, ensure_local_opensearch_wired

ensure_local_db_wired()
ensure_local_opensearch_wired()


def _prod_target_violations():
    """返回当前 live config 中"simulate 关闭却指向生产/非本地存储"的违规项列表（空=安全）。"""
    from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target

    cfg = get_config()
    violations = []
    # RDS：simulate_db 关闭时，host 必须本地且不得命中生产指纹（含 staging，因其与生产同物理实例）
    if not cfg.simulate_db:
        h = cfg.rds.host
        if h not in _LOCAL_HOSTS or is_prod_target("rds", h):
            violations.append(f"RDS host={h!r}")
    # HA3/检索：simulate_opensearch 关闭时，endpoint 不得命中生产指纹
    if not cfg.simulate_opensearch:
        ep = getattr(getattr(cfg, "alibaba_vector", None), "endpoint", "") or ""
        if is_prod_target("search", ep):
            violations.append(f"HA3 endpoint={ep!r}")
    return violations


def _assert_no_prod_targets_at_collection():
    violations = _prod_target_violations()
    if violations:
        raise RuntimeError(
            "[PROD-GUARD] 拒绝在指向生产的环境下运行测试套件——"
            + "; ".join(violations)
            + "。本套件含真实 DML 与夹具（部分为无 WHERE 整表语句），只允许默认 simulate 模式或"
            "本地 dev 栈（localhost）。如确需远端只读评测，请改用 prod_access 只读路径。"
        )


_assert_no_prod_targets_at_collection()


@pytest.fixture(autouse=True)
def _refuse_prod_targets():
    """每个测试前：再判一次生产目标（兜住会话中 config 单例被改写），并重置连接池。"""
    violations = _prod_target_violations()
    if violations:
        pytest.fail(
            "[PROD-GUARD] 测试解析到生产/非本地存储目标且 simulate 关闭——"
            + "; ".join(violations)
            + "，拒绝运行以防 WHERE-less DML 误打生产。",
            pytrace=False,
        )
    # 关闭"陈旧生产连接池跨测试复用"窗口：池为 None 时无副作用
    try:
        import opensearch_pipeline.pipeline_nodes as _pn

        _pn._reset_db_pool()
    except Exception:
        pass
    yield
