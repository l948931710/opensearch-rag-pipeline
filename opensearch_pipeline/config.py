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
from typing import Optional
from pathlib import Path


class EnvironmentMismatchError(ValueError):
    """环境标签（RAG_ENVIRONMENT）与物理目标（RDS/HA3/OSS 指向）不一致。

    继承 ValueError：与既有生产守卫的异常风格一致，pytest.raises(ValueError) 兼容。
    """


# 生产物理目标指纹（非密钥，仅实例标识子串）。交叉校验与运行时守卫共用。
PROD_FINGERPRINTS = {
    "rds": ("rm-bp15j7wekd5738f093o",),
    "search": ("ha-cn-kgl4slr1n01",),
    "oss": ("fuling-knowledge-base",),
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal", ""}

# Staging HA3 表名允许的后缀（2026-06-15 起，含 _s）。
# 由来：用户最初按 docs/environment_design.md 想建 `_stg` 后缀的 HA3 表，
# 但阿里云控制台那次 _stg 表建失败（具体原因未深查），改用 `_s` 后缀建成功。
# 守卫这边把 _s 和 _stg 都接受，docs/environment_design.md 也同步更新。
# **不要**用于 RDS 库名校验——RDS 那边仍强制 _stg（fuling_knowledge_stg 已建好）。
_STAGING_HA3_SUFFIXES = ("_stg", "_s")

# 守卫豁免变量（语义见 docs/environment_design.md）：
#   RAG_ALLOW_REMOTE_DB=read_only_ack      非 production 标签下连接远程/生产 RDS 的显式声明
#   RAG_ALLOW_REMOTE_SEARCH=read_only_ack  同上，针对 HA3/OpenSearch
_ACK_VALUE = "read_only_ack"


def is_prod_target(kind: str, value: str) -> bool:
    """value 是否命中生产物理目标指纹。kind ∈ PROD_FINGERPRINTS。

    oss 用精确匹配：staging 桶名（fuling-knowledge-base-staging）以生产桶名为前缀，
    子串匹配会误判。rds/search 用子串匹配（值是带域名后缀的完整 endpoint）。
    """
    v = (value or "").lower()
    if kind == "oss":
        return v in PROD_FINGERPRINTS["oss"]
    return any(fp in v for fp in PROD_FINGERPRINTS.get(kind, ()))


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
    #    override=True（file-wins）是刻意设计：环境身份必须原子化，残留的 shell export
    #    不能把生产端点拼进本地运行。被遮蔽的 shell 变量在 banner 中显式列出；
    #    确需单变量临时覆盖时用 RAG_ALLOW_SHELL_OVERRIDE=VAR1,VAR2 白名单回填。
    rag_env = os.environ.get("RAG_ENV", "").lower()

    # RAG_ENV=test 已更名 prod_ro（名实归一：它的真实用途是"从公网只读访问生产"）
    if rag_env == "test" and (project_root / ".env.prod_ro").exists():
        import warnings
        warnings.warn("RAG_ENV=test 已弃用，请改用 RAG_ENV=prod_ro（语义：生产只读）",
                      DeprecationWarning, stacklevel=2)
        rag_env = "prod_ro"

    shadowed = []
    if rag_env:
        env_file = project_root / f".env.{rag_env}"
        if env_file.exists():
            _shell_snapshot = dict(os.environ)
            load_dotenv(env_file, override=True)
            shadowed = sorted(
                k for k, v in _shell_snapshot.items()
                if (k.startswith(("RAG_", "DINGTALK_")) or k == "DASHSCOPE_API_KEY")
                and os.environ.get(k) != v
            )
            # 逃生口：白名单变量保留 shell 值（单次实验性覆盖用）
            allow = [s.strip() for s in
                     _shell_snapshot.get("RAG_ALLOW_SHELL_OVERRIDE", "").split(",") if s.strip()]
            for k in allow:
                if k in _shell_snapshot:
                    os.environ[k] = _shell_snapshot[k]
                    if k in shadowed:
                        shadowed.remove(k)
        else:
            print(f"  ⚠️ RAG_ENV={rag_env} 但 {env_file} 不存在，仅使用 .env")

    # 3. 打印环境标识
    _print_env_banner(rag_env, shadowed)

def _print_env_banner(rag_env: str, shadowed: Optional[list] = None):
    """启动时打印当前环境标识，避免误操作。"""
    rds_host = os.environ.get("RAG_RDS_HOST", "localhost")
    ha3_host = os.environ.get("RAG_HA3_ENDPOINT", "")
    os_host = os.environ.get("RAG_OPENSEARCH_HOST", "") or ha3_host
    env_label = os.environ.get("RAG_ENVIRONMENT", "development")

    if rag_env == "production":
        icon = "🚀"
        label = "PRODUCTION (阿里云生产)"
    elif rag_env in ("test", "prod_ro"):
        icon = "🔎"
        label = "PROD-RO (生产只读诊断)"
    elif rag_env == "staging":
        icon = "🎭"
        label = "STAGING (预演环境)"
    elif rag_env == "local":
        icon = "🏠"
        label = "LOCAL (本地开发)"
    elif rag_env.startswith("local_ab_"):
        icon = "⚖️"
        label = f"LOCAL-EVAL ({rag_env.removeprefix('local_ab_')} 臂)"
    else:
        icon = "⚙️"
        label = f"DEFAULT ({env_label})"

    print(f"  {icon} 环境: {label} | RDS={rds_host} | Search={os_host or 'localhost'}")
    if shadowed:
        print(f"  ⚠️ 以下 shell 变量被 .env.{rag_env} 遮蔽（file-wins）: {', '.join(shadowed)}"
              f" —— 临时覆盖请用 RAG_ALLOW_SHELL_OVERRIDE")

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
    # 签名 URL 有效期（秒），RAG_OSS_URL_EXPIRES。卡片重建路径会按 oss_key 重签，
    # 所以默认 1h 只需覆盖「活跃会话内看图」的窗口。
    signed_url_expires: int = 3600


@dataclass
class RDSConfig:
    """阿里云 RDS MySQL 配置。"""
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "fuling_knowledge"
    # 问答运营库（qa_session_log/user_feedback/escalation_ticket）；STAGING 用 fuling_operation_stg
    operation_database: str = "fuling_operation"
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
    # ── 路由式重排序（DashScope rerank，见 reranker.py / eval_harness rerank A/B）──
    # 默认关闭；开启后 retrieve_and_enrich 会 over-fetch rerank_pool 个候选 → 重排 → 取 top_k。
    rerank_enable: bool = False             # RAG_RERANK_ENABLE
    rerank_text_model: str = "qwen3-rerank"      # 纯文本候选池
    rerank_vl_model: str = "qwen3-vl-rerank"     # 含图片候选池（图文重排）
    # 候选池含图片时，纯文本路径也路由到 VL 重排。
    # 数据驱动（rerank A/B，image-pool n=40）：纯文本重排即使用 visual_summary 富文本，
    # 图片类 recall@1 仅 0.725 < baseline 0.825 < VL 0.85 → 含图片走 VL 更优。
    rerank_route_vl: bool = True
    rerank_pool: int = 20                   # 重排前 over-fetch 的候选池大小
    rerank_timeout: int = 15                # 重排 API 超时（秒）；超时即降级为原始顺序


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
    max_ocr_pages: int = 50
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
    # Qwen3 思考模式：默认关闭。开启时模型先生成大量 reasoning_content（被问答代码丢弃），
    # 实测使总时长 38.5s→8.6s、首字 34s→1.3s，且 reasoning 挤占 max_tokens 预算导致答案被截断。
    # RAG 答案有检索上下文兜底，无需思考。如需对照可设 RAG_LLM_ENABLE_THINKING=true。
    enable_thinking: bool = False


@dataclass
class RAGConfig:
    """RAG 问答 API 配置。"""
    # RAG_TOP_K；7 为 2026-06 评测锁定值（top_k=7 + stitch ±1 ≈ 5.7k chars ≤ max_context_chars）
    default_top_k: int = 7
    max_context_chars: int = 6000
    api_port: int = 8000
    max_history_turns: int = 10
    # ── 相关度分数阈值（weighted fusion score） ──────────────────
    # 用于 _format_context 中标记 "高/中/低" 相关度，引导 LLM 忽略低分文档。
    # 默认值基于 120-query 评测数据标定：
    #   P25≈5.0, P50≈7.2, P75≈9.0
    # 🔧 2026-06-07 重标定（eval_harness 251 题，重建后 fuling_kb_chunks 实测分布）：
    #   正确 top-1 命中 score：mean≈7.63, P10≈5.56, P50≈7.73, P75≈8.56
    #   旧阈值 8.0/5.0 下仅 ~38% 正确命中标为"高"；新阈值 7.7/5.8 → ~50% 标"高"，
    #   且 ~85% 正确命中 ≥"中"。可经 RAG_SCORE_THRESHOLD_HIGH/_MEDIUM 覆盖。
    # ⚠️ 正/负样本 score 区分度本身偏弱（Youden J≈0.46）：阈值调整只是缓解，
    #    根因需 reranker / 调融合权重 / 按 query 归一化（见 eval_harness/recalibration.json）。
    # ⚠️ 如果切换 hybrid_fusion 从 "weighted" 到 "rrf"，score 分布
    #    会完全不同（RRF 分数 ∈ [0, 1]），必须重新标定这两个值。
    score_threshold_high: float = 7.7
    score_threshold_medium: float = 5.8
    # 重排序开启时，相关度标签改用 rerank 分（0~1）。
    # 2026-06-07 标定（eval_harness 251 题 rerank-on 实测）：正确 top-1 命中 mean≈0.91
    # (P50 0.93)，负例 mean≈0.75 (P50 0.74)。high=0.9/medium=0.8 → 正确命中 69% 高 / 92% ≥中，
    # 负例 65% 低（仅 23% 高）；rerank 分区分度（Youden J≈0.60）远优于融合分。
    rerank_score_threshold_high: float = 0.9    # RAG_RERANK_SCORE_THRESHOLD_HIGH
    rerank_score_threshold_medium: float = 0.8  # RAG_RERANK_SCORE_THRESHOLD_MEDIUM
    # ── 低置信度护栏（soft answerability guard）──────────────────
    # 离线标定（eval_harness/gate_calibration.json，251 题路由重排 top-1 分）结论：
    # 正/负分布重叠严重——任何硬闸门要拦截 >30% 负例就要误拒 ≥20% 正例，且部分
    # 陷阱负例（问不存在的文档/型号，但库里有近似文档）分数高达 0.93+，硬性
    # "低分即 NO_RESULT" 不可部署。改为软护栏：top 分落入低置信带（< medium 阈值）
    # 时在 system prompt 末尾追加"逐条核对、不对题必须明确拒答"的强化指令，由能
    # 读到内容的 LLM 做第二级判别，分数只作先验。RAG_LOW_CONFIDENCE_GUARD 控制。
    # ✅ 实测（multi_doc_ab v2，26 负例 + 50 正例生成对照）：负例拦截 0.50→0.654
    # （+4/0 翻转），正例误拒两臂均 0/50，关键词覆盖不变 → 建议生产置 true。
    low_confidence_guard: bool = False
    # ── 多意图查询分解（multi-doc retrieval，见 query_decomposer.py）──
    # off  → 不分解（默认）；auto → 启发式触发后才调 LLM 分解；llm → 每查询都判别。
    # 跨文档综合问题单查询 R@1 仅 ~8%（topk_window_sweep + 251 题 gold 复确认）：
    # top-k 被单一最相似文档占满。分解后各子查询并行检索、轮转交错合并。
    # ⏸️ 实测（multi_doc_ab v2，24 跨文档 + 50 单文档配对）：per-doc coverage 仅
    # +1.0~1.7pp（CI 下界 0），可分解意图（如"女职工和未成年工"1→3/4 docs）真实
    # 受益但占比小；~30% 查询触发判别调用 +~1s。单文档 0 回归。维持默认 off，
    # 若生产 qa_session_log 多意图问题占比可观再启用。详见 reports/multi_doc_guard_findings.md。
    multi_query_mode: str = "off"   # RAG_MULTI_QUERY_MODE
    multi_query_max: int = 3        # RAG_MULTI_QUERY_MAX：最多拆出的子查询数
    decompose_timeout: int = 8      # RAG_DECOMPOSE_TIMEOUT：分解调用超时（秒），失败即不分解
    # ── 文档多样性限额（doc diversity cap）──────────────────────
    # 跨文档问题的另一失败形态：问题本身单意图（无从分解），但答案分散在多份文档，
    # 而 top-k 被最相似文档的 chunk 占满（rerank 池 recall@10≈0.99，第二目标文档
    # 挤不进 top-7）。>0 时最终 top_k 内同一文档最多保留 cap 条（从重排池回填），
    # 0 = 关闭。仅在重排开启（有 over-fetch 池）时有实际效果。
    # ❌ 实测（multi_doc_ab v2）：本语料 cov_frac −2.8pp（CI [−8.3, 0]），未过非劣界，
    # 单文档丢 1 个 recall@1 —— 轻度有害，保持 0。根因：近重复文档家族（告知书
    # （新）/（松门）是不同 doc_id）使文档级限额错位换出 gold chunk。
    doc_diversity_cap: int = 0      # RAG_DOC_DIVERSITY_CAP
    # ── 纯文本生成开关（pure-text mode） ─────────────────────────
    # True  → 生成纯文字回答：system prompt 去掉 <<IMG:N>> 图片插入规则，
    #         context 不再注入 <<IMG:N>> 标记，卡片只展示文字（图片语义仍以
    #         visual_summary 文本形式保留在 context 中，不丢失信息）。
    # False → 默认的图文穿插模式（multimodal）。
    # 经 RAG_PURE_TEXT 环境变量覆盖；亦可在 generate_answer 调用处按请求覆盖。
    pure_text: bool = False
    # ── 钉钉流式卡片（打字机效果）─────────────────────────────────
    # True  → 钉钉机器人以流式 AI 卡片逐步输出回答（需在钉钉卡片平台注册流式卡片
    #         模板并配置 DINGTALK_STREAM_CARD_TEMPLATE_ID）。
    # False → 默认行为：等待 LLM 完成后一次性发送成品互动卡片。
    # 模板缺失时自动降级为非流式路径，故开启此开关也不会破坏现有行为。
    dingtalk_streaming: bool = False
    # 流式卡片更新节流间隔(ms)，避免触发钉钉流式更新接口限流。
    dingtalk_stream_interval_ms: int = 500
    # ── 图片召回增强（image co-surfacing）─────────────────────────
    # True  → 多模态渲染路径（SSE / 图文卡片）检索后，对 top 文档补充其最相关的
    #         image chunk 并插入到同文档正文之后，解决"文本类查询挤掉同文档图片"
    #         导致答案缺图的召回缺口。每次多模态检索会多一次 HA3 过滤查询。
    # False → 全局关闭（如对延迟敏感）。仅在调用方显式 opt-in 时才生效，故纯文本
    #         路径与 /api/ask 不受影响。
    image_cosurface: bool = True
    # ── 答案图片数量上限（轮转配额）────────────────────────────
    # build_content_blocks 的图片配额：每个被 <<IMG:N>> 引用的步骤/文档先各取 1 张
    # （轮转），有余额再按引用顺序补各自剩余图。默认 6 = "每步一张 + 少量补充"，
    # 依据 2026-06-11 语料分布（带图文档 p50=7 张图/4 个带图步骤；旧上限 3 + 顺序
    # 整段消耗使扫码枪类后位步骤图永远被前位多图步骤挤掉）。
    max_answer_images: int = 6      # RAG_MAX_ANSWER_IMAGES
    # ── 步骤卡兄弟扩展的超大家族防洪上限 ─────────────────────────
    # expand_step_context 的意图筛选按 step_no 数值区间选兄弟：正常 SOP（step_no
    # 1..N 基本互异）窗口只取 2-3 个；但超大手册（如富岭U8+人事部操作手册，48 卡
    # 共享一个 parent 且 41 个 step_no=0）会让区间筛选退化成全家族扩展（~15k 字），
    # 把真正命中的小节挤出 context 预算（2026-06-11 J-r120_23 拒答根因）。
    # 家族筛选结果超过该上限时，收缩为「命中卡 + 同 section_title 伙伴 + 文档序
    # ±2 窗口」；≤ 上限的正常 SOP 行为逐字节不变。0 = 关闭防洪（不推荐）。
    step_expand_family_cap: int = 12  # RAG_STEP_EXPAND_FAMILY_CAP


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
    readonly: bool = False                  # RAG_READONLY：PROD-RO 会话声明，写路径守卫强制拦截
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


def _require_ack(var: str) -> bool:
    """读取守卫豁免变量。空=未豁免；read_only_ack=豁免；其他值=拼写错误，直接 raise（R7）。"""
    v = os.environ.get(var, "")
    if v not in ("", _ACK_VALUE):
        raise EnvironmentMismatchError(
            f"[ENV GUARD] {var}={v!r} 不是合法值，只接受 '{_ACK_VALUE}'（防 typo 静默放行）")
    return v == _ACK_VALUE


def _validate_environment_target_consistency(config: "PipelineConfig") -> None:
    """环境标签 ↔ 物理目标交叉校验（fail-fast，发生在任何连接建立之前）。

    规则前置条件：仅当对应子系统 simulate=False 时才评估（make sim / 单测天然跳过）。
    规则表与豁免变量语义见 docs/environment_design.md。
    """
    env = (config.environment or "development").lower()
    search_targets = " ".join(filter(None, (
        config.alibaba_vector.endpoint, config.alibaba_vector.instance_id,
        config.opensearch.host)))

    if env in ("development", "local", ""):
        # R1：dev 标签禁止远程 RDS（豁免=只读声明）
        if not config.simulate_db and config.rds.host not in _LOCAL_HOSTS:
            if not _require_ack("RAG_ALLOW_REMOTE_DB"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 但 RDS_HOST={config.rds.host!r} 是远程地址。"
                    f"只读场景请显式 export RAG_ALLOW_REMOTE_DB={_ACK_VALUE}")
        # R2：dev 标签禁止生产检索目标
        if not config.simulate_opensearch and is_prod_target("search", search_targets):
            if not _require_ack("RAG_ALLOW_REMOTE_SEARCH"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 但检索目标命中生产指纹（{search_targets!r}）。"
                    f"只读场景请显式 export RAG_ALLOW_REMOTE_SEARCH={_ACK_VALUE}")

    elif env in ("staging", "test"):
        # R3：staging/test 标签指向生产实例时——要么是 STAGING 形态（库/表带 _stg 后缀，合法），
        #     要么是 PROD-RO 形态（必须显式只读声明）
        if not config.simulate_db and is_prod_target("rds", config.rds.host) \
                and not config.rds.database.endswith("_stg"):
            if not _require_ack("RAG_ALLOW_REMOTE_DB"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 指向生产 RDS（database={config.rds.database}，"
                    f"非 _stg 库）。PROD-RO 会话请 export RAG_ALLOW_REMOTE_DB={_ACK_VALUE}")
        if not config.simulate_opensearch and is_prod_target("search", search_targets) \
                and not config.alibaba_vector.table_name.endswith(_STAGING_HA3_SUFFIXES):
            if not _require_ack("RAG_ALLOW_REMOTE_SEARCH"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 指向生产检索实例"
                    f"（table={config.alibaba_vector.table_name!r}，非 _stg/_s 表）。"
                    f"PROD-RO 会话请 export RAG_ALLOW_REMOTE_SEARCH={_ACK_VALUE}")

    if env == "production":
        # R4：生产标签指 localhost 必为配错，无豁免
        if not config.simulate_db and config.rds.host in _LOCAL_HOSTS:
            raise EnvironmentMismatchError(
                f"[ENV GUARD] environment=production 但 RDS_HOST={config.rds.host!r} 是本地地址")
        # R5：生产无任何检索后端
        if not config.simulate_opensearch and not search_targets.strip():
            raise EnvironmentMismatchError(
                "[ENV GUARD] environment=production 但未配置任何检索后端（HA3/OpenSearch 均为空）")

    # D7：production/staging 实际启用 HA3 时表名必须显式声明（消除历史双标默认值）
    if env in ("production", "staging") and not config.simulate_opensearch \
            and config.alibaba_vector.endpoint and not config.alibaba_vector.table_name:
        raise EnvironmentMismatchError(
            "[ENV GUARD] HA3 endpoint 已配置但 RAG_HA3_TABLE_NAME 为空——"
            "请显式声明表名（生产=fuling_kb_chunks / 预演=fuling_kb_chunks_stg 或 fuling_kb_chunks_s）")

    # STAGING overlay 的资源后缀强约束（防 staging 配置半生不熟指向生产资源；无豁免）
    if os.environ.get("RAG_ENV", "").lower() == "staging":
        problems = []
        if env != "staging":
            problems.append(f"RAG_ENVIRONMENT 必须为 staging（当前 {env}）")
        if not config.simulate_db and not config.rds.database.endswith("_stg"):
            problems.append(f"RDS_DATABASE 必须以 _stg 结尾（当前 {config.rds.database}）")
        if not config.simulate_opensearch and config.alibaba_vector.endpoint \
                and not config.alibaba_vector.table_name.endswith(_STAGING_HA3_SUFFIXES):
            problems.append(f"HA3_TABLE_NAME 必须以 _stg 或 _s 结尾"
                            f"（当前 {config.alibaba_vector.table_name!r}）")
        if not config.simulate_oss and not config.oss.bucket_name.endswith("-staging"):
            problems.append(f"OSS_BUCKET_NAME 必须以 -staging 结尾（当前 {config.oss.bucket_name}）")
        if problems:
            raise EnvironmentMismatchError("[ENV GUARD] RAG_ENV=staging 资源约束不满足: " + "; ".join(problems))


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
        readonly=_env_bool("READONLY", False),
        log_level=_env("LOG_LEVEL", "INFO"),
        max_concurrent_tasks=_env_int("MAX_CONCURRENT_TASKS", 5),
        max_retry_count=_env_int("MAX_RETRY_COUNT", 3),
        scan_batch_size=_env_int("SCAN_BATCH_SIZE", 50),

        oss=OSSConfig(
            endpoint=_env("OSS_ENDPOINT"),
            access_key_id=_env("OSS_ACCESS_KEY_ID"),
            access_key_secret=_env("OSS_ACCESS_KEY_SECRET"),
            bucket_name=_env("OSS_BUCKET_NAME", "fuling-knowledge-base"),
            signed_url_expires=_env_int("OSS_URL_EXPIRES", 3600),
        ),

        rds=RDSConfig(
            host=_env("RDS_HOST", "localhost"),
            port=_env_int("RDS_PORT", 3306),
            user=_env("RDS_USER", "root"),
            password=_env("RDS_PASSWORD"),
            database=_env("RDS_DATABASE", "fuling_knowledge"),
            operation_database=_env("RDS_OPERATION_DATABASE", "fuling_operation"),
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
            # 默认空（曾默认 fuling_knowledge_vector——一张从未存在的表，与生产 fuling_kb_chunks 双标）。
            # production/staging 实际启用 HA3 时表名为空会在交叉校验中 fail-fast，逼迫显式声明。
            table_name=_env("HA3_TABLE_NAME", ""),
            pk_field=_env("HA3_PK_FIELD", "id"),
            enable_hybrid=_env_bool("HA3_ENABLE_HYBRID", True),
            hybrid_fusion=_env("HA3_HYBRID_FUSION", "weighted"),
            rrf_rank_constant=_env_int("HA3_RRF_RANK_CONSTANT", 60),
            knn_weight=_env_float("HA3_KNN_WEIGHT", 0.7),
            text_weight=_env_float("HA3_TEXT_WEIGHT", 0.3),
            text_search_field=_env("HA3_TEXT_SEARCH_FIELD", "chunk_text"),
            hybrid_knn_top_k=_env_int("HA3_HYBRID_KNN_TOP_K", 100),
            rerank_enable=_env_bool("RERANK_ENABLE", False),
            rerank_text_model=_env("RERANK_TEXT_MODEL", "qwen3-rerank"),
            rerank_vl_model=_env("RERANK_VL_MODEL", "qwen3-vl-rerank"),
            rerank_route_vl=_env_bool("RERANK_ROUTE_VL", True),
            rerank_pool=_env_int("RERANK_POOL", 20),
            rerank_timeout=_env_int("RERANK_TIMEOUT", 15),
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
            max_ocr_pages=_env_int("OCR_MAX_PAGES", 50),
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
            enable_thinking=_env_bool("LLM_ENABLE_THINKING", False),  # RAG_LLM_ENABLE_THINKING
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
            # ⚠️ _env_int 自带 RAG_ 前缀：这四项原先写成 _env_int("RAG_TOP_K") 等，
            # 实际读的是 RAG_RAG_TOP_K —— 文档名（RAG_TOP_K）永远不生效。
            default_top_k=_env_int("TOP_K", 7),                      # RAG_TOP_K
            max_context_chars=_env_int("MAX_CONTEXT_CHARS", 6000),   # RAG_MAX_CONTEXT_CHARS
            api_port=_env_int("API_PORT", 8000),                     # RAG_API_PORT
            max_history_turns=_env_int("MAX_HISTORY_TURNS", 10),     # RAG_MAX_HISTORY_TURNS
            pure_text=_env_bool("PURE_TEXT", False),               # RAG_PURE_TEXT
            # 相关度标签阈值（高/中/低）；可经 RAG_SCORE_THRESHOLD_HIGH / _MEDIUM 覆盖。
            score_threshold_high=_env_float("SCORE_THRESHOLD_HIGH", 7.7),       # RAG_SCORE_THRESHOLD_HIGH
            score_threshold_medium=_env_float("SCORE_THRESHOLD_MEDIUM", 5.8),   # RAG_SCORE_THRESHOLD_MEDIUM
            rerank_score_threshold_high=_env_float("RERANK_SCORE_THRESHOLD_HIGH", 0.9),
            rerank_score_threshold_medium=_env_float("RERANK_SCORE_THRESHOLD_MEDIUM", 0.8),
            low_confidence_guard=_env_bool("LOW_CONFIDENCE_GUARD", False),  # RAG_LOW_CONFIDENCE_GUARD
            multi_query_mode=_env("MULTI_QUERY_MODE", "off").lower(),       # RAG_MULTI_QUERY_MODE
            multi_query_max=_env_int("MULTI_QUERY_MAX", 3),                 # RAG_MULTI_QUERY_MAX
            decompose_timeout=_env_int("DECOMPOSE_TIMEOUT", 8),             # RAG_DECOMPOSE_TIMEOUT
            doc_diversity_cap=_env_int("DOC_DIVERSITY_CAP", 0),             # RAG_DOC_DIVERSITY_CAP
            dingtalk_streaming=_env_bool("DINGTALK_STREAMING", False),          # RAG_DINGTALK_STREAMING
            dingtalk_stream_interval_ms=_env_int("DINGTALK_STREAM_INTERVAL_MS", 500),  # RAG_DINGTALK_STREAM_INTERVAL_MS
            image_cosurface=_env_bool("IMAGE_COSURFACE", True),                 # RAG_IMAGE_COSURFACE
            max_answer_images=_env_int("MAX_ANSWER_IMAGES", 6),                 # RAG_MAX_ANSWER_IMAGES
            step_expand_family_cap=_env_int("STEP_EXPAND_FAMILY_CAP", 12),      # RAG_STEP_EXPAND_FAMILY_CAP
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
            
        # VLM（caption/审计）模型挂在 ocr 配置上但独立解析（RAG_VLM_MODEL），必须单独纳入守卫，
        # 否则 RAG_VLM_MODEL=gemini-* 会绕过检查直达图像通道；为空时按运行时约定回退 ocr.model。
        checks = [
            ("LLM", config.llm.api_base_url, config.llm.model),
            ("OCR", config.ocr.api_base_url, config.ocr.model),
            ("VLM", config.ocr.api_base_url, config.ocr.vlm_model or config.ocr.model),
            ("Embedding", config.embedding.api_base_url, config.embedding.model),
        ]
        for name, base_url, model_name in checks:
            base_url = base_url or ""
            model_name = model_name or ""

            if "google" in base_url.lower() or "gemini" in model_name.lower():
                raise ValueError(
                    f"🚨 [PRODUCTION SECURITY GUARD] {name} config resolved to Google Gemini "
                    f"(base_url='{base_url}', model='{model_name}') under '{config.environment}' environment! "
                    f"Production runs must strictly utilize Alibaba Cloud (Qwen) services."
                )

    # 💡 环境守卫第二层：环境标签 ↔ 物理目标交叉校验（规则表见函数 docstring）
    _validate_environment_target_consistency(config)

    return config



# 单例
_config: Optional[PipelineConfig] = None


def get_config() -> PipelineConfig:
    """获取全局配置（惰性加载）。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
