# -*- coding: utf-8 -*-
"""
diagram_splits.py — 仅 PDF 构建侧使用的「宽图拆解」覆盖表（不改 architecture.md）。

architecture.md 里 §2 整体架构、§5.1 数据流是两张超宽横向图，缩到页宽后字太小。
这里把它们各拆成若干 TB(竖排) 子图，view_doc.py 构建文档时按 (文件名, 块序号) 命中
覆盖，用这些可读子图替换整张宽图。源文档保持不变。

结构： SPLITS[<md 文件名 stem>][<mermaid 块序号，1-based>] = [(小标题, mermaid 源码), ...]
"""
from __future__ import annotations

# ── §2 整体架构 → 3 张 ──────────────────────────────────────────────
_ARCH_OVERVIEW = '''flowchart TB
    SRC["数据源<br/>OSS raw/ 原始文档"]
    ING["摄取平面 · DataWorks 批<br/>raw → canonical → safe-chunk → 索引"]
    STO["存储层<br/>OSS · RDS MySQL · HA3 向量索引"]
    SRV["服务平面 · SAE 在线<br/>检索 + 重排 + LLM 生成"]
    FE["前端<br/>钉钉机器人 · 钉钉小程序"]
    EXT["外部服务<br/>DashScope/百炼 · 钉钉开放平台"]
    SRC --> ING
    ING ==写入/推送向量==> STO
    STO --> SRV --> FE
    SRV -. 检索(只读) .-> STO
    ING -. 模型调用 .-> EXT
    SRV -. 嵌入/重排/LLM .-> EXT
'''

_ARCH_INGEST = '''flowchart TB
    RAW[("OSS raw/")]
    subgraph DW["DataWorks 每日批 · orchestrator.py(原子抢占/失锁守卫/回滚)"]
        D1["DAG 1 raw_to_canonical<br/>扫描 · 注册 · 抽取+OCR · 规范化"]
        D2["DAG 2 canonical_to_safe_chunk<br/>分类/风险 · PII · 脱敏/隔离 · 分块 · 写 chunk_meta"]
        D3["DAG 3 chunk_to_opensearch<br/>抢锁 · 嵌入 · 推送 · 状态回写 · 停用旧版本"]
        D1 --> D2 --> D3
    end
    FUN["VLM 图片漏斗<br/>启发式 → OCR 密度 → Qwen-VL 审核"]
    OSS[("OSS<br/>canonical/ · rag-ready/")]
    RDS[("RDS<br/>document_meta · chunk_meta")]
    HA3[("HA3 向量索引")]
    RAW --> D1
    D1 -. 图片路由 .-> FUN
    D1 --> OSS
    D2 --> OSS
    D1 --> RDS
    D2 --> RDS
    D3 --> RDS
    D3 ==推送向量==> HA3
'''

_ARCH_SERVE = '''flowchart TB
    Q["用户提问<br/>钉钉 Bot / 小程序 / API"]
    PERM["身份解析 + 权限<br/>userid→dept · 白名单校验(防注入)"]
    ENTRY["API · 钉钉 Bot 入口"]
    RL["rate_limiter<br/>公网防刷四层准入"]
    subgraph CORE["共享服务核心"]
        RET["retriever<br/>三路混合检索 + 拼接 + 扩展"]
        RR["reranker<br/>路由式重排(文本/VL)"]
        GEN["llm_generator<br/>答案生成 + 图文交错"]
        RET --> RR --> GEN
    end
    HA3[("HA3<br/>服务端权限 filter")]
    RDS[("RDS<br/>邻居/step · 问答日志")]
    DS["DashScope<br/>嵌入 / 重排 / LLM"]
    FE["钉钉卡片 / 小程序"]
    Q --> PERM --> ENTRY --> RL --> CORE
    RET ==混合检索==> HA3
    RET -. 邻居拼接±1 / step .-> RDS
    CORE -. 嵌入/重排/LLM .-> DS
    CORE -. 问答日志/反馈 .-> RDS
    GEN --> FE
'''

# ── §5.1 在线问答数据流 → 2 张 ──────────────────────────────────────
_FLOW_RETRIEVE = '''flowchart TB
    U["用户提问"]
    AUTH["身份解析<br/>userid → user_role → dept + 白名单"]
    EMB["Query Embedding<br/>DashScope 原生 API · dense+sparse(只算一次)"]
    MQ{"多意图分解?<br/>(默认 off)"}
    FAN["多路扇出检索<br/>轮转交错合并"]
    HY["HA3 三路混合检索<br/>Dense+Sparse kNN + BM25<br/>weighted 0.7/0.3 · 服务端 dept filter"]
    COVER["封面页降权"]
    NEXT(["接下图：重排 → 后处理 → 生成"])
    U --> AUTH --> EMB --> MQ
    MQ -- 是 --> FAN --> COVER
    MQ -- 否 --> HY --> COVER
    COVER -.-> NEXT
'''

_FLOW_GENERATE = '''flowchart TB
    COVER(["承上图：封面降权后的候选"])
    RRK{"重排开启?<br/>RAG_RERANK_ENABLE"}
    RR["路由式重排 pool=20 → top7<br/>文本 qwen3-rerank / 带图 qwen3-vl-rerank"]
    CAP["文档多样性限额"]
    POST["后处理<br/>邻居拼接±1 → Step Card 扩展 → 图召回(opt-in)"]
    GEN["llm_generator<br/>Qwen 生成 · 高/中/低 置信 · IMG:N 交错"]
    OUT["答案输出<br/>钉钉流式卡片 / SSE / JSON"]
    LOG["answer_flow.build_qa_log_kwargs<br/>→ qa_session_log + 会话历史追加"]
    COVER --> RRK
    RRK -- 是 --> RR --> CAP
    RRK -- 否 --> CAP
    CAP --> POST --> GEN --> OUT --> LOG
'''

SPLITS = {
    "architecture": {
        1: [
            ("§2 系统全景（骨架）", _ARCH_OVERVIEW),
            ("§2 摄取平面详图", _ARCH_INGEST),
            ("§2 服务平面详图", _ARCH_SERVE),
        ],
        3: [
            ("§5.1 在线数据流 · 检索（前半）", _FLOW_RETRIEVE),
            ("§5.1 在线数据流 · 重排到生成（后半）", _FLOW_GENERATE),
        ],
    },
}
