# -*- coding: utf-8 -*-
"""
config.py — 管线配置中心

所有配置从环境变量读取，支持多环境 .env 文件。

环境切换:
  RAG_ENV=local       → .env + .env.local       (真实 API + 本地 MySQL/OpenSearch)
  RAG_ENV=test        → .env + .env.test        (真实 API + 阿里云 RDS/HA3，本地测试检索)
  RAG_ENV=production  → .env + .env.production  (阿里云生产，DataWorks/钉钉服务)
  未设置              → .env                    (默认，向后兼容)
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path

def _load_env_files():
    """按 RAG_ENV 加载对应的 .env 文件。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    # 找到项目根目录（config.py 所在目录的上一级）
    project_root = Path(__file__).resolve().parent.parent

    # 1. 先加载 .env（共享配置：API keys, model names）
    base_env = project_root / ".env"
    if base_env.exists():
        load_dotenv(base_env, override=False)

    # 2. 再加载 .env.{RAG_ENV}（环境特定配置：存储层地址/凭证）
    #    override=True → 环境文件覆盖 .env 中的同名变量
    rag_env = os.environ.get("RAG_ENV", "").lower()
    if rag_env:
        env_file = project_root / f".env.{rag_env}"
        if env_file.exists():
            load_dotenv(env_file, override=True)
        else:
            print(f"  ⚠️ RAG_ENV={rag_env} 但 {env_file} 不存在，仅使用 .env")

    # 3. 打印环境标识
    _print_env_banner(rag_env)

def _print_env_banner(rag_env: str):
    """启动时打印当前环境标识，避免误操作。"""
    rds_host = os.environ.get("RAG_RDS_HOST", "localhost")
    ha3_host = os.environ.get("RAG_HA3_ENDPOINT", "")
    os_host = os.environ.get("RAG_OPENSEARCH_HOST", "") or ha3_host
    env_label = os.environ.get("RAG_ENVIRONMENT", "development")

    if rag_env == "production":
        icon = "🚀"
        label = "PRODUCTION (阿里云生产)"
    elif rag_env == "test":
        icon = "🧪"
        label = "TEST (阿里云测试)"
    elif rag_env == "local":
        icon = "🏠"
        label = "LOCAL (本地开发)"
    else:
        icon = "⚙️"
        label = f"DEFAULT ({env_label})"

    print(f"  {icon} 环境: {label} | RDS={rds_host} | Search={os_host or 'localhost'}")

_load_env_files()


@dataclass
class OSSConfig:
    """阿里云 OSS 配置。"""
    endpoint: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    bucket_name: str = "fuling-knowledge-base"
    # OSS 路径前缀
    raw_prefix: str = "raw/"
    canonical_prefix: str = "processing/canonical/"
    redacted_prefix: str = "processing/redacted/"
    rag_ready_prefix: str = "rag-ready/"
    index_jobs_prefix: str = "index-jobs/opensearch/"
    quarantine_prefix: str = "quarantine/"


@dataclass
class RDSConfig:
    """阿里云 RDS MySQL 配置。"""
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "fuling_knowledge"
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 30


@dataclass
class OpenSearchConfig:
    """阿里云 OpenSearch 配置。"""
    host: str = ""
    port: int = 9200
    auth_user: str = ""
    auth_password: str = ""
    index_name: str = "fuling_knowledge_v1"
    use_ssl: bool = True
    verify_certs: bool = True
    # 工程限制
    max_bulk_size_bytes: int = 1_500_000    # 1.5MB per bulk (safe margin under 2MB)
    max_field_size_bytes: int = 1_000_000   # 1MB per text field
    bulk_timeout_seconds: int = 60


@dataclass
class AlibabaVectorSearchConfig:
    """阿里云 OpenSearch 向量检索版 (HA3 Engine) 配置。"""
    endpoint: str = ""                # 实例 API 域名（不包含 http:// 前缀）
    instance_id: str = ""             # 实例 ID
    access_user_name: str = ""        # 用户名
    access_pass_word: str = ""        # 密码
    table_name: str = "fuling_knowledge_vector"
    pk_field: str = "id"
    # 混合检索配置（BM25 + Dense + Sparse 三路融合）
    enable_hybrid: bool = True              # 启用 BM25 混合检索（False 则降级为纯向量检索）
    hybrid_fusion: str = "weighted"          # 融合策略："rrf" 或 "weighted"（基线测试 weighted R@1=100% > rrf 97.87%）
    rrf_rank_constant: int = 60             # RRF 融合的 rankConstant 参数
    knn_weight: float = 0.7                 # 加权模式下 kNN 权重
    text_weight: float = 0.3               # 加权模式下 text (BM25) 权重
    text_search_field: str = "chunk_text"   # BM25 全文检索字段名（需配置 TEXT 倒排索引）
    hybrid_knn_top_k: int = 100             # kNN 路的候选池大小


@dataclass
class EmbeddingConfig:
    """Embedding 模型配置。"""
    api_key: str = ""
    api_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "text-embedding-004"
    dimension: int = 1024
    batch_size: int = 10                    # API limit (DashScope limit is 10)
    max_retries: int = 3


@dataclass
class OCRConfig:
    """OCR + VLM 视觉配置。"""
    api_key: str = ""
    api_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-3.1-flash-lite"            # OCR 专用模型
    vlm_model: str = ""                              # VLM caption/审计模型（为空则 fallback 到 model）
    max_ocr_pages: int = 5
    ocr_threshold_chars: int = 100


@dataclass
class RebuildConfig:
    """VLM/OCR 版面重建（layout-rebuild）成本熔断配置：单文档 + 单次运行预算。

    单价默认值为保守估计（work_report.md: 4000 页扫描 PDF ≈ 数百元 → ~0.06 RMB/页量级），
    需用真实 DashScope 账单标定；均可经 RAG_REBUILD_* 环境变量覆盖。
    """
    enabled: bool = False          # 总开关；默认关（VLM rebuilder 尚未启用）→ 熔断器 no-op
    max_pages: int = 50            # 单文档计费单元硬上限（页+图），超出即封存
    doc_budget_rmb: float = 5.0    # 单文档预算 RMB，预估超出 → 封存 + 回退规则输出
    run_budget_rmb: float = 200.0  # 单次运行累计预算 RMB，超出 → 熔断，后续仅规则输出
    ocr_page_rmb: float = 0.06     # 单页 OCR-fallback 单价
    vlm_image_rmb: float = 0.04    # 单张嵌入式图片 VLM 单价
    refine_tables: bool = False    # Increment 2: 对结构错乱的 PDF 表格做 VLM 精修（数字保真闸把关；需 enabled=True 才生效，以确保成本熔断器在线）


@dataclass
class LLMConfig:
    """分类/风险评估 LLM 配置。"""
    api_key: str = ""
    api_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-3.1-flash-lite"
    temperature: float = 0.1
    max_retries: int = 2
    max_tokens: int = 2048


@dataclass
class RAGConfig:
    """RAG 问答 API 配置。"""
    default_top_k: int = 5
    max_context_chars: int = 6000
    api_port: int = 8000
    max_history_turns: int = 10
    # ── 相关度分数阈值（weighted fusion score） ──────────────────
    # 用于 _format_context 中标记 "高/中/低" 相关度，引导 LLM 忽略低分文档。
    # 默认值基于 120-query 评测数据标定：
    #   P25≈5.0, P50≈7.2, P75≈9.0
    #   score_high=8.0 → 约 top 35% 标为"高"
    #   score_medium=5.0 → 约 next 40% 标为"中"
    #   <5.0 → 约 bottom 25% 标为"低"
    # ⚠️ 如果切换 hybrid_fusion 从 "weighted" 到 "rrf"，score 分布
    #    会完全不同（RRF 分数 ∈ [0, 1]），必须重新标定这两个值。
    score_threshold_high: float = 8.0
    score_threshold_medium: float = 5.0
    # ── 纯文本生成开关（pure-text mode） ─────────────────────────
    # True  → 生成纯文字回答：system prompt 去掉 <<IMG:N>> 图片插入规则，
    #         context 不再注入 <<IMG:N>> 标记，卡片只展示文字（图片语义仍以
    #         visual_summary 文本形式保留在 context 中，不丢失信息）。
    # False → 默认的图文穿插模式（multimodal）。
    # 经 RAG_PURE_TEXT 环境变量覆盖；亦可在 generate_answer 调用处按请求覆盖。
    pure_text: bool = False


@dataclass
class ChunkStrategy:
    """分类切分策略。"""
    max_chunk_chars: int
    overlap_chars: int

@dataclass
class ChunkerConfig:
    """切分器配置。"""
    min_chunk_chars: int = 50
    max_token_count: int = 2000
    
    # 类别特定策略 (可以通过环境变量或在初始化时覆盖)
    manual_strategy: ChunkStrategy = field(default_factory=lambda: ChunkStrategy(400, 80))
    sop_strategy: ChunkStrategy = field(default_factory=lambda: ChunkStrategy(600, 100))
    faq_strategy: ChunkStrategy = field(default_factory=lambda: ChunkStrategy(600, 100))
    clause_strategy: ChunkStrategy = field(default_factory=lambda: ChunkStrategy(1000, 150))


@dataclass
class PipelineConfig:
    """管线总配置。"""
    # 运行模式
    simulate: bool = True                   # 全局模拟主开关，如果未单独指定以下子配置，默认继承此值
    simulate_db: bool = True                # 是否模拟 RDS 数据库读写
    simulate_opensearch: bool = True        # 是否模拟 OpenSearch 读写
    simulate_oss: bool = True               # 是否模拟 OSS 读写
    simulate_api: bool = True               # 是否模拟外部 API（LLM, Embedding, OCR），不发送真实外部网络请求
    environment: str = "development"        # development / staging / production
    log_level: str = "INFO"

    # 子配置
    oss: OSSConfig = field(default_factory=OSSConfig)
    rds: RDSConfig = field(default_factory=RDSConfig)
    opensearch: OpenSearchConfig = field(default_factory=OpenSearchConfig)
    alibaba_vector: AlibabaVectorSearchConfig = field(default_factory=AlibabaVectorSearchConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    chunker: ChunkerConfig = field(default_factory=ChunkerConfig)
    rag: RAGConfig = field(default_factory=RAGConfig)
    rebuild: RebuildConfig = field(default_factory=RebuildConfig)

    # 处理限制
    max_concurrent_tasks: int = 5
    max_retry_count: int = 3
    scan_batch_size: int = 50


def load_config() -> PipelineConfig:
    """
    从环境变量加载配置。

    环境变数命名约定：
      RAG_SIMULATE=true
      RAG_SIMULATE_API=true
      RAG_OSS_ENDPOINT=oss-cn-chengdu.aliyuncs.com
      RAG_RDS_HOST=rm-xxx.mysql.rds.aliyuncs.com
      RAG_OPENSEARCH_HOST=xxx.opensearch.aliyuncs.com
      RAG_GEMINI_API_KEY=AIzaSy...
    """

    def _env(key: str, default: str = "") -> str:
        return os.environ.get(f"RAG_{key}", default)

    def _env_int(key: str, default: int = 0) -> int:
        val = os.environ.get(f"RAG_{key}", "")
        return int(val) if val else default

    def _env_bool(key: str, default: bool = True) -> bool:
        val = os.environ.get(f"RAG_{key}", "").lower()
        if val in ("false", "0", "no"):
            return False
        if val in ("true", "1", "yes"):
            return True
        return default

    def _env_float(key: str, default: float = 0.0) -> float:
        val = os.environ.get(f"RAG_{key}", "")
        return float(val) if val else default

    rag_simulate = _env_bool("SIMULATE", True)
    rag_simulate_db = _env_bool("SIMULATE_DB", rag_simulate)
    rag_simulate_opensearch = _env_bool("SIMULATE_OPENSEARCH", rag_simulate)
    rag_simulate_oss = _env_bool("SIMULATE_OSS", rag_simulate)
    rag_simulate_api = _env_bool("SIMULATE_API", rag_simulate)

    # 优先加载 DashScope API Key
    dashscope_key = _env("DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    gemini_key = _env("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")

    # DataWorks(杭州) VPC 打通至百炼(北京)，使用北京 VPC 内网域名
    _is_prod = os.environ.get("RAG_ENVIRONMENT", "development") == "production"
    ds_domain = os.environ.get("DASHSCOPE_VPC_DOMAIN", "vpc-cn-beijing.dashscope.aliyuncs.com") if _is_prod else "dashscope.aliyuncs.com"

    # LLM 动态配置
    llm_key = _env("LLM_API_KEY") or dashscope_key or gemini_key
    default_llm_base = f"https://{ds_domain}/compatible-mode/v1" if dashscope_key else "https://generativelanguage.googleapis.com/v1beta"
    llm_base_url = _env("LLM_API_BASE_URL") or default_llm_base
    default_llm_model = "qwen3.6-plus" if dashscope_key else "gemini-3.1-flash-lite"
    llm_model = _env("LLM_MODEL") or default_llm_model

    # OCR 动态配置
    ocr_key = _env("OCR_API_KEY") or dashscope_key or gemini_key
    default_ocr_base = f"https://{ds_domain}/api/v1" if dashscope_key else "https://generativelanguage.googleapis.com/v1beta"
    ocr_base_url = _env("OCR_API_BASE_URL") or default_ocr_base
    default_ocr_model = "qwen-vl-ocr-latest" if dashscope_key else "gemini-3.1-flash-lite"
    ocr_model = _env("OCR_MODEL") or default_ocr_model
    # VLM caption/审计模型：独立于 OCR，默认 qwen3-vl-plus
    default_vlm_model = "qwen3-vl-plus" if dashscope_key else "gemini-3.1-flash-lite"
    vlm_model = _env("VLM_MODEL") or default_vlm_model

    # Embedding 动态路由配置
    env_embedding_model = os.environ.get("RAG_EMBEDDING_MODEL")
    default_emb_model = "text-embedding-v4" if dashscope_key else "gemini-embedding-2"
    emb_model = env_embedding_model or default_emb_model

    is_emb_dashscope = "qwen" in emb_model.lower() or "text-embedding" in emb_model.lower()

    emb_key = _env("EMBEDDING_API_KEY")
    if not emb_key:
        emb_key = dashscope_key if is_emb_dashscope else gemini_key

    emb_base = _env("EMBEDDING_API_BASE_URL")
    if not emb_base:
        emb_base = f"https://{ds_domain}" if is_emb_dashscope else "https://generativelanguage.googleapis.com/v1beta"

    config = PipelineConfig(
        simulate=rag_simulate,
        simulate_db=rag_simulate_db,
        simulate_opensearch=rag_simulate_opensearch,
        simulate_oss=rag_simulate_oss,
        simulate_api=rag_simulate_api,
        environment=_env("ENVIRONMENT", "development"),
        log_level=_env("LOG_LEVEL", "INFO"),
        max_concurrent_tasks=_env_int("MAX_CONCURRENT_TASKS", 5),
        max_retry_count=_env_int("MAX_RETRY_COUNT", 3),
        scan_batch_size=_env_int("SCAN_BATCH_SIZE", 50),

        oss=OSSConfig(
            endpoint=_env("OSS_ENDPOINT"),
            access_key_id=_env("OSS_ACCESS_KEY_ID"),
            access_key_secret=_env("OSS_ACCESS_KEY_SECRET"),
            bucket_name=_env("OSS_BUCKET_NAME", "fuling-knowledge-base"),
        ),

        rds=RDSConfig(
            host=_env("RDS_HOST", "localhost"),
            port=_env_int("RDS_PORT", 3306),
            user=_env("RDS_USER", "root"),
            password=_env("RDS_PASSWORD"),
            database=_env("RDS_DATABASE", "fuling_knowledge"),
        ),

        opensearch=OpenSearchConfig(
            host=_env("OPENSEARCH_HOST"),
            port=_env_int("OPENSEARCH_PORT", 9200),
            auth_user=_env("OPENSEARCH_USER"),
            auth_password=_env("OPENSEARCH_PASSWORD"),
            index_name=_env("OPENSEARCH_INDEX", "fuling_knowledge_v1"),
            use_ssl=_env_bool("OPENSEARCH_USE_SSL", True),
            verify_certs=_env_bool("OPENSEARCH_VERIFY_CERTS", True),
        ),

        alibaba_vector=AlibabaVectorSearchConfig(
            endpoint=_env("HA3_ENDPOINT"),
            instance_id=_env("HA3_INSTANCE_ID"),
            access_user_name=_env("HA3_USER"),
            access_pass_word=_env("HA3_PASSWORD"),
            table_name=_env("HA3_TABLE_NAME", "fuling_knowledge_vector"),
            pk_field=_env("HA3_PK_FIELD", "id"),
            enable_hybrid=_env_bool("HA3_ENABLE_HYBRID", True),
            hybrid_fusion=_env("HA3_HYBRID_FUSION", "weighted"),
            rrf_rank_constant=_env_int("HA3_RRF_RANK_CONSTANT", 60),
            knn_weight=_env_float("HA3_KNN_WEIGHT", 0.7),
            text_weight=_env_float("HA3_TEXT_WEIGHT", 0.3),
            text_search_field=_env("HA3_TEXT_SEARCH_FIELD", "chunk_text"),
            hybrid_knn_top_k=_env_int("HA3_HYBRID_KNN_TOP_K", 100),
        ),

        embedding=EmbeddingConfig(
            api_key=emb_key,
            api_base_url=emb_base,
            model=emb_model,
            dimension=_env_int("EMBEDDING_DIMENSION", 1024),
            batch_size=10 if is_emb_dashscope else 25,
        ),

        ocr=OCRConfig(
            api_key=ocr_key,
            api_base_url=ocr_base_url,
            model=ocr_model,
            vlm_model=vlm_model,
            max_ocr_pages=_env_int("OCR_MAX_PAGES", 5),
            ocr_threshold_chars=_env_int("OCR_THRESHOLD_CHARS", 100),
        ),

        rebuild=RebuildConfig(
            enabled=_env_bool("REBUILD_ENABLED", False),                  # RAG_REBUILD_ENABLED
            max_pages=_env_int("REBUILD_MAX_PAGES", 50),                  # RAG_REBUILD_MAX_PAGES
            doc_budget_rmb=_env_float("REBUILD_DOC_BUDGET_RMB", 5.0),     # RAG_REBUILD_DOC_BUDGET_RMB
            run_budget_rmb=_env_float("REBUILD_RUN_BUDGET_RMB", 200.0),   # RAG_REBUILD_RUN_BUDGET_RMB
            ocr_page_rmb=_env_float("REBUILD_COST_PER_PAGE_RMB", 0.06),   # RAG_REBUILD_COST_PER_PAGE_RMB
            vlm_image_rmb=_env_float("REBUILD_COST_PER_IMAGE_RMB", 0.04), # RAG_REBUILD_COST_PER_IMAGE_RMB
            refine_tables=_env_bool("REBUILD_REFINE_TABLES", False),      # RAG_REBUILD_REFINE_TABLES
        ),

        llm=LLMConfig(
            api_key=llm_key,
            api_base_url=llm_base_url,
            model=llm_model,
            max_tokens=_env_int("LLM_MAX_TOKENS", 2048),
        ),
        chunker=ChunkerConfig(
            min_chunk_chars=_env_int("CHUNKER_MIN_CHARS", 50),
            max_token_count=_env_int("CHUNKER_MAX_TOKENS", 2000),
            manual_strategy=ChunkStrategy(
                max_chunk_chars=_env_int("CHUNKER_MANUAL_MAX", 400),
                overlap_chars=_env_int("CHUNKER_MANUAL_OVERLAP", 80)
            ),
            sop_strategy=ChunkStrategy(
                max_chunk_chars=_env_int("CHUNKER_SOP_MAX", 600),
                overlap_chars=_env_int("CHUNKER_SOP_OVERLAP", 100)
            ),
            faq_strategy=ChunkStrategy(
                max_chunk_chars=_env_int("CHUNKER_FAQ_MAX", 600),
                overlap_chars=_env_int("CHUNKER_FAQ_OVERLAP", 100)
            ),
            clause_strategy=ChunkStrategy(
                max_chunk_chars=_env_int("CHUNKER_CLAUSE_MAX", 1000),
                overlap_chars=_env_int("CHUNKER_CLAUSE_OVERLAP", 150)
            )
        ),
        rag=RAGConfig(
            default_top_k=_env_int("RAG_TOP_K", 5),
            max_context_chars=_env_int("RAG_MAX_CONTEXT_CHARS", 6000),
            api_port=_env_int("RAG_API_PORT", 8000),
            max_history_turns=_env_int("RAG_MAX_HISTORY_TURNS", 10),
            pure_text=_env_bool("PURE_TEXT", False),               # RAG_PURE_TEXT
        ),
    )

    # 💡 生产安全守卫：当处于 production 或 staging 环境下，坚决杜绝 fallback 到 Gemini！
    # 强制校验所有大模型/视觉/向量 API 配置必须为阿里云 DashScope（或者是明确的非 Gemini，比如专有端点）
    if config.environment in ("production", "staging"):
        if not dashscope_key:
            raise ValueError(
                f"🚨 [PRODUCTION SECURITY GUARD] DashScope API Key is not configured under '{config.environment}' environment! "
                f"To protect privacy & security, falling back to Google Gemini is strictly forbidden in production."
            )
            
        for name, subcfg in [("LLM", config.llm), ("OCR", config.ocr), ("Embedding", config.embedding)]:
            base_url = subcfg.api_base_url or ""
            model_name = subcfg.model or ""
            
            if "google" in base_url.lower() or "gemini" in model_name.lower():
                raise ValueError(
                    f"🚨 [PRODUCTION SECURITY GUARD] {name} config resolved to Google Gemini "
                    f"(base_url='{base_url}', model='{model_name}') under '{config.environment}' environment! "
                    f"Production runs must strictly utilize Alibaba Cloud (Qwen) services."
                )

    return config



# 单例
_config: Optional[PipelineConfig] = None


def get_config() -> PipelineConfig:
    """获取全局配置（惰性加载）。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
