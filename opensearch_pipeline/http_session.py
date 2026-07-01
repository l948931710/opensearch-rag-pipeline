# -*- coding: utf-8 -*-
"""
http_session.py — DashScope 出站 HTTP 的共享连接池（性能第一梯队 #2，2026-07-01）

此前 embedding_client / llm_generator / reranker 每次调用都走裸 `requests.post`
——每问答 2-3 次全新 TCP+TLS 握手（各 ~3 RTT）；摄取全量重灌按千次计。三个模块
的目标同为 dashscope.aliyuncs.com，故共享一个 Session/连接池收益最大。

用法：调用方 `from opensearch_pipeline.http_session import http_post as _http_post`
后以 `_http_post(url, json=..., timeout=...)` 替代 `requests.post(...)`——签名、
返回值、异常语义与 requests.post 完全一致（含 stream=True 上下文管理器用法）。
tests 以 `<module>._http_post` 为 patch 接缝（不要再 patch `<module>.requests.post`，
Session.post 不经过它）。

线程安全：requests.Session + urllib3 池对并发请求是线程安全的（cookie jar 内部
加锁）；懒初始化用双检锁避免重复建池。
"""

import threading

import requests

_SESSION = None
_LOCK = threading.Lock()

# serving 线程池（默认 40，可调至 ~120-200）+ 摄取 RAG_VLM_CONCURRENCY=8 的并发上限；
# 超出 pool_maxsize 时 urllib3 现建临时连接（block=False 默认），只降复用率不阻塞。
_POOL_MAXSIZE = 64


def get_session() -> requests.Session:
    """进程级共享 Session（懒初始化，双检锁）。"""
    global _SESSION
    if _SESSION is None:
        with _LOCK:
            if _SESSION is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=4, pool_maxsize=_POOL_MAXSIZE)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


def http_post(url, **kwargs):
    """requests.post 的连接复用替身——参数与异常语义一致，仅传输层复用连接。"""
    return get_session().post(url, **kwargs)
