# -*- coding: utf-8 -*-
"""
online_eval.py — 28 条评测查询在线 HA3 端到端评测

对比离线评测（BM25+余弦）与在线 HA3 检索（dense+sparse 混合）的真实效果差异。
每条查询通过 search_chunks() 走完整链路：DashScope native API → HA3 向量检索。
用 ground-truth 关键词组验证 Top-K 返回的 chunk 是否命中。
"""

import json
import os
import re
import sys
import time

# 加载 .env
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    # 确保 DASHSCOPE_API_KEY 可用
    if not os.environ.get("DASHSCOPE_API_KEY"):
        os.environ["DASHSCOPE_API_KEY"] = os.environ.get("RAG_DASHSCOPE_API_KEY", "")

from opensearch_pipeline.retriever import search_chunks

# ═══════════════════════════════════════════════════════════════
# 28 条评测查询 + ground-truth 关键词组
# ═══════════════════════════════════════════════════════════════

EVAL_QUERIES = [
    {"id": "Q01", "cat": "manual", "query": "每日奶茶杯和杯盖装配测水试验下午是在几点到几点进行？",
     "keywords": "(13:30--15:00 OR 13：30--15:00)"},
    {"id": "Q02", "cat": "manual", "query": "测水试验中，杯盖吸管孔处需要粘贴什么？杯盖上又要安装什么？",
     "keywords": "(胶带) AND (盖塞)"},
    {"id": "Q03", "cat": "manual", "query": "32位和64位英特尔CPU的针脚结构有什么主要区别？",
     "keywords": "(478) AND (lga775 OR lga 775)"},
    {"id": "Q04", "cat": "manual", "query": "主板上的LGA 775处理器压杆要怎么打开？",
     "keywords": "(压杆) AND (推 OR 微压)"},
    {"id": "Q05", "cat": "manual", "query": "录入采购发票时，选择录入普通发票还是专用发票的依据是什么？",
     "keywords": "(实际收到) AND (发票类型)"},
    {"id": "Q06", "cat": "manual", "query": "发票结算的主要目的是什么？如果次月入库但本月结算，系统会生成什么单据？",
     "keywords": "(回冲单) AND (红蓝字)"},
    {"id": "Q07", "cat": "manual", "query": "录入或导入工价单后，要怎么做才能让它认定为生效并在日工资单中取数？",
     "keywords": "(保存) AND (审核)"},
    {"id": "Q08", "cat": "manual", "query": "在系统里录入半成品工价单和成品工价单的路径是什么？",
     "keywords": "(工资核算) AND (工价)"},
    {"id": "Q09", "cat": "manual", "query": "成品仓库使用PDA扫码入库后，如何根据合格的检验单生成产品入库单？",
     "keywords": "(检验合格 OR 合格的检验单) AND (产成品入库单)"},
    {"id": "Q10", "cat": "manual", "query": "出库时使用条码扫码枪生成销售出库单，能够省略哪些手工操作步骤？",
     "keywords": "(扫码枪 OR 条码枪) AND (省略2、3、4、5步骤 OR 省略2)"},
    {"id": "Q11", "cat": "manual", "query": "班组长在接收到日计划表后，需要把生产日计划中的哪些信息记录到数量本上？",
     "keywords": "(模具) AND (客户名称) AND (商检号) AND (剩余箱数)"},
    {"id": "Q12", "cat": "manual", "query": "班组长在共享文件夹里打开生产计划单后，需要把其中的哪些包装相关信息记录到数量本上？",
     "keywords": "(包装方式) AND (克重) AND (袋子规格) AND (印刷方式)"},
    {"id": "Q13", "cat": "manual", "query": "领料单打印后，如果生产计划数量超过1000箱，班组长需要在单据右上角写什么备注？",
     "keywords": "(1000箱 OR 1000) AND (每天 OR 拉料)"},
    {"id": "Q14", "cat": "manual", "query": "打印出来的领料单共有三份，分别需要分发交接给哪些岗位的仓库管理或作业人员？",
     "keywords": "(辅料工) AND (包装袋仓管) AND (纸箱仓管)"},
    {"id": "Q15", "cat": "manual", "query": "班组长在系统打印交货单时，应该如何根据计划单的包材来判定和填写自定义包装类型？",
     "keywords": "(袋 OR 手包) AND (膜 OR 机包)"},
    {"id": "Q16", "cat": "manual", "query": "纸吸管耐热测试的机器温度是多少？浸泡热水需要测多久？",
     "keywords": "(60±1度 OR 60±1°) AND (5分钟)"},
    {"id": "Q17", "cat": "manual", "query": "烘干后的纸吸管如果耐高温测试不合格，具体的后续重新测试和报废流程是怎样的？",
     "keywords": "(50度 OR 50°) AND (常温可乐) AND (报废)"},
    {"id": "Q18", "cat": "manual", "query": "产品入库单打印完后，白红黄各联单该怎么分发和交接？",
     "keywords": "(白联 OR 留底) AND (红联 OR 财务部) AND (黄联 OR 成本部)"},
    {"id": "Q19", "cat": "manual", "query": "如果车间生产消耗量大于系统领用量，仓库人员应该如何处理领料和出库？",
     "keywords": "(补料申请单 OR 补料单)"},
    {"id": "Q20", "cat": "manual", "query": "新员工入职以及离职老员工重新回公司就职，在U8系统里分别通过什么功能操作？",
     "keywords": "(入职登记 OR 重新入职申请)"},
    {"id": "Q21", "cat": "manual", "query": "出口货物发货单新增完成时，需要在系统里录入哪些跟单信息？",
     "keywords": "(封箱号) AND (跟单员) AND (柜型)"},
    {"id": "Q22", "cat": "sop", "query": "新员工试用期满要转正，人事部门和员工本人需要在到期前多少天分别完成什么准备？",
     "keywords": "(前10天 OR 试用小结) AND (前5天 OR 员工能力鉴定表)"},
    {"id": "Q23", "cat": "sop", "query": "在公司连续工作已满10年但未满20年的员工，每年可以享受多少天的带薪年休假？",
     "keywords": "(已满10年) AND (年休假10天 OR 10天)"},
    {"id": "Q24", "cat": "manual", "query": "海外发票系统中，发票出库生成时如果参考海外仓库，系统是如何匹配并自动生成参照数据的？",
     "keywords": "(海外仓库) AND (出库数量) AND (匹配 OR 库存)"},
    {"id": "Q25", "cat": "manual", "query": "如果车间生产时损耗过大，导致正常的生产订单领料不够用，应该通过什么单据继续申请领料？",
     "keywords": "(补料申请单)"},
    {"id": "Q26", "cat": "faq", "query": "怎么申请公司的无线WiFi账号？流程是怎样的？",
     "keywords": "(Wi-Fi申请流程 OR wifi申请流程) AND (验证码) AND (FL-Enterprise)"},
    {"id": "Q27", "cat": "faq", "query": "打印机卡纸不能用了，拨打哪个内线电话联系系统管理员？",
     "keywords": "(8088) AND (IT部 OR 内线分机 OR 联系系统管理员)"},
    {"id": "Q28", "cat": "faq", "query": "刚入职的新员工，前三天吃饭怎么解决？",
     "keywords": "(领用餐券 OR 餐券) AND (宿舍楼一楼食堂 OR 食堂 OR 免费用餐)"},
]

# 离线评测基准（Manual_600_120, Strict Rank）
OFFLINE_BASELINE = {
    "Q01": 1, "Q02": 1, "Q03": 1, "Q04": 1, "Q05": 1, "Q06": 1, "Q07": 1,
    "Q08": 1, "Q09": 1, "Q10": 1, "Q11": 1, "Q12": 1, "Q13": 1, "Q14": 1,
    "Q15": 1, "Q16": 1, "Q17": 1, "Q18": 1, "Q19": 1, "Q20": 2, "Q21": 1,
    "Q22": 1, "Q23": 1, "Q24": 1, "Q25": 1, "Q26": 1, "Q27": 1, "Q28": 1,
}


# ═══════════════════════════════════════════════════════════════
# 关键词验证逻辑
# ═══════════════════════════════════════════════════════════════

def check_keyword_group(text: str, keyword_expr: str) -> bool:
    """
    验证 text 是否满足 keyword_expr 的 AND/OR 组合逻辑。
    
    格式: "(kw1 OR kw2) AND (kw3) AND (kw4 OR kw5)"
    每个 () 内是 OR 关系，各组之间是 AND 关系。
    """
    text_lower = text.lower()
    
    # 分割 AND 组
    groups = re.split(r'\s+AND\s+', keyword_expr)
    
    for group in groups:
        # 去掉括号
        group = group.strip().strip('()')
        # 分割 OR 选项
        options = [opt.strip() for opt in re.split(r'\s+OR\s+', group)]
        # 至少一个匹配
        if not any(opt.lower() in text_lower for opt in options):
            return False
    return True


def find_best_rank(results: list, keyword_expr: str, top_k: int = 10) -> int:
    """在 Top-K 结果中找到第一个满足关键词验证的排名。返回 0 表示未命中。"""
    for i, r in enumerate(results[:top_k]):
        chunk_text = r.get("chunk_text", "")
        if check_keyword_group(chunk_text, keyword_expr):
            return i + 1
    return 0


# ═══════════════════════════════════════════════════════════════
# 主评测逻辑
# ═══════════════════════════════════════════════════════════════

def run_evaluation():
    print("=" * 70)
    print("  HA3 在线检索端到端评测 (28 Queries)")
    print(f"  Search: DashScope native API (dense+sparse) → HA3 Vector Search")
    print("=" * 70)
    print()

    top_k = 10
    results_log = []
    
    hit_at_1 = 0
    hit_at_5 = 0
    hit_at_10 = 0
    mrr_sum = 0.0
    total = len(EVAL_QUERIES)

    for eq in EVAL_QUERIES:
        qid = eq["id"]
        query = eq["query"]
        keywords = eq["keywords"]
        offline_rank = OFFLINE_BASELINE.get(qid, 0)

        print(f"  {qid} [{eq['cat']}] {query[:50]}...")
        
        t0 = time.time()
        try:
            chunks = search_chunks(query, top_k=top_k)
            latency = int((time.time() - t0) * 1000)
        except Exception as e:
            print(f"       ❌ ERROR: {e}")
            results_log.append({"id": qid, "rank": 0, "error": str(e)})
            continue

        # 检索到的 chunk 用 chunk_text_store 字段（HA3 实际返回的字段名）
        for c in chunks:
            if "chunk_text" not in c and "chunk_text_store" in c:
                c["chunk_text"] = c["chunk_text_store"]

        rank = find_best_rank(chunks, keywords, top_k=top_k)
        
        # 统计
        if rank == 1:
            hit_at_1 += 1
        if 1 <= rank <= 5:
            hit_at_5 += 1
        if 1 <= rank <= 10:
            hit_at_10 += 1
        if rank > 0:
            mrr_sum += 1.0 / rank

        # 对比离线
        status = "✅" if rank > 0 else "❌ MISS"
        diff = ""
        if rank > 0 and offline_rank > 0:
            if rank < offline_rank:
                diff = f" ⬆️ (+{offline_rank - rank})"
            elif rank > offline_rank:
                diff = f" ⬇️ (-{rank - offline_rank})"
            else:
                diff = " =="
        elif rank == 0 and offline_rank > 0:
            diff = f" ⬇️ REGRESSION (offline=#{offline_rank})"

        top1_preview = ""
        top1_score = ""
        if chunks:
            top1_preview = chunks[0].get("chunk_text", "")[:60]
            top1_score = f"{chunks[0].get('score', 0):.4f}"

        print(f"       {status} Rank=#{rank} (offline=#{offline_rank}){diff} | "
              f"score={top1_score} | {latency}ms")
        if rank == 0 and chunks:
            print(f"       Top-1: {top1_preview}...")
        print()

        results_log.append({
            "id": qid, "cat": eq["cat"], "query": query,
            "online_rank": rank, "offline_rank": offline_rank,
            "top1_score": top1_score, "latency_ms": latency,
            "n_results": len(chunks),
        })
        
        # 避免 API 限流
        time.sleep(0.5)

    # ═══════════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════════
    mrr = mrr_sum / total

    print("=" * 70)
    print("  汇总结果")
    print("=" * 70)
    print()
    print(f"  {'指标':<25} {'在线 (HA3)':<15} {'离线 (BM25+cos)':<15} {'差异':<10}")
    print(f"  {'-'*65}")

    offline_r1 = sum(1 for v in OFFLINE_BASELINE.values() if v == 1) / total * 100
    offline_r5 = sum(1 for v in OFFLINE_BASELINE.values() if 1 <= v <= 5) / total * 100
    offline_mrr = sum(1.0/v for v in OFFLINE_BASELINE.values() if v > 0) / total

    online_r1 = hit_at_1 / total * 100
    online_r5 = hit_at_5 / total * 100

    print(f"  {'Strict R@1':<25} {online_r1:>6.2f}%        {offline_r1:>6.2f}%        {online_r1 - offline_r1:>+.2f}%")
    print(f"  {'Strict R@5':<25} {hit_at_5/total*100:>6.2f}%        {offline_r5:>6.2f}%        {hit_at_5/total*100 - offline_r5:>+.2f}%")
    print(f"  {'Strict R@10':<25} {hit_at_10/total*100:>6.2f}%        {'100.00':>6}%        {hit_at_10/total*100 - 100:>+.2f}%")
    print(f"  {'MRR':<25} {mrr:>6.4f}         {offline_mrr:>6.4f}         {mrr - offline_mrr:>+.4f}")
    print()
    
    # 回归分析
    regressions = [r for r in results_log if r.get("online_rank", 0) == 0 and r.get("offline_rank", 0) > 0]
    improvements = [r for r in results_log if r.get("online_rank", 0) > 0 and r.get("online_rank") < r.get("offline_rank", 999)]
    
    if regressions:
        print(f"  ⚠️  回归 ({len(regressions)} 条): 离线命中但在线未命中")
        for r in regressions:
            print(f"     - {r['id']}: {r['query'][:40]}...")
    
    if improvements:
        print(f"  ✅ 改善 ({len(improvements)} 条): 在线排名优于离线")
        for r in improvements:
            print(f"     - {r['id']}: #{r['offline_rank']} → #{r['online_rank']}")
    
    print()

    # 保存详细结果
    output_path = Path(__file__).resolve().parent.parent / "scratch" / "online_eval_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_queries": total,
                "online_R@1": f"{online_r1:.2f}%",
                "online_R@5": f"{hit_at_5/total*100:.2f}%",
                "online_R@10": f"{hit_at_10/total*100:.2f}%",
                "online_MRR": f"{mrr:.4f}",
                "offline_R@1": f"{offline_r1:.2f}%",
                "offline_MRR": f"{offline_mrr:.4f}",
                "regressions": len(regressions),
                "improvements": len(improvements),
            },
            "queries": results_log,
        }, f, ensure_ascii=False, indent=2)
    print(f"  📄 详细结果已保存: {output_path}")


if __name__ == "__main__":
    run_evaluation()
