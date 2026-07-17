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

import logging
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
    # 本体控制面独立库（schema/027-030 表族；PR-B P0-02：fuling_ro 持库级
    # GRANT SELECT ON fuling_operation.*，ontology 表留在运营库时服务层行过滤可被
    # 直连绕过——独立库 + 不授 fuling_ro 才是机器可强制的隔离）；STAGING 用 _stg 后缀
    ontology_database: str = "fuling_ontology"
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 30
    # RDS 传输加密（P0-02 报告1，2026-07-10 外审）：默认空 = 现网连接行为不变（不传 ssl）。
    # 配了 ssl_ca（阿里云 RDS CA 证书路径）→ db.py/prod_access 建连时传 ssl={ca,verify}。
    # ssl_verify_cert：验证服务端证书（需 ssl_ca）。生产未启用时启动**告警不阻断**
    # （用户选「先不强制」——拿到 CA 配好后再把告警升为硬断言）。
    ssl_ca: str = ""
    ssl_verify_cert: bool = True

    def pymysql_ssl_args(self) -> dict:
        """P0-02：pymysql.connect 的 ssl 关键字。
        配了 ssl_ca → {"ssl": {"ca": <path>, "check_hostname"/"verify_cert": ...}}；
        未配 → {"ssl_disabled": True}【显式明文】。不能返回 {}：pymysql 2.x 默认
        PREFERRED 模式——RDS 实例开通 SSL（2026-07-17）后服务端广播能力位，未显式
        禁用的客户端会按自身 pymysql/OpenSSL 版本自动尝试 TLS，握手失败不回退直接
        报错（conda OpenSSL3 实测拒 RSA-kx 套件即断连）。行为必须由配置决定，
        不能由客户端库版本决定；要开 TLS 走 RAG_RDS_SSL_CA 显式配置。"""
        ca = (self.ssl_ca or "").strip()
        if not ca:
            return {"ssl_disabled": True}
        return {"ssl": {"ca": ca, "check_hostname": bool(self.ssl_verify_cert)},
                "ssl_verify_cert": bool(self.ssl_verify_cert)}


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
    # ── 三路客户端融合（2026-07-13 金集 A/B 判决 w3_s10，docs/ha3_client_fusion_3way_ab_2026-07-13.md）──
    # /search 不支持 sparse（522 盲行事故根因）后「既救盲行又保 sparse」的正确路径：
    # dense(/query) + sparse(/query) + BM25(/search) 三臂并行、客户端 min-max 归一加权融合
    # （缺席不罚分）。金集 recall@1 +3.6pp vs 去 sparse 服务端混合，盲行 5/5 rank1。
    # **默认开（生产语义随包生效，SAE 无需注入 env；同 8fc80f8 先例）**——
    # RAG_HA3_CLIENT_FUSION=false 为 kill switch（回落去-sparse 服务端混合）。
    # 档位阈值随之由 load_config 守卫自动套标定值 0.57/0.52（env 显式优先）。
    # sparse 权重 0.1 为金集最优（0.2/0.3 开始反噬），调参须重跑金集 A/B。
    client_fusion_enable: bool = True         # RAG_HA3_CLIENT_FUSION（=false 为逃生舱）
    client_fusion_dense_weight: float = 0.7   # RAG_HA3_CLIENT_FUSION_DENSE_WEIGHT（D 臂）
    client_fusion_sparse_weight: float = 0.1  # RAG_HA3_CLIENT_FUSION_SPARSE_WEIGHT（S 臂）
    client_fusion_text_weight: float = 0.3    # RAG_HA3_CLIENT_FUSION_TEXT_WEIGHT（B 臂）
    client_fusion_pool: int = 50              # RAG_HA3_CLIENT_FUSION_POOL（每臂候选池）
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
    api_base_url: str = "https://dashscope.aliyuncs.com"        # 默认 DashScope-native（残留清理；env-loader 仍按 key 动态路由）
    model: str = "text-embedding-v4"
    dimension: int = 1024
    batch_size: int = 10                    # API limit (DashScope limit is 10)
    max_retries: int = 3


@dataclass
class OCRConfig:
    """OCR + VLM 视觉配置。"""
    api_key: str = ""
    api_base_url: str = "https://dashscope.aliyuncs.com/api/v1"  # 默认 DashScope-native（残留清理；env-loader 仍按 key 动态路由）
    model: str = "qwen-vl-ocr-latest"               # OCR 专用模型
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
    # G8：跨进程/跨实例的日累计预算（RDS rag_runtime_contract 共享账本；北京日界）。
    # 0 = 关闭（仅进程内 run_budget）。进程内预算无法跨 orchestrator 实例聚合的
    # 已记录限制由此补上；账本不可达时 fail-open 回退进程内行为。
    daily_budget_rmb: float = 0.0  # RAG_REBUILD_DAILY_BUDGET_RMB


@dataclass
class LLMConfig:
    """分类/风险评估 LLM 配置。"""
    api_key: str = ""
    api_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 默认 DashScope-native（残留清理；env-loader 仍按 key 动态路由）
    model: str = "qwen3.7-plus"
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
    # 服务端会话历史（Phase 2/3）：开启后 /api/ask(/stream) 回填 conversation_id 到 qa_session_log，
    # 并启用 /api/conversations 列表/读取/软删除。默认关；需先在 RDS 应用 schema/006 再开。
    conversation_history: bool = False      # RAG_CONVERSATION_HISTORY
    # 跨部门检索授权放行（Phase D）：开启后 retriever 过滤追加 allowed_depts OR 项、to_ha3_doc 推送
    # allowed_depts、入库按 approved 授权聚合写 chunk_meta.allowed_depts。默认关；需先 HA3 加
    # allowed_depts 字段(Step 2) + 回填(Step 3) 再开。flag 关时全链路与现状逐字节一致。
    allowed_depts_acl: bool = False         # RAG_ALLOWED_DEPTS_ACL
    # 主命中 RDS 复核（盲区审计 P3-1）：邻居/扩展路径一直有 is_active=1 + 同权限复核，
    # 而主 HA3 命中此前直接投放——ACL 收紧/下线与 HA3 投影之间的延迟窗内，旧值按旧口径
    # 被逐字投放。开启后主命中按权威表复核 is_active + permission_level/owner_dept 一致性，
    # 漂移即丢弃（fail-closed 方向）；权威表不可达则保留结果（HA3 服务端过滤仍是第一道
    # 边界，本检查是投影延迟的防御纵深，不应让 RDS 故障放大为全站无答案）。
    main_hit_revalidate: bool = True        # RAG_MAIN_HIT_REVALIDATE
    # 思考过程下发（深度思考「思考过程」披露条）：开启后，thinking=True 时把 reasoning_content 作为
    # {"type":"reasoning"} 附加帧流式下发（与 chunk 并行；老客户端忽略未知帧类型 → 向后兼容）。默认关
    # （reasoning 更费带宽且暴露思维链是产品取舍）；仅「thinking 开 + 本 flag 开」时下发，否则照旧丢弃。
    stream_reasoning: bool = False          # RAG_STREAM_REASONING
    # Tier B-流式：generate_answer_stream 的 DashScope 调用改经 agent 底座 ModelGateway 发出。
    # 请求体与既有裸 _http_post 等值、对外 yield 契约逐字节不变（等值门=
    # tests/test_serving_gateway_equivalence.py）；max_retries=0 保持原单次调用语义（不引入
    # 重试/沿链 fallback 的行为变化）。默认 OFF——热路径传输层替换走保守灰度，本 flag 即 kill switch。
    serving_model_gateway: bool = False     # RAG_SERVING_MODEL_GATEWAY
    # ── QA 日志查询侧 PII 脱敏（OBS-qa-pii 整改）──────────────────
    # qa_session_log.query_text/answer_text 此前明文落盘：用户可能输入身份证/手机号，
    # 答案可能回显受限文档里的 PII。开启后在写库前用 redaction.redact_text（与入库侧
    # 同一套正则，纯本地、无 LLM/网络）做**不可逆**掩码，仅落占位符。默认 ON（安全
    # 方向，与入库侧 hash+mask 姿态对齐）；置 false 仅用于本地调试取证。
    qa_log_pii_redact: bool = True          # RAG_QA_LOG_PII_REDACT
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
    # #7：rerank + cosurface 同开时，is_low_confidence_band 只看 rerank 分(0-1)、忽略只带融合分的
    # cosurface 补图 chunk → 重排文本弱而某图强时过度拒答。开启后把 rerank 分(阈 0.9/0.8)与融合分
    # (阈 7.7/5.8)各按自量纲的 medium/high 归一到统一 [0,1] 置信度再取 max（medium 线=0.5）。默认
    # OFF：先 eval A/B（正/负例置信分离 Youden J）验证不回归再灰度。见 llm_generator._confidence_norm。
    confidence_cross_scale: bool = False    # RAG_CONFIDENCE_CROSS_SCALE
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
    # ── 通用能力分级开放（general ability tiers，见 intent_router/general_answerer）──
    # off（默认，行为与现状逐字节一致）| smalltalk（T1 寒暄/元问题 canned）
    # | office（T1+T2 办公辅助：翻译/润色/摘要/写作/办公软件用法/简单计算）
    # | full（T1+T2+T3 通识问答；实时信息类天气/汇率/新闻恒拒）。
    # 硬性不变量（tests/test_intent_router.py 钉死）：公司信号一票否决先于办公白名单
    # （I1 宁拒不编）；企业相关未覆盖只走引导式拒答、绝不让通用模型按常识补齐（I2）；
    # 失败路径分诊 fail-closed（I3）。每用户日配额在 rate_limiter（RAG_GENERAL_DAILY_QUOTA，
    # 照 RAG_THINKING_DAILY_QUOTA 先例由 _load_limits 直读环境变量）。
    general_ability_mode: str = "off"   # RAG_GENERAL_ABILITY_MODE
    # T2/T3 灰度部门白名单（acl 组 code CSV，如 "it,admin"；空 = 全员）。T1 canned 不受限。
    general_ability_depts: str = ""     # RAG_GENERAL_ABILITY_DEPTS
    # 通用层模型（ModelGateway quick 档前身）。空 = 复用 config.llm.model（保证开箱可用）；
    # 生产建议设为 turbo 档型号降本（以百炼当期型号名为准）。
    general_llm_model: str = ""         # RAG_GENERAL_LLM_MODEL
    general_max_tokens: int = 800       # RAG_GENERAL_MAX_TOKENS：通用回答 token 上限
    triage_timeout: int = 6             # RAG_TRIAGE_TIMEOUT：失败路径分诊超时（秒），失败即按 enterprise 拒答
    # 引导式拒答话术（独立于通用层，可单独先行灰度）：拒答时附换说法建议/相近文档标题/知识贡献入口。
    guided_refusal: bool = False        # RAG_GUIDED_REFUSAL
    # 低置信带流式门控：SSE 缓冲开场 ~48 字符，命中拒答句式则改道失败序列（仅 mode!=off 时参与）。
    general_stream_gate: bool = True    # RAG_GENERAL_STREAM_GATE
    # 敏感词运维追加（CSV，逐词按字面匹配并入前置硬红线与禁兜底词表）。
    sensitive_extra_words: str = ""     # RAG_SENSITIVE_EXTRA_WORDS
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
    # ── 图片标记宽容归一化 ──────────────────────────────────────
    # True → 解析/清洗 <<IMG:N>> 前先把 LLM 偶发的畸形变体（全角冒号 <<IMG：3>>、
    #        【IMG:3】等）归一化为标准形式，避免字面残片漏渲染给用户。
    # False（默认）→ 行为与历史逐字节一致，变体既不出图也不被清除。
    img_marker_lenient: bool = False  # RAG_IMG_MARKER_LENIENT
    # ── 步骤扩展的带图兄弟保底 ──────────────────────────────────
    # K>0 → expand_step_context 意图筛选后若入选兄弟全部无图而家族里有带图
    #       step_card，按步号最近原则补入最多 K 张带图兄弟（不影响 locate_field
    #       意图；family-cap 收缩时同样保住带图行）。解决宽泛问题命中概述卡时
    #       真正带截图的操作步被扩展窗切掉、答案恒无图的召回缺口。
    # 0（默认）→ 关闭，行为与历史逐字节一致。
    expand_image_keep: int = 0  # RAG_EXPAND_IMAGE_KEEP
    # ── procedure_parent 展开子卡的类型归位 ────────────────────
    # True → expand_step_context 从 procedure_parent 命中展开的子步骤 chunk_type
    #        归位为 step_card（父卡本体保持 procedure_parent），使子卡进入现有
    #        step_card 全链路：LLM 获得 [📷 图片]/<<IMG:N>> 标记、渲染层可提图。
    #        否则子卡继承父类型，其 RDS 装载的 image_refs 在生成与渲染两端均不可达。
    # False（默认）→ 行为与历史逐字节一致。
    parent_child_as_stepcard: bool = False  # RAG_PARENT_CHILD_AS_STEPCARD
    # ── 会话历史 <<IMG:N>> 标记清洗（#F-mm5）───────────────────
    # True → (1) 入史前剥掉答案里的 <<IMG:N>> 标记（qa_session_log 的 answer_text
    #        不受影响，日志保真）；(2) LLM 回放历史时对 assistant 轮再洗一遍（覆盖
    #        存量已污染历史与客户端显式传入的 req.history）；(3) 图文 prompt 追加
    #        「不要模仿历史标记」规则。堵 follow-up 噪声图的机制性来源：历史里的
    #        <<IMG:N>> 会诱导 LLM 模仿，而 N 按当前轮 image_map 解析，界内即穿透
    #        全部渲染防线附上无关图（2026-06-10 已知症状的代码根源）。
    # False（默认）→ 行为与历史逐字节一致。
    history_strip_img_markers: bool = False  # RAG_HISTORY_STRIP_IMG_MARKERS
    # ── context 截断的带图压缩条目补救（#F-mm11b）───────────────
    # True → _format_context 截断丢弃尾部 chunk 时，对其中带图的以「header+正文
    #        前 200 字」压缩条目补回最多 3 条，保住 [📷 图片] <<IMG:N>> 提示
    #        （否则 step 扩展+邻居拼接常态性顶超 6000，尾部带图步骤卡的图在
    #        referenced-only 下恒出不来）。压缩条目用显式 10% 溢出预算
    #        （context 上限 = max_chars*1.1）。⚠️ 200 字残文与规则 9（数字须
    #        出自原文）有张力 —— e2e judge 把关（correctness/faithfulness
    #        无回退）后才开。半截标记防漏修复不挂此 flag（常开）。
    # False（默认）→ 不补救；截断行为与历史一致（除半截标记修复）。
    ctx_img_aware_trunc: bool = False  # RAG_CTX_IMG_AWARE_TRUNC
    # ── 候选池带图探测（#F-mm10a，rerank ON 专用）──────────────
    # True → rerank 之前对池内 step_card 批量探测 RDS image_refs_json 并附到
    #        chunk 上：重排发生在 expand 之前，step_card 此时无 image_refs（RDS
    #        未拉）且 HA3 行无 source_image → reranker._img_key 恒 None，
    #        qwen3-vl-rerank 对带图 step_card 结构性失明。探测后 VL 路由经既有
    #        any(_img_key) 通路自动激活，锁档的图池 VL 增益（0.825→0.850 R@1）
    #        才真正作用于 step_card 主体。
    #        ⚠️ rollout 注意：探测会把更多 query 从文本 qwen3-rerank 切到
    #        qwen3-vl-rerank——两模型分数分布若不同，高/中/低标签标定
    #        （rerank 0.9/0.8）会漂，开启前按 rerank_ab 口径复跑并 sanity check。
    # False（默认）→ 行为与历史逐字节一致。
    rerank_img_probe: bool = False  # RAG_RERANK_IMG_PROBE
    # ── 近平局带图倾斜（#F-mm10b，rerank OFF 专用）─────────────
    # True → rerank OFF 时 over-fetch 到 image_tiebreak_pool 个候选（over-fetch
    #        绑死在本 flag 内，不做独立 env），探测带图后对融合分差 < eps 的相邻
    #        近平局把带图载体前移，再显式截回 top_k——给 rerank OFF 的生产主路径
    #        一个不付 rerank 延迟/成本的「带图 step_card 反挤出」手段（HA3 端
    #        size=top_k 截断使排第 8 的带图卡任何后续机制都救不回）。
    #        仅单查询路径生效（multi-query 轮转合并语义与相邻交换不兼容，缺口
    #        已在 _multi_query_search docstring 声明）。eps 须金集标定后调整，
    #        取错会用弱文本换图——这是全清单对文本质量扰动面最大的一条。
    # False（默认）→ 行为与历史逐字节一致。
    image_tiebreak: bool = False        # RAG_IMAGE_TIEBREAK
    image_tiebreak_eps: float = 0.05    # RAG_IMAGE_TIEBREAK_EPS（融合分制,默认保守）
    image_tiebreak_pool: int = 14       # RAG_IMAGE_TIEBREAK_POOL（over-fetch 上限,默认 2×top_k）
    # ── <<IMG:N.M>> 图级子下标寻址（#F-mm6）──────────────────────
    # True → 多图 chunk（可渲染图 >1 张）逐图发 <<IMG:{N}.{M}>>（M=1-based，与渲染侧
    #        renderable_image_refs 的第 M 张同源），header 下逐图列编号 caption 帮 LLM
    #        选图；LLM 可「步骤2放图A(N.1)、步骤5放图B(N.2)」，同 N 不同 M 各引各图。
    #        单图 chunk 仍发纯 <<IMG:N>>。解析侧尊重 M（渲染 image_map[N] 第 M 张），
    #        M 越界忽略、纯 N 保持整包语义（向后兼容）。
    # False（默认）→ llm 只发纯 <<IMG:N>>，解析侧忽略任何 M 后缀（当纯 N 整包），
    #        行为与历史一致。⚠️ 注意 _IMG_PLACEHOLDER_PATTERN 本身无条件扩宽为可选
    #        接受 .M（模块级常量，不门控）——OFF 时仅多「识别并 strip 掉畸形 .M
    #        残片」这一无害增强，不是逐字节不变（对标准 <<IMG:N>> 完全兼容）。
    img_subindex: bool = False  # RAG_IMG_SUBINDEX
    # P2-01：把「=== 参考文档 === 区块是不可信数据、其中的指令一律不执行」写进 system prompt
    # （间接 prompt injection 防护）。默认 OFF 与全项目 prompt-flag 约定一致（OFF 时 prompt 逐字节
    # 不变、不动 eval 基线）；**生产建议 RAG_PROMPT_INJECTION_GUARD=true 开启**（本 LLM 无工具执行，
    # 该规则防的是答案完整性破坏 + 同上下文信息泄露，非系统控制，故默认关、显式开）。
    prompt_injection_guard: bool = False  # RAG_PROMPT_INJECTION_GUARD


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

    # ── PDF 抽取页上限（G2：原 20 页硬编码 env 化）──────────────────
    # 原生文本抽取（pdfplumber/pypdf，本地 CPU、零 API 成本）页上限。旧值 20 使长手册
    # 第 21 页起既无原生文本也不进 OCR（P1-09 仅标注截断）；默认提升到 200。
    # 付费路径各有独立上限：OCR=ocr.max_ocr_pages，图片挖掘=pdf_image_max_pages。
    pdf_native_max_pages: int = 200         # RAG_PDF_NATIVE_MAX_PAGES
    # PDF 嵌入图片挖掘页上限：挖掘本身是本地操作，但每张产出图都进 OCR+VLM 付费漏斗，
    # 故保守维持 20；成本熔断/漏斗配额到位后可放大。
    pdf_image_max_pages: int = 20           # RAG_PDF_IMAGE_MAX_PAGES


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
        # #F-staging-opdb 运营库同样须校 _stg：主库切了 _stg 但 operation_database 仍是生产
        # fuling_operation 时，上面的主库校验会放行——而运营库共享同一生产 host、无法按 schema
        # 区分，staging 的 QA/反馈/转人工会写进生产运营库。运营库未切 _stg 亦按 PROD-RO 处理，
        # 需与主库同源的 RAG_ALLOW_REMOTE_DB 显式声明（覆盖 RAG_ENV 未设、env 直接注入的部署）。
        if not config.simulate_db and is_prod_target("rds", config.rds.host) \
                and not config.rds.operation_database.endswith("_stg"):
            if not _require_ack("RAG_ALLOW_REMOTE_DB"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 指向生产 RDS 运营库"
                    f"（RDS_OPERATION_DATABASE={config.rds.operation_database}，非 _stg 库）。"
                    f"PROD-RO 会话请 export RAG_ALLOW_REMOTE_DB={_ACK_VALUE}")
        # 本体库同纪律（PR-B）：生产 host + 非 _stg 本体库 → 同源 ack
        if not config.simulate_db and is_prod_target("rds", config.rds.host) \
                and not config.rds.ontology_database.endswith("_stg"):
            if not _require_ack("RAG_ALLOW_REMOTE_DB"):
                raise EnvironmentMismatchError(
                    f"[ENV GUARD] environment={env} 指向生产 RDS 本体库"
                    f"（RDS_ONTOLOGY_DATABASE={config.rds.ontology_database}，非 _stg 库）。"
                    f"PROD-RO 会话请 export RAG_ALLOW_REMOTE_DB={_ACK_VALUE}")
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
        # P0-02（报告1）：生产 RDS 传输加密自检——**告警不阻断**（用户选「先不强制」，
        # 拿到阿里云 RDS CA 证书配好 RAG_RDS_SSL_CA 后可把本告警升为硬断言）。
        if not config.simulate_db and not (config.rds.ssl_ca or "").strip():
            logging.getLogger(__name__).warning(
                "[P0-02] environment=production 但 RDS 未启用 TLS（RAG_RDS_SSL_CA 未配）——"
                "RDS 链路承载身份/权限/日志，明文传输是审计缺口；请尽快配 CA 证书。")

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
        # #F-staging-opdb 运营库同样须切 _stg：此前只校主库，漏设 RAG_RDS_OPERATION_DATABASE 时
        # operation_database 仍是默认 fuling_operation（生产运营库）→ staging 的 QA/反馈/转人工
        # 会明文写进生产运营库，污染生产审计流水。运营库未切 _stg 一律 fail-fast。
        if not config.simulate_db and not config.rds.operation_database.endswith("_stg"):
            problems.append(f"RDS_OPERATION_DATABASE 必须以 _stg 结尾（当前 {config.rds.operation_database}）")
        if not config.simulate_db and not config.rds.ontology_database.endswith("_stg"):
            problems.append(f"RDS_ONTOLOGY_DATABASE 必须以 _stg 结尾（当前 {config.rds.ontology_database}）")
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
    default_llm_model = "qwen3.7-plus" if dashscope_key else "gemini-3.1-flash-lite"
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

    # 【P1-15】纯 RDS 作业的模型解析豁免（重评报告「纯 RDS job 无必要要求 DashScope key」）：
    # RAG_NO_MODEL_RESOLUTION=ack 时 llm/ocr/vlm/embedding 全部解析为**惰性哨兵**——无供应商
    # 端点、无 key。比「塞假 key」强的 fail-closed：任何意外模型调用立刻失败在空端点，绝无
    # 静默兜底到 Gemini 的通道。生产供应商守卫据此豁免「必须有 DashScope key」（禁 Gemini
    # 名称检查照跑，哨兵天然通过）；嵌入制度守卫同步豁免（声明不嵌入的作业没有污染索引的
    # 通道）。适用面：DataWorks retention / ontology backfill / invariants 等纯 RDS 节点——
    # 会真调模型的作业（如 ops_health_monitor 的 embedding 对账）**禁用**本旗标。
    _no_model_resolution = os.environ.get("RAG_NO_MODEL_RESOLUTION", "").strip().lower() == "ack"
    if _no_model_resolution:
        _SENTINEL_MODEL = "model-resolution-disabled"
        llm_key = ocr_key = emb_key = ""
        llm_base_url = ocr_base_url = emb_base = ""
        llm_model = ocr_model = vlm_model = emb_model = _SENTINEL_MODEL
        is_emb_dashscope = False

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
        pdf_native_max_pages=_env_int("PDF_NATIVE_MAX_PAGES", 200),
        pdf_image_max_pages=_env_int("PDF_IMAGE_MAX_PAGES", 20),

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
            ontology_database=_env("RDS_ONTOLOGY_DATABASE", "fuling_ontology"),
            ssl_ca=_env("RDS_SSL_CA", ""),
            ssl_verify_cert=_env_bool("RDS_SSL_VERIFY_CERT", True),
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
            client_fusion_enable=_env_bool("HA3_CLIENT_FUSION", True),
            client_fusion_dense_weight=_env_float("HA3_CLIENT_FUSION_DENSE_WEIGHT", 0.7),
            client_fusion_sparse_weight=_env_float("HA3_CLIENT_FUSION_SPARSE_WEIGHT", 0.1),
            client_fusion_text_weight=_env_float("HA3_CLIENT_FUSION_TEXT_WEIGHT", 0.3),
            client_fusion_pool=_env_int("HA3_CLIENT_FUSION_POOL", 50),
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
            daily_budget_rmb=_env_float("REBUILD_DAILY_BUDGET_RMB", 0.0),  # RAG_REBUILD_DAILY_BUDGET_RMB
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
            api_port=_env_int("API_PORT", 8000),                     # RAG_API_PORT（消费方=Makefile api 目标；Dockerfile/SAE 固定 8000）
            max_history_turns=_env_int("MAX_HISTORY_TURNS", 10),     # RAG_MAX_HISTORY_TURNS
            pure_text=_env_bool("PURE_TEXT", False),               # RAG_PURE_TEXT
            # 相关度标签阈值（高/中/低）；可经 RAG_SCORE_THRESHOLD_HIGH / _MEDIUM 覆盖。
            conversation_history=_env_bool("CONVERSATION_HISTORY", False),
            allowed_depts_acl=_env_bool("ALLOWED_DEPTS_ACL", False),            # RAG_ALLOWED_DEPTS_ACL
            main_hit_revalidate=_env_bool("MAIN_HIT_REVALIDATE", True),         # RAG_MAIN_HIT_REVALIDATE
            stream_reasoning=_env_bool("STREAM_REASONING", False),              # RAG_STREAM_REASONING
            serving_model_gateway=_env_bool("SERVING_MODEL_GATEWAY", False),    # RAG_SERVING_MODEL_GATEWAY
            qa_log_pii_redact=_env_bool("QA_LOG_PII_REDACT", True),             # RAG_QA_LOG_PII_REDACT
            score_threshold_high=_env_float("SCORE_THRESHOLD_HIGH", 7.7),       # RAG_SCORE_THRESHOLD_HIGH
            score_threshold_medium=_env_float("SCORE_THRESHOLD_MEDIUM", 5.8),   # RAG_SCORE_THRESHOLD_MEDIUM
            rerank_score_threshold_high=_env_float("RERANK_SCORE_THRESHOLD_HIGH", 0.9),
            rerank_score_threshold_medium=_env_float("RERANK_SCORE_THRESHOLD_MEDIUM", 0.8),
            low_confidence_guard=_env_bool("LOW_CONFIDENCE_GUARD", False),  # RAG_LOW_CONFIDENCE_GUARD
            confidence_cross_scale=_env_bool("CONFIDENCE_CROSS_SCALE", False),  # RAG_CONFIDENCE_CROSS_SCALE (#7)
            multi_query_mode=_env("MULTI_QUERY_MODE", "off").lower(),       # RAG_MULTI_QUERY_MODE
            multi_query_max=_env_int("MULTI_QUERY_MAX", 3),                 # RAG_MULTI_QUERY_MAX
            decompose_timeout=_env_int("DECOMPOSE_TIMEOUT", 8),             # RAG_DECOMPOSE_TIMEOUT
            doc_diversity_cap=_env_int("DOC_DIVERSITY_CAP", 0),             # RAG_DOC_DIVERSITY_CAP
            general_ability_mode=_env("GENERAL_ABILITY_MODE", "off").lower(),   # RAG_GENERAL_ABILITY_MODE
            general_ability_depts=_env("GENERAL_ABILITY_DEPTS", ""),            # RAG_GENERAL_ABILITY_DEPTS
            general_llm_model=_env("GENERAL_LLM_MODEL", ""),                    # RAG_GENERAL_LLM_MODEL
            general_max_tokens=_env_int("GENERAL_MAX_TOKENS", 800),             # RAG_GENERAL_MAX_TOKENS
            triage_timeout=_env_int("TRIAGE_TIMEOUT", 6),                       # RAG_TRIAGE_TIMEOUT
            guided_refusal=_env_bool("GUIDED_REFUSAL", False),                  # RAG_GUIDED_REFUSAL
            general_stream_gate=_env_bool("GENERAL_STREAM_GATE", True),         # RAG_GENERAL_STREAM_GATE
            sensitive_extra_words=_env("SENSITIVE_EXTRA_WORDS", ""),            # RAG_SENSITIVE_EXTRA_WORDS
            dingtalk_streaming=_env_bool("DINGTALK_STREAMING", False),          # RAG_DINGTALK_STREAMING
            dingtalk_stream_interval_ms=_env_int("DINGTALK_STREAM_INTERVAL_MS", 500),  # RAG_DINGTALK_STREAM_INTERVAL_MS
            image_cosurface=_env_bool("IMAGE_COSURFACE", True),                 # RAG_IMAGE_COSURFACE
            max_answer_images=_env_int("MAX_ANSWER_IMAGES", 6),                 # RAG_MAX_ANSWER_IMAGES
            step_expand_family_cap=_env_int("STEP_EXPAND_FAMILY_CAP", 12),      # RAG_STEP_EXPAND_FAMILY_CAP
            img_marker_lenient=_env_bool("IMG_MARKER_LENIENT", False),          # RAG_IMG_MARKER_LENIENT
            expand_image_keep=_env_int("EXPAND_IMAGE_KEEP", 0),                 # RAG_EXPAND_IMAGE_KEEP
            parent_child_as_stepcard=_env_bool("PARENT_CHILD_AS_STEPCARD", False),  # RAG_PARENT_CHILD_AS_STEPCARD
            history_strip_img_markers=_env_bool("HISTORY_STRIP_IMG_MARKERS", False),  # RAG_HISTORY_STRIP_IMG_MARKERS
            ctx_img_aware_trunc=_env_bool("CTX_IMG_AWARE_TRUNC", False),            # RAG_CTX_IMG_AWARE_TRUNC
            rerank_img_probe=_env_bool("RERANK_IMG_PROBE", False),                  # RAG_RERANK_IMG_PROBE
            image_tiebreak=_env_bool("IMAGE_TIEBREAK", False),                      # RAG_IMAGE_TIEBREAK
            image_tiebreak_eps=_env_float("IMAGE_TIEBREAK_EPS", 0.05),              # RAG_IMAGE_TIEBREAK_EPS
            image_tiebreak_pool=_env_int("IMAGE_TIEBREAK_POOL", 14),                # RAG_IMAGE_TIEBREAK_POOL
            img_subindex=_env_bool("IMG_SUBINDEX", False),                          # RAG_IMG_SUBINDEX
            prompt_injection_guard=_env_bool("PROMPT_INJECTION_GUARD", False),       # RAG_PROMPT_INJECTION_GUARD
        ),
    )

    # 💡 生产安全守卫：当处于 production 或 staging 环境下，坚决杜绝 fallback 到 Gemini！
    # 强制校验所有大模型/视觉/向量 API 配置必须为阿里云 DashScope（或者是明确的非 Gemini，比如专有端点）
    _env_label_prod = config.environment in ("production", "staging")

    # 【P2-27】生产安全姿态断言：RAG_QA_LOG_PII_REDACT 仅限本地开发调试取证，
    # production/staging 下关闭它会把用户问题里的手机号/身份证等 PII 明文写入 qa_session_log
    # （消费方 qa_logger._qa_log_pii_redact_on），必须与供应商守卫同级 fail-fast。
    if _env_label_prod and not config.rag.qa_log_pii_redact:
        raise ValueError(
            f"🚨 [PRODUCTION SECURITY GUARD] RAG_QA_LOG_PII_REDACT=false 在 '{config.environment}' 环境被禁止！"
            f"该开关仅限本地开发调试取证使用；生产/预演关闭它会把用户提问中的 PII（手机号/身份证等）"
            f"明文落盘 qa_session_log。请移除该环境变量（默认即 True=掩码）。"
        )

    # 【Agent P1】职责分离硬门（重评报告 §6「self-approval 危险开关缺生产启动断言」）：
    # RAG_AGENT_ALLOW_SELF_APPROVAL 是 dev/单人联调的逃生门——production/staging 打开它
    # 意味着发起人可自批 HIGH_WRITE，审批闭环整体失效，必须与供应商守卫同级 fail-fast。
    # routes/agent._self_approval_allowed 另有运行时环境复核（双保险，防启动后注入）。
    if _env_label_prod and os.environ.get("RAG_AGENT_ALLOW_SELF_APPROVAL",
                                          "").strip().lower() in ("1", "true", "yes", "on"):
        raise ValueError(
            f"🚨 [PRODUCTION SECURITY GUARD] RAG_AGENT_ALLOW_SELF_APPROVAL 在 '{config.environment}' "
            f"环境被禁止！该开关允许发起人自批高风险写操作（职责分离失效），仅限本地开发联调。"
            f"请移除该环境变量。"
        )

    # 【批次5 P0-07d，unknown-unknowns 外审】生产安全姿态断言：强制认证与 ACL fail-closed
    # 是 main P0 加固（0cbb0f8）的两根梁，代码默认 off 是「代码先行、部署后开」的过渡态——
    # production/staging 启动时必须显式表态：要么把两个 flag 打开，要么设
    # RAG_ALLOW_LEGACY_OPEN_PROD 显式承认延续旧开放姿态（过渡逃生口，环境变量到位后
    # 应删除）。「部署漏配安全变量 → 告警后继续」到此收口为 fail-closed。
    # P1-14（外审核查 2026-07-16）：ack 绑当日日期——`ack:<YYYY-MM-DD>`（仿 env_guard
    # RAG_DESTRUCTIVE_PROD_ACK 惯例，午夜过期）。无日期的裸 `ack` 会变成永久逃生口：
    # 设完就忘、逐渐制度化，正是外审点名的形态。每次重启/重部署都要求当日重签，
    # 姿态缺口不可能被遗忘；启用即 critical 日志（可告警特征）。
    # ⚠️ RDS TLS（RAG_RDS_SSL_CA）维持告警不阻断——记录在案的用户决策（本文件 P0-02 注），
    # CA 证书到位后再升硬断言，不在本断言范围。
    if _env_label_prod:
        _posture_missing = [
            env for env in ("RAG_REQUIRE_AUTH", "RAG_ACL_FAIL_CLOSED")
            if os.environ.get(env, "").strip().lower() not in ("1", "true", "yes", "on")]
        if _posture_missing:
            from datetime import datetime as _dt
            _ack_raw = os.environ.get("RAG_ALLOW_LEGACY_OPEN_PROD", "").strip().lower()
            _today = _dt.now().strftime("%Y-%m-%d")
            if _ack_raw == f"ack:{_today}":
                logging.getLogger(__name__).critical(
                    "🚨 [LEGACY-OPEN POSTURE] '%s' 环境以 RAG_ALLOW_LEGACY_OPEN_PROD=%s "
                    "延续旧开放姿态运行（%s 未开启）——当日有效，明日重启需重签；"
                    "环境变量到位后请删除该逃生口（P1-14）。",
                    config.environment, _ack_raw, "/".join(_posture_missing))
            else:
                _hint = ("旧格式 `ack` 已失效（无期限逃生口会被遗忘）；" if _ack_raw else "")
                raise ValueError(
                    f"🚨 [PRODUCTION SECURITY GUARD] '{config.environment}' 环境未开启 "
                    f"{'/'.join(_posture_missing)}（强制认证 / ACL fail-closed）。"
                    f"请在部署环境变量中开启它们；{_hint}确需延续旧开放姿态（过渡期）须"
                    f"显式设 **当日** RAG_ALLOW_LEGACY_OPEN_PROD=ack:{_today}"
                    f"（P1-14 日期绑定，午夜过期；unknown-unknowns 批次5 P0-07d）。")

    # 【批次5 P1-10，unknown-unknowns 外审】拓扑防呆：单 worker + 进程内内存态是隐藏
    # 约束（Dockerfile 钉 --workers 1）——声明多副本（RAG_EXPECTED_REPLICAS>1）而
    # 会话/限流/去重/token 四态仍是 memory 后端时，语义会按副本分裂（会话丢、限流
    # 失效、cancel 跨实例失联）。副本数无法自动感知，靠部署侧显式声明触发断言。
    if _env_label_prod:
        try:
            _replicas = int(os.environ.get("RAG_EXPECTED_REPLICAS", "1") or 1)
        except ValueError:
            _replicas = 1
        if _replicas > 1:
            _mem_backends = [
                k for k in ("RAG_SESSION_BACKEND", "RAG_RATE_LIMIT_BACKEND",
                            "RAG_MSG_DEDUP_BACKEND", "RAG_TOKEN_CACHE_BACKEND")
                if os.environ.get(k, "memory").strip().lower() != "redis"]
            # P1-10 增量（外审核查 2026-07-16）：agent 开着时事件中继也必须 redis——
            # 多副本下 SSE/回放挂在单实例本地队列上，另一副本的消费者永远收不到帧
            # （审批帧丢失=审批黑洞）。agent off 不要求（中继无消费面）。
            if os.environ.get("RAG_AGENT_ENABLE", "").strip().lower() in (
                    "1", "true", "yes", "on") and \
                    os.environ.get("RAG_AGENT_EVENT_RELAY", "").strip().lower() != "redis":
                _mem_backends.append("RAG_AGENT_EVENT_RELAY")
            if _mem_backends:
                raise ValueError(
                    f"🚨 [PRODUCTION SECURITY GUARD] RAG_EXPECTED_REPLICAS={_replicas}>1 "
                    f"而 {_mem_backends} 仍为内存后端——多副本下会话/限流/去重/token/事件流"
                    f"语义将按副本分裂。请切换 redis 后端（配 RAG_REDIS_URL 同出）或移除"
                    f"副本声明（unknown-unknowns 批次5 P1-10 + 外审核查增量）。")

    # 【P2-28/P2-6】供应商守卫触发条件 = 自报标签 OR 生产物理指纹（is_prod_target）：
    # 此前只键于标签——dev 标签经 RAG_ALLOW_REMOTE_DB/SEARCH=read_only_ack 实连生产 RDS/HA3、
    # 且只配 GEMINI key 时，模型解析全路由 Google，生产 chunk_text/查询内容会被 POST 到 Google。
    # 现在只要「碰生产物理目标」，供应商约束（必须 DashScope、禁 Gemini）就与 production 同级生效；
    # 带 DashScope key 的 prod_ro 只读评测（解析到 Qwen）照常通过。
    # 各目标仅在对应 simulate=False 时评估（与 _validate_environment_target_consistency 同一前置，
    # make sim / 单测天然跳过）；OSS 因默认 bucket 名即生产指纹（fuling-knowledge-base），
    # 额外要求 endpoint 非空——未配 endpoint 连不上任何桶，不构成「碰生产」。
    _search_targets = " ".join(filter(None, (
        config.alibaba_vector.endpoint, config.alibaba_vector.instance_id,
        config.opensearch.host)))
    _touches_prod_target = (
        (not config.simulate_db and is_prod_target("rds", config.rds.host))
        or (not config.simulate_opensearch and is_prod_target("search", _search_targets))
        or (not config.simulate_oss and bool(config.oss.endpoint)
            and is_prod_target("oss", config.oss.bucket_name))
    )

    if _env_label_prod or _touches_prod_target:
        _guard_scope = config.environment if _env_label_prod \
            else f"{config.environment} + prod-target-fingerprint"
        # P1-15：RAG_NO_MODEL_RESOLUTION=ack（模型全解析为惰性哨兵）时豁免 key 要求——
        # 「必须有 DashScope key」防的是**兜底到 Gemini**，哨兵状态无任何供应商通道，
        # 比有 key 更强；下方禁 Gemini 名称检查照跑（哨兵天然通过）。
        if not dashscope_key and not _no_model_resolution:
            raise ValueError(
                f"🚨 [PRODUCTION SECURITY GUARD] DashScope API Key is not configured under '{_guard_scope}' environment! "
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
                    f"(base_url='{base_url}', model='{model_name}') under '{_guard_scope}' environment! "
                    f"Production runs must strictly utilize Alibaba Cloud (Qwen) services."
                )

    # 【P3-8】嵌入制度兼容守卫（非生产也生效）：整个检索制度假设 dense+sparse +
    # EMBEDDING_DIMENSION（HA3 表 1024 维、查询侧 dense&sparse 双路）。无 DashScope key 时
    # 嵌入静默兜底到 gemini-embedding-2——无 sparse、原生维度不同，此状态下建的索引 / 跑的
    # eval 与生产制度不兼容且不报错（数悄然失效）。simulate 全程哈希向量不受影响；只有
    # 「真嵌入（simulate 关）+ 配置了检索后端 + 解析到非 DashScope 嵌入模型」三条同时成立
    # 才 fail-fast。刻意实验用 RAG_ALLOW_INCOMPATIBLE_EMBEDDING=ack 显式放行。
    _has_search_backend = bool(config.alibaba_vector.endpoint or config.opensearch.host)
    if (not rag_simulate and not is_emb_dashscope and _has_search_backend
            and not _no_model_resolution
            and os.environ.get("RAG_ALLOW_INCOMPATIBLE_EMBEDDING", "") != "ack"):
        raise EnvironmentMismatchError(
            f"🚨 [EMBEDDING REGIME GUARD] 嵌入模型解析为 '{emb_model}'（非 DashScope）但已配置"
            f"检索后端：该兜底无 sparse 向量且原生维度 ≠ EMBEDDING_DIMENSION={config.embedding.dimension}，"
            f"与 dense+sparse 检索制度不兼容——此状态下建的索引/评测结果全部失真。"
            f"请配置 DASHSCOPE_API_KEY（或 RAG_EMBEDDING_MODEL=text-embedding-v4），"
            f"纯本地烟囱测试请用 RAG_SIMULATE=true；刻意实验设 RAG_ALLOW_INCOMPATIBLE_EMBEDDING=ack。"
        )

    # 💡 环境守卫第二层：环境标签 ↔ 物理目标交叉校验（规则表见函数 docstring）
    _validate_environment_target_consistency(config)

    # #9：rrf 融合分尺度 ~0.0x，而相关度档位(高/中/低)与低置信护栏阈值(7.7/5.8)按 weighted 融合分
    # 标定（见 RAGConfig score_threshold_*）。一旦切 rrf 且未开 rerank（rerank 会改用 0.9/0.8 尺度），
    # llm_generator 的 score_level/score_relevance/is_low_confidence_band 会把几乎所有命中误标为「低」
    # 并常态触发软拒答——且不报错。当前无 rrf 校准阈值，故此处只 loud-warn，不静默也不硬拦。
    _av = config.alibaba_vector
    if getattr(_av, "hybrid_fusion", "weighted") == "rrf" and not getattr(_av, "rerank_enable", False):
        print(
            "⚠️ [CONFIG GUARD] HA3_HYBRID_FUSION=rrf 且 rerank 关闭：相关度档位/低置信护栏阈值"
            "(7.7/5.8) 按 weighted 融合分标定，rrf 分尺度(~0.0x)下会把几乎所有命中误标为「低」并"
            "触发软拒答。请改回 weighted，或为 rrf 单独标定 score_threshold_*（当前未提供 rrf 校准值）。"
        )

    # 三路客户端融合的档位阈值自动标定（2026-07-13 只读标定：40q×top7 对旧含-sparse 分布做
    # 分位数匹配，scratch/score_threshold_calibration_20260713.json + docs/ha3_client_fusion_
    # 3way_ab_2026-07-13.md）。融合对外 score=knn_weight*dense_IP+text_weight*BM25_raw，
    # 分数域 ~[0.02,0.64]，7.7/5.8 旧阈值下全部命中落「低」→ 软拒答常态触发。融合开启且未
    # 显式设阈值时自动套标定值（env RAG_SCORE_THRESHOLD_HIGH/MEDIUM 显式设置永远优先）；
    # release-gate refreeze 时应以更大样本复标。
    if getattr(_av, "client_fusion_enable", False):
        if "RAG_SCORE_THRESHOLD_HIGH" not in os.environ:
            config.rag.score_threshold_high = 0.57
        if "RAG_SCORE_THRESHOLD_MEDIUM" not in os.environ:
            config.rag.score_threshold_medium = 0.52
        print(
            "ℹ️ [CONFIG GUARD] RAG_HA3_CLIENT_FUSION=true：相关度档位阈值套用客户端融合标定值"
            f"（high={config.rag.score_threshold_high} / medium={config.rag.score_threshold_medium}；"
            "显式设 RAG_SCORE_THRESHOLD_HIGH/MEDIUM 可覆盖）。"
        )
    elif (config.environment in ("production", "staging")
          and os.environ.get("RAG_HA3_KNN_SPARSE_ENABLE", "false").lower() != "true"
          and "RAG_SCORE_THRESHOLD_HIGH" not in os.environ
          and not getattr(_av, "rerank_enable", False)):
        # 去-sparse 服务端混合（8fc80f8 新默认）上线的同款隐患：/search 分数域塌缩到
        # ~[0.54,0.71]（sparse 曾是 7.7/5.8 标定母体的分数主体），旧阈值下全部命中标「低」。
        # 标定参考值 high=0.65/medium=0.60（同一次标定）。沿 rrf 先例只 loud-warn 不自动改
        # （存量评测/基线锚定 7.7/5.8 语义），正式值随 release-gate refreeze 定。
        print(
            "⚠️ [CONFIG GUARD] 去-sparse 服务端混合 + 档位阈值仍为 7.7/5.8（含-sparse 旧尺度）："
            "分数域塌缩后全部命中会标「低」并常态触发软拒答。部署前请设 "
            "RAG_SCORE_THRESHOLD_HIGH/MEDIUM（标定参考 0.65/0.60）或开启 rerank/客户端融合。"
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
