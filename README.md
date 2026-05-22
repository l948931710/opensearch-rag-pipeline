# OpenSearch RAG Pipeline

企业级知识库文档处理管线：文件提取 → 分类脱敏 → 切分 → OpenSearch 索引。

## 架构

```
raw/ → DAG 1 → canonical/ → DAG 2 → rag-ready/ + chunk_meta → DAG 3 → OpenSearch
```

| DAG | 名称 | 节点数 | 职责 |
|-----|------|--------|------|
| DAG 1 | raw_to_canonical | 4 | 统一提取（PDF/DOCX/TXT/OCR） |
| DAG 2 | canonical_to_safe_chunk | 7 | 分类 → 脱敏 → 发布 → 切分 → 持久化 (write_chunk_meta) |
| DAG 3 | chunk_to_opensearch | 6 | 抢占锁定 (acquire_lock) → Embedding → Bulk Payload → OpenSearch 写入 → 旧版停用 (deactivate_old) |
| DAG 4 | retrieval_eval | 2 | 检索评测（Phase 3） |

### 跨 DAG 安全顺序

```
DAG 2: classify → detect → redact → publish → chunk → validate → write_chunk_meta
                                                                      │
                                                                 (必须先落盘)
                                                                      ▼
DAG 3: acquire_index_lock → generate_embeddings → build_opensearch_payload → push_to_opensearch → update_index_status → deactivate_old
```

> **关键不变量**：新 chunk 写入 RDS (`write_chunk_meta`) 并成功推送至 OpenSearch (`push_to_opensearch`) 必须在旧版本停用 (`deactivate_old`) 之前完成。
> 如果反过来，中间任何环节失败都会导致文档从索引中"消失"。

## 快速开始

```bash
# 1. 克隆
cd ~/Downloads/opensearch-rag-pipeline

# 2. 安装
pip install -e ".[dev]"

# 3. 运行模拟
make sim          # normal 场景
make sim-all      # 全部 4 个场景
make graph        # 打印 DAG 依赖图

# 4. 运行测试
make test
```

## 目录结构

```
opensearch-rag-pipeline/
├── opensearch_pipeline/
│   ├── config.py              # 配置中心（env vars）
│   ├── dag_engine.py          # DAG 引擎
│   ├── dag_definitions.py     # 4 条 DAG 定义
│   ├── pipeline_nodes.py      # 节点实现
│   ├── chunker.py             # 文档切分器
│   ├── run_simulation.py      # 模拟运行器
│   └── extraction/            # 统一提取层
│       ├── schema.py           # ExtractionResult / ExtractedBlock
│       ├── unified_extractor.py # 策略分发器
│       ├── pdf_extractor.py    # PDF 提取（PyPDF2）
│       ├── docx_extractor.py   # DOCX 提取（python-docx + regex）
│       ├── text_extractor.py   # TXT/MD 结构化解析
│       └── ocr_client.py       # Qwen-VL OCR（按页）
├── schema/
│   └── 001_opensearch_pipeline.sql  # RDS 表结构
├── tests/
│   ├── test_extraction.py     # 提取层测试
│   ├── test_chunker.py        # 切分器测试
│   └── test_pipeline.py       # DAG 集成测试
├── pyproject.toml
├── Makefile
├── .env.example
└── .gitignore
```

## 配置

复制 `.env.example` → `.env`，填入服务配置：

```bash
cp .env.example .env
```

模拟模式（`RAG_SIMULATE=true`）不需要任何外部服务。

## 模拟场景

| 场景 | 命令 | 说明 |
|------|------|------|
| normal | `make sim` | 标准 SOP 文档，低风险 |
| sensitive | `make sim-sensitive` | 含身份证/手机号，高风险隔离 |
| multi | `--scenario multi` | 2 个文档并行处理 |
| version_update | `make sim-version` | v1→v2 版本更新，旧 chunk 停用 |

## RDS 表

在 `schema/001_opensearch_pipeline.sql` 中定义：

- **chunk_meta** — Chunk 管理（索引数据源）
- **opensearch_bulk_job** — Bulk 索引任务跟踪
- **document_sensitive_finding** — 敏感信息检测记录
- **document_version 增量字段** — extraction/chunk/index 状态

## 依赖

| 包 | 用途 | 必需 |
|----|------|------|
| pypdf | PDF 文本提取 | 核心 |
| python-docx | DOCX 段落/表格 | 核心 |
| PyMuPDF | PDF→图片（OCR 前处理） | 可选 |
| opensearch-py | OpenSearch 写入 | 生产 |
| pymysql | RDS 连接 | 生产 |
| oss2 | 阿里云 OSS | 生产 |
| dashscope | Embedding / OCR / LLM | 生产 |
