# -*- coding: utf-8 -*-
"""
diagram_splits.py — 仅 PDF 构建侧使用的「宽图拆解」覆盖表（不改 architecture.md）。

architecture.md 里 §2 整体架构、§5.1 数据流是两张超宽横向图，缩到页宽后字太小。
这里把它们各拆成若干 **LR(横向)** 子图——横向铺开后是「宽而矮」的形状，正好填满
A4 页宽、且不会因为太高而被分页守卫挤到下一页留下整页空白（竖排 TB 会偏瘦高、易孤页）。

结构： SPLITS[<md 文件名 stem>][<mermaid 块序号，1-based>] = [(小标题, mermaid 源码), ...]
"""
from __future__ import annotations

# ── §2 整体架构 → 3 张（横向，矮）──────────────────────────────────
_ARCH_OVERVIEW = '''flowchart LR
    SRC["数据源<br/>OSS raw/ 原始文档"]
    ING["摄取平面<br/>DataWorks · 4 DAG<br/>raw→canonical→chunk→索引"]
    SRV["服务平面<br/>SAE 在线<br/>检索 + 重排 + LLM 生成"]
    FE["前端<br/>钉钉机器人<br/>钉钉小程序"]
    STO[("存储层<br/>OSS · RDS MySQL · HA3 向量索引")]
    EXT["外部服务<br/>DashScope/百炼<br/>钉钉开放平台"]
    SRC --> ING --> SRV --> FE
    ING ==写入/推送==> STO
    STO ==检索(只读)==> SRV
    ING -. 模型调用 .-> EXT
    SRV -. 嵌入/重排/LLM .-> EXT
'''

_ARCH_INGEST = '''flowchart LR
    RAW[("OSS raw/")]
    subgraph DW["DataWorks 批 · orchestrator(抢占/失锁守卫/回滚)"]
        direction LR
        D1["DAG1<br/>解析+OCR"] --> D2["DAG2<br/>分类·PII·分块"] --> D3["DAG3<br/>嵌入·推送·版本切换"]
    end
    FUN["VLM 图片漏斗<br/>启发式→OCR→Qwen-VL"]
    OSS[("OSS<br/>canonical·rag-ready")]
    RDS[("RDS<br/>chunk_meta")]
    HA3[("HA3 向量索引")]
    RAW --> D1
    D1 -. 图片 .-> FUN
    D1 --> OSS
    D2 --> OSS
    D1 --> RDS
    D2 --> RDS
    D3 --> RDS
    D3 ==推送向量==> HA3
'''

_ARCH_SERVE = '''flowchart LR
    Q["用户提问<br/>Bot / 小程序 / API"]
    PERM["身份+权限<br/>dept · 白名单"]
    ENTRY["API · 钉钉 Bot"]
    RL["rate_limiter<br/>公网防刷四层"]
    subgraph CORE["共享服务核心"]
        direction LR
        RET["retriever<br/>三路混合检索"] --> RR["reranker<br/>路由重排"] --> GEN["llm_generator<br/>生成+图文"]
    end
    HA3[("HA3<br/>权限 filter")]
    RDS[("RDS<br/>邻居 · 日志")]
    DS["DashScope<br/>嵌入/重排/LLM"]
    FE["钉钉卡片 / 小程序"]
    Q --> PERM --> ENTRY --> RL --> CORE
    RET ==检索==> HA3
    RET -. 邻居/step .-> RDS
    CORE -. 模型 .-> DS
    CORE -. 日志/反馈 .-> RDS
    GEN --> FE
'''

# ── §5.1 在线问答数据流 → 2 张（横向，矮）──────────────────────────
_FLOW_RETRIEVE = '''flowchart LR
    U["用户提问"]
    AUTH["身份解析<br/>userid→dept + 白名单"]
    EMB["Query Embedding<br/>dense+sparse(算一次)"]
    MQ{"多意图<br/>分解?"}
    FAN["多路扇出<br/>轮转交错"]
    HY["HA3 三路混合<br/>Dense+Sparse+BM25<br/>weighted 0.7/0.3 · dept filter"]
    COVER["封面页降权"]
    NEXT(["接下图：重排→生成"])
    U --> AUTH --> EMB --> MQ
    MQ -- 是 --> FAN --> COVER
    MQ -- 否 --> HY --> COVER
    COVER -.-> NEXT
'''

_FLOW_GENERATE = '''flowchart LR
    COVER(["承上图<br/>封面降权后候选"])
    RRK{"重排开启?<br/>RAG_RERANK_ENABLE"}
    RR["路由式重排<br/>pool 20 → top 7<br/>文本 qwen3-rerank<br/>带图 qwen3-vl-rerank"]
    POST["多样性限额 + 后处理<br/>邻居拼接 ±1<br/>Step Card 扩展<br/>图召回(opt-in)"]
    GEN["生成 + 输出 + 落库<br/>高/中/低 · IMG:N 交错<br/>流式 / SSE / JSON<br/>qa_session_log + 历史"]
    COVER --> RRK
    RRK -- 是 --> RR --> POST
    RRK -- 否 --> POST
    POST --> GEN
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
