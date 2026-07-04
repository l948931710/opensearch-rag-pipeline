# -*- coding: utf-8 -*-
"""
test_ha3_client_coupling.py — HA3 解析器/字段清单归属 clients 后的耦合守卫。

背景（2026-07-03 重构）：_parse_ha3_response / _DEFAULT_OUTPUT_FIELDS 原是 retriever
的私有实现，stage-3 parity 守卫（gate 不可逆的旧版本 deactivate）、ha3_reconcile、
reconcile 三个批处理路径反向 import 它们——serving 侧一次答案路径调优（删重字段、改
score 归一化）会静默改变 deactivate 安全检查的"存在/漂移"判定口径。现实现上移至
clients.py（parse_ha3_response / HA3_DEFAULT_OUTPUT_FIELDS），retriever 以旧名
re-export 同一对象。本文件把这些不变量钉死：将来任何人在 retriever 里重新定义
_parse_ha3_response（再分叉），或让批处理字段集悄悄跟着 serving 清单走，都会在这里红。
"""

from opensearch_pipeline import clients, retriever


def test_retriever_parse_reexport_is_clients_object():
    """retriever._parse_ha3_response 必须是 clients.parse_ha3_response 本体（同一对象）。

    恒等断言（is）而非行为断言：只要有人在 retriever 里重新 def 一份实现，
    即使行为暂时一致也立刻失败——防的是"再分叉"本身。
    """
    assert retriever._parse_ha3_response is clients.parse_ha3_response


def test_retriever_default_fields_reexport_is_clients_object():
    """retriever._DEFAULT_OUTPUT_FIELDS 必须是 clients.HA3_DEFAULT_OUTPUT_FIELDS 本体。"""
    assert retriever._DEFAULT_OUTPUT_FIELDS is clients.HA3_DEFAULT_OUTPUT_FIELDS


def test_parity_fields_pinned_and_independent():
    """批处理 parity/对账字段集：包含判定所需列，且与 serving 默认清单是独立对象。

    - 必含列 = 各安全检查真正消费的：id（PK 相符）、chunk_id/doc_id（orphan 分类 +
      enum hint）、chunk_text_store（parity drift）、version_no/chunk_type（CS3 报表）。
    - `is not` 断言：两份清单绝不共享 list 对象——serving 侧原地增删字段
      （append/remove）不得波及 deactivate gate / orphan 删除的查询投影。
    """
    required = {"id", "chunk_id", "doc_id", "version_no", "chunk_text_store", "chunk_type"}
    assert required <= set(clients.HA3_PARITY_OUTPUT_FIELDS)
    assert clients.HA3_PARITY_OUTPUT_FIELDS is not clients.HA3_DEFAULT_OUTPUT_FIELDS
    # 当前 parity 集是 serving 集的子集（引擎端字段真名一致）；若将来 serving 清单改名/
    # 删列导致不再成立，这条会红——届时需要人工确认 parity 列在 HA3 schema 中仍然存在。
    assert set(clients.HA3_PARITY_OUTPUT_FIELDS) <= set(clients.HA3_DEFAULT_OUTPUT_FIELDS)
