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

import os

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
    # OSS：simulate_oss 关闭时 bucket 不得命中生产桶（批次8，ultra conftest:34——此前只查
    # RDS+HA3：RAG_SIMULATE_OSS=false + 生产桶凭证可双闸全过，夹具 put/delete 直打生产 OSS；
    # 唯一兜底 GuardedBucket 曾漏 copy_object。精确匹配语义见 is_prod_target("oss")：
    # staging 桶名以生产桶名为前缀，子串匹配会误判）。
    if not getattr(cfg, "simulate_oss", True):
        _bucket = getattr(getattr(cfg, "oss", None), "bucket_name", "") or ""
        if is_prod_target("oss", _bucket):
            violations.append(f"OSS bucket={_bucket!r}")
    # 标准 OpenSearch（本地 dev 回退）：simulate_opensearch 关闭时 host 非本地即拒——
    # 远端写风险与 HA3 同级，而 search 指纹只覆盖 HA3 端点形态，标准 OpenSearch 按本地
    # 白名单判（local_stack 接线的本地栈 host=localhost 照常放行）。
    if not cfg.simulate_opensearch:
        _osh = getattr(getattr(cfg, "opensearch", None), "host", "") or ""
        _osh_bare = _osh.split("://")[-1].split(":")[0]
        if _osh and _osh_bare not in _LOCAL_HOSTS:
            violations.append(f"OpenSearch host={_osh!r}")
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
    # 清空性能批次引入的进程内缓存（query-embed LRU / 读时 ACL TTL / 看板 TTL / 机器人部门
    # TTL / 缺口 TTL），防止跨测试串数据；仅清已导入的模块（未导入则跳过，避免为清缓存反而拉起模块）。
    import sys as _sys
    for _mod, _fn in (("opensearch_pipeline.retriever", "_query_embed_cache_clear"),
                      ("opensearch_pipeline.dingtalk_identity", "_live_acl_cache_clear"),
                      ("opensearch_pipeline.dingtalk_identity", "_bot_dept_cache_clear"),
                      ("opensearch_pipeline.routes.kb_console", "_dashboard_cache_clear"),
                      ("opensearch_pipeline.routes.contribution", "_gaps_cache_clear"),
                      ("opensearch_pipeline.qa_facts", "_fact_state_clear")):
        try:
            m = _sys.modules.get(_mod)
            if m is not None:
                getattr(m, _fn)()
        except Exception:
            pass
    yield


@pytest.fixture(autouse=True)
def _dummy_llm_key_in_simulate(monkeypatch):
    """根治「测试依赖开发机 .env 的 ambient LLM key」这类雷（本地有 .env 全绿 / CI 干净检出全红）。

    症状回顾：generate_answer / generate_answer_stream 在调（已被测试 mock 的）传输层【之前】先过
    `if not llm.api_key: raise`（llm_generator.py:780）。本机 .env 供了真实 key → 门通过 → 本地绿；
    CI 的 actions/checkout / 全新 clone 无 .env → key 为空 → 在 mock 生效前就 raise → 每次 merge 都红。
    （2026-07-02 定位：test_stream_reasoning 的 3 个用例即因此长期在 CI 挂。）

    根治：simulate 模式下若 llm.api_key 为空，统一注入哑 key，让「key 存在」前置门通过——真实 LLM
    调用在 simulate 下本就被各测试 mock，哑 key 只满足存在性检查、不发任何网络。此后新写的 LLM
    路径测试无需再各自记得注入 key，也不会再依赖本机 .env。

    刻意的边界：
      - 只设 api_key【字段】，不碰 DASHSCOPE_API_KEY【env】→ 不触发模型名重解析（Gemini↔Qwen），
        也不影响 test_config_loading 里走 _fresh_load 的用例（它们另建 config，不读这个缓存对象）。
      - 仅 simulate；RAG_ENV=local 真库集成不注入，尊重真实配置。
      - 只管 llm；ocr/embedding 有测试刻意验「无 key」路径（如 test_real_extractors 直接
        OCRClient(api_key="")），不在此代劳。
      - monkeypatch 每测试结束自动还原 → 无跨测试泄漏；已显式用 llm_key_present 的测试值相同、不冲突。"""
    from opensearch_pipeline.config import get_config

    cfg = get_config()
    if getattr(cfg, "simulate", False):
        llm = getattr(cfg, "llm", None)
        if llm is not None and not getattr(llm, "api_key", ""):
            monkeypatch.setattr(llm, "api_key", "test-dummy-key", raising=False)
    yield


@pytest.fixture(autouse=True)
def _ocr_page_cache_off(monkeypatch):
    """G8 OCR 页缓存在测试套件里默认关闭。

    缓存键=模型+渲染字节 sha256——多个 OCR 测试用相同的合成页字节 + 相同桩模型名，
    默认开缓存会让后跑的测试命中先跑测试写下的条目（如"本页应 FAILED"却拿到缓存
    DONE 文本），且往仓库 scratch/ 落 sqlite。专门的缓存行为测试显式 setenv true
    + chdir(tmp_path) 隔离；monkeypatch 自动还原，无跨测试泄漏。"""
    if os.environ.get("RAG_OCR_PAGE_CACHE") is None:
        monkeypatch.setenv("RAG_OCR_PAGE_CACHE", "false")
    yield


@pytest.fixture
def llm_key_present(monkeypatch):
    """让 config.llm.api_key 在测试内非空 → node_classify 走"已配置 key"分支（其 LLM 调用由各测试自行 mock）。

    keyless CI 隔离：这些测试验证"key 已配置时分类正常进行"的路径，但缺 key 时 node 的 fail-safe
    会在 mock 触达前短路（→ mock 未被调用）。本 fixture 在测试内注入 dummy key 建立正确前置条件：
      - 不依赖开发机 .env；- 不发真实网络（run_gemini_classification 已被各测试 patch）；
      - monkeypatch 测试结束自动还原 → 无环境泄漏、无顺序依赖。
    非 autouse：仅显式声明该参数的测试启用。"""
    from opensearch_pipeline.config import get_config

    monkeypatch.setattr(get_config().llm, "api_key", "test-dummy-key", raising=False)
    return "test-dummy-key"


# ---------------------------------------------------------------------------
# perf F#48（pytest-xdist 并行化）：配合 `-n auto --dist loadgroup` 使用。
# 按模块名分组 = 复刻 --dist loadfile 语义（同文件测试落同 worker，天然兼容既有的
# 模块级 autouse fixture / import 期 env setdefault）；共享本地 dev 栈的模块（真实
# DML、含无 WHERE 整表清空的 reset fixture、并发认领互斥用例、prod-guard 全局态）
# 强制归入同一 group → 同一 worker 串行执行，防止并行 worker 互相清库/串状态。
# 串行运行（无 -n）时本钩子只是给 item 挂个惰性 marker，零行为影响。
# ---------------------------------------------------------------------------
_LOCAL_STACK_SERIAL_MODULES = {
    "test_pipeline.py",       # reset_db_state 整表清空 + 真实写 chunk_meta/bulk_job
    "test_concurrency.py",    # 真实并发认领互斥（行锁语义）
    "test_msg_dedup_rds_integration.py",  # P2-04b 钉钉去重真库并发（fuling_operation DML）
    "test_agent_dispatch_bind_db.py",     # R1 反向锚真库并发（agent_run DML+幂等 ALTER）
    "test_majors_alpha_m4_db.py",         # α3 幂等键真库并发（agent_run DML，uk_run_client_req）
    "test_majors_alpha_m6_db.py",         # α5 配额哨兵真库并发（approval_request DML+行锁）
    "test_classification.py", # RDS 集成用例
    "test_image_funnel.py",   # RDS 集成用例
    "test_simulate_prod_guard.py",  # 操纵 prod-guard/config 全局态
    "test_kb_db_integration.py",    # kb console 端点真库回归（qa/feedback/doc 真实 DML）
    # 批次6（P1-06）：agent 真库契约族——共享 fuling_operation 真实 DML（agent_run/
    # tool_registry/approval_*），并发 worker 互跑会串状态/互删治理行
    "test_agent_runtime_e2e_local_db.py",
    "test_agent_runtime_registry_store.py",
    "test_agent_runtime_approval_store.py",
    "test_agent_approval_integrity_db.py",
    "test_unknown_unknowns_batch5.py",   # 批次5 cross-heal 真库用例（agent_run/approval DML）
    # 2026-07-17 flake 根治：ontology 真库契约族——三文件共享 fuling_ontology 真实 DML。
    # 此前各自打标（__pyt_/__pyl_/__pys_）只隔离了「数据互删」，隔不开 InnoDB 锁：清扫的
    # LIKE 全扫 DELETE（next-key 锁）× mint 复合事务（ref_seq 热点行+FOR UPDATE）跨 worker
    # 死锁 → victim 整事务回滚、SteadyDB 单句重放 → uk_ref 撞号连环红（1062，机理详见
    # ontology/store.py::_begin）。store 审计还跨库写 fuling_operation.agent_audit_log，
    # 与 agent 真库族同表——必须并入同一 group 串行，独立成组仍会跨组死锁。
    "test_ontology_store.py",
    "test_ontology_link_semantics.py",
    "test_ontology_sem.py",
    # PR-3 Stage C：真库段对 fuling_operation.tool_invocation/agent_tool_operation 真实
    # DML（含刻意 1062 撞主键）——与 agent 真库族同表，并入同组串行
    "test_agent_pr3_operation_ledger.py",
    # PR-3 Stage D：真库段对 agent_run 真实 DML（durable cancel 标记往返）——同上
    "test_agent_pr3_stage_d.py",
    # PR-4：摄取租约故障注入——document_version 真实 DML + 行锁阻塞用例，与
    # test_pipeline（整表清空）/test_concurrency（行锁语义）同表，必须同组串行
    "test_ingest_lease_db.py",
    # F3（2026-07-21 自 main 9e87131 迁移）：双连接锁序测试——document_meta/chunk_meta/
    # document_version 真实行锁 + 两连接并发 DML，与摄取真库族同表，必须同组串行
    "test_reconcile_races.py",
}


# ⚠️ tryfirst 必须加：worker 进程里 conftest 在 _prepareconfig 阶段注册、WorkerInteractor 之后
# 注册（pluggy LIFO → 后注册先执行），若不 tryfirst，xdist 的 modifyitems（读 marker 拼 @group
# 后缀）会先于本钩子执行 → marker 打晚了 → loadgroup 退化为逐测试分发、DB 模块互相清库。
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # 不能用 pluginmanager.hasplugin("xdist") 判断：worker 端该插件名不存在（WorkerInteractor
    # 匿名注册）。import 探测在 controller/worker 两端行为一致；未安装 xdist 时跳过。
    try:
        import xdist  # noqa: F401
    except ImportError:
        return
    import os as _os

    for item in items:
        mod = item.nodeid.split("::", 1)[0]
        base = _os.path.basename(mod)
        group = "local-db-stack" if base in _LOCAL_STACK_SERIAL_MODULES else mod
        item.add_marker(pytest.mark.xdist_group(group))


@pytest.fixture(autouse=True)
def _reset_ready_probe_cache():
    """批次7（ultra api:593）：/api/ready 探针结果进程级 TTL 缓存——非 sim 的 readiness
    测试各自 monkeypatch 探针并断言当次结果，缓存跨测试残留会串台。仅在 api 已导入时
    重置（sys.modules 探测，零 import 成本）。"""
    import os as _os
    import sys as _sys
    _os.environ.setdefault("RAG_READY_CACHE_TTL_S", "0")   # 套件默认关缓存（专项测试显式覆盖）
    _mod = _sys.modules.get("opensearch_pipeline.api")
    if _mod is not None and hasattr(_mod, "_READY_CACHE"):
        _mod._READY_CACHE.update({"t": 0.0, "body": None, "ok": True})
    yield
