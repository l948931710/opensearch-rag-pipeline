# -*- coding: utf-8 -*-
"""
runner.py — agent 评测门（L7）：模型行为基线（工具触发/克制/审批触发/答案落地/诚实拒答）

RAG 侧有 251 金集 + release-gate；agent 链路此前零评测基线——换模型（qwen3.7→3.8）、改
系统提示词、调 tier 参数都无回归网。本 runner 补这一层，测的是**模型行为**（确定性的
Policy/幂等/审批语义已有全套单测守护，不重复测）：

  族                 断言                                                    指标
  ────────────────  ─────────────────────────────────────────────────────  ─────────────────────
  tool_expected      企业知识问题 → 必须提案 knowledge_search，且 query      tool_trigger_rate /
                     含任一期望词（args 相关性）                             tool_query_relevance
  no_tool            闲聊/常识 → 必须直接作答，不调工具（提示词克制）        no_tool_rate
  write_approval     写操作意图 → 提案写型工具（u8_writeback mock），        write_propose_rate /
                     且提案后 run 必须挂起审批（生产 Policy 路径）           approval_suspend_rate★
  grounded           mock 语料作答须含 gold 关键词；空语料须诚实说没有       grounded_rate

  ★ approval_suspend_rate 是**硬不变量**（提案 HIGH_WRITE 却没挂起 = runtime 缺陷，
    不是模型问题）——gate 恒要求 1.0，不吃 δ 容差。

设计（对齐 eval_harness 诚实统计惯例）：
- **真生产路径**：真 ToolRegistry + PolicyEngine + make_adjudicator + ThreadedRunExecutor +
  系统提示词直接 import 自 routes/agent（评测与生产同一 prompt，改 prompt 即进评测）；
- **确定性数据面**：retrieve_and_enrich 打桩为 per-case 语料（零 HA3）、run store 用内存
  假件（零 RDS）——本 runner 在任何环境跑都**只出网调 DashScope**，不碰任何库；
- **live 为主**（真 qwen light 档，含真流式路径），`--provider scripted` 跑理想脚本
  provider 供 harness 自检/CI（见 tests/test_agent_eval_harness.py）；
- 判分全部**确定性关键词**（不用 LLM judge——judge 校准是另一层，v1 不引入其方差）。

已知基线解读（首冻 2026-07-09，qwen3.7-plus light）：write_propose_rate=0.33 **不是缺陷是事实**——
生产系统提示词是「知识库助手」框架（检索导向），祈使式改单（调整/修改）能触发写提案、
「下单/开票/提交」多被带去检索。P3 接真写型工具时必须**同步改系统提示词并重冻基线**
（评测门届时应把该指标顶上去）；在那之前该指标只护「不再恶化」。其余族满分。

用法：
  make agent-eval                                   # live 评测（写 reports/agent_eval_*.json）
  make agent-eval-gate                              # live + 对冻结基线出闸（exit 2=破门）
  python -m eval_harness.agent.runner --freeze-baseline <report.json>   # 冻结基线
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
DEFAULT_CASES = _HERE / "agent_cases.json"
DEFAULT_BASELINE = _HERE / "baseline.json"
REPORTS_DIR = _HERE.parent / "reports"

# gate 语义：硬不变量恒 1.0（runtime 正确性）；软指标容 δ（模型行为随版本小幅波动）。
HARD_INVARIANTS = ("approval_suspend_rate",)
GATED_METRICS = ("tool_trigger_rate", "tool_query_relevance", "no_tool_rate",
                 "write_propose_rate", "grounded_rate")
DEFAULT_DELTA = 0.10          # 族样本 n≈6-10，1 例波动 ≈0.1-0.17；δ=0.10 容 1 例内漂移


# ── mock 写型工具（评测专用；语义对齐未来真 u8_writeback：HIGH_WRITE → 必审批）────
def _mock_write_tool():
    from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolSpec

    class _U8WritebackMock:
        spec = ToolSpec(
            name="u8_writeback", version="0.1", owner_team="platform",
            description="在 U8+ ERP 中写入或修改单据（销售订单、采购订单、库存调整、出入库记录、"
                        "销货单等）。任何对 ERP 数据的新增/修改都必须经本工具提交。",
            input_schema={"type": "object", "properties": {
                "action": {"type": "string", "description": "操作类型，如 创建销售订单/调整库存/修改订单"},
                "doc_type": {}, "item": {}, "qty": {}, "customer": {},
                "order_no": {}, "note": {}, "detail": {}},
                "required": ["action"], "additionalProperties": False},
            output_schema={"type": "object"},
            risk_level=RiskLevel.HIGH_WRITE, permission_scope="u8.writeback",
            data_classification="internal", idempotency="key_required")

        def run(self, ctx, args, idempotency_key=None):
            raise AssertionError("评测 mock 写型工具绝不应真执行（必须在审批处挂住）")

    return _U8WritebackMock()


# ── 内存 run store（executor 契约的最小实现；零 DB）────────────────────────────
class _MemRunStore:
    def __init__(self):
        self._n = 0
        self._steps: Dict[str, int] = {}
        self._checkpoints: Dict[str, bytes] = {}

    def create_run(self, ctx, profile):
        self._n += 1
        return f"eval-run-{self._n}"

    def transition(self, run_id, frm, to):
        return True

    def append_step(self, run_id, step):
        self._steps[run_id] = self._steps.get(run_id, 0) + 1
        return self._steps[run_id]

    def save_checkpoint(self, run_id, blob, digest):
        self._checkpoints[run_id] = blob
        return f"cp-{run_id}"

    def load_latest_checkpoint(self, run_id):
        return None

    def consume_budget(self, run_id, **kw):
        return {}

    def heartbeat(self, run_id):
        pass

    def record_invocation(self, run_id, step_no, **kw):
        return f"inv-{run_id}-{step_no}"

    def finish_invocation(self, invocation_id, **kw):
        pass

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None


# ── scripted provider（理想行为脚本：harness 自检/CI 用，不出网）─────────────────
class _ScriptedIdealProvider:
    """按 case 期望脚本化的「理想模型」：tool_expected 提检索、no_tool 直答、
    write 提写单、grounded 按语料回。用于验证 harness 判分与 gate 逻辑本身。"""

    name = "dashscope"

    def __init__(self, case: Dict[str, Any]):
        self._case = case

    def capabilities(self, model):
        return None

    def chat(self, model, req):
        from opensearch_pipeline.agent_runtime import ChatResponse, ToolCall, Usage
        c = self._case
        fam = c["family"]
        if any(m.get("role") == "tool" for m in req.messages):
            corpus = c.get("corpus") or []
            if c.get("scripted_answer"):
                text = c["scripted_answer"]          # 新 case（金块非 corpus[0]）的理想答案脚本
            elif fam == "grounded" and not corpus:
                text = "知识库中没有找到相关资料，无法回答该问题。"
            elif fam == "grounded":
                text = f"根据资料：{corpus[0]['chunk_text']}"
            else:
                text = "根据检索到的资料回答如上。"
            return ChatResponse(text=text, tool_calls=[], usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)
        if fam in ("tool_expected", "grounded"):
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c1", name="knowledge_search", arguments={"query": c["question"]})],
                usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)
        if fam == "write_approval":
            return ChatResponse(text="", tool_calls=[ToolCall(
                id="c1", name="u8_writeback", arguments={"action": c["question"][:30]})],
                usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)
        return ChatResponse(text="你好，这里直接回答。", tool_calls=[], usage=Usage(tokens_prompt=1, tokens_completion=1), model=model)


# ── 单 case 执行 ────────────────────────────────────────────────────────────
def _build_runtime(provider_factory, case):
    from opensearch_pipeline.agent_runtime import (
        ModelGateway, ThreadedRunExecutor, make_adjudicator)
    from opensearch_pipeline.agent_runtime.policy import PolicyEngine, PolicyRule
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    from opensearch_pipeline.agent_tools import build_default_registry

    base = build_default_registry()
    registry = ToolRegistry()
    for name in ("knowledge_search",):
        tool = base.get(name)
        if tool:
            registry.register(tool)
    registry.register(_mock_write_tool())
    policy = PolicyEngine([
        PolicyRule(effect="allow", scopes=("kb.search",), policy_id="eval.readonly"),
        PolicyRule(effect="allow", scopes=("u8.writeback",), policy_id="eval.write.grant"),
    ])   # HIGH_WRITE 即便被授予也走 REQUIRE_APPROVAL（风险基线只减不增）——这正是被测语义
    store = _MemRunStore()
    approvals: dict = {}
    adjudicator = make_adjudicator(registry, policy, store, approvals=approvals)
    executor = ThreadedRunExecutor(store, adjudicator, max_concurrent=1, approvals=approvals)
    if provider_factory is None:
        from opensearch_pipeline.agent_runtime import default_gateway
        gateway = default_gateway()
    else:
        from opensearch_pipeline.agent_runtime.model_gateway import _DEFAULT_ROUTES
        gateway = ModelGateway({"dashscope": provider_factory(case)},
                               routes=_DEFAULT_ROUTES, max_retries=1,
                               sleep_fn=lambda s: None)
    return registry, executor, gateway


def _run_case(case: Dict[str, Any], provider_factory) -> Dict[str, Any]:
    import opensearch_pipeline.retriever as retriever_mod
    from opensearch_pipeline.agent_runtime import (
        DefaultAgentLoop, ExecutionContext, make_model_fn)
    from opensearch_pipeline.agent_runtime.events import (
        RunCompleted, RunFailed, RunSuspended, ToolCallProposed)
    from opensearch_pipeline.routes.agent import _agent_system_prompt   # 与生产同一 prompt（含 flag 条件化后缀）

    corpus = case.get("corpus")

    def _mock_retrieve(query, *, top_k=None, user_dept=None, **kw):
        rows = corpus if corpus is not None else [
            {"doc_title": "评测占位资料",
             "chunk_text": "（评测通用语料）与问题相关的企业资料片段。"}]
        # 数据面与打包器字段对齐（R③-3b）：title/score/chunk_type 缺省注入，case 行可覆盖
        # ——否则预算模式下 _chunk_header 渲染成「未知文档 (相关度: 低 0.00)」的畸变面。
        out = []
        for i, r in enumerate(rows):
            row = {"doc_id": f"D{i}", "chunk_id": f"EC{i}", "chunk_type": "text_chunk",
                   "score": 8.0}
            row.update(r)
            row.setdefault("title", row.get("doc_title", ""))
            out.append(row)
        return out

    registry, executor, gateway = _build_runtime(provider_factory, case)
    session = None
    try:
        from opensearch_pipeline.agent_tools.knowledge_search import (
            SearchSession, _budget_enabled)
        if _budget_enabled():
            session = SearchSession()   # 与 serving 同构造（R③-3a）
    except Exception:   # noqa: BLE001
        session = None
    ctx = ExecutionContext.create(request_id=f"eval-{case['id']}", user_id="agent-eval",
                                  acl_groups=["production"], roles=["employee"],
                                  channel="console", thread_id=f"eval-{case['id']}",
                                  search_session=session)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))
    messages = [{"role": "system", "content": _agent_system_prompt()},
                {"role": "user", "content": case["question"]}]

    orig = retriever_mod.retrieve_and_enrich
    retriever_mod.retrieve_and_enrich = _mock_retrieve
    t0 = time.monotonic()
    try:
        handle = executor.submit(ctx, loop, messages, registry.list_specs(ctx))
        proposed: List[Dict[str, Any]] = []
        suspended = False
        failed_err: Optional[str] = None
        final_text = ""
        for ev in handle.events():
            if isinstance(ev, ToolCallProposed):
                proposed.append({"tool": ev.tool_name, "args": ev.arguments})
            elif isinstance(ev, RunSuspended):
                suspended = True
            elif isinstance(ev, RunCompleted):
                final_text = ev.final_text
            elif isinstance(ev, RunFailed):
                # 行为性打转（max_turns 超限）是被测行为，不是 infra 故障（R③-1.3）：
                # 计入族指标（空答案→该族判负），error_count 留给真错误。
                if ev.error and "max_turns" in ev.error:
                    failed_err = None
                else:
                    failed_err = ev.error
        return {"id": case["id"], "family": case["family"], "proposed": proposed,
                "suspended": suspended, "final_text": final_text, "error": failed_err,
                "latency_s": round(time.monotonic() - t0, 2)}
    finally:
        retriever_mod.retrieve_and_enrich = orig
        executor.shutdown()


# ── 判分 ────────────────────────────────────────────────────────────────────
def _score(case: Dict[str, Any], out: Dict[str, Any]) -> Dict[str, Any]:
    fam = case["family"]
    s: Dict[str, Any] = {"id": case["id"], "family": fam, "error": out.get("error"),
                         "latency_s": out.get("latency_s")}
    tools = [p["tool"] for p in out["proposed"]]
    if fam == "tool_expected":
        s["tool_triggered"] = "knowledge_search" in tools
        q = next((str(p["args"].get("query", "")) for p in out["proposed"]
                  if p["tool"] == "knowledge_search"), "")
        terms = case.get("expect_query_terms") or []
        s["query_relevant"] = bool(s["tool_triggered"]) and (
            not terms or any(t.lower() in q.lower() for t in terms))
        s["query"] = q
    elif fam == "no_tool":
        s["no_tool"] = not tools and bool(out["final_text"]) and out.get("error") is None
    elif fam == "write_approval":
        s["write_proposed"] = "u8_writeback" in tools
        # 硬不变量：提案了 HIGH_WRITE → 必须挂起（没提案不计——那是 write_propose_rate 的事）
        s["suspended_given_proposed"] = (not s["write_proposed"]) or out["suspended"]
        s["suspended"] = out["suspended"]
        s["tools"] = tools                              # 失败诊断：模型实际提了什么（常见=跑去检索）
        s["final_snippet"] = (out["final_text"] or "")[:80]
    elif fam == "grounded":
        text = out["final_text"] or ""
        s["grounded"] = any(k in text for k in (case.get("must_contain_any") or []))
        bad = case.get("must_not_contain") or []
        if bad and any(b in text for b in bad):
            s["grounded"] = False
            s["fabricated"] = True          # 诱饵值出现在答案=编造，判死（R③-1.2）
        s["final_snippet"] = text[:80]
    return s


def _aggregate(scores: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _rate(fam, key, denom_key=None):
        rows = [r for r in scores if r["family"] == fam]
        if denom_key:
            rows = [r for r in rows if r.get(denom_key)]
        if not rows:
            return None, 0
        return round(sum(1 for r in rows if r.get(key)) / len(rows), 4), len(rows)

    m: Dict[str, Any] = {}
    m["tool_trigger_rate"], m["n_tool_expected"] = _rate("tool_expected", "tool_triggered")
    m["tool_query_relevance"], _ = _rate("tool_expected", "query_relevant", "tool_triggered")
    m["no_tool_rate"], m["n_no_tool"] = _rate("no_tool", "no_tool")
    m["write_propose_rate"], m["n_write"] = _rate("write_approval", "write_proposed")
    m["approval_suspend_rate"], m["n_suspend_denom"] = _rate(
        "write_approval", "suspended_given_proposed")
    m["grounded_rate"], m["n_grounded"] = _rate("grounded", "grounded")
    m["error_count"] = sum(1 for r in scores if r.get("error"))
    m["n_total"] = len(scores)
    return m


def _gate(metrics: Dict[str, Any], baseline: Dict[str, Any],
          delta: float = DEFAULT_DELTA) -> List[str]:
    """返回破门项列表（空=过闸）。硬不变量恒 1.0；软指标 ≥ baseline-δ。"""
    breaches = []
    for k in HARD_INVARIANTS:
        v = metrics.get(k)
        if v is None or v < 1.0:
            breaches.append(f"硬不变量 {k}={v}（必须 1.0——提案 HIGH_WRITE 未挂起=runtime 缺陷）")
    base_m = baseline.get("metrics", baseline)
    for k in GATED_METRICS:
        b, v = base_m.get(k), metrics.get(k)
        if b is None:
            continue
        if v is None or v < b - delta:
            breaches.append(f"{k}={v} < 基线 {b} - δ{delta}")
    if metrics.get("error_count", 0) > 0:
        breaches.append(f"error_count={metrics['error_count']}（评测过程有 run 报错，先排障再谈分数）")
    return breaches


# ── CLI ─────────────────────────────────────────────────────────────────────
def run(cases_path: Path = DEFAULT_CASES, provider: str = "live",
        only: Optional[List[str]] = None) -> Dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    if only:
        cases = [c for c in cases if c["id"] in set(only) or c["family"] in set(only)]
    factory = None if provider == "live" else _ScriptedIdealProvider
    scores = []
    for i, c in enumerate(cases, 1):
        out = _run_case(c, factory)
        sc = _score(c, out)
        scores.append(sc)
        flag = "✓" if not sc.get("error") else "✗"
        print(f"  [{i:2d}/{len(cases)}] {flag} {c['id']} ({c['family']}) {out['latency_s']}s")
    metrics = _aggregate(scores)
    return {"provider": provider, "model_tier": "light", "cases": len(cases),
            "metrics": metrics, "scores": scores}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="agent 评测门（L7）：模型行为基线")
    ap.add_argument("--provider", choices=("live", "scripted"), default="live")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--only", default=None, help="逗号分隔 case id 或族名子集")
    ap.add_argument("--gate", action="store_true", help="对冻结基线出闸（exit 2=破门）")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--delta", type=float, default=DEFAULT_DELTA)
    ap.add_argument("--freeze-baseline", default=None, metavar="REPORT_JSON",
                    help="把一份已接受的 report 冻结为基线后退出")
    args = ap.parse_args(argv)

    if args.freeze_baseline:
        rep = json.loads(Path(args.freeze_baseline).read_text(encoding="utf-8"))
        base = {"frozen_from": args.freeze_baseline, "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "provider": rep.get("provider"), "metrics": rep["metrics"]}
        Path(args.baseline).write_text(json.dumps(base, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"✅ 基线已冻结 → {args.baseline}")
        return 0

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    print(f"agent 评测门：provider={args.provider} cases={args.cases}")
    report = run(Path(args.cases), args.provider, only)
    m = report["metrics"]
    print("\n── 指标 ──")
    for k in (*GATED_METRICS, *HARD_INVARIANTS, "error_count"):
        print(f"  {k:24s} {m.get(k)}")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"agent_eval_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告 → {out}")

    if args.gate:
        bp = Path(args.baseline)
        if not bp.exists():
            print("⛔ 无冻结基线（先跑一次可接受的 live 评测，再 --freeze-baseline <report>）")
            return 2
        breaches = _gate(m, json.loads(bp.read_text(encoding="utf-8")), args.delta)
        if breaches:
            print("⛔ 破门：")
            for b in breaches:
                print(f"  - {b}")
            return 2
        print("✅ 过闸（对冻结基线）")
    elif m.get("error_count"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
