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
                      ("opensearch_pipeline.org_sync", "_children_cache_clear"),
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
    "test_classification.py", # RDS 集成用例
    "test_image_funnel.py",   # RDS 集成用例
    "test_simulate_prod_guard.py",  # 操纵 prod-guard/config 全局态
    "test_kb_db_integration.py",    # kb console 端点真库回归（qa/feedback/doc 真实 DML）
    "test_reconcile_races.py",      # F3 双连接锁序测试（真实行锁/两连接并发 DML）
    # PR-4：摄取租约故障注入——document_version 真实 DML + 行锁阻塞用例，与
    # test_pipeline（整表清空）/test_concurrency（行锁语义）同表，必须同组串行
    "test_ingest_lease_db.py",
    "test_kb_badge_parity_db.py",
    # 2026-08-05：真连本地栈且 `DROP DATABASE` 固定库名（比无 WHERE DELETE 更烈——整库没了）。
    # 与同形态的 test_kb_badge_parity_db 一致处置；库名另加 pid 后缀做单边兜底（锁是双边协议）。
    "test_pagination_stability.py",
    # 2026-08-06：退役期改归属→恢复→投影收敛（document_meta/document_version/chunk_meta/
    # dept_dim 真实 DML）与 skip-gate prior-status 对照实验（document_version 真实 DML +
    # 跑 node_build_canonical）。都按固定 doc_id 精确清理，但与 test_pipeline 的**无 WHERE
    # 整表清空**同表 —— 不进组就会被它连坐清掉种子，症状是"断言看到 0 行"而非报错。
    "test_retire_owner_change_convergence_db.py",
    "test_skip_gate_prior_version_status_db.py",
    # 贡献域 node 轴（2026-08-07，schema/067）：dept_dim/staff_dim/user_role/
    # dept_admin_*_grant/kb_contribution/document_meta/document_version/kb_doc_node_grant
    # 全是真实 DML，且与 test_pipeline 的**无 WHERE 整表清空**同表 —— 不进组就会被连坐清掉
    # 种子（症状是「断言看到 0 行」而非报错）。
    "test_contribution_node_axis_db.py",
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
def _local_stack_xproc_lock(request):
    """串行组成员在**跨 pytest 进程**维度也互斥（2026-08-05）。

    `_LOCAL_STACK_SERIAL_MODULES` 声明的是「这些模块不得并发」，但上面那个 xdist 分组
    钩子只能保证**单个 pytest 进程内**同 worker 串行。两个 pytest 同时跑（多 worktree /
    多会话并行开发）时该保证完全失效，本组的无 WHERE 整表 DML 会清掉对方在飞的 seed 行
    （实测：test_ingest_lease_db 25/25 红、test_kb_db_integration 7/30 红；干净单进程 12/12 绿）。
    这里补上进程级互斥，锁语义与降级行为见 tests/local_stack.local_stack_exclusive。

    ⚠️ 两条**边界**，别误读成结构性保证：
      · **双边协议**——只对同样带本 fixture 的进程有效；老 worktree 不带，照样能撞。
        故受害测试必须保留自己的兜底判据，不能因为有锁就删。
      · 本 fixture 是 **function scope**，只罩得住同为 function scope 的 fixture 与测试体
        （实测：conftest autouse 的 setup 早于模块级 autouse `clean_db`、teardown 晚于它）。
        **module/class/session scope 的 fixture 天生在锁外**（高 scope 先 setup、后 teardown）。
        今天组内没有高 scope 的真库 seed fixture（test_pipeline `dag2_ctx` 走 simulate、
        test_kb_badge_parity_db `_rows` 是纯内存），将来加则会静默逃逸——不做收集期硬门是因为
        「哪个 fixture 碰库」不可判定，一刀切会误伤上述两个无害 fixture。
    """
    base = os.path.basename(request.node.nodeid.split("::", 1)[0])
    if base not in _LOCAL_STACK_SERIAL_MODULES:
        yield
        return
    from tests.local_stack import (
        ensure_local_db_wired, local_stack_exclusive, sweep_stale_scratch_schemas,
    )
    if not ensure_local_db_wired():      # 无本地栈（CI 普通 test job）⇒ 零成本 no-op
        yield
        return
    sweep_stale_scratch_schemas()        # 每进程一次；回收崩溃遗留的 <前缀><pid> 探针库
    with local_stack_exclusive():
        yield


@pytest.fixture(autouse=True)
def _restore_global_config_cache():
    """`opensearch_pipeline.config._config` 是**进程级全局**，且惰性加载后**永不失效**。

    ── 这条 fixture 治的是什么（2026-08-04，xdist flake 家族根因）────────────────
    7 个测试模块用「`monkeypatch.setenv(...)` + `CONF._config = None`」来强制按新 env 重建
    配置。`monkeypatch` 会在测试结束**还原 env**，但**不会**还原这个缓存 ⇒ 缓存里留着
    **按已被还原的那份 env 建出来的 config**，后续所有测试都读到它。

    确定性复现（本次据以定位）：
        pytest tests/test_sensitive_guard_eval.py \
               tests/test_stream_gate.py::TestStreamGateE2E::test_flag_off_refusal_passthrough_unchanged
    前者设 `RAG_GENERAL_ABILITY_MODE=office` / `RAG_SENSITIVE_QUERY_GUARD` 并清缓存；
    后者本应看到「flag 全关」的纯拒答，实得带 `"source": "guard"` + `"suggest_titles"` 的改道流
    ⇒ **必红**。单跑必绿。

    在 xdist 下，conftest 按**模块**分组（`group = mod`）⇒ 同模块内顺序确定、不会互踩；
    但一个 worker 会**顺序跑很多模块**，谁先谁后随分发变化 ⇒ 表现为**间歇性**红。
    这就是 `test_stream_gate` / `test_miniapp_serving` 那一族「单跑必绿、全量偶红、
    干净树亦复现」的成因——**不是 xdist 的问题，是进程级状态泄漏**。

    修法只还原**对象身份**，不无条件重建：只 monkeypatch 配置对象**属性**的测试（绝大多数）
    留下的是同一个对象、由 monkeypatch 自行还原，本 fixture 对它们零成本；只有真正**替换/清空**
    过缓存的测试才会被回滚。原值为 None（进程首测）时还原成 None，下次 get_config 按当时
    （已还原的）env 重建，语义正确。
    """
    import opensearch_pipeline.config as _C
    _saved = _C._config
    yield
    if _C._config is not _saved:
        _C._config = _saved


@pytest.fixture(autouse=True)
def _reset_node_acl_capability_cache():
    """node-ACL capability 探测的 positive-only 进程内缓存（access_grants，2026-07-31）。

    该缓存建模的是「这个物理库的 acl_mode 列已存在」这一**单调事实**，键 = (host, database)。
    生产里它永不失效；但测试用**同一套 config** 伪造「已 apply / 未 apply」两种 schema 形态，
    先跑的 True 会把后跑的「未 apply」用例污染成 node（实测：test_node_acl_write_paths.py::
    test_modes_default_legacy_when_migration_absent 单跑绿、全量红）。故每测清空。
    仅在模块已导入时处理（sys.modules 探测，零 import 成本）。"""
    import sys as _sys
    _mod = _sys.modules.get("opensearch_pipeline.access_grants")
    if _mod is not None and hasattr(_mod, "_NODE_SCHEMA_PRESENT"):
        _mod._NODE_SCHEMA_PRESENT.clear()
    yield
    _mod = _sys.modules.get("opensearch_pipeline.access_grants")
    if _mod is not None and hasattr(_mod, "_NODE_SCHEMA_PRESENT"):
        _mod._NODE_SCHEMA_PRESENT.clear()


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


# ── 🔴 告警出口在测试期必须断开（2026-08-06 实地事故）────────────────────────
# 事故经过：`RAG_OPS_ALERT_WEBHOOK` 一配进 `.env`，`get_config()` 就把它灌进 `os.environ`；
# 而 `alerting.send_ops_alert` 是**调用时**读 env 的。于是接下来的几轮 `make test` /
# 变异测试**真的往生产钉钉群发了一批告警**（test_alerting / test_queue_monitor /
# test_rate_limiter / test_reconcile 等多处会走到真实告警路径且未打桩）。
#
# ⚠️ 这个地雷在 webhook 未配时**完全不可见** —— 与本仓一路在修的「flag 关着掩盖缺陷」同族。
# ⚠️ 靠 `_LAST_SENT` 的 60s 去重挡不住：它是**进程内**的，每次 pytest / 每个 xdist worker
#    都是新进程，去重槽全新。
#
# 断在 env 这一层（而不是打桩 send_ops_alert）：alerting 每次调用都现读 env，
# 清掉它就等于让所有路径统一回到"未配 = 记账 no-op"，与生产未配时的行为完全一致。
@pytest.fixture(autouse=True, scope="session")
def _never_alert_from_tests():
    for k in ("RAG_OPS_ALERT_WEBHOOK", "RAG_OPS_ALERT_SECRET", "RAG_OPS_ALERT_WEBHOOK_ALLOW"):
        os.environ.pop(k, None)
    yield
