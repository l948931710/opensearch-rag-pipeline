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


def _get_opensearch_client(ctx: dict = None, *, bounded: bool = False):
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

        runtime_options = None
        if bounded:
            # F3（对账竞态根治）：reconciler 持 RDS 行锁跨 HA3 网络 I/O，必须有确定的
            # 单请求上界。SDK 默认 max_attempts=2 且 autoretry 恒被置 True（client.py 源码
            # 自注："如果想要关闭重试，可以手动设置maxAttempts=0"）——0 = 恰好一次尝试。
            from alibabacloud_tea_util import models as util_models
            runtime_options = util_models.RuntimeOptions(
                max_attempts=0, connect_timeout=5000, read_timeout=10000)

        ha3_config = Config(
            endpoint=clean_endpoint,
            instance_id=cfg.instance_id,
            access_user_name=cfg.access_user_name,
            access_pass_word=cfg.access_pass_word,
            runtime_options=runtime_options,
        )
        client = Client(ha3_config)
        if bounded:
            # 能力断言（fail-closed）：SDK 形态变化悄悄丢掉 runtime_options 时，绝不能
            # 让 reconciler 以为持锁时长有界。构造后核验【客户端生效值】而非入参。
            ro = getattr(client, "_runtime_options", None)
            if (ro is None or getattr(ro, "max_attempts", None) != 0
                    or getattr(ro, "connect_timeout", None) != 5000
                    or getattr(ro, "read_timeout", None) != 10000):
                raise RuntimeError(
                    "HA3 SDK 未生效 bounded runtime options（max_attempts=0/5s/10s）——"
                    "持锁对账拒绝在无超时上界下运行（fail-closed）。effective=%r" % ro)
        return client
    else:
        # Fallback to standard OpenSearch for local development / testing
        from opensearchpy import OpenSearch
        os_cfg = config.opensearch
        auth = (os_cfg.auth_user, os_cfg.auth_password) if os_cfg.auth_user and os_cfg.auth_password else None
        extra_kwargs = {}
        if bounded:
            # bounded（F3 对账路径）：与 HA3 分支同款确定上界；非 bounded 不传参，
            # 保持 opensearchpy 默认超时语义逐字不变（显式 None 会被当"无超时"）。
            extra_kwargs["timeout"] = 10
        client = OpenSearch(
            hosts=[{'host': os_cfg.host, 'port': os_cfg.port}],
            http_compress=True,
            http_auth=auth,
            use_ssl=os_cfg.use_ssl,
            verify_certs=os_cfg.verify_certs,
            ssl_assert_hostname=False,
            ssl_show_warn=False,
            **extra_kwargs,
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


# ──────────────────────────────────────────────────────────────────────────────
# HA3 确定性枚举 + 权威 fetch（2026-07-22 终局，见 memory ha3-inverted-enumeration）
#
# 背景：零向量 + id 范围 filter 的 QueryRequest **不是全集枚举**——零向量与任何向量
# 内积恒为 0，全部文档得分并列，返回哪个子集由 ANN 遍历/剪枝路径决定，是「任意但确定
# 的子集」且随 build/merge 漂移（实测零向量恒缺 113 行、eps 常数向量恒缺另 220 行，
# 交集仅 5）。历次「行蒸发」全是该枚举伪影，数据从未丢失。
#
# 本节提供两条替代原语，**批处理/对账/校验一律走这里，禁止再用零向量枚举**：
#   ha3_enumerate_bucket —— 纯倒排单桶枚举（发现未知 PK，fetch 做不到的方向）
#   ha3_fetch_by_pks     —— 官方主键 fetch（已知 PK 的权威存在性判定）
# ──────────────────────────────────────────────────────────────────────────────

# 倒排枚举锚点：双态覆盖（HA3 侧 is_active=0 的僵尸行也要被发现）。
#
# ✅ 阿里售后 2026-07-25 正式确认：**「通过 is_active 的做法是没有问题的，但是效率不高。
# 我们没有 match-all 的写法，推荐是通过索引去筛选。」** ⇒ 本锚点是受支持的用法（不再是
# 未定义行为），且官方确认 match-all 确实不存在。
#
# 为什么范围不能写进 queryString（实测 9 种变体恒返 0，无 errorCode）：官方答复
# 「id 是**主键索引**，可能不支持 range 查询；可以对 id 字段再建一个**数值索引**」。
# 官方区间语法本身是对的（`索引名:[v1,v2]` 闭 / `(v1,v2)` 开 / `(,v2)` 半开），但前提是
# 该字段有**数值范围索引**——主键索引只支持精确匹配。故当前范围一律走 `filter` 参数。
#
# 【未采纳的优化路径】给 id 加数值索引后可改用 queryString 区间、少发请求。**暂不做**：
# HA3 加索引很可能要重建表，而全量重建是本项目历史上最高危的操作（曾因回放过期 Swift log
# 上线空 generation 导致检索中断）；当前口径全表 34s（夜检作业，55 桶×~0.6s），
# 「效率不高」在这个量级无实际代价。语料量级大幅上涨、或恰好因别的原因重建表时再合并进去。
#
# 完整性口径仍限定在「双态 is_active 锚点契约内 + 仅覆盖扫描区间」，不宣称物理全表无条件完整
# （未写 is_active 的行仍会隐身；helper 的 is_active 健康判据即为此兜底）。
HA3_ENUM_ANCHOR = "is_active:'1' OR is_active:'0'"
HA3_ENUM_BUCKET = 500     # 桶宽：必须 ≤ 此值，保证单页装得下
HA3_ENUM_SIZE = 1000      # size：桶宽 2 倍余量；返回数达此值即判饱和（不可信）
_HA3_FETCH_BATCH = 100    # /vector-service/fetch 单批 id 上限


def _ha3_body(resp) -> Any:
    """统一 HA3 响应体三种形态（str / bytes / 带 to_map 的 SDK 对象）。"""
    b = getattr(resp, "body", resp)
    if isinstance(b, (str, bytes)):
        return json.loads(b)
    return b.to_map() if hasattr(b, "to_map") else b


def _ha3_error_code(body: Dict[str, Any]):
    """返回失败用的 errorCode（0/None/空 均视为无错），否则返回原值。"""
    ec = body.get("errorCode")
    if ec in (None, 0, "0", ""):
        return None
    return ec


def ha3_enumerate_bucket(client, table_name: str, lo: int, hi: int,
                         output_fields: List[str] = None) -> Dict[str, Any]:
    """单桶**倒排**枚举（纯 SearchRequest，无 knn/vector，单页、绝不翻页）。

    为什么不翻页：纯倒排下全部 score=0.0、无稳定排序，`from`/`size` 深分页会跨页重复
    ——实测某窗 3 页共返 2504 条但唯一值仅 2169，**漏 335 == 重 335**。故桶宽必须窄到
    单页装得下（≤HA3_ENUM_BUCKET），返回数达 size 即判饱和不可信。

    返回 {"rows": {pk: {...parse_ha3_response 标准字段..., "is_active": "0"|"1"}},
          "healthy": bool, "reason": str|None}。

    **绝不 raise**（SDK 异常转成 healthy=False + reason）；**绝不回退零向量**——
    枚举不可用时由调用方 fail-closed，不是换一个已知有盲区的口径继续。

    健康判据（任一为真 → healthy=False）：桶宽非法 / body errorCode 非零 /
    coveredPercent 缺失或不可解析或 ≠1.0 / 返回数达 size（饱和）/ result 非 list /
    PK 缺失或不可解析或越桶 / 同桶重复 PK / is_active 缺失或不在 {"0","1"} /
    raw 与 parsed 的 PK 集不一致。
    """
    from alibabacloud_ha3engine_vector.models import SearchRequest, TextQuery

    def _bad(reason):
        return {"rows": {}, "healthy": False, "reason": reason}

    if not (0 < hi - lo <= HA3_ENUM_BUCKET):
        return _bad(f"桶宽非法 [{lo},{hi}) 宽={hi - lo}（须 0<宽≤{HA3_ENUM_BUCKET}）")
    fields = list(output_fields or HA3_PARITY_OUTPUT_FIELDS)
    for must in ("id", "is_active"):    # 两者都是健康判据的输入，漏传则自动补
        if must not in fields:
            fields.append(must)
    try:
        resp = client.search(SearchRequest(
            table_name=table_name, size=HA3_ENUM_SIZE, output_fields=fields,
            text=TextQuery(query_string=HA3_ENUM_ANCHOR,
                           query_params={"default_op": "OR"},
                           filter=f"id>={lo} AND id<{hi}")))
        body = _ha3_body(resp)
    except Exception as e:  # noqa: BLE001 — 契约：绝不外抛，转 unhealthy
        return _bad(f"search 异常 {type(e).__name__}: {str(e)[:160]}")

    if not isinstance(body, dict):
        return _bad(f"响应体形态异常: {type(body).__name__}")
    ec = _ha3_error_code(body)
    if ec is not None:
        return _bad(f"errorCode={ec} errorMsg={str(body.get('errorMsg'))[:120]}")
    raw = body.get("result")
    if not isinstance(raw, list):
        return _bad(f"result 非 list: {type(raw).__name__}")
    # ⚠️ 不读 totalCount：实测它被 size 截顶（size=3→3 / 10→10 / 100→34 / 1000→34，
    # 真实命中 34），是本次返回条数而非命中总数，用它计数会得到假结论。
    if len(raw) >= HA3_ENUM_SIZE:
        return _bad(f"桶饱和 returned={len(raw)} >= size={HA3_ENUM_SIZE}（桶太宽，结果不完整）")
    cp = body.get("coveredPercent")
    try:
        if cp is None or float(cp) != 1.0:
            return _bad(f"coveredPercent={cp}（分片未全覆盖，枚举不完整）")
    except (TypeError, ValueError):
        return _bad(f"coveredPercent 不可解析: {cp!r}")

    # is_active 按 **PK 关联**（不按响应位置）——parse_ha3_response 会重塑字段且不透传它。
    raw_by_pk: Dict[int, Any] = {}
    for it in raw:
        f = it.get("fields", it) if isinstance(it, dict) else {}
        try:
            pk = int(it.get("id", f.get("id")))
        except (TypeError, ValueError, AttributeError):
            return _bad(f"PK 缺失或不可解析: {str(it)[:120]}")
        if pk in raw_by_pk:
            return _bad(f"同桶重复 PK={pk}（响应异常，枚举不可信）")
        if not (lo <= pk < hi):
            return _bad(f"PK={pk} 越出请求桶 [{lo},{hi})（filter 未生效）")
        act = f.get("is_active")
        act = act[0] if isinstance(act, list) and act else act
        act = str(act) if act is not None else None
        if act not in ("0", "1"):
            return _bad(f"PK={pk} 的 is_active={act!r} 缺失或非法（锚点契约破裂）")
        raw_by_pk[pk] = act

    rows: Dict[int, Dict[str, Any]] = {}
    for r in parse_ha3_response(resp):
        try:
            pk = int(r.get("id"))
        except (TypeError, ValueError):
            return _bad(f"parsed PK 不可解析: {str(r)[:120]}")
        rows[pk] = dict(r, is_active=raw_by_pk.get(pk))
    if set(rows) != set(raw_by_pk):
        return _bad(f"raw 与 parsed 的 PK 集不一致 raw={len(raw_by_pk)} parsed={len(rows)}")
    return {"rows": rows, "healthy": True, "reason": None}


def ha3_fetch_by_pks(client, table_name: str, pks, output_fields: List[str] = None,
                     batch: int = _HA3_FETCH_BATCH) -> Dict[str, Any]:
    """官方主键 fetch（/vector-service/fetch）——**已知 PK 的权威存在性判定**。

    2026-07-22 终局定性：存在性判定唯 fetch 为准（query 枚举无论什么向量都只是候选源）。
    注意 fetch **只能验证已知 id 是否在场，无法发现未知 id**——orphan/extra 方向必须
    用 ha3_enumerate_bucket。

    返回 {"rows_by_pk": {pk: {...}}, "missing_pks": [...], "unknown_pks": [...],
          "errors": [str]}。批异常/畸形整批归 unknown（**绝不误判为 missing**）。
    绝不 raise。
    """
    from alibabacloud_ha3engine_vector.models import FetchRequest

    fields = list(output_fields or HA3_PARITY_OUTPUT_FIELDS)
    if "id" not in fields:
        fields.append("id")
    ids: List[int] = []
    for p in pks:
        try:
            ids.append(int(p))
        except (TypeError, ValueError):
            continue
    rows_by_pk: Dict[int, Dict[str, Any]] = {}
    unknown: List[int] = []
    errors: List[str] = []
    for i in range(0, len(ids), max(1, batch)):
        sub = ids[i:i + max(1, batch)]
        try:
            body = _ha3_body(client.fetch(FetchRequest(
                table_name=table_name, ids=[str(x) for x in sub],
                include_vector=False, output_fields=fields)))
            if not isinstance(body, dict):
                raise RuntimeError(f"响应体形态异常: {type(body).__name__}")
            ec = _ha3_error_code(body)
            if ec is not None:
                raise RuntimeError(f"errorCode={ec} errorMsg={str(body.get('errorMsg'))[:120]}")
            res = body.get("result")
            if res is None:
                res = []
            if not isinstance(res, list):
                raise RuntimeError(f"result 非 list: {type(res).__name__}")
            want = set(sub)
            seen_batch = set()
            for it in res:
                f = it.get("fields", it) if isinstance(it, dict) else {}
                try:
                    pk = int(it.get("id", f.get("id")))
                except (TypeError, ValueError, AttributeError):
                    raise RuntimeError(f"PK 不可解析: {str(it)[:120]}")
                if pk not in want:
                    raise RuntimeError(f"返回 PK={pk} 不在请求集合内")
                if pk in seen_batch:
                    raise RuntimeError(f"同批重复 PK={pk}")
                seen_batch.add(pk)
                rows_by_pk[pk] = dict(f, id=str(pk))
        except Exception as e:  # noqa: BLE001 — 单批失败只废该批，绝不染成 missing
            unknown.extend(sub)
            errors.append(f"batch@{i}: {type(e).__name__}: {e}"[:200])
    unknown_set = set(unknown)
    missing = [p for p in ids if p not in rows_by_pk and p not in unknown_set]
    return {"rows_by_pk": rows_by_pk, "missing_pks": missing,
            "unknown_pks": sorted(unknown_set), "errors": errors[:10]}


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
