# -*- coding: utf-8 -*-
"""
run_simulation.py — 端到端管线模拟测试

使用模拟文档数据，依次运行 4 条 DAG，验证完整链路：
  raw → 解析 → 脱敏 → 切分 → embedding → 索引 → 检索测试

用法：
  python -m opensearch_pipeline.run_simulation                 # 运行全部 4 条 DAG
  python -m opensearch_pipeline.run_simulation --dag 1         # 只运行 DAG 1
  python -m opensearch_pipeline.run_simulation --dag 1,2       # 运行 DAG 1 和 2
  python -m opensearch_pipeline.run_simulation --sensitive      # 测试含敏感信息的文档
  python -m opensearch_pipeline.run_simulation --graph          # 打印 DAG 结构图
"""

import argparse
import json
import os
import sys
from datetime import datetime

from opensearch_pipeline.dag_definitions import (
    build_dag1_raw_to_canonical,
    build_dag2_canonical_to_chunk,
    build_dag3_chunk_to_opensearch,
    build_dag4_retrieval_eval,
)


# ═══════════════════════════════════════════════════════════════
# 模拟文档数据
# ═══════════════════════════════════════════════════════════════

MOCK_DOCUMENT_NORMAL = """
# 钉钉审批货代发票作业指导书

## 一、目的

本指导书规定了使用钉钉审批中心审核货代发票的标准流程，确保各部门按照统一规范完成发票审核。

## 二、适用范围

适用于行政部、财务部所有需要通过钉钉平台审核货代发票的员工。

## 三、审核流程

### 3.1 登录钉钉

1. 打开钉钉 APP 或桌面端
2. 进入「工作台」→「审批中心」
3. 在待处理列表中找到对应的货代发票审核任务

### 3.2 核对发票信息

审核人需核对以下信息：

| 检查项 | 标准 | 备注 |
| --- | --- | --- |
| 发票号码 | 与系统记录一致 | 不一致需退回 |
| 金额 | 与合同约定一致 | 误差不超过0.01元 |
| 开票日期 | 不超过180天 | 超期需部门经理审批 |
| 税率 | 6%/9%/13% | 需与合同约定一致 |
| 购买方名称 | 重庆富岭实业有限公司 | 必须全称 |

### 3.3 审批操作

1. 确认信息无误后，点击「同意」
2. 如需退回，点击「退回」并填写退回原因
3. 审核完成后，系统自动通知申请人

## 四、注意事项

1. 所有发票必须在收到后5个工作日内完成审核
2. 单笔金额超过50000元需增加财务总监审批
3. 涉及海运费的发票需附上提单复印件
4. 审核过程中如发现异常，应立即通知部门主管

## 五、相关制度

本指导书依据《重庆富岭实业有限公司财务管理制度》（2025年修订版）制定。
如有修订请联系行政部知识管理岗。
"""

MOCK_DOCUMENT_SENSITIVE = """
# 2025年度员工薪资调整方案

## 一、调整对象

本次薪资调整覆盖全公司正式员工，合计326人。

## 二、调整方案

### 2.1 各部门调整比例

| 部门 | 调整幅度 | 涉及人数 |
| --- | --- | --- |
| 生产部 | 8%-12% | 180人 |
| 行政部 | 5%-8% | 45人 |
| 财务部 | 6%-10% | 28人 |
| 研发部 | 10%-15% | 35人 |

### 2.2 关键岗位薪资

以下为调整后的关键岗位月薪（税前）：
- 生产主管：15000-18000元/月
- 质量经理：20000-25000元/月
- 财务总监：35000-40000元/月

## 三、审批流程

1. 人力资源部制作调整表
2. 各部门负责人确认
3. 财务总监审核预算
4. 总经理批准

联系人：李明（手机：13812345678）
身份证号：500382199501150012
邮箱：liming@fuling.com

## 四、预算

2025年度薪资调整预算总额：RMB 5,600,000元
资金来源：公司运营利润
银行卡号：6222 0200 0000 1234 567
"""


MOCK_DOCUMENT_SOP = """
# 注塑机日常点检 SOP

## 一、目的

规范注塑车间设备日常点检流程，确保设备运行安全和产品质量。

## 二、适用范围

适用于注塑车间所有注塑机（型号包括：HTF-160、HTF-300、HTF-500）。

## 三、点检内容

### 3.1 开机前点检

（一）检查液压油
- 油位应在油标中线以上
- 油温应在35-55℃范围内
- 油色应为透明淡黄色，如呈浑浊或深色需更换

（二）检查模具
- 确认模具安装牢固
- 检查冷却水路无泄漏
- 模具表面无损伤或腐蚀

（三）检查安全装置
- 安全门开关正常
- 急停按钮功能正常
- 防护罩完好

### 3.2 运行中巡检

每2小时巡检一次，重点检查：

1. 注塑压力是否稳定（允许波动范围±5%）
2. 料筒温度是否在设定范围内
3. 产品外观有无缺陷（缩水、飞边、变色）
4. 冷却水温度和流量

### 3.3 停机后检查

1. 关闭加热系统
2. 清理料斗残料
3. 模具喷涂防锈剂
4. 填写《设备运行日志》

## 四、异常处理

如发现以下情况，应立即停机并通知设备维修组：
- 液压系统异常响声
- 油温超过65℃
- 注塑压力持续波动超过±10%
- 安全装置失效
"""


MOCK_DOCUMENT_NORMAL_V2 = """
# 钉钉审批货代发票作业指导书（2026年修订版）

## 一、目的

本指导书规定了使用钉钉审批中心审核货代发票的标准流程。
2026年修订版新增了电子发票审核和跨境物流发票处理流程。

## 二、适用范围

适用于行政部、财务部、国际贸易部所有需要通过钉钉平台审核货代发票的员工。

## 三、审核流程

### 3.1 登录钉钉

1. 打开钉钉 APP 或桌面端
2. 进入「工作台」→「审批中心」
3. 在待处理列表中找到对应的货代发票审核任务

### 3.2 核对发票信息

审核人需核对以下信息：

| 检查项 | 标准 | 备注 |
| --- | --- | --- |
| 发票号码 | 与系统记录一致 | 不一致需退回 |
| 金额 | 与合同约定一致 | 误差不超过0.01元 |
| 开票日期 | 不超过180天 | 超期需部门经理审批 |
| 税率 | 6%/9%/13% | 需与合同约定一致 |
| 购买方名称 | 重庆富岭实业有限公司 | 必须全称 |
| 电子发票验真 | 通过税务局验真平台 | 2026年新增要求 |

### 3.3 电子发票审核（2026年新增）

1. 确认电子发票 XML 签名有效
2. 核对发票代码与国家税务总局电子发票平台一致
3. 检查发票状态为正常（非红冲或作废）

### 3.4 审批操作

1. 确认信息无误后，点击「同意」
2. 如需退回，点击「退回」并填写退回原因
3. 审核完成后，系统自动通知申请人

## 四、注意事项

1. 所有发票必须在收到后5个工作日内完成审核
2. 单笔金额超过50000元需增加财务总监审批
3. 跨境物流发票需附上提单和报关单复印件（2026年更新）
4. 审核过程中如发现异常，应立即通知部门主管

## 五、相关制度

本指导书依据《重庆富岭实业有限公司财务管理制度》（2026年修订版）制定。
如有修订请联系行政部知识管理岗。
"""

def get_test_data(scenario: str = "normal") -> dict:
    """构建测试数据。"""
    scenarios = {
        "normal": {
            "raw_tasks": [{
                "doc_id": "DOC_ADMIN_20260518_001",
                "version_no": 1,
                "bucket_name": "fuling-knowledge-base",
                "raw_key": "raw/admin/钉钉审批货代发票作业指导书.docx",
                "filename": "钉钉审批货代发票作业指导书.docx",
                "dept": "admin",
                "file_ext": "docx",
                "mock_text": MOCK_DOCUMENT_NORMAL,
            }],
            "mock_classification": {
                "category_l1": "sop",
                "category_l2": "business_sop",
                "permission_level": "public",
                "kb_type": "public",
                "faq_eligible": True,
                "confidence": 0.92,
                "summary": "钉钉审批货代发票的标准操作流程，包含登录、核对信息和审批操作步骤。",
            },
            "test_queries": [
                "货代发票审核需要检查哪些信息？",
                "发票金额超过多少需要财务总监审批？",
                "审核流程中如何退回发票？",
            ],
        },
        "sensitive": {
            "raw_tasks": [{
                "doc_id": "DOC_HR_20260518_002",
                "version_no": 1,
                "bucket_name": "fuling-knowledge-base",
                "raw_key": "raw/hr/2025年度员工薪资调整方案.docx",
                "filename": "2025年度员工薪资调整方案.docx",
                "dept": "hr",
                "file_ext": "docx",
                "mock_text": MOCK_DOCUMENT_SENSITIVE,
            }],
            "mock_classification": {
                "category_l1": "record",
                "category_l2": "personnel",
                "permission_level": "restricted",
                "kb_type": "restricted",
                "faq_eligible": False,
                "confidence": 0.95,
                "summary": "2025年度薪资调整方案，包含各部门调整比例和关键岗位薪资。",
            },
            "test_queries": [
                "今年薪资调整幅度是多少？",
                "财务总监的月薪是多少？",
            ],
        },
        "multi": {
            "raw_tasks": [
                {
                    "doc_id": "DOC_ADMIN_20260518_001",
                    "version_no": 1,
                    "bucket_name": "fuling-knowledge-base",
                    "raw_key": "raw/admin/钉钉审批货代发票作业指导书.docx",
                    "filename": "钉钉审批货代发票作业指导书.docx",
                    "dept": "admin",
                    "file_ext": "docx",
                    "mock_text": MOCK_DOCUMENT_NORMAL,
                },
                {
                    "doc_id": "DOC_PROD_20260518_003",
                    "version_no": 1,
                    "bucket_name": "fuling-knowledge-base",
                    "raw_key": "raw/production/注塑机日常点检SOP.docx",
                    "filename": "注塑机日常点检SOP.docx",
                    "dept": "production",
                    "file_ext": "docx",
                    "mock_text": MOCK_DOCUMENT_SOP,
                },
            ],
            "mock_classification": {
                "category_l1": "sop",
                "category_l2": "equipment_sop",
                "permission_level": "dept_internal",
                "kb_type": "department",
                "faq_eligible": True,
                "confidence": 0.90,
                "summary": "注塑机日常点检标准操作流程，含开机前/运行中/停机后三阶段。",
            },
            "test_queries": [
                "注塑机开机前需要检查什么？",
                "液压油温度超过多少度要停机？",
                "货代发票审核需要检查哪些信息？",
                "发票金额超过多少需要总监审批？",
            ],
        },
        "embedded_images": {
            "raw_tasks": [
                {
                    "doc_id": "DOC_IMG_DOCX_001",
                    "version_no": 1,
                    "bucket_name": "fuling-knowledge-base",
                    "raw_key": "raw/production/奶茶杯测水试验作业指导书.docx",
                    "filename": "奶茶杯测水试验作业指导书.docx",
                    "dept": "production",
                    "file_ext": "docx",
                    # 无 mock_text → 使用 local_path 真实文件提取
                    "local_path": "fuling_chunk_exp/production_注塑事业部_FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx",
                },
                {
                    "doc_id": "DOC_IMG_PDF_001",
                    "version_no": 1,
                    "bucket_name": "fuling-knowledge-base",
                    "raw_key": "raw/it/电脑安装作业指导书.pdf",
                    "filename": "电脑安装作业指导书.pdf",
                    "dept": "it",
                    "file_ext": "pdf",
                    "local_path": "fuling_chunk_exp/it_FL-CW-XXH-003-《电脑安装》作业指导书.pdf",
                },
            ],
            "mock_classification": {
                "category_l1": "sop",
                "category_l2": "equipment_sop",
                "permission_level": "public",
                "kb_type": "public",
                "faq_eligible": False,
                "confidence": 0.90,
                "summary": "含嵌入图片的作业指导书，用于测试图片提取全链路。",
            },
            "test_queries": [
                "奶茶杯测水试验的操作步骤",
                "电脑安装操作步骤",
            ],
        },
    }

    return scenarios.get(scenario, scenarios["normal"])


def get_version_update_data():
    """
    版本更新场景：同一文档从 v1 -> v2。

    模拟 existing_opensearch_chunks 表示 v1 已在索引中，
    新的 raw_tasks 带 version_no=2 表示用户上传了新版本。
    node_deactivate_old_chunks 应该将 v1 chunks 停用。
    """
    return {
        "raw_tasks": [{
            "doc_id": "DOC_ADMIN_20260518_001",
            "version_no": 2,
            "bucket_name": "fuling-knowledge-base",
            "raw_key": "raw/admin/钉钉审批货代发票作业指导书_v2.docx",
            "filename": "钉钉审批货代发票作业指导书（2026年修订版）.docx",
            "dept": "admin",
            "file_ext": "docx",
            "mock_text": MOCK_DOCUMENT_NORMAL_V2,
        }],
        "mock_classification": {
            "category_l1": "sop",
            "category_l2": "business_sop",
            "permission_level": "public",
            "kb_type": "public",
            "faq_eligible": True,
            "confidence": 0.93,
            "summary": "钉钉审批货代发票的标准操作流程（2026年修订版），新增电子发票审核。",
        },
        "existing_opensearch_chunks": [
            {
                "chunk_id": "DOC_ADMIN_20260518_001_v1_c0000_A1B2C3D4",
                "doc_id": "DOC_ADMIN_20260518_001",
                "version_no": 1,
                "chunk_text": "旧版本：审核人需核对发票号码、金额、开票日期...",
                "index_status": "INDEXED",
            },
            {
                "chunk_id": "DOC_ADMIN_20260518_001_v1_c0001_E5F6G7H8",
                "doc_id": "DOC_ADMIN_20260518_001",
                "version_no": 1,
                "chunk_text": "旧版本：所有发票必须在收到后5个工作日内完成审核...",
                "index_status": "INDEXED",
            },
        ],
        "test_queries": [
            "电子发票审核流程是什么？",
            "货代发票审核需要检查哪些信息？",
            "跨境物流发票需要附什么材料？",
        ],
    }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def run_pipeline(dag_ids: list, scenario: str = "normal", show_graph: bool = False):
    """运行指定 DAG。"""
    if scenario == "version_update":
        ctx = get_version_update_data()
    else:
        ctx = get_test_data(scenario)

    dag_builders = {
        "1": ("DAG 1", build_dag1_raw_to_canonical),
        "2": ("DAG 2", build_dag2_canonical_to_chunk),
        "3": ("DAG 3", build_dag3_chunk_to_opensearch),
        "4": ("DAG 4", build_dag4_retrieval_eval),
    }

    print(f"\n{'#' * 70}")
    print(f"  OpenSearch Pipeline Simulation")
    print(f"  Scenario: {scenario}")
    print(f"  DAGs: {', '.join(dag_ids)}")
    print(f"  Time: {datetime.now().isoformat()}")
    print(f"{'#' * 70}")

    for dag_id in dag_ids:
        name, builder = dag_builders[dag_id]
        dag = builder()

        if show_graph:
            dag.print_dag_graph()
            continue

        ctx = dag.run(ctx)

    if not show_graph:
        # 输出最终摘要
        _print_final_summary(ctx, scenario)


def _print_final_summary(ctx: dict, scenario: str):
    """打印管线最终执行摘要。"""
    print(f"\n{'█' * 70}")
    print(f"  PIPELINE EXECUTION SUMMARY")
    print(f"{'█' * 70}")

    # 文档处理
    canonicals = ctx.get("canonicals", [])
    print(f"\n  📄 Documents Processed: {len(canonicals)}")
    for doc in canonicals:
        risk = doc.get("risk_level", "?")
        action = doc.get("redaction_action", "?")
        category = f"{doc.get('category_l1', '?')}/{doc.get('category_l2', '?')}"
        print(f"     {doc['doc_id']}: risk={risk}, action={action}, category={category}")

    # Chunk 统计
    valid_chunks = ctx.get("valid_chunks", [])
    invalid_chunks = ctx.get("invalid_chunks", [])
    print(f"\n  🧩 Chunks: {len(valid_chunks)} valid, {len(invalid_chunks)} invalid")

    type_counts = {}
    for c in valid_chunks:
        type_counts[c.chunk_type] = type_counts.get(c.chunk_type, 0) + 1
    for ctype, count in type_counts.items():
        print(f"     {ctype}: {count}")

    # 索引状态
    result = ctx.get("index_result", {})
    if result:
        print(f"\n  🔍 Index Result:")
        print(f"     Status: {result.get('status')}")
        print(f"     Indexed: {result.get('indexed')}")
        print(f"     Index: {result.get('index_name')}")

    # 检索评测
    report = ctx.get("eval_report", {})
    if report:
        summary = report.get("summary", {})
        print(f"\n  📊 Eval Report:")
        print(f"     Queries tested: {summary.get('total_queries_tested')}")
        print(f"     Avg top-1 score: {summary.get('avg_top1_score')}")

    # Bulk payload
    payload_size = ctx.get("bulk_payload_size", 0)
    if payload_size:
        print(f"\n  📦 Bulk Payload: {payload_size:,} bytes")
        print(f"     OSS key: {ctx.get('bulk_oss_key', 'N/A')}")

    # 敏感信息
    risk_docs = [d for d in canonicals if d.get("risk_level") in ("medium", "high")]
    if risk_docs:
        print(f"\n  ⚠️ Risk Documents: {len(risk_docs)}")
        for doc in risk_docs:
            hits = doc.get("risk_hits", [])
            hit_summary = ", ".join(set(h["keyword"] for h in hits[:5]))
            print(f"     {doc['doc_id']}: {doc['risk_level']} — {hit_summary}")

    # 版本更新 / 旧 chunk 停用
    deactivated = ctx.get("deactivated_chunks", [])
    if deactivated:
        print(f"\n  🔄 Version Update: {len(deactivated)} old chunks deactivated")
        for d in deactivated[:3]:
            print(f"     {d['chunk_id']}: v{d['old_version']} -> v{d['new_version']}")

    # 发布
    published = ctx.get("published_count", None)
    if published is not None:
        print(f"\n  📤 Published: {published} documents to rag-ready/")

    # chunk_meta 写入
    chunk_meta_written = ctx.get("chunk_meta_written", None)
    if chunk_meta_written is not None:
        print(f"  💾 chunk_meta: {chunk_meta_written} records written")

    print(f"\n{'█' * 70}\n")


def main():
    parser = argparse.ArgumentParser(description="OpenSearch Pipeline Simulation")
    parser.add_argument(
        "--dag", type=str, default="1,2,3,4",
        help="DAG IDs to run, comma-separated (default: 1,2,3,4)",
    )
    parser.add_argument(
        "--scenario", type=str, default="normal",
        choices=["normal", "sensitive", "multi", "version_update", "embedded_images"],
        help="Test scenario (default: normal)",
    )
    parser.add_argument(
        "--graph", action="store_true",
        help="Print DAG structure graph only (don't execute)",
    )

    args = parser.parse_args()
    dag_ids = [d.strip() for d in args.dag.split(",")]

    # 验证 DAG ID
    valid_ids = {"1", "2", "3", "4"}
    for d in dag_ids:
        if d not in valid_ids:
            print(f"Error: invalid DAG ID '{d}'. Valid: {valid_ids}")
            sys.exit(1)

    run_pipeline(dag_ids, scenario=args.scenario, show_graph=args.graph)


if __name__ == "__main__":
    main()
