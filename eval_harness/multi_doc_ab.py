# -*- coding: utf-8 -*-
"""multi_doc_ab.py — 多意图查询分解 + 低置信度护栏的配对 live A/B 评测。

被测改动（默认全关，评测决定是否开启）：
  1. RAG_MULTI_QUERY_MODE（query_decomposer.py + retriever._multi_query_search）：
     跨文档综合问题分解为子查询并行检索、轮转交错合并。
  2. RAG_LOW_CONFIDENCE_GUARD（llm_generator.LOW_CONFIDENCE_RULE）：top 重排分落入
     低置信带时在 system prompt 追加强化拒答指令（generation-side，不动检索）。

设计（配对、同索引、同 gold、rerank ON = 当前生产配置）：
  A) multi-doc 集（golden_full 中 expected_docs≥2 且 ≥2 个已解析 doc_id，n≈24；
     注意其中近半是"枚举型" gold（预期 4~6 份文档），top_k=7 上下文结构上装不下全部，
     full-coverage 对这类 case 是上界极低的指标——按 n_expected 分层另行汇总）
     arm off / auto / llm，指标 = 最终 top-7 上下文的 full-coverage（全部目标文档进上下文）、
     per-doc coverage、首个 gold 命中排名。配对检验：exact McNemar + bootstrap CI。
  B) 单文档无回归集（n=50，固定种子抽样）：三 arm 跑同样指标 + 触发率 + 延迟，
     验证分解不伤单文档查询。
  C) 负例集（26）：分解触发率（auto/llm）+ 护栏生成 A/B（拦截率 off vs on）。
  D) 护栏正例侧：单文档集里护栏实际触发（top<medium）的 case 生成 A/B，
     量化新增误拒与关键词覆盖变化。

用法：
  python -m eval_harness.multi_doc_ab            # 全量
  python -m eval_harness.multi_doc_ab --quick    # 烟测（少量 case、跳过护栏生成）
  python -m eval_harness.multi_doc_ab --skip-guard
"""
from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from math import comb

from . import envboot  # noqa: F401  (side-effecting: live public endpoints, read-only)
from .ha3live import install_into_retriever
from .matching import chunk_matches_expected, gold_doc_rank, hard_refusal, keyword_coverage
from .metrics import bootstrap_ci, mean

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(HERE, "goldset", "golden_full.json")
OUT = os.path.join(HERE, "reports", "multi_doc_ab.json")

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(msg, flush=True)


# ── case selection ──────────────────────────────────────────────────────

def resolved_pairs(case):
    """已解析的 (title, doc_id) gold 对（按 doc_id 去重）。"""
    seen, pairs = set(), []
    for r in case.get("resolution") or []:
        did = r.get("doc_id")
        title = r.get("title") or r.get("expected")
        if not did or did in seen:
            continue
        seen.add(did)
        pairs.append((title, did))
    return pairs


def load_cases(quick=False):
    gold = json.load(open(GOLD, encoding="utf-8"))
    multi, single, negs = [], [], []
    for c in gold:
        if c.get("kind") == "negative":
            negs.append(c)
            continue
        if not c.get("live_scorable"):
            continue
        pairs = resolved_pairs(c)
        c["_pairs"] = pairs
        if len(c.get("expected_docs") or []) >= 2 and len(pairs) >= 2:
            multi.append(c)
        elif len(pairs) == 1:
            single.append(c)
    rng = random.Random(7)
    rng.shuffle(single)
    single = single[:50]
    if quick:
        multi, single, negs = multi[:3], single[:3], negs[:2]
    return multi, single, negs


# ── retrieval arms ──────────────────────────────────────────────────────

def _install_trigger_recorder():
    """包一层 maybe_decompose 以记录每个查询的分解结果（不改变行为）。"""
    import opensearch_pipeline.query_decomposer as QD
    orig = QD.maybe_decompose
    book = {}

    def recording(q):
        subs = orig(q)
        book[q] = subs
        return subs

    QD.maybe_decompose = recording
    return book


def run_retrieval_arm(cases, mode, label, trigger_book, doc_cap=0):
    """一个 arm：设 multi_query_mode=mode + doc_diversity_cap=doc_cap，全 case 检索并打分。"""
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.retriever import retrieve_and_enrich

    get_config().rag.multi_query_mode = mode
    get_config().rag.doc_diversity_cap = doc_cap
    trigger_book.clear()   # 防止上一 arm 的分解记录污染本 arm 的触发率统计
    rows = {}

    def one(c):
        q = c["query"]
        t0 = time.time()
        err = None
        chunks = []
        for attempt in (1, 2):   # 瞬时故障重试一次；仍失败则记 error（不计为检索 miss）
            try:
                chunks = retrieve_and_enrich(q, top_k=7)
                err = None
                break
            except Exception as e:
                err = f"{e}"[:200]
                log(f"  ! {c['qid']} 检索异常 (attempt {attempt}): {e}")
                time.sleep(1.5)
        lat = int((time.time() - t0) * 1000)
        pairs = c.get("_pairs") or []
        matched = [any(chunk_matches_expected(ch, [n], [d]) for ch in chunks)
                   for n, d in pairs]
        names = [n for n, _ in pairs]
        ids = [d for _, d in pairs]
        subs = trigger_book.get(q, [])
        scorable = bool(pairs) and err is None
        rows[c["qid"]] = {
            "qid": c["qid"], "query": q, "n_expected": len(pairs),
            "n_matched": sum(matched), "error": err,
            "full_coverage": scorable and all(matched),
            "coverage_frac": (sum(matched) / len(pairs)) if scorable else None,
            "first_gold_rank": gold_doc_rank(chunks, names, ids) if scorable else None,
            "latency_ms": lat,
            "triggered": len(subs) >= 2, "sub_queries": subs,
            "doc_titles": [ch.get("title", "") for ch in chunks],
            "_chunks": chunks,  # 仅在内存中供护栏阶段复用，落盘前剔除
        }
        return None

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, cases))
    get_config().rag.doc_diversity_cap = 0
    done = [r for r in rows.values() if r["coverage_frac"] is not None]
    cov = (f"full_coverage={mean([1.0 if r['full_coverage'] else 0.0 for r in done]):.3f}, "
           f"cov_frac={mean([r['coverage_frac'] for r in done]):.3f}, ") if done else ""
    log(f"[{label}] mode={mode} cap={doc_cap}: n={len(rows)}, {cov}"
        f"trigger={mean([1.0 if r['triggered'] else 0.0 for r in rows.values()]):.2f}, "
        f"lat_p50={sorted(r['latency_ms'] for r in rows.values())[len(rows)//2]}ms")
    return rows


# ── paired statistics ───────────────────────────────────────────────────

def mcnemar_exact(n_a_only, n_b_only):
    """exact two-sided McNemar（二项检验，p 截到 1.0）。"""
    n = n_a_only + n_b_only
    if n == 0:
        return 1.0
    k = min(n_a_only, n_b_only)
    p = 2 * sum(comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    return min(1.0, p)


NONINF_MARGIN = 0.05   # 无回归判定：coverage_frac delta 的 bootstrap CI 下界 > -5pp


def paired_summary(rows_a, rows_b, label_a, label_b):
    """full_coverage 的 exact McNemar + coverage_frac/latency 的配对 bootstrap CI。

    任一臂出错（error 非空）的 case 整对剔除（不会被记成检索 miss）；
    no-regression 论断用非劣界（CI 下界 > -NONINF_MARGIN），不用"不显著"。
    """
    qids = sorted(set(rows_a) & set(rows_b))
    a = [rows_a[q] for q in qids]
    b = [rows_b[q] for q in qids]
    ok = [(x, y) for x, y in zip(a, b) if not x.get("error") and not y.get("error")]
    scored = [(x, y) for x, y in ok if x["coverage_frac"] is not None
              and y["coverage_frac"] is not None]
    b_only = sum(1 for x, y in scored if (not x["full_coverage"]) and y["full_coverage"])
    a_only = sum(1 for x, y in scored if x["full_coverage"] and (not y["full_coverage"]))
    deltas_cov = [y["coverage_frac"] - x["coverage_frac"] for x, y in scored]
    deltas_lat = [float(y["latency_ms"] - x["latency_ms"]) for x, y in ok]
    cov_ci = bootstrap_ci(deltas_cov)
    return {
        "n": len(scored),
        "n_errors_excluded": len(qids) - len(ok),
        f"full_coverage_{label_a}": round(mean([1.0 if x["full_coverage"] else 0.0
                                                for x, _ in scored]), 4),
        f"full_coverage_{label_b}": round(mean([1.0 if y["full_coverage"] else 0.0
                                                for _, y in scored]), 4),
        "flips_gained": b_only, "flips_lost": a_only,
        "mcnemar_p": round(mcnemar_exact(a_only, b_only), 5),
        "coverage_frac_delta_mean": round(mean(deltas_cov), 4),
        "coverage_frac_delta_ci": cov_ci,
        "noninferior": (cov_ci or {}).get("lo", -1) > -NONINF_MARGIN,
        "latency_delta_ms_mean": round(mean(deltas_lat), 1) if deltas_lat else None,
        "latency_delta_ms_ci": bootstrap_ci(deltas_lat) if deltas_lat else None,
    }


# ── guard (generation-side) A/B ─────────────────────────────────────────

def _fired(chunks):
    """与 llm_generator._is_low_confidence 同逻辑（rerank 分优先，medium 阈值）。"""
    from opensearch_pipeline.config import get_config
    cfg = get_config().rag
    rr = [c["rerank_score"] for c in chunks
          if isinstance(c.get("rerank_score"), (int, float))]
    if rr:
        return max(rr) < cfg.rerank_score_threshold_medium
    fused = [c["score"] for c in chunks if isinstance(c.get("score"), (int, float))]
    return bool(fused) and max(fused) < cfg.score_threshold_medium


def run_guard_ab(cases, baseline_rows, kind):
    """对每个 case 用 baseline（mode=off）检索结果做 guard off/on 生成对照。

    护栏未触发的 case 两臂 payload 完全一致 → 只生成一次、双臂复用。
    """
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.llm_generator import generate_answer

    cfg = get_config()
    cfg.rag.multi_query_mode = "off"
    rows = []
    # guard 开关是进程级全局配置：必须串行化「置位 + 生成」，否则并发线程会互相
    # 污染对方 arm 的 system prompt（生成是长尾，锁的代价可接受）。
    gen_lock = threading.Lock()

    def gen(query, chunks, guard_on):
        with gen_lock:
            cfg.rag.low_confidence_guard = guard_on
            try:
                for attempt in (1, 2):
                    try:
                        return generate_answer(query, chunks)["answer"]
                    except Exception as e:
                        log(f"  ! 生成异常 (attempt {attempt}): {e}")
                        time.sleep(1.5)
                return None   # 两次失败 → 该 case 记 error，不计入任何统计
            finally:
                cfg.rag.low_confidence_guard = False

    def one(c):
        q = c["query"]
        base = baseline_rows.get(c["qid"])
        if base is None or base.get("error"):
            return
        chunks = base["_chunks"]
        if not chunks:
            return  # 空检索本就走 NO_RESULT，护栏无涉
        fired = _fired(chunks)
        ans_off = gen(q, chunks, guard_on=False)
        ans_on = gen(q, chunks, guard_on=True) if fired else ans_off
        kws = c.get("keyword_gt") or []
        rows.append({
            "qid": c["qid"], "kind": kind, "query": q, "fired": fired,
            "error": None if (ans_off is not None and ans_on is not None) else "GEN_FAILED",
            "top_score": max((ch.get("score") or 0) for ch in chunks),
            "hard_refusal_off": hard_refusal(ans_off),
            "hard_refusal_on": hard_refusal(ans_on),
            "kw_cov_off": keyword_coverage(ans_off, kws) if kws else None,
            "kw_cov_on": keyword_coverage(ans_on, kws) if kws else None,
            "answer_off": (ans_off or "")[:400], "answer_on": (ans_on or "")[:400],
        })

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(one, cases))
    return rows


def guard_summary(rows):
    n_err = sum(1 for r in rows if r.get("error"))
    rows = [r for r in rows if not r.get("error")]
    neg = [r for r in rows if r["kind"] == "negative"]
    pos = [r for r in rows if r["kind"] == "positive"]
    pos_fired = [r for r in pos if r["fired"]]
    out = {
        "n_negative": len(neg),
        "neg_fired_rate": round(mean([1.0 if r["fired"] else 0.0 for r in neg]), 3) if neg else None,
        "neg_interception_off": round(mean([1.0 if r["hard_refusal_off"] else 0.0 for r in neg]), 3) if neg else None,
        "neg_interception_on": round(mean([1.0 if r["hard_refusal_on"] else 0.0 for r in neg]), 3) if neg else None,
        "n_positive": len(pos), "n_positive_fired": len(pos_fired),
        "pos_overrefusal_off": round(mean([1.0 if r["hard_refusal_off"] else 0.0 for r in pos]), 4) if pos else None,
        "pos_overrefusal_on": round(mean([1.0 if r["hard_refusal_on"] else 0.0 for r in pos]), 4) if pos else None,
    }
    kw = [(r["kw_cov_off"], r["kw_cov_on"]) for r in pos_fired
          if r["kw_cov_off"] == r["kw_cov_off"] and r["kw_cov_off"] is not None]
    if kw:
        out["pos_fired_kw_cov_off"] = round(mean([a for a, _ in kw]), 3)
        out["pos_fired_kw_cov_on"] = round(mean([b for _, b in kw]), 3)
    # 负例侧 McNemar（off→on 拦截翻转）
    gained = sum(1 for r in neg if r["hard_refusal_on"] and not r["hard_refusal_off"])
    lost = sum(1 for r in neg if r["hard_refusal_off"] and not r["hard_refusal_on"])
    out["neg_flips_gained"] = gained
    out["neg_flips_lost"] = lost
    out["neg_mcnemar_p"] = round(mcnemar_exact(lost, gained), 5)
    out["n_gen_errors_excluded"] = n_err
    return out


# ── main ────────────────────────────────────────────────────────────────

def strip_chunks(rows):
    return {q: {k: v for k, v in r.items() if k != "_chunks"} for q, r in rows.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-guard", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    install_into_retriever()
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    cfg.alibaba_vector.rerank_enable = True   # 生产已开启重排，评测保持一致
    cfg.rag.low_confidence_guard = False
    trigger_book = _install_trigger_recorder()

    multi, single, negs = load_cases(quick=args.quick)
    log(f"cases: multi={len(multi)} single={len(single)} neg={len(negs)} "
        f"(table={os.environ.get('RAG_HA3_TABLE_NAME')}, rerank=on)")

    # 预热（不计分）：均衡 TLS/连接池/嵌入缓存等冷启动效应，避免固定 arm 顺序
    # 把冷启动成本全记在第一个 arm 上、污染跨 arm 延迟对比。
    if not args.quick:
        log("warm-up pass (uncounted) ...")
        _wb = {}
        run_retrieval_arm(multi + single + negs, "off", "warm-up", _wb)

    report = {"meta": {"table": os.environ.get("RAG_HA3_TABLE_NAME"),
                       "rerank_enable": True, "top_k": 7,
                       "n_multi": len(multi), "n_single": len(single),
                       "n_negative": len(negs)},
              "multidoc": {}, "single": {}, "negatives": {}, "guard": {}}

    # — A) multi-doc 五臂：off / auto / llm（分解）+ cap4 / auto+cap4（文档限额）—
    ARMS_MULTI = [("off", 0), ("auto", 0), ("llm", 0), ("off", 4), ("auto", 4)]
    arms_multi = {}
    for mode, cap in ARMS_MULTI:
        name = f"{mode}_cap{cap}" if cap else mode
        arms_multi[name] = run_retrieval_arm(multi, mode, "multi-doc", trigger_book, doc_cap=cap)
    report["multidoc"]["arms"] = {m: strip_chunks(r) for m, r in arms_multi.items()}
    for treat in ("auto", "llm", "off_cap4", "auto_cap4"):
        report["multidoc"][f"off_vs_{treat}"] = paired_summary(
            arms_multi["off"], arms_multi[treat], "off", treat)
        log(f"multi-doc off→{treat}: "
            f"{json.dumps(report['multidoc'][f'off_vs_{treat}'], ensure_ascii=False)}")

    # 结构分层：枚举型 gold（n_expected≥4，top-7 装不下）vs 可达型（2~3 docs）；
    # 另报"≥2 份目标文档进上下文"的部分覆盖率（对枚举型更有意义的指标）。
    strata = {}
    for arm_name, rows in arms_multi.items():
        ok = [r for r in rows.values() if r["coverage_frac"] is not None]
        small = [r for r in ok if r["n_expected"] <= 3]
        big = [r for r in ok if r["n_expected"] >= 4]
        strata[arm_name] = {
            "n_2to3": len(small),
            "full_coverage_2to3": round(mean([1.0 if r["full_coverage"] else 0.0
                                              for r in small]), 4) if small else None,
            "cov_frac_2to3": round(mean([r["coverage_frac"] for r in small]), 4)
            if small else None,
            "n_4plus": len(big),
            "cov_frac_4plus": round(mean([r["coverage_frac"] for r in big]), 4)
            if big else None,
            "at_least_2_docs_rate": round(mean([1.0 if r["n_matched"] >= 2 else 0.0
                                                for r in ok]), 4) if ok else None,
        }
    report["multidoc"]["strata"] = strata
    log(f"multi-doc strata: {json.dumps(strata, ensure_ascii=False)}")

    # — B) 单文档无回归（含部署候选组合 auto+cap4）—
    ARMS_SINGLE = [("off", 0), ("auto", 0), ("llm", 0), ("off", 4), ("auto", 4)]
    arms_single = {}
    for mode, cap in ARMS_SINGLE:
        name = f"{mode}_cap{cap}" if cap else mode
        arms_single[name] = run_retrieval_arm(single, mode, "single-doc", trigger_book,
                                              doc_cap=cap)
    report["single"]["arms"] = {m: strip_chunks(r) for m, r in arms_single.items()}
    for m in ("auto", "llm", "off_cap4", "auto_cap4"):
        report["single"][f"off_vs_{m}"] = paired_summary(arms_single["off"], arms_single[m],
                                                         "off", m)
        # recall@1 配对（first_gold_rank==1）；任一臂出错的 case 剔除
        qids = [q for q in sorted(set(arms_single["off"]) & set(arms_single[m]))
                if not arms_single["off"][q].get("error") and not arms_single[m][q].get("error")]
        r1 = lambda rows, q: rows[q]["first_gold_rank"] == 1  # noqa: E731
        gained = sum(1 for q in qids if r1(arms_single[m], q) and not r1(arms_single["off"], q))
        lost = sum(1 for q in qids if r1(arms_single["off"], q) and not r1(arms_single[m], q))
        report["single"][f"off_vs_{m}"]["recall1_flips_gained"] = gained
        report["single"][f"off_vs_{m}"]["recall1_flips_lost"] = lost
        report["single"][f"off_vs_{m}"]["recall1_mcnemar_p"] = round(mcnemar_exact(lost, gained), 5)
        log(f"single off→{m}: {json.dumps(report['single'][f'off_vs_{m}'], ensure_ascii=False)}")

    # — C) 负例：baseline 检索（护栏复用）+ auto 触发率 —
    negs_base = run_retrieval_arm(negs, "off", "negatives", trigger_book)
    negs_auto = run_retrieval_arm(negs, "auto", "negatives", trigger_book)
    report["negatives"]["trigger_rate_auto"] = round(
        mean([1.0 if r["triggered"] else 0.0 for r in negs_auto.values()]), 3)
    report["negatives"]["rows_auto"] = strip_chunks(negs_auto)

    # — D) 护栏生成 A/B —
    if not args.skip_guard and not args.quick:
        guard_rows = []
        guard_rows += run_guard_ab(negs, negs_base, "negative")
        guard_rows += run_guard_ab(single, arms_single["off"], "positive")
        report["guard"]["rows"] = guard_rows
        report["guard"]["summary"] = guard_summary(guard_rows)
        log(f"guard: {json.dumps(report['guard']['summary'], ensure_ascii=False)}")

    json.dump(report, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"wrote {args.out}")


if __name__ == "__main__":
    main()
