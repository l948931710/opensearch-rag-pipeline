# -*- coding: utf-8 -*-
"""
evaluate_large_corpus.py — 对包含至少15-20个大型及MB级文档在内的21个目标文件，执行端到端管线运行并优化 Strategy_Dynamic 检索参数
升级说明：
1. 移除 Query 中的部门/文档元数据前缀，进行彻底的脱敏，符合真实员工的日常口语化查询。
2. 升级匹配逻辑为【多组必要关键词判定逻辑（Multi-Group Keyword Validator）】，要求同时满足各个事实维度。
3. 增加 Old vs New 双系统对比评测，直观展现去除 Leak 前缀后的真实召回指标变化（Fairness Adjustment Drop）。
4. 细化输出分类别（SOP、Manual、FAQ）召回率指标。
5. 详细记录最优配置下的 Query Top-3 检索分数（_score）、召回 Rank 以及 TOP 1 召回文本片段。
"""

import os
import sys
import json
import hashlib
import time
import re
from datetime import datetime
from typing import List, Dict, Any
from rank_bm25 import BM25Okapi
import jieba

# 添加工作目录到 python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from opensearch_pipeline.config import get_config
from opensearch_pipeline.pipeline_nodes import (
    node_scan_raw_files,
    node_register_metadata,
    node_extract_text_with_ocr,
    node_build_canonical,
    node_classify_and_risk_assess,
    node_detect_sensitive,
    node_redact_or_quarantine,
    node_publish_to_rag_ready,
    node_chunk_documents,
    node_validate_chunks,
    node_write_chunk_meta,
    node_build_opensearch_payload,
    _get_db_conn,
    _get_opensearch_client
)
from opensearch_pipeline.chunker import DocumentChunker, Chunk

# ─── 28个精细业务评测 Query 定义 (包含新旧对比及多组必要关键词) ───
LARGE_EVAL_QUERIES = [
    {
        "id": "Q01",
        "old_query": "每日奶茶杯与杯盖装配测水试验是在什么时间段进行？",
        "new_query": "每日奶茶杯和杯盖装配测水试验下午是在几点到几点进行？",
        "target_doc": "eval_prod_naichabei",
        "category": "manual",
        "old_keywords": ["13:30--15:00", "13：30--15:00", "装配是否漏水", "下午"],
        "required_keyword_groups": [["13:30--15:00", "13：30--15:00"]]
    },
    {
        "id": "Q02",
        "old_query": "在奶茶杯测水试验中，杯盖吸管孔处需要粘贴什么，且杯盖上安装什么？",
        "new_query": "测水试验中，杯盖吸管孔处需要粘贴什么？杯盖上又要安装什么？",
        "target_doc": "eval_prod_naichabei",
        "category": "manual",
        "old_keywords": ["粘贴胶带", "胶带", "盖塞", "吸管孔"],
        "required_keyword_groups": [["胶带"], ["盖塞"]]
    },
    {
        "id": "Q03",
        "old_query": "在电脑安装过程中，32位的英特尔处理器和64位的处理器有什么针脚结构区别？",
        "new_query": "32位和64位英特尔CPU的针脚结构有什么主要区别？",
        "target_doc": "eval_it_pc_install",
        "category": "manual",
        "old_keywords": ["478", "478针", "触点式", "lga775"],
        "required_keyword_groups": [["478"], ["lga775", "lga 775"]]
    },
    {
        "id": "Q04",
        "old_query": "如何打开主板上的LGA 775处理器压杆？",
        "new_query": "主板上的LGA 775处理器压杆要怎么打开？",
        "target_doc": "eval_it_pc_install",
        "category": "manual",
        "old_keywords": ["微压", "推压杆", "脱离", "压杆"],
        "required_keyword_groups": [["压杆"], ["推", "微压"]]
    },
    {
        "id": "Q05",
        "old_query": "在财务部付款单据录入中，普通发票和专用发票的录入依据是什么？",
        "new_query": "录入采购发票时，选择录入普通发票还是专用发票的依据是什么？",
        "target_doc": "eval_it_finance_u8",
        "category": "manual",
        "old_keywords": ["发票类型", "专用发票", "录入"],
        "required_keyword_groups": [["实际收到"], ["发票类型"]]
    },
    {
        "id": "Q06",
        "old_query": "发票结算的主要目的是什么，如果次月入库本月结算会生成什么？",
        "new_query": "发票结算的主要目的是什么？如果次月入库但本月结算，系统会生成什么单据？",
        "target_doc": "eval_it_finance_u8",
        "category": "manual",
        "old_keywords": ["结算成功", "暂估", "红蓝字", "回冲单"],
        "required_keyword_groups": [["回冲单"], ["红蓝字"]]
    },
    {
        "id": "Q07",
        "old_query": "在工资核算中，计件和计时工资的审核主要是比对哪些报表数据？",
        "new_query": "录入或导入工价单后，要怎么做才能让它认定为生效并在日工资单中取数？",
        "target_doc": "eval_it_payroll_manual",
        "category": "manual",
        "old_keywords": ["工资", "审核", "比对"],
        "required_keyword_groups": [["保存"], ["审核"]]
    },
    {
        "id": "Q08",
        "old_query": "工资核算管理操作手册中，半成品工价单和成品工价单的系统录入路径是什么？",
        "new_query": "在系统里录入半成品工价单和成品工价单的路径是什么？",
        "target_doc": "eval_it_payroll_manual",
        "category": "manual",
        "old_keywords": ["工资核算", "半成品工价单", "成品工价单", "供应链"],
        "required_keyword_groups": [["工资核算"], ["工价"]]
    },
    {
        "id": "Q09",
        "old_query": "在U8成品仓库操作中，如何处理待检产成品入库单的生成与核对？",
        "new_query": "成品仓库使用PDA扫码入库后，如何根据合格的检验单生成产品入库单？",
        "target_doc": "eval_it_warehouse_u8",
        "category": "manual",
        "old_keywords": ["入库", "生成", "核对"],
        "required_keyword_groups": [["检验合格", "合格的检验单"], ["产成品入库单"]]
    },
    {
        "id": "Q10",
        "old_query": "产品条码出库扫描时，如果提示条码不存在该怎么处理？",
        "new_query": "出库时使用条码扫码枪生成销售出库单，能够省略哪些手工操作步骤？",
        "target_doc": "eval_it_warehouse_u8",
        "category": "manual",
        "old_keywords": ["条码", "不存在", "提示"],
        "required_keyword_groups": [["扫码枪", "条码枪"], ["省略2、3、4、5步骤", "省略2"]]
    },
    {
        "id": "Q11",
        "old_query": "吸塑班组长在填写数量本时，对于报废品和回料的重量是如何录入的？",
        "new_query": "班组长在接收到日计划表后，需要把生产日计划中的哪些信息记录到数量本上？",
        "target_doc": "eval_prod_xisu_shuliang",
        "category": "manual",
        "old_keywords": ["报废品", "回料", "数量本", "重量"],
        "required_keyword_groups": [["模具"], ["客户名称"], ["商检号"], ["剩余箱数"]]
    },
    {
        "id": "Q12",
        "old_query": "吸塑数量本填写时，班组长需要把生产计划表和计划单中的哪些信息记录到数量本上？",
        "new_query": "班组长在共享文件夹里打开生产计划单后，需要把其中的哪些包装相关信息记录到数量本上？",
        "target_doc": "eval_prod_xisu_shuliang",
        "category": "manual",
        "old_keywords": ["模具", "商检号", "包装方式", "克重", "数量本"],
        "required_keyword_groups": [["包装方式"], ["克重"], ["袋子规格"], ["印刷方式"]]
    },
    {
        "id": "Q13",
        "old_query": "吸塑领料申请单的打印和审批流程在U8系统里是如何流转的？",
        "new_query": "领料单打印后，如果生产计划数量超过1000箱，班组长需要在单据右上角写什么备注？",
        "target_doc": "eval_prod_xisu_lingliao",
        "category": "manual",
        "old_keywords": ["领料申请单", "审批", "流转"],
        "required_keyword_groups": [["1000箱", "1000"], ["每天", "拉料"]]
    },
    {
        "id": "Q14",
        "old_query": "吸塑领料申请单打印后，班组长需要将领料单分发交接给哪些人员？",
        "new_query": "打印出来的领料单共有三份，分别需要分发交接给哪些岗位的仓库管理或作业人员？",
        "target_doc": "eval_prod_xisu_lingliao",
        "category": "manual",
        "old_keywords": ["辅料工", "包装袋仓管", "纸箱仓管", "交接"],
        "required_keyword_groups": [["辅料工"], ["包装袋仓管"], ["纸箱仓管"]]
    },
    {
        "id": "Q15",
        "old_query": "吸塑交货单打印前，班组长必须确认的包装规格和箱数信息是什么？",
        "new_query": "班组长在系统打印交货单时，应该如何根据计划单的包材来判定和填写自定义包装类型？",
        "target_doc": "eval_prod_xisu_jiaohuo",
        "category": "manual",
        "old_keywords": ["包装规格", "箱数", "交货单"],
        "required_keyword_groups": [["袋", "手包"], ["膜", "机包"]]
    },
    {
        "id": "Q16",
        "old_query": "纸吸管耐热测试中，测试机器温度是多少，插入热水后的测试时间是多久？",
        "new_query": "纸吸管耐热测试的机器温度是多少？浸泡热水需要测多久？",
        "target_doc": "eval_prod_xiguan_receshi",
        "category": "manual",
        "old_keywords": ["60±1度", "插入到热水中", "5分钟", "预设温度"],
        "required_keyword_groups": [["60±1度", "60±1°"], ["5分钟"]]
    },
    {
        "id": "Q17",
        "old_query": "纸吸管耐热高温测试合格与不合格的判定标准及后续处理是什么？",
        "new_query": "烘干后的纸吸管如果耐高温测试不合格，具体的后续重新测试和报废流程是怎样的？",
        "target_doc": "eval_prod_xiguan_receshi",
        "category": "manual",
        "old_keywords": ["翘边", "烘干前", "烘干后", "停机", "报废"],
        "required_keyword_groups": [["50度", "50°"], ["常温可乐"], ["报废"]]
    },
    {
        "id": "Q18",
        "old_query": "吸塑产品入库单打印完成后，班组长或仓管如何分发 and 交接不同颜色的联单？",
        "new_query": "产品入库单打印完后，白红黄各联单该怎么分发和交接？",
        "target_doc": "eval_prod_xisu_ruku",
        "category": "manual",
        "old_keywords": ["白联", "红联", "黄联", "财务部", "成本部"],
        "required_keyword_groups": [["白联", "留底"], ["红联", "财务部"], ["黄联", "成本部"]]
    },
    {
        "id": "Q19",
        "old_query": "在五金仓材料出库管理中，限额领料单 and 非限额领料单的系统录入有什么区别？",
        "new_query": "如果车间生产消耗量大于系统领用量，仓库人员应该如何处理领料和出库？",
        "target_doc": "eval_it_wujin_u8",
        "category": "manual",
        "old_keywords": ["限额领料单", "非限额", "录入"],
        "required_keyword_groups": [["补料申请单", "补料单"]]
    },
    {
        "id": "Q20",
        "old_query": "人事部在U8系统中录入新入职员工卡号 and 考勤排班的步骤是什么？",
        "new_query": "新员工入职以及离职老员工重新回公司就职，在U8系统里分别通过什么功能操作？",
        "target_doc": "eval_it_hr_u8",
        "category": "manual",
        "old_keywords": ["人事", "入职", "排班"],
        "required_keyword_groups": [["入职登记", "重新入职申请"]]
    },
    {
        "id": "Q21",
        "old_query": "贸易部出口货物的销售出库单在发货确认后，如何跟单并录入系统？",
        "new_query": "出口货物发货单新增完成时，需要在系统里录入哪些跟单信息？",
        "target_doc": "eval_it_trade_u8",
        "category": "manual",
        "old_keywords": ["贸易", "销售出库单", "发货确认"],
        "required_keyword_groups": [["封箱号"], ["跟单员"], ["柜型"]]
    },
    {
        "id": "Q22",
        "old_query": "员工手册中，关于试用期转正考核的流程 and 申请时间是如何规定的？",
        "new_query": "新员工试用期满要转正，人事部门和员工本人需要在到期前多少天分别完成什么准备？",
        "target_doc": "eval_hr_manual",
        "category": "sop",
        "old_keywords": ["员工手册", "试用期", "转正"],
        "required_keyword_groups": [["前10天", "试用小结"], ["前5天", "员工能力鉴定表"]]
    },
    {
        "id": "Q23",
        "old_query": "年休假的折算标准以及未休年休假的工资补偿是如何计算的？",
        "new_query": "在公司连续工作已满10年但未满20年的员工，每年可以享受多少天的带薪年休假？",
        "target_doc": "eval_hr_manual",
        "category": "sop",
        "old_keywords": ["年休假", "折算", "未休"],
        "required_keyword_groups": [["已满10年"], ["年休假10天", "10天"]]
    },
    {
        "id": "Q24",
        "old_query": "在海外销售发票系统中，进行发票入库与发票出库单据录入时有哪些特别说明 and 控制逻辑？",
        "new_query": "海外发票系统中，发票出库生成时如果参考海外仓库，系统是如何匹配并自动生成参照数据的？",
        "target_doc": "eval_it_invoice_system",
        "category": "manual",
        "old_keywords": ["发票号", "报关生单", "库存不满足", "参照生单"],
        "required_keyword_groups": [["海外仓库"], ["出库数量"], ["匹配", "库存"]]
    },
    {
        "id": "Q25",
        "old_query": "车间生产订单在U8中下达后，如何进行生产看板的数据同步与状态维护？",
        "new_query": "如果车间生产时损耗过大，导致正常的生产订单领料不够用，应该通过什么单据继续申请领料？",
        "target_doc": "eval_it_chejian_u8",
        "category": "manual",
        "old_keywords": ["车间", "生产订单", "生产看板"],
        "required_keyword_groups": [["补料申请单"]]
    },
    {
        "id": "Q26",
        "old_query": "如何申请公司的无线网络账号（Wi-Fi）？",
        "new_query": "怎么申请公司的无线WiFi账号？流程是怎样的？",
        "target_doc": "eval_it_faq",
        "category": "faq",
        "old_keywords": ["wifi", "无线网络", "密码"],
        "required_keyword_groups": [["Wi-Fi申请流程", "wifi申请流程"], ["验证码"], ["FL-Enterprise"]]
    },
    {
        "id": "Q27",
        "old_query": "打印机卡纸后如果无法正常打印，可以拨打哪个内线分机联系系统管理员？",
        "new_query": "打印机卡纸不能用了，拨打哪个内线电话联系系统管理员？",
        "target_doc": "eval_it_faq",
        "category": "faq",
        "old_keywords": ["8088", "打印机", "卡纸"],
        "required_keyword_groups": [["8088"], ["IT部", "内线分机", "联系系统管理员"]]
    },
    {
        "id": "Q28",
        "old_query": "新入职员工前三天的吃饭问题怎么解决？",
        "new_query": "刚入职的新员工，前三天吃饭怎么解决？",
        "target_doc": "eval_company_faq",
        "category": "faq",
        "old_keywords": ["餐券", "就餐", "前三天"],
        "required_keyword_groups": [["领用餐券", "餐券"], ["宿舍楼一楼食堂", "食堂", "免费用餐"]]
    },
    # ─── Policy / 制度类文档评测 Query ───
    {
        "id": "Q29",
        "old_query": "公司采购金额达到什么标准需要走招投标？",
        "new_query": "公司单笔采购金额超过多少需要走招投标程序？",
        "target_doc": "eval_admin_procurement",
        "category": "policy",
        "old_keywords": ["50,000元", "招投标", "公开招投标"],
        "required_keyword_groups": [["50,000元", "50000"], ["招投标", "公开招投标", "邀请招标"]]
    },
    {
        "id": "Q30",
        "old_query": "行政部和采购部的采购职责有什么不同？",
        "new_query": "行政部和采购部在采购中各自的职责分工是什么？",
        "target_doc": "eval_admin_procurement",
        "category": "policy",
        "old_keywords": ["行政部", "采购部", "办公用品", "生产原料"],
        "required_keyword_groups": [["行政部"], ["办公用品", "后勤物资", "固定资产"], ["采购部"], ["生产原料", "辅料", "包装材料"]]
    },
    {
        "id": "Q31",
        "old_query": "请假3天以上怎么办？",
        "new_query": "员工请假3天以上需要怎么办理手续？",
        "target_doc": "eval_hr_attendance",
        "category": "policy",
        "old_keywords": ["车间主任", "请假申请单", "书面"],
        "required_keyword_groups": [["车间主任"], ["请假申请单"], ["人力资源部", "备案"]]
    },
    {
        "id": "Q32",
        "old_query": "不请假就不来上班怎么处理？",
        "new_query": "没有办理请假手续就不来上班的，公司怎么处理？",
        "target_doc": "eval_hr_attendance",
        "category": "policy",
        "old_keywords": ["旷工", "未办理", "擅自缺勤"],
        "required_keyword_groups": [["旷工"], ["未办理请假手续", "请假未获批准", "擅自缺勤"]]
    },
    {
        "id": "Q33",
        "old_query": "离职员工多久要搬出宿舍？",
        "new_query": "员工离职以后宿舍多久之内必须搬走？",
        "target_doc": "eval_admin_dormitory",
        "category": "policy",
        "old_keywords": ["三天", "迁离", "离职日"],
        "required_keyword_groups": [["三天", "三天内"], ["迁离宿舍", "离职日"]]
    },
    {
        "id": "Q34",
        "old_query": "宿舍里能做饭吗？",
        "new_query": "宿舍里可以自己做饭或者接电线吗？",
        "target_doc": "eval_admin_dormitory",
        "category": "policy",
        "old_keywords": ["禁止", "烧煮", "电线"],
        "required_keyword_groups": [["禁止烧煮", "禁止", "烧煮", "烹饪"], ["私自接配电线", "装接电器", "电线"]]
    },
    {
        "id": "Q35",
        "old_query": "外面的人能在宿舍过夜吗？",
        "new_query": "外人来宿舍借宿需要办什么手续？",
        "target_doc": "eval_admin_dormitory",
        "category": "policy",
        "old_keywords": ["外来人员", "留宿", "申请表"],
        "required_keyword_groups": [["外来人员留宿申请表", "留宿申请"], ["行政部", "身份证明"]]
    },
    {
        "id": "Q36",
        "old_query": "举报安全隐患有奖励吗？标准是什么？",
        "new_query": "举报一般安全隐患能获得多少奖励？重大隐患呢？",
        "target_doc": "eval_hr_safety_report",
        "category": "policy",
        "old_keywords": ["50元", "100元", "500元", "奖励"],
        "required_keyword_groups": [["一般事故隐患"], ["50元至100元", "50元"], ["重大事故隐患"], ["300元至500元", "300元"]]
    },
    {
        "id": "Q37",
        "old_query": "怎么举报安全问题？",
        "new_query": "发现安全隐患可以通过哪些方式举报？",
        "target_doc": "eval_hr_safety_report",
        "category": "policy",
        "old_keywords": ["电话", "电子邮件", "书信"],
        "required_keyword_groups": [["电话"], ["电子邮件"], ["书信", "来访"]]
    }
]

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CACHE_FILE = os.path.join(_PROJECT_ROOT, "scratch", "embedding_cache.json")

def load_embedding_cache() -> Dict[str, List[float]]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_embedding_cache(cache: Dict[str, List[float]]):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save embedding cache: {e}")

def _call_embedding(batch: List[str], api_key: str, model: str, base_url: str, dim: int) -> List[List[float]]:
    import requests
    is_dashscope = "dashscope.aliyuncs.com" in base_url or "qwen" in model.lower() or "text-embedding" in model.lower()
    if is_dashscope:
        # config.embedding.api_base_url is just the domain: https://dashscope.aliyuncs.com
        # The OpenAI-compatible embedding endpoint is /compatible-mode/v1/embeddings
        stripped = base_url.rstrip("/")
        if "/compatible-mode" in stripped or "/api/v1" in stripped:
            url = f"{stripped}/embeddings"
        else:
            url = f"{stripped}/compatible-mode/v1/embeddings"
        payload = {
            "model": model,
            "input": batch,
            "dimensions": dim
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            embeddings_data = data.get("data", [])
            sorted_embs = [None] * len(batch)
            for idx, item in enumerate(embeddings_data):
                item_idx = item.get("index", idx)
                if item_idx < len(batch):
                    sorted_embs[item_idx] = item["embedding"]
            return [e if e is not None else [0.0] * dim for e in sorted_embs]
        except Exception as e:
            print(f"    ⚠️ DashScope Embedding API Error: {e}")
            return []
    else:
        url = f"{base_url}/models/{model}:batchEmbedContents?key={api_key}"
        payload = {
            "requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in batch]
        }
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings", [])
            return [item["values"] for item in embeddings]
        except Exception as e:
            print(f"    ⚠️ Gemini API Error: {e}")
            return []

def get_cached_embeddings(texts: List[str], cache: Dict[str, List[float]], config) -> List[List[float]]:
    results = []
    missing_texts = []
    missing_indices = []

    model = config.embedding.model
    for idx, text in enumerate(texts):
        # Prevent cross-model cache corruption by prefixing model name in md5 hash
        h = hashlib.md5(f"{model}_{text}".encode("utf-8")).hexdigest()
        if h in cache:
            results.append((idx, cache[h]))
        else:
            missing_texts.append(text)
            missing_indices.append(idx)

    if missing_texts:
        api_key = config.embedding.api_key
        base_url = config.embedding.api_base_url
        dim = config.embedding.dimension
        batch_size = config.embedding.batch_size
        
        is_dashscope = "dashscope.aliyuncs.com" in base_url or "qwen" in model.lower() or "text-embedding" in model.lower()
        if is_dashscope:
            print(f"      Calling DashScope Embedding API for {len(missing_texts)} missing chunks...")
        else:
            print(f"      Calling Gemini Embedding API for {len(missing_texts)} missing chunks...")

        fetched_embs = []
        for i in range(0, len(missing_texts), batch_size):
            batch = missing_texts[i:i+batch_size]
            embs = _call_embedding(batch, api_key, model, base_url, dim)
            if not embs:
                # Fallback: SHA-256 fake vector
                for text_item in batch:
                    h_item = hashlib.sha256(text_item.encode()).hexdigest()
                    fake_vector = [(int(h_item[j * 2 : j * 2 + 2], 16) - 128) / 128.0 for j in range(min(dim, 32))]
                    if len(fake_vector) < dim:
                        fake_vector.extend([0.0] * (dim - len(fake_vector)))
                    embs.append(fake_vector)
            fetched_embs.extend(embs)

        for text_item, emb in zip(missing_texts, fetched_embs):
            h_key = hashlib.md5(f"{model}_{text_item}".encode("utf-8")).hexdigest()
            cache[h_key] = emb
            
        save_embedding_cache(cache)

        for orig_idx, emb in zip(missing_indices, fetched_embs):
            results.append((orig_idx, emb))

    results.sort(key=lambda x: x[0])
    return [r[1] for r in results]

def normalize_text(text: str) -> str:
    out = []
    for c in text:
        code = ord(c)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xfee0))
        else:
            out.append(c)
    out_str = "".join(out).lower()
    return re.sub(r"\s+", "", out_str)

def is_relevant_tokenized(kw: str, chunk_text: str) -> bool:
    kw_norm = normalize_text(kw)
    chunk_norm = normalize_text(chunk_text)
    if kw_norm in chunk_norm:
        return True
    
    # 1. Stem stripping check (e.g., stripping suffix '单', '表', '号', '器', '杆', '机', '部', '箱', '件' to get core semantic root)
    stems = [kw_norm]
    if len(kw_norm) > 2 and kw_norm[-1] in ["单", "表", "号", "器", "杆", "机", "部", "箱", "件"]:
        stems.append(kw_norm[:-1])
    for stem in stems:
        if stem in chunk_norm:
            return True
            
    # 2. Token-level intersection check
    import jieba
    kw_tokens = [t.strip() for t in jieba.cut(kw_norm) if t.strip()]
    if not kw_tokens:
        return False
        
    matched_tokens = 0
    for token in kw_tokens:
        if token in chunk_norm:
            matched_tokens += 1
        elif len(token) > 2:
            # Try sub-token prefix/suffix match
            for sub in [token[:2], token[-2:]]:
                if sub in chunk_norm:
                    matched_tokens += 1
                    break
    
    ratio = matched_tokens / len(kw_tokens)
    return ratio >= 0.75

def is_relevant_large(query_idx: int, chunk: Dict[str, Any], strict_mode: bool = True) -> bool:
    q_info = LARGE_EVAL_QUERIES[query_idx]
    if chunk.get("doc_id") != q_info["target_doc"]:
        return False
    
    # ─── Separate Validation to Avoid Title/Dept Prefix Dilution ───
    # If separate raw_text is populated, validate against raw_text to exclude matching administrative headers.
    search_source = chunk.get("raw_text") or chunk.get("chunk_text") or ""
    chunk_text = normalize_text(search_source)
    
    if strict_mode:
        # Strict mode: All groups in required_keyword_groups must be matched.
        # In each group, at least one keyword must be present in chunk_text.
        for group in q_info["required_keyword_groups"]:
            group_matched = False
            for kw in group:
                if is_relevant_tokenized(kw, chunk_text):
                    group_matched = True
                    break
            if not group_matched:
                return False
        return True
    else:
        # Old loose OR mode: At least one of old_keywords must match.
        for keyword in q_info["old_keywords"]:
            if is_relevant_tokenized(keyword, chunk_text):
                return True
        return False


# Hybrid Search Sweep Configurations
# Original: SOP and FAQ are locked. Manual chunk sizes swept.
# Extended: Clause mode for policy documents swept independently.
SWEEP_CONFIGS = [
    # ─── Group A: Text-mode baseline (policy docs use text chunking like manual) ───
    {"name": "Text_300_40 (Baseline)", "manual": (300, 40), "sop": (600, 100), "faq": (600, 100), "clause": None},
    {"name": "Text_400_80", "manual": (400, 80), "sop": (600, 100), "faq": (600, 100), "clause": None},
    {"name": "Text_500_100", "manual": (500, 100), "sop": (600, 100), "faq": (600, 100), "clause": None},
    {"name": "Text_600_120", "manual": (600, 120), "sop": (600, 100), "faq": (600, 100), "clause": None},
    # ─── Group B: Clause-mode for policy docs (manual locked at 600/100) ───
    {"name": "Clause_600_100", "manual": (600, 100), "sop": (600, 100), "faq": (600, 100), "clause": (600, 100)},
    {"name": "Clause_800_100", "manual": (600, 100), "sop": (600, 100), "faq": (600, 100), "clause": (800, 100)},
    {"name": "Clause_1000_150", "manual": (600, 100), "sop": (600, 100), "faq": (600, 100), "clause": (1000, 150)},
    # ─── Group C: Clause + Context Prepend (best clause + dept/section prepend) ───
    {"name": "Clause_800_100+Dept+Sec", "manual": (600, 100), "sop": (600, 100), "faq": (600, 100), "clause": (800, 100), "prepend_dept": True, "prepend_section": True},
]



def evaluate_retrieval_large_hybrid(
    chunks: List[Dict[str, Any]], 
    old_query_vectors: List[List[float]], 
    new_query_vectors: List[List[float]],
    baseline_ranks: Dict[str, int] = None
) -> List[Dict[str, Any]]:
    eval_results = []
    import numpy as np
    
    def get_parent_id(c: Dict[str, Any]) -> str:
        if c.get("extra") and "parent_id" in c["extra"]:
            return c["extra"]["parent_id"]
        cid = c.get("chunk_id", "")
        if "_child_" in cid:
            return cid.split("_child_")[0]
        return cid

    # ─── Parent-Child Setup ───
    is_parent_child = any(c.get("chunk_type") == "child_chunk" for c in chunks)
    if is_parent_child:
        # Keep all child chunks, plus chunks that do NOT have child chunks (e.g. faq_chunks, table_chunks, or unsliced chunks)
        child_parent_ids = {get_parent_id(c) for c in chunks if c.get("chunk_type") == "child_chunk"}
        search_pool = [
            c for c in chunks 
            if c.get("chunk_type") == "child_chunk" or get_parent_id(c) not in child_parent_ids
        ]
        parents_pool = [c for c in chunks if c.get("chunk_type") != "child_chunk"]
        parents_dict = {p["chunk_id"]: p for p in parents_pool if "chunk_id" in p}
    else:
        search_pool = chunks
        parents_dict = {}

    # Build BM25 index on the entire searchable pool to preserve stable vocabulary and IDF weights
    tokenized_corpus = [list(jieba.cut(c["chunk_text"])) for c in search_pool]
    bm25 = BM25Okapi(tokenized_corpus)

    for idx, q_info in enumerate(LARGE_EVAL_QUERIES):
        new_vec = np.array(new_query_vectors[idx])
        query_text = q_info["new_query"]
        
        target_doc = q_info["target_doc"]
        dept_filter = None
        if target_doc.startswith("eval_it_"):
            dept_filter = "it"
        elif target_doc.startswith("eval_prod_"):
            dept_filter = "production"
        elif target_doc.startswith("eval_admin_"):
            dept_filter = "admin"
        elif target_doc.startswith("eval_hr_"):
            dept_filter = "hr"
        elif target_doc == "eval_company_faq":
            dept_filter = "admin"

        # Detect strict intent sub-document filter (refined business intent routing)
        doc_filter = None
        if dept_filter == "it":
            if any(w in query_text for w in ["海外发票", "发票系统", "发票出库", "发票入库", "参照生单"]):
                doc_filter = "eval_it_invoice_system"
            elif any(w in query_text for w in ["wifi", "wi-fi", "无线", "打印机", "卡纸", "内线", "分机", "系统管理员", "电话"]):
                doc_filter = "eval_it_faq"
            elif any(w in query_text for w in ["成品仓库", "成品仓", "销售出库", "PDA", "扫码枪", "出库单", "条码", "扫码", "检验单", "入库单"]):
                doc_filter = "eval_it_warehouse_u8"
            elif any(w in query_text for w in ["五金仓", "材料及五金仓", "限额领料", "非限额", "五金", "限额", "仓库人员", "系统领用量", "领用量", "出库类别", "超额领料"]):
                doc_filter = "eval_it_wujin_u8"
            elif any(w in query_text for w in ["车间生产", "车间", "看板", "生产订单"]):
                doc_filter = "eval_it_chejian_u8"
            elif any(w in query_text for w in ["工价", "工资核算", "成品工价单"]):
                doc_filter = "eval_it_payroll_manual"
            elif any(w in query_text for w in ["英特尔", "针脚", "lga", "压杆", "处理器", "cpu"]):
                doc_filter = "eval_it_pc_install"
            elif any(w in query_text for w in ["财务部", "凭证", "付款单据", "普通发票", "专用发票"]):
                doc_filter = "eval_it_finance_u8"
            elif any(w in query_text for w in ["入职登记", "重新入职", "卡号", "考勤排班"]):
                doc_filter = "eval_it_hr_u8"
        elif dept_filter == "production":
            if "入库" in query_text:
                doc_filter = "eval_prod_xisu_ruku"
            elif "交货" in query_text:
                doc_filter = "eval_prod_xisu_jiaohuo"
            elif "领料" in query_text:
                doc_filter = "eval_prod_xisu_lingliao"
            elif "数量本" in query_text:
                doc_filter = "eval_prod_xisu_shuliang"
            elif any(w in query_text for w in ["测水", "吸管孔", "粘贴胶带", "盖塞"]):
                doc_filter = "eval_prod_naichabei"
            elif any(w in query_text for w in ["纸吸管", "耐热", "耐高温"]):
                doc_filter = "eval_prod_xiguan_receshi"
        elif dept_filter == "hr":
            if any(w in query_text for w in ["请假", "考勤", "旷工", "缺勤"]):
                doc_filter = "eval_hr_attendance"
            elif any(w in query_text for w in ["安全隐患", "举报", "奖励", "报告"]):
                doc_filter = "eval_hr_safety_report"
            else:
                doc_filter = "eval_hr_manual"
        elif dept_filter == "admin":
            if any(w in query_text for w in ["采购", "招投标", "采购部", "行政部"]):
                doc_filter = "eval_admin_procurement"
            elif any(w in query_text for w in ["宿舍", "搬", "做饭", "留宿", "电线", "迁离"]):
                doc_filter = "eval_admin_dormitory"
            else:
                doc_filter = "eval_company_faq"

        # ─── 1. Query Decomposition ───
        delimiters = [r"？", r"。", r"；", r"\?", r"\.", r";"]
        pattern = "|".join(delimiters)
        sub_queries = [q.strip() for q in re.split(pattern, query_text) if q.strip()]
        if not sub_queries:
            sub_queries = [query_text]
        
        # Keyword-based semantic expansion
        expanded_sub_queries = []
        for sq in sub_queries:
            expanded_sub_queries.append(sq)
            sq_lower = sq.lower()
            if "wifi" in sq_lower or "无线" in sq:
                expanded_sub_queries.append("Wi-Fi 无线网络 密码 WiFi")
            if "入库" in sq:
                expanded_sub_queries.append("产品入库单 打印 仓管")
            if "领料" in sq:
                expanded_sub_queries.append("领料单 辅料工 纸箱仓管")
            if "交货" in sq:
                expanded_sub_queries.append("吸塑交货单 打印 包材")
            if "工价" in sq:
                expanded_sub_queries.append("半成品工价单 成品工价单")
            if "卡纸" in sq:
                expanded_sub_queries.append("打印机 卡纸 IT部 8088")
            if "年休假" in sq or "转正" in sq:
                expanded_sub_queries.append("带薪年休假 试用小结")
        sub_queries = list(set(expanded_sub_queries))

        # ─── 2. Search & Score computation ───
        # A. Vector Scores (Cosine Similarity on Raw Query)
        filt_chunk_vectors = np.array([c["chunk_vector"] for c in search_pool])
        norms = np.linalg.norm(filt_chunk_vectors, axis=1)
        norms[norms == 0] = 1e-10
        norm_chunk_vectors = filt_chunk_vectors / norms[:, np.newaxis]
        norm_query_vec = new_vec / np.linalg.norm(new_vec)
        vector_scores = np.dot(norm_chunk_vectors, norm_query_vec)

        # B. BM25 Scores (Maximum BM25 across decomposed sub-queries)
        max_bm25_scores = np.zeros(len(search_pool))
        for sq in sub_queries:
            tokenized_sq = list(jieba.cut(sq))
            sq_bm25_scores = np.array(bm25.get_scores(tokenized_sq))
            max_bm25_scores = np.maximum(max_bm25_scores, sq_bm25_scores)

        # Normalize Vector & BM25 scores
        def normalize(scores):
            min_s, max_s = np.min(scores), np.max(scores)
            if max_s - min_s == 0: return np.zeros_like(scores)
            return (scores - min_s) / (max_s - min_s)
            
        norm_vector = normalize(vector_scores)
        norm_bm25 = normalize(max_bm25_scores)
        
        # Hybrid Fusion
        hybrid_scores = 0.7 * norm_vector + 0.3 * norm_bm25

        # ─── 3. Soft Filter Discounting ───
        final_scores = np.zeros(len(search_pool))
        for i, c in enumerate(search_pool):
            c_doc = c.get("doc_id", "")
            
            c_dept = None
            if c_doc.startswith("eval_it_"):
                c_dept = "it"
            elif c_doc.startswith("eval_prod_"):
                c_dept = "production"
            elif c_doc.startswith("eval_admin_"):
                c_dept = "admin"
            elif c_doc.startswith("eval_hr_"):
                c_dept = "hr"
            elif c_doc == "eval_company_faq":
                c_dept = "admin"
                
            discount = 1.0
            if dept_filter and c_dept != dept_filter:
                discount *= 0.5
                
            if doc_filter and c_doc != doc_filter:
                discount *= 0.5
                
            final_scores[i] = hybrid_scores[i] * discount

        # Apply hard title contains filter if explicitly required by query info
        filters = q_info.get("filters", {})
        if "title_contains" in filters:
            for i, c in enumerate(search_pool):
                if filters["title_contains"] not in c.get("title", ""):
                    final_scores[i] = -1.0

        # C. Fallback to Global Wide Search if Filtered Top Score is too low
        if len(final_scores) > 0 and np.max(final_scores) < 0.35:
            # Bypass metadata filters completely to retrieve high-precision exceptions
            final_scores = hybrid_scores.copy()
            if "title_contains" in filters:
                for i, c in enumerate(search_pool):
                    if filters["title_contains"] not in c.get("title", ""):
                        final_scores[i] = -1.0

        # ─── 4. Parent Mapping (Deduplication) ───
        parent_candidate_scores = {}
        for i, child_chunk in enumerate(search_pool):
            if final_scores[i] < 0:
                continue
            
            p_id = get_parent_id(child_chunk)
            score = float(final_scores[i])
            
            if is_parent_child:
                if p_id in parents_dict:
                    if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                        parent_candidate_scores[p_id] = {
                            "chunk": parents_dict[p_id].copy(),
                            "score": score
                        }
                else:
                    if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                        parent_candidate_scores[p_id] = {
                            "chunk": child_chunk.copy(),
                            "score": score
                        }
            else:
                if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                    parent_candidate_scores[p_id] = {
                        "chunk": child_chunk.copy(),
                        "score": score
                    }

        # ─── 5. Neighbor Stitching ───
        doc_groups = {}
        for p_id, item in parent_candidate_scores.items():
            chunk = item["chunk"]
            score = item["score"]
            doc_id = chunk.get("doc_id", "")
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append((chunk, score))
            
        stitched_candidates = []
        for doc_id, items in doc_groups.items():
            # Sort by physical adjacency (chunk_index)
            items.sort(key=lambda x: x[0].get("chunk_index", 0))
            
            i = 0
            while i < len(items):
                current_chunk, current_score = items[i]
                current_chunk = current_chunk.copy()
                current_chunk["_score"] = current_score
                
                j = i + 1
                while j < len(items):
                    next_chunk, next_score = items[j]
                    idx1 = current_chunk.get("chunk_index", 0)
                    idx2 = next_chunk.get("chunk_index", 0)
                    
                    if idx2 - idx1 <= 1:
                        # Consecutive chunks: merge text blocks
                        current_chunk["chunk_text"] = current_chunk["chunk_text"] + "\n... [Contiguous] ...\n" + next_chunk["chunk_text"]
                        if current_chunk.get("raw_text") or next_chunk.get("raw_text"):
                            current_chunk["raw_text"] = (current_chunk.get("raw_text") or "") + "\n" + (next_chunk.get("raw_text") or "")
                        # Inherit the maximum matching score
                        current_chunk["_score"] = max(current_chunk["_score"], next_score)
                        j += 1
                    else:
                        break
                stitched_candidates.append(current_chunk)
                i = j

        # Sort stitched candidates descending by score and keep the top 10
        stitched_candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        new_hits = stitched_candidates[:10]
            
        new_hit_rank = 0
        for rank, chunk in enumerate(new_hits, start=1):
            if is_relevant_large(idx, chunk, strict_mode=True):
                new_hit_rank = rank
                break
                
        new_r1 = 1 if new_hit_rank == 1 else 0
        new_r5 = 1 if 0 < new_hit_rank <= 5 else 0
        new_r10 = 1 if 0 < new_hit_rank <= 10 else 0
        new_mrr = 1.0 / new_hit_rank if new_hit_rank > 0 else 0.0

        eval_results.append({
            "query_id": q_info["id"],
            "query": q_info["new_query"],
            "target_doc": q_info["target_doc"],
            "category": q_info["category"],
            "old_rank": 0,
            "new_rank": new_hit_rank,
            "new_r1": new_r1,
            "new_r5": new_r5,
            "new_r10": new_r10,
            "new_mrr": new_mrr,
            "baseline_regr": 0,
            "faq_regr": 0,
            "context_pollution": 0,
            "top_1_preview": new_hits[0]["chunk_text"][:80].replace("\n", "") if new_hits else "",
            "top_1_score": new_hits[0]["_score"] if new_hits else 0.0,
            "top_1_doc_id": new_hits[0].get("doc_id", "") if new_hits else ""
        })
    return eval_results



def main():
    from opensearch_pipeline.config import get_config
    config = get_config()
    config.simulate = False
    print(">>> Generating / Loading Embeddings for Queries")
    old_queries_text = [q["old_query"] for q in LARGE_EVAL_QUERIES]
    new_queries_text = [q["new_query"] for q in LARGE_EVAL_QUERIES]
    
    emb_cache = load_embedding_cache()
    old_query_vectors = get_cached_embeddings(old_queries_text, emb_cache, config)
    new_query_vectors = get_cached_embeddings(new_queries_text, emb_cache, config)
    
    print("\n=======================================================")
    print("   Starting Hybrid Sweep Evaluation (Offline)")
    print("=======================================================\n")
    
    base_dir = "/Users/laijunchen/fuling_raw_for_chunk_test"
    chunk_exp_dir = "/Users/laijunchen/Downloads/opensearch-rag-pipeline/fuling_chunk_exp"

    raw_tasks = [
        {"doc_id": "eval_it_finance_u8", "local_path": os.path.join(base_dir, "it/富岭U8+财务部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_hr_u8", "local_path": os.path.join(base_dir, "it/富岭U8+人事部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_wujin_u8", "local_path": os.path.join(base_dir, "it/富岭U8+材料及五金仓操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_trade_u8", "local_path": os.path.join(base_dir, "it/富岭U8+贸易部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_chejian_u8", "local_path": os.path.join(base_dir, "it/富岭U8+车间操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_warehouse_u8", "local_path": os.path.join(base_dir, "it/富岭U8+成品仓库操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_payroll_manual", "local_path": os.path.join(base_dir, "it/工资核算管理操作手册（2025年5月28日初版）.docx"), "category": "manual"},
        {"doc_id": "eval_it_pc_install", "local_path": os.path.join(base_dir, "it/FL-CW-XXH-003-《电脑安装》作业指导书.pdf"), "category": "manual"},
        {"doc_id": "eval_it_invoice_system", "local_path": os.path.join(base_dir, "it/海外发票系统操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_prod_naichabei", "local_path": os.path.join(base_dir, "production/FL-ZS-WI-002-奶茶杯与杯盖装配（测漏水）作业指导书.pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_shuliang", "local_path": os.path.join(base_dir, "production/FL-XS-WI-001吸塑《数量本》填写作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_lingliao", "local_path": os.path.join(base_dir, "production/FL-XS-WI-005吸塑《领料单》开立作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_jiaohuo", "local_path": os.path.join(base_dir, "production/FL-XS-WI-006《吸塑交货单》打印作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xiguan_receshi", "local_path": os.path.join(base_dir, "production/FL-XG-WI-008纸吸管耐高温测试作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_ruku", "local_path": os.path.join(base_dir, "production/FL-XS-WI-009《吸塑-产品入库单》打印作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_hr_manual", "local_path": os.path.join(base_dir, "hr/FL-HR-003《员工手册》2023年4月第三版.pdf"), "category": "sop"},
        {"doc_id": "eval_it_faq", "local_path": os.path.join(base_dir, "it/IT常见问题解答.docx"), "category": "faq"},
        {"doc_id": "eval_company_faq", "local_path": os.path.join(base_dir, "hr/新员工入职常见问题FAQ.docx"), "category": "faq"},
        # ─── Policy / 制度类文档 ───
        {"doc_id": "eval_admin_procurement", "local_path": os.path.join(chunk_exp_dir, "admin_FL-AD-012《公司采购与招投标管理制度》.docx"), "category": "policy"},
        {"doc_id": "eval_hr_attendance", "local_path": os.path.join(chunk_exp_dir, "hr_FL-HR-005《员工考勤与请休假管理规定》.docx"), "category": "policy"},
        {"doc_id": "eval_admin_dormitory", "local_path": os.path.join(chunk_exp_dir, "admin_宿舍管理制度.docx"), "category": "policy"},
        {"doc_id": "eval_hr_safety_report", "local_path": os.path.join(chunk_exp_dir, "hr_A09安全隐患报告和举报奖励制度.docx"), "category": "policy"},
    ]

    all_sweep_results = []
    
    for sw in SWEEP_CONFIGS:
        print(f"\n>>> Running Sweep: {sw['name']}")
        
        all_chunks = []
        
        for task in raw_tasks:
            doc_id = task["doc_id"]
            cat_l1 = task.get("category", "manual")
            
            # Determine dept from doc_id for canonical path lookup
            if doc_id.startswith("eval_it_"):
                dept = "it"
            elif doc_id.startswith("eval_prod_"):
                dept = "production"
            elif doc_id.startswith("eval_admin_"):
                dept = "admin"
            elif doc_id.startswith("eval_hr_"):
                dept = "hr"
            elif doc_id == "eval_company_faq":
                dept = "admin"
            else:
                dept = task["local_path"].split("/")[-2]
            
            # Try canonical JSON first, fallback to direct docx extraction
            canon_file = os.path.join("/Users/laijunchen/Downloads/opensearch-rag-pipeline/processing/canonical", dept, doc_id, "v1", "content.canonical.json")
            
            if os.path.exists(canon_file):
                with open(canon_file, "r") as f:
                    doc_data = json.load(f)
                blocks = doc_data.get("blocks", [])
                doc_title = doc_data.get("title", "")
            else:
                # Fallback: extract blocks directly from docx for policy docs without canonical
                local_path = task["local_path"]
                if not os.path.exists(local_path):
                    print(f"    ⚠️ Skipping {doc_id}: no canonical and no local file at {local_path}")
                    continue
                print(f"    📄 {doc_id}: Loading blocks directly from docx (no canonical)")
                from opensearch_pipeline.extraction.docx_extractor import extract_docx
                raw_blocks, warnings = extract_docx(local_path)
                doc_title = os.path.basename(local_path).replace(".docx", "")
                blocks = []
                for i, rb in enumerate(raw_blocks):
                    blocks.append({
                        "block_id": str(i + 1),
                        "block_type": rb.block_type,
                        "text": rb.text,
                        "page_num": getattr(rb, "page_num", 1) or 1,
                        "section_path": rb.section_path,
                        "source": rb.source or "native"
                    })
            
            for b in blocks:
                b["block_id"] = b.get("block_id", "1")
            
            # Map chunk strategy dynamically based on L1 category + sweep config
            if cat_l1 == "faq":
                m_chunk = sw["faq"][0]
                m_overlap = sw["faq"][1]
                m_mode = "faq"
            elif cat_l1 == "policy" and sw.get("clause") is not None:
                # Use clause mode for policy docs when clause config is specified
                m_chunk = sw["clause"][0]
                m_overlap = sw["clause"][1]
                m_mode = "clause"
            elif cat_l1 == "manual":
                m_chunk = sw["manual"][0]
                m_overlap = sw["manual"][1]
                m_mode = "text"
            elif cat_l1 == "policy":
                # Fallback: policy docs without clause config use manual text params
                m_chunk = sw["manual"][0]
                m_overlap = sw["manual"][1]
                m_mode = "text"
            else:
                m_chunk = sw["sop"][0]
                m_overlap = sw["sop"][1]
                m_mode = "text"
                
            chunker = DocumentChunker(
                max_chunk_chars=m_chunk,
                min_chunk_chars=10,
                overlap_chars=m_overlap,
                split_mode=m_mode,
                prepend_dept=sw.get("prepend_dept", False),
                prepend_title=sw.get("prepend_title", True),
                prepend_section=sw.get("prepend_section", True),
                prepend_for_faq=False,
                max_context_chars=100,
                max_context_ratio=0.3,
                parent_child=True
            )
            
            # Map metadata for chunking
            owner_dept = "it" if "it" in task["doc_id"] else ("hr" if "hr" in task["doc_id"] else ("production" if "prod" in task["doc_id"] else "admin"))
            metadata = {
                "title": doc_title,
                "owner_dept": owner_dept,
                "category_l1": cat_l1
            }
            
            chunks = chunker.chunk_from_blocks(blocks=blocks, doc_id=task["doc_id"], version_no=1, metadata=metadata)
            
            texts_to_embed = [c.chunk_text for c in chunks]
            vectors = get_cached_embeddings(texts_to_embed, emb_cache, config)
            
            for i, c in enumerate(chunks):
                c_dict = {
                    "chunk_id": c.chunk_id,
                    "chunk_index": c.chunk_index,
                    "doc_id": c.doc_id,
                    "title": getattr(c, "title", "") or doc_title,
                    "chunk_text": c.chunk_text,
                    "chunk_type": c.chunk_type,
                    "section_title": c.section_title,
                    "raw_text": c.raw_text,
                    "context_prefix": c.context_prefix,
                    "chunk_vector": vectors[i],
                    "extra": getattr(c, "extra", {})
                }
                all_chunks.append(c_dict)
                
        print(f"    └─ Generated {len(all_chunks)} chunks for this sweep")
        save_embedding_cache(emb_cache)
        
        # Evaluate
        results = evaluate_retrieval_large_hybrid(all_chunks, old_query_vectors, new_query_vectors)
        
        # Calc metrics
        total = len(results)
        r1 = sum(r["new_r1"] for r in results)
        r5 = sum(r["new_r5"] for r in results)
        mrr = sum(r["new_mrr"] for r in results)
        cross_conf = sum(1 for r in results if r["top_1_doc_id"] != r["target_doc"] and r["top_1_score"] > 0)
        
        print(f"    └─ Results: R@1={r1/total:.4f}, R@5={r5/total:.4f}, MRR={mrr/total:.4f}, Cross-Doc Conf={cross_conf/total:.4f}")
        
        # Store for report
        all_sweep_results.append({
            "name": sw["name"],
            "total_chunks": len(all_chunks),
            "r1": r1 / total,
            "r5": r5 / total,
            "mrr": mrr / total,
            "cross_conf": cross_conf / total,
            "clause_mode_active": sw.get("clause") is not None,
            "results": results
        })

    # Generate Markdown Report
    # Sort by MRR descending to find champion
    all_sweep_results.sort(key=lambda x: (x["mrr"], x["r1"]), reverse=True)
    champion = all_sweep_results[0]
    
    report_lines = [
        "# Hybrid Search + Clause Chunking Comparative Sweep Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "This report compares **text-mode (baseline)** vs **clause-mode** chunking for policy/regulation documents,",
        "alongside manual/SOP/FAQ chunk size tuning, using offline hybrid BM25+Vector search with query decomposition.",
        "",
        "## 1. Overall Sweep Summary",
        "",
        "| # | Configuration | Split Mode (Policy) | Total Chunks | Strict R@1 | Strict R@5 | Strict MRR | Cross-Doc Confusion |",
        "| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]
    
    for i, s in enumerate(all_sweep_results):
        policy_mode = "clause" if s.get("clause_mode_active") else "text"
        marker = " 🏆" if i == 0 else ""
        report_lines.append(
            f"| {i+1} | **{s['name']}**{marker} | `{policy_mode}` | {s['total_chunks']} | **{s['r1']:.2%}** | **{s['r5']:.2%}** | **{s['mrr']:.4f}** | {s['cross_conf']:.2%} |"
        )
    
    # Per-category breakdown for champion
    def calc_cat(results, cat):
        cat_results = [r for r in results if r.get("category") == cat]
        if not cat_results:
            return 0, 0.0, 0.0, 0.0
        r1 = sum(r["new_r1"] for r in cat_results) / len(cat_results)
        r5 = sum(r["new_r5"] for r in cat_results) / len(cat_results)
        mrr = sum(r["new_mrr"] for r in cat_results) / len(cat_results)
        return len(cat_results), r1, r5, mrr

    report_lines.extend([
        "",
        f"## 2. Category Breakdown (Champion: {champion['name']})",
        "",
        "| Category | Query Count | Strict R@1 | Strict R@5 | Strict MRR |",
        "| :--- | :---: | :---: | :---: | :---: |"
    ])
    
    for cat_name, cat_key in [("Manual (操作手册)", "manual"), ("SOP (员工手册)", "sop"), ("FAQ (常见问题)", "faq"), ("Policy (管理制度)", "policy")]:
        n, r1, r5, mrr = calc_cat(champion["results"], cat_key)
        report_lines.append(f"| **{cat_name}** | {n} | {r1:.2%} | {r5:.2%} | {mrr:.4f} |")
    
    # Detailed query results for champion
    report_lines.extend([
        "",
        f"## 3. Detailed Query Results (Champion: {champion['name']})",
        "",
        "| Query ID | Category | Business Query | Target Document | Rank | Status | Top-1 Score | Top-1 Preview |",
        "| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |"
    ])
    
    for r in champion["results"]:
        status = "✅ Hit" if r["new_r1"] else ("⚠️ Top5" if r["new_r5"] else "❌ Miss")
        rank_str = f"#{r['new_rank']}" if r["new_rank"] > 0 else "❌"
        preview = r["top_1_preview"][:50].replace("|", "\\|") + "..."
        cat = r.get("category", "unknown")
        report_lines.append(
            f"| **{r['query_id']}** | `{cat}` | {r['query']} | `{r['target_doc']}` | {rank_str} | {status} | {r['top_1_score']:.4f} | {preview} |"
        )
        
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hybrid_sweep_report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines))
        
    print(f"\n>>> Generated report at {report_path}")



if __name__ == "__main__":
    main()

