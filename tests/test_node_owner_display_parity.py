# -*- coding: utf-8 -*-
"""台账/审批展示面的 **node 口径** 守卫（2026-08-05 口径对账产出）。

这一族缺陷长得都一样、也都不会报错:node 文档的 `document_meta.owner_dept` 按契约**恒为
空串**(归属在 `owner_dept_id` 上),于是任何"直接渲染 owner_dept"或"以 owner_dept 判空"
的展示面,对 node 文档就会**静默退化**成空白或整段消失。生产实测:270 篇「指定部门」
上传的共享对象完全不可见、799 篇 node 文档的审批历史没有归属。

因此本文件钉的是**行为**(node 输入 → 有意义的输出),不是实现细节。
"""
import pytest

from opensearch_pipeline.routes import kb_console


class _Cur:
    """按 SQL 关键字派发的最小游标(只覆盖被测两个 helper 的查询)。"""

    def __init__(self, grants=(), depts=()):
        self._grants, self._depts, self._rows = grants, depts, []

    def execute(self, sql, params=None):
        if "kb_doc_node_grant" in sql:
            self._rows = list(self._grants)
        elif "dept_dim" in sql:
            self._rows = list(self._depts)
        else:                                     # pragma: no cover - 防守
            self._rows = []

    def fetchall(self):
        return self._rows


# ---------------------------------------------------------------- 共享节点批量解析

def test_shared_labels_排除归属节点自身():
    """摄取默认给归属节点写一条 subtree 授权(pipeline_nodes 步骤 1b)——那是「本部门可见」
    的实现方式,不是共享。不排除的话每篇文档都会凭空多出一个自己的名字。"""
    cur = _Cur(grants=[("D1", 10, "人力资源部", 1), ("D1", 20, "生产中心", 1)])
    out = kb_console._kb_shared_node_labels(cur, [("D1", 10)])
    assert out == {"D1": ["生产中心"]}


def test_shared_labels_标注失活节点():
    """授权还在、节点没了 = 正是需要解释的状态。INNER JOIN 会把它悄悄隐藏。"""
    cur = _Cur(grants=[("D1", 10, "人力资源部", 1), ("D1", 99, "已撤部门", 0)])
    out = kb_console._kb_shared_node_labels(cur, [("D1", 10)])
    assert out == {"D1": ["已撤部门（已失效）"]}


def test_shared_labels_节点缺行回_id_串():
    """LEFT JOIN 未命中(name/is_active 皆 None):回 id 串并标失效,不是抛异常也不是丢行。"""
    cur = _Cur(grants=[("D1", 77, None, None)])
    out = kb_console._kb_shared_node_labels(cur, [("D1", 10)])
    assert out == {"D1": ["77（已失效）"]}


def test_shared_labels_空输入不查库():
    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("空输入不应发起查询")

    assert kb_console._kb_shared_node_labels(_Boom(), []) == {}
    assert kb_console._kb_shared_node_labels(_Boom(), [("D1", None)]) == {}


def test_shared_labels_查询失败_fail_open():
    """展示 enrichment:失败必须退化成"不显示共享",绝不能把台账主查询带崩。"""
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("kb_doc_node_grant 不存在")

    assert kb_console._kb_shared_node_labels(_Boom(), [("D1", 10)]) == {}


def test_shared_labels_只按活跃授权():
    """SQL 必须带 revoked_at IS NULL —— 撤销过的共享不该继续挂在台账副行上。"""
    seen = {}

    class _C(_Cur):
        def execute(self, sql, params=None):
            seen["sql"] = sql
            super().execute(sql, params)

    kb_console._kb_shared_node_labels(_C(grants=[]), [("D1", 10)])
    assert "revoked_at IS NULL" in seen["sql"]


# ---------------------------------------------------------------- 展示面接线守卫

def test_my_docs_回填_shared_labels():
    """接线守卫:helper 正确 ≠ 列表真的带上了它(两者曾各自正确、中间没接起来)。"""
    import inspect

    src = inspect.getsource(kb_console.kb_my_docs)
    assert "_kb_shared_node_labels(" in src, "my-docs 没有调用共享节点解析"
    assert "shared_labels=" in src, "解析结果没有回填进 KbDocItem"


def test_browse_不外发共享拓扑():
    """browse 是「看得见但不能管」的他部门文档 —— 授权拓扑不外扩到管辖范围之外。
    这条是**有意的不对称**,不是漏改;若将来要放宽,请连同本断言一起改并说明理由。"""
    import inspect

    assert "_kb_shared_node_labels(" not in inspect.getsource(kb_console.kb_browse)


@pytest.mark.parametrize("field", ["owner_key", "owner_label"])
def test_审批历史_DTO_带归属键(field):
    from opensearch_pipeline.routes.kb_access import KbApprovalHistoryItem

    assert field in KbApprovalHistoryItem.model_fields, (
        f"审批历史缺 {field}:node 文档的 owner_dept 恒空串,"
        "前端 `if (r.owner_dept)` 会让整段「归属」消失")


def test_审批历史_access_与_upload_两段都解析节点名():
    """contribution(贡献分类组码)/admin_grant(无文档作用域)有意不填;
    另两段是文档归属轴,**必须**填,否则 799 篇 node 文档的审批历史没有归属。"""
    import inspect

    from opensearch_pipeline.routes import kb_access

    src = inspect.getsource(kb_access.kb_approval_history)
    assert src.count("pending_nodes.append(") == 2, "access/upload 应各记一次待解析节点"
    assert "_kb_node_names(" in src, "没有批量解析节点现名"
    assert src.count('kind="contribution"') == 1 and "category_dept 是**贡献分类**" in (
        inspect.getsource(kb_access.KbApprovalHistoryItem)), "有意不填的理由必须留在 DTO 注释里"
