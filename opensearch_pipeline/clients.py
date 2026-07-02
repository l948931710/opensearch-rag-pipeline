# -*- coding: utf-8 -*-
"""
clients.py — 外部存储/检索客户端工厂（OSS Bucket / HA3 / OpenSearch）

从 pipeline_nodes.py 机械搬移（F-A1 结构债拆分，2026-07-01）。pipeline_nodes
仍 re-export 全部名字（含 `_resolve_simulate`），摄取节点与既有 tests 的
monkeypatch 目标（`opensearch_pipeline.pipeline_nodes._get_opensearch_client` 等）
不受影响。simulate 解析与写守卫（GuardedBucket）逐字保留。
"""

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

        # 去除 endpoint 中的 http:// 或 https:// 前缀保护
        clean_endpoint = cfg.endpoint.replace("http://", "").replace("https://", "")

        ha3_config = Config(
            endpoint=clean_endpoint,
            instance_id=cfg.instance_id,
            access_user_name=cfg.access_user_name,
            access_pass_word=cfg.access_pass_word
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
        print(f"    ├─ [HA3 Engine] Table and mappings are fully managed on Alibaba Cloud Web Console. Skipping dynamic creation.")
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
