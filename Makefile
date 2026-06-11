.PHONY: help install dev sim sim-all sim-sensitive sim-version test lint clean graph

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

api: ## 启动 RAG 问答 API 服务
	python -m uvicorn opensearch_pipeline.api:app --host 0.0.0.0 --port 8000 --reload

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

# ── Test ──

test: ## 运行测试
	python -m pytest tests/ -v --tb=short

test-cov: ## 运行测试 + 覆盖率
	python -m pytest tests/ -v --cov=opensearch_pipeline --cov-report=term-missing

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
