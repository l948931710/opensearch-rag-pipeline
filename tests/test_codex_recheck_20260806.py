# -*- coding: utf-8 -*-
"""2026-08-06 codex 补评审的四条修复（backlog §B/§D 的遗留缺陷）。

来源:`docs/ops/codex_recheck_backlog_2026-08-03.md` 里 15 个从未过外部评审的提交。
codex 给出 1 BLOCKER + 5 MAJOR,我方逐条核验后 **7/7 属实、0 推翻**。本文件钉住其中
不需业务裁决即可修的部分。

⚠️ 这批缺陷的共同形态是「**写了但没接线**」或「**宣称与实现不符**」——
不报错、不告警,只是承诺的那件事根本没发生。因此断言一律钉**行为/接线**,不钉实现细节。
"""
import inspect

import pytest


# ────────────────── ① /api/search 重开时必须带 node-ACL 读身份 ──────────────────

def test_search_端点已彻底删除():
    """原为「/api/search 必须传 acl_ctx」的守卫。**当日晚些时候 Sam 拍板彻底删除该端点**，
    断言随之改为「它不会被悄悄加回来」。

    删除理由：补上 acl_ctx 只堵住了两个洞之一（另一个是敏感 guard 受它自己默认关闭的
    flag 门控），而全仓 console / 小程序 / 钉钉 / eval 四类调用方对它**零消费**。
    与其维护一条随时会再漂移的「已认证但未受治理」的原始检索面，不如删掉。
    详细形状守卫见 tests/test_rag_api.py::TestSearchEndpointRemoved。
    """
    from opensearch_pipeline import api

    assert not hasattr(api, "search"), "/api/search 处理函数被加回来了"
    assert "/api/search" not in {getattr(r, "path", "") for r in api.app.routes}


def test_敏感_guard_受自己的_flag_门控_不受_search_开关影响():
    """codex 指出 eb1162f 的宣称「开回来也过敏感 guard」**只对一半**:
    `sensitive_guard_route` 受 **RAG_SENSITIVE_QUERY_GUARD** 门控,该 flag 默认关时恒 None。
    两个开关要同时开才有 guard。此前的测试是把 `sensitive_guard_route` patch 成命中,
    **没有覆盖两个真实配置开关的组合** ⇒ 该宣称从未被验证过。
    """
    from opensearch_pipeline.intent_router import sensitive_guard_route

    src = inspect.getsource(sensitive_guard_route)
    assert "sensitive_query_guard" in src and "return None" in src, (
        "guard 入口不再自带 flag 门控 —— 端点 docstring 里那段配置说明需同步订正")


def test_search_总闸_flag_也一并清除():
    """端点删了但 flag 留着 = 给「悄悄加回来」铺好路，且 config 里会留一个恒无效的旋钮。"""
    from opensearch_pipeline.config import get_config

    assert not hasattr(get_config().rag, "search_endpoint_enable"), \
        "RAG_SEARCH_ENDPOINT_ENABLE 总闸残留"


# ────────────────── ④ get_conversation 的 200 条静默截断 ──────────────────

def test_会话详情不再静默截断():
    """原实现:SQL 固定 `LIMIT 200` + 无条件 `has_more=False`,docstring 还自称「全部消息」。
    超长会话第 201 条起静默消失。它不在 P3-3 定义的六个管理队列里,故当时漏网。"""
    from opensearch_pipeline import api

    src = inspect.getsource(api.get_conversation)
    assert "LIMIT 201" in src, "没取 N+1 探针行,无从判断是否截断"
    # ⚠️ 两个坑都踩过,别再放宽:
    #   · 不能断言「全文无 has_more=False」——config 关时的提前返回(空列表)用它是合法的;
    #   · 也不能直接 count ——**docstring 里就写着这个串**(讲述被修的旧行为),会多数一次。
    # 故先剥掉 docstring 再数代码体。
    body = src.replace(api.get_conversation.__doc__ or "", "")
    assert body.count("has_more=False") == 1, (
        "主返回路径疑似仍在无条件回 has_more=False(合法的只有 config 关时的空返回一处)")
    assert "has_more=_truncated" in src, "截断标志没接到响应上"
    # ⚠️ 别断言「docstring 不含『全部消息』」——新 docstring 正是**引用旧说法**来讲清楚被修的
    # 是什么(本文件首版实测被自己绊倒)。要钉的是**摘要行**不再作此承诺 + 上限被写明。
    doc = api.get_conversation.__doc__ or ""
    assert "全部" not in doc.splitlines()[0], "docstring 摘要行仍承诺返回全部消息"
    assert "硬上限 200 条" in doc, "docstring 没写明上限,读者仍会以为拿到了全部"


def test_会话详情截断判定的边界(monkeypatch):
    """恰好 200 条 ⇒ 不算截断;201 条 ⇒ 截断且只回 200 条(探针行不外发)。"""
    from opensearch_pipeline import api

    src = inspect.getsource(api.get_conversation)
    assert "_truncated = len(rows) > 200" in src, "截断判定不是「探针行存在」语义"
    assert "rows = rows[:200]" in src, "探针行没被切掉,会多外发一条"


# ────────────────── ②③ 前端两条见 vitest ──────────────────

@pytest.mark.parametrize("path,needle,why", [
    ("console-app/src/composables/useKb.ts", "reviewTasksSeq",
     "loadReviewTasks 缺代际校验 ⇒ 追加在途时换视图会把两种视图混在一起"),
    ("console-app/src/composables/useContribute.ts", "mineSeq",
     "loadMine 缺代际校验 ⇒ 采纳/驳回触发替换后,在途旧页仍会追加到新列表"),
    ("console-app/src/components/manage/DocTable.vue", "truncatedQueues.myAccessRequests",
     "my-access 截断标志后端在回、前端在存,却零消费 ⇒ 已申请的文档错显为「申请授权」"),
])
def test_前端三处接线存在(path, needle, why):
    """跨语言接线守卫:这三处的共同病灶就是「写了但没接线」,Python 侧看不见,
    而 vitest 只在对应 spec 里覆盖 —— 这里做一道廉价的存在性兜底,防整体删除。"""
    import pathlib

    assert needle in pathlib.Path(path).read_text(encoding="utf-8"), why
