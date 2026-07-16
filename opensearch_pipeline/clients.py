# -*- coding: utf-8 -*-
"""
clients.py — 外部存储/检索客户端工厂（OSS Bucket / HA3 / OpenSearch）

从 pipeline_nodes.py 机械搬移（F-A1 结构债拆分，2026-07-01）。pipeline_nodes
仍 re-export 全部名字（含 `_resolve_simulate`），摄取节点与既有 tests 的
monkeypatch 目标（`opensearch_pipeline.pipeline_nodes._get_opensearch_client` 等）
不受影响。simulate 解析与写守卫（GuardedBucket）逐字保留。

2026-07-03：HA3 响应解析（parse_ha3_response）与输出字段清单
（HA3_DEFAULT_OUTPUT_FIELDS / HA3_PARITY_OUTPUT_FIELDS）从 retriever.py 上移至此
——serving 与批处理（stage-3 parity / HA3 对账）共用的 HA3 客户端层语义，消除
批处理反向依赖 serving 私有名。retriever 仍以旧下划线名 re-export 同一对象
（绑定恒等由 tests/test_ha3_client_coupling.py 看住）。
"""

import json
import os
from typing import Any, Dict, List

from opensearch_pipeline.config import get_config


def _resolve_simulate(ctx: dict, kind: str, default=None) -> bool:
    """统一解析 simulate 开关：ctx 细粒度键 > ctx 全局 "simulate" > 兜底值。

    兜底值默认取 config.simulate_<kind>；个别调用方（如 OSS 客户端包装）用自身参数
    兜底时显式传 default。此前这条三层取值在 ~19 处手写复制，并已出现漂移
    （orchestrator 的 stage-2 loader 少了 ctx["simulate"] 一层）。
    """
    if default is None:
        default = getattr(get_config(), f"simulate_{kind}")
    return ctx.get(f"simulate_{kind}", ctx.get("simulate", default))


def _get_opensearch_client(ctx: dict = None):
    from opensearch_pipeline.config import get_config
    config = get_config()

    # 💡 如果是模拟模式，我们不需要真正的客户端，返回 Mock 字符串以允许干跑/Simulation 顺利通过。
    #    DAG 节点必须传 ctx：开关按 ctx 细粒度 > ctx 全局 > config 解析（与 _get_oss_bucket 一致），
    #    否则 ctx/config 不一致时真实跑会拿到 mock、假装 INDEXED 后又真删 RDS 旧版本（裂脑）。
    simulate_opensearch = config.simulate_opensearch
    if ctx is not None:
        simulate_opensearch = _resolve_simulate(ctx, "opensearch", default=simulate_opensearch)
    if simulate_opensearch:
        return "MOCK_HA3_CLIENT"

    cfg = config.alibaba_vector

    # 💡 强健的设计：自适应支持标准开源 OpenSearch 以及阿里云向量检索版（HA3）
    # 如果配置了 HA3_ENDPOINT 则使用阿里云专用 SDK；否则优雅降级为本地/开发标准 OpenSearch 客户端
    if cfg and cfg.endpoint:
        from alibabacloud_ha3engine_vector.client import Client
        from alibabacloud_ha3engine_vector.models import Config
        from alibabacloud_tea_util.models import RuntimeOptions

        # 去除 endpoint 中的 http:// 或 https:// 前缀保护
        clean_endpoint = cfg.endpoint.replace("http://", "").replace("https://", "")

        # 2026-07-16(100 条丢件重推现场):SDK 不传 runtime_options 时默认读超时过短,
        # pushDocuments 的大 payload(百级 chunk×dense1024+sparse ≈ MB 级)在公网/家宽
        # 稳定超时→整批 failed(查询 API 小响应不受影响,故检索一直正常)。显式给足
        # 读超时;env 可调,单位毫秒。查询路径共用此客户端,读超时放宽无副作用
        # (真挂死由重试层兜底)。
        _read_to = int(os.environ.get("RAG_HA3_READ_TIMEOUT_MS", "60000"))
        _conn_to = int(os.environ.get("RAG_HA3_CONNECT_TIMEOUT_MS", "10000"))
        ha3_config = Config(
            endpoint=clean_endpoint,
            instance_id=cfg.instance_id,
            access_user_name=cfg.access_user_name,
            access_pass_word=cfg.access_pass_word,
            runtime_options=RuntimeOptions(read_timeout=_read_to, connect_timeout=_conn_to),
        )
        return Client(ha3_config)
    else:
        # Fallback to standard OpenSearch for local development / testing
        from opensearchpy import OpenSearch
        os_cfg = config.opensearch
        auth = (os_cfg.auth_user, os_cfg.auth_password) if os_cfg.auth_user and os_cfg.auth_password else None
        client = OpenSearch(
            hosts=[{'host': os_cfg.host, 'port': os_cfg.port}],
            http_compress=True,
            http_auth=auth,
            use_ssl=os_cfg.use_ssl,
            verify_certs=os_cfg.verify_certs,
            ssl_assert_hostname=False,
            ssl_show_warn=False
        )
        return client


def parse_ha3_response(resp) -> List[Dict[str, Any]]:
    """将 HA3 QueryResponse 解析为标准化的结果列表。

    2026-07-03 从 retriever.py `_parse_ha3_response` 逐字上移（HA3 客户端层语义，
    serving 检索与批处理 parity/对账共用）；retriever 以旧名 re-export 同一对象。
    """
    body = resp.body
    if isinstance(body, str):
        body = json.loads(body)
    elif hasattr(body, "to_map"):
        body = body.to_map()

    results = []
    if isinstance(body, dict):
        raw = body.get("result", body.get("hits", body.get("data", [])))
        if isinstance(raw, dict):
            raw = raw.get("hits", raw.get("items", []))
        if isinstance(raw, list):
            results = raw

    parsed = []
    for item in results:
        fields = item.get("fields", item)
        # HA3 的 MULTI_STRING 字段返回列表 (e.g. ['image'])，需要归一化为字符串
        raw_chunk_type = fields.get("chunk_type", "")
        if isinstance(raw_chunk_type, list):
            raw_chunk_type = raw_chunk_type[0] if raw_chunk_type else ""
        parsed.append({
            # chunk_id 是 step_card/visual_knowledge 的 RDS 重建键，也是 expand_step_context
            # 末尾去重的唯一键——必须透传，否则去重会把所有无 id 的 chunk 折叠成一个。
            "chunk_id": fields.get("chunk_id", ""),
            # HA3 主键：chunk_id 为空（历史 chunk）时各处 `chunk_id or id` 回退键的实体
            "id": str(item.get("id") or fields.get("id") or ""),
            "chunk_text": fields.get("chunk_text_store", fields.get("chunk_text", "")),
            "title": fields.get("title", ""),
            "section_title": fields.get("section_title", ""),
            "doc_id": fields.get("doc_id", ""),
            # version_no：答案血缘——使一条已落库回答能溯源到精确的文档版本(配合 HA3_DEFAULT_OUTPUT_FIELDS)
            "version_no": fields.get("version_no", 0),
            "category_l1": fields.get("category_l1", ""),
            "chunk_index": fields.get("chunk_index", 0),
            "page_num": fields.get("page_num", 0),
            "kb_type": fields.get("kb_type", "public"),
            # P2-02：字段缺失（投影漂移/旧索引/异常文档）时默认按最严的 restricted 兜底，
            # 绝不把权限未知的命中当 public 处理（fail-closed 标签；真实 restricted 本就被
            # 引擎端过滤不会返回，故这里只在字段漂移时生效，正常流量零影响）。
            "permission_level": fields.get("permission_level") or "restricted",
            "owner_dept": fields.get("owner_dept", ""),
            "chunk_type": raw_chunk_type,
            "source_image": fields.get("source_image", ""),
            "visual_summary": fields.get("visual_summary", ""),
            "score": item.get("score", item.get("_score", 0)),
        })
    return parsed


# HA3 查询统一返回字段——serving 答案路径的默认清单
# （search_chunks / cosurface_doc_images / 文档展开共用，避免漂移）。
# ⚠️ 批处理 parity/对账不要用这份清单，用下面 pin 死的 HA3_PARITY_OUTPUT_FIELDS。
HA3_DEFAULT_OUTPUT_FIELDS = [
    "id", "chunk_id", "doc_id", "version_no", "chunk_text_store", "title", "section_title",
    "category_l1", "chunk_index", "page_num", "kb_type",
    "permission_level", "owner_dept", "chunk_type",
    "source_image", "visual_summary",
]

# 批处理/对账（stage-3 parity 守卫、HA3 orphan reconcile、CS3 扫描）专用最小字段集。
# ⚠️ 故意独立于 HA3_DEFAULT_OUTPUT_FIELDS pin 死（不共享对象、不做子集引用）：serving 为
# 答案路径增删字段/调整默认清单，绝不改变各安全检查（旧版本 deactivate gate / orphan 删除）
# 对"存在/漂移"的判定口径。只含这些路径真正消费的列：
#   id（PK 相符判定）· chunk_id/doc_id（orphan 分类与 id-range enum hint）
#   · version_no/chunk_type（CS3 对账报表）· chunk_text_store（parity drift 子检查）。
HA3_PARITY_OUTPUT_FIELDS = [
    "id", "chunk_id", "doc_id", "version_no", "chunk_text_store", "chunk_type",
]


def _get_oss_bucket(ctx: dict = None):
    """获取阿里云 OSS Bucket 客户端。"""
    from opensearch_pipeline.config import get_config
    config = get_config()

    # Resolve simulate_oss flag from context or global config
    simulate_oss = config.simulate_oss
    if ctx is not None:
        simulate_oss = _resolve_simulate(ctx, "oss", default=simulate_oss)

    # Safe fallback: if credentials are dummy or empty, force simulation to prevent developer test errors
    access_id = config.oss.access_key_id
    if not access_id or access_id.strip() in ("xxx", ""):
        return None, True

    if simulate_oss:
        return None, True

    # Real mode: oss2 is strictly required!
    try:
        import oss2
    except ImportError:
        raise ImportError(
            "oss2 library is not installed, but real Aliyun OSS integration is requested "
            "(simulate_oss is False and OSS credentials are configured). "
            "Please ensure 'oss2' is added to requirements.txt."
        )

    auth = oss2.Auth(config.oss.access_key_id, config.oss.access_key_secret)
    bucket = oss2.Bucket(auth, config.oss.endpoint, config.oss.bucket_name)
    # 写守卫代理：非生产环境写生产桶需当日 ack（读/签名透传）。本地正常形态是
    # simulate_oss=true 不进此分支——代理只防"误设 simulate_oss=false + 生产桶"的配置漂移。
    from opensearch_pipeline.env_guard import GuardedBucket
    return GuardedBucket(bucket, config.oss.bucket_name), False


def _ensure_opensearch_index(client, index_name: str, dimension: int):
    """确保 OpenSearch 索引存在并具有正确的 Lucene KNN 映射。"""
    # 如果是 HA3 Engine 客户端，其表结构由阿里云控制台可视化配置，不可在此动态创建，直接跳过
    if hasattr(client, "push_documents") or client == "MOCK_HA3_CLIENT":
        print("    ├─ [HA3 Engine] Table and mappings are fully managed on Alibaba Cloud Web Console. Skipping dynamic creation.")
        return

    if not client.indices.exists(index=index_name):
        body = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100
                }
            },
            "mappings": {
                "properties": {
                    "id": {"type": "keyword"},
                    "chunk_id": {"type": "keyword"},
                    "doc_id": {"type": "keyword"},
                    "version_no": {"type": "integer"},
                    "chunk_index": {"type": "integer"},
                    "chunk_text": {"type": "text"},
                    "source_image": {"type": "keyword"},
                    "visual_summary": {"type": "text"},
                    "chunk_vector": {
                        "type": "knn_vector",
                        "dimension": dimension,
                        "method": {
                            "name": "hnsw",
                            "space_type": "l2",
                            "engine": "lucene",
                            "parameters": {"ef_construction": 128, "m": 24}
                        }
                    },
                    "chunk_type": {"type": "keyword"},
                    "owner_dept": {"type": "keyword"},
                    "permission_level": {"type": "keyword"},
                    "is_active": {"type": "boolean"}
                }
            }
        }
        client.indices.create(index=index_name, body=body)
        print(f"    └─ [OpenSearch] Created index '{index_name}' with KNN dimension {dimension}")
