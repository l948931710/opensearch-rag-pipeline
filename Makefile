.PHONY: help install dev sim sim-all sim-sensitive sim-version test lint clean graph diagrams diagrams-list release-gate eval-baseline-freeze

# ═══════════════════════════════════════════════════════════════
# OpenSearch RAG Pipeline — Makefile
# ═══════════════════════════════════════════════════════════════

help: ## 显示帮助
	@echo ""
	@echo "  OpenSearch RAG Pipeline"
	@echo "  ────────────────────────────────────────"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Setup ──

install: ## 安装核心依赖
	pip install -e .

dev: ## 安装开发依赖 (含 pytest, ruff)
	pip install -e ".[dev]"

prod: ## 安装生产依赖 (含 opensearch-py, oss2, pymysql)
	pip install -e ".[production,ocr]"

api-install: ## 安装 API 依赖 (FastAPI + uvicorn)
	pip install -e ".[api,production]"

api: ## 启动 RAG 问答 API 服务（端口可用 RAG_API_PORT 覆盖，默认 8000）
	python -m uvicorn opensearch_pipeline.api:app --host 0.0.0.0 --port $${RAG_API_PORT:-8000} --reload

agent-worker: ## PR-3 Stage D：durable dispatch 独立 worker（需 RAG_AGENT_ENABLE + RAG_AGENT_DURABLE_DISPATCH）
	python -m opensearch_pipeline.agent_worker

# ── Simulation ──

sim: ## 运行 normal 场景模拟
	python -m opensearch_pipeline.run_simulation --scenario normal

sim-all: ## 运行全部 4 个场景
	@echo "\n═══ Normal ═══" && python -m opensearch_pipeline.run_simulation --scenario normal
	@echo "\n═══ Sensitive ═══" && python -m opensearch_pipeline.run_simulation --scenario sensitive
	@echo "\n═══ Multi-doc ═══" && python -m opensearch_pipeline.run_simulation --scenario multi
	@echo "\n═══ Version Update ═══" && python -m opensearch_pipeline.run_simulation --scenario version_update

sim-sensitive: ## 运行敏感文档场景
	python -m opensearch_pipeline.run_simulation --scenario sensitive

sim-version: ## 运行版本更新场景
	python -m opensearch_pipeline.run_simulation --scenario version_update

sim-dag1: ## 只运行 DAG 1
	python -m opensearch_pipeline.run_simulation --dag 1 --scenario normal

sim-dag2: ## 只运行 DAG 1,2
	python -m opensearch_pipeline.run_simulation --dag 1,2 --scenario normal

# ── Graph ──

graph: ## 打印 DAG 依赖图
	python -m opensearch_pipeline.run_simulation --graph

# ── Diagrams（把 .md 里的 mermaid 块离线导出成 SVG）──

diagrams: ## 渲染全仓库 mermaid 图为 SVG → docs/diagrams/（需 node/npx，首次拉 mmdc）
	python scripts/render_mermaid.py

diagrams-list: ## 只列出仓库里的 mermaid 块，不渲染（零依赖）
	python scripts/render_mermaid.py --list

# ── Test ──

test: ## 运行测试（perf F#48：xdist 并行，共享本地 dev 栈的模块经 conftest 分组同 worker 串行）
	python -m pytest tests/ -v --tb=short -n auto --dist loadgroup

test-serial: ## 运行测试（串行——排查并行相关 flake / 无 pytest-xdist 环境用）
	python -m pytest tests/ -v --tb=short

test-cov: ## 运行测试 + 覆盖率
	python -m pytest tests/ -v --cov=opensearch_pipeline --cov-report=term-missing

miniapp-test: ## 小程序纯函数单测（markdown/typewriter，node 内置 runner，无须 IDE）
	node --test fuling-rag-miniapp/tests/units.test.mjs

# ── Quality ──

lint: ## 代码检查
	python -m ruff check opensearch_pipeline/ tests/

lint-fix: ## 自动修复
	python -m ruff check --fix opensearch_pipeline/ tests/

# ── Clean ──

clean: ## 清理缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info/

# ── 本地受控 A/B 评测环境（详见 docs/local_eval_env.md）──

ab-up: ## 启动 A/B 双实例 (:8001 新管线 / :8002 旧对照)
	bash scripts/local_eval_env.sh up

ab-down: ## 停止 A/B 双实例 (ab-down ALL=1 额外清 :8000)
	bash scripts/local_eval_env.sh down $(if $(ALL),--all,)

ab-status: ## A/B 环境总览 (实例/索引/DB/配置)
	bash scripts/local_eval_env.sh status

ab-smoke: ## A/B 双端各问 1 题验证可用
	bash scripts/local_eval_env.sh smoke

# ── dim9 evaluation release gate (pre-deploy) — 详见 docs/eval_release_gate.md ──
RAG_PY ?= python3

release-gate: ## 部署前评测闸(dim9 闭环): run→auto-judge→merge --strict; exit≠0 阻断发布
	bash deploy/eval_release_gate.sh

general-ability-eval: ## 通用能力评测门(门2防劫持+门3分级路由,离线零网络; LIVE=1 加真实分诊/生成核验)
	$(RAG_PY) -m eval_harness.general_ability_eval $(if $(LIVE),--live,)

eval-baseline-freeze: ## 冻结评测基线(首次可接受 gate 后一次性): make eval-baseline-freeze RESULTS=<rundir>/report.json
	@test -n "$(RESULTS)" || { echo "用法: make eval-baseline-freeze RESULTS=<rundir>/report.json"; exit 2; }
	$(RAG_PY) -m eval_harness.run_eval baseline-freeze --results "$(RESULTS)" --baseline eval_harness/goldset/baseline.json

# ── agent 评测门(L7)：模型行为基线(工具触发/克制/审批触发/答案落地) — 见 eval_harness/agent/runner.py ──
agent-eval: ## agent 行为评测(live 真模型,零 DB;需 DashScope key)
	$(RAG_PY) -m eval_harness.agent.runner --provider live

agent-eval-gate: ## agent 评测出闸:对冻结基线,硬不变量恒 1.0;exit 2=破门
	$(RAG_PY) -m eval_harness.agent.runner --provider live --gate

agent-eval-baseline-freeze: ## 冻结 agent 评测基线: make agent-eval-baseline-freeze RESULTS=eval_harness/reports/agent_eval_<ts>.json
	@test -n "$(RESULTS)" || { echo "用法: make agent-eval-baseline-freeze RESULTS=<report.json>"; exit 2; }
	$(RAG_PY) -m eval_harness.agent.runner --freeze-baseline "$(RESULTS)"

# ── agent 压测（生产上线 stress test）— 设计/手册见 docs/agent_stress_test_launch_plan_2026-07-12.md ──
stress-smoke: ## 压测冒烟(mock 单测 + S0 基线;本地 MySQL 缺失时 DB 断言自动降级)
	$(RAG_PY) -m pytest tests/test_stress_harness_smoke.py -q
	$(RAG_PY) -m stress_harness.runner --scenario S0 --tier local --scale smoke

stress-local: ## 全场景矩阵 S0-S8(零成本 mock 档;需本地 MySQL): make stress-local SCALE=full
	$(RAG_PY) -m stress_harness.runner --scenario matrix --tier local --scale $(or $(SCALE),smoke)

stress-staging: ## staging 真实计费档(硬预算中止):需 STRESS_STAGING_ACK/STRESS_TARGET_URL/STRESS_TARGET_TOKEN
	@test "$(STRESS_STAGING_ACK)" = "I_UNDERSTAND_COSTS" || { echo "需 STRESS_STAGING_ACK=I_UNDERSTAND_COSTS(真实计费,手册见设计文档 §6)"; exit 2; }
	@test -n "$(STRESS_BUDGET_MODEL_CALLS)" || { echo "需 STRESS_BUDGET_MODEL_CALLS=<N>(模型调用硬预算)"; exit 2; }
	STRESS_STAGING_ACK=$(STRESS_STAGING_ACK) $(RAG_PY) -m stress_harness.runner --tier staging --budget-model-calls $(STRESS_BUDGET_MODEL_CALLS)
