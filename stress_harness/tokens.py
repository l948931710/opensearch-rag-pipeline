# -*- coding: utf-8 -*-
"""tokens — 合成用户会话令牌铸造（与被测服务共享 RAG_SESSION_SIGNING_KEY）。

不走 /api/auth/dingtalk（需要真机一次性 authcode）；直接 in-process 调
auth_token.issue_session_token——服务端与压测端共享同一签名密钥即互认。
每用户独立 uid（限流按 uid 记账，S7 之外限流关闭但 uid 仍用于 DB 断言归因）。
"""
from __future__ import annotations

import os
from typing import List

SIGNING_KEY = "stress-local-key"
DEFAULT_GROUPS = ["production"]


def mint(n: int, groups: List[str] | None = None, prefix: str = "su") -> List[str]:
    """铸 n 个合成用户的 Bearer token（su0001..suNNNN）。"""
    os.environ.setdefault("RAG_SESSION_SIGNING_KEY", SIGNING_KEY)
    from opensearch_pipeline.auth_token import issue_session_token
    return [issue_session_token(f"{prefix}{i:04d}", dept=list(groups or DEFAULT_GROUPS))
            for i in range(1, n + 1)]


def mint_one(user_id: str, groups: List[str] | None = None) -> str:
    os.environ.setdefault("RAG_SESSION_SIGNING_KEY", SIGNING_KEY)
    from opensearch_pipeline.auth_token import issue_session_token
    return issue_session_token(user_id, dept=list(groups or DEFAULT_GROUPS))
