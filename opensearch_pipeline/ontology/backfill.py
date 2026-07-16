# -*- coding: utf-8 -*-
"""
backfill.py — 历史别名回填 worker（P0 PR9；S1 四合法落库路径之二的离线 auto-activate 点）。

职责（P0-P1 落地细化 §4.2）：遍历快照/观测导出（历史 Excel、U盘导出、单据编号观测；
U8 同步库待 go/no-go ① 后经 U8SnapshotSource 接入）→ 批量产候选进 steward 工作台；
幂等（按 (namespace, norm_value) 去重）；低优先级、可断点续跑；每轮输出
resolution_coverage（M2 门指标，单调增长）。

与播种共用同一决策核心（seeding.seed_snapshot——skip-active 幂等 / may_auto_activate
唯一 auto 入口 / 低置信入 case / case 愈合对账），差异仅两个开关：
- **mention 观测语义（默认）**：输入是流程中出现的编号观测，不是主数据档案——
  无候选**不铸新对象**，一律入 resolution_case 聚合观测；铸对象权留给主数据播种与
  steward 工作台（防脏观测繁殖重复品）。``--mode master`` 才回到播种语义
  （U8 档案类快照的增量回灌）。
- evidence.source='backfill'（工作台证据快照可溯源到回填轮）。

纪律：
- **--dry-run 默认**；``--commit`` 额外要求 ``RAG_ONTOLOGY_BACKFILL_ENABLE=true`` 第二道闸
  （retention 同款 staged rollout：DataWorks 节点 stage-1 恒 dry-run 观察，stage-2 人工
  同时翻 DRY_RUN 与该 env——单开关误触不成灾）；
- 真实回填（staging/prod 数据）与真实播种同一道组织门：go/no-go ①③④ 签字前禁止；
- 节点部署 user-gated（dataworks_nodes/ontology_backfill_node.py 打包/建节点须用户点头）；
- 跨环境误指由 GuardedDBConnection / env_guard 兜底（非生产标签写生产目标在连接层拦）。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

from opensearch_pipeline.ontology.seeding import CsvSnapshotSource, SeedReport, seed_snapshot

ENABLE_ENV = "RAG_ONTOLOGY_BACKFILL_ENABLE"
# PR-E（P0-07）：生产回填第三道闸——当日 date-bound token（Sam 专属，gate 签字后才发）
PROD_ACK_ENV = "RAG_ONTOLOGY_BACKFILL_PROD_ACK"


def _prod_ack_valid(source_fp: Optional[str]) -> bool:
    """`<op>:<YYYY-MM-DD>:<docset_hash>` 三段全要（2026-07-11 重审计 §2：此前只有
    <op>:<date>，同日 token 可复用于任意 CSV——对齐 RAG_ALLOW_UNFROZEN_RECHUNK 的
    docset-bound 纪律）。docset_hash = 快照文件 sha256 的前缀（≥8 位 hex），须与
    本轮实读文件一致；指纹算不出（非文件源）→ 恒 False（fail-closed）。"""
    raw = os.environ.get(PROD_ACK_ENV, "").strip()
    parts = raw.split(":")
    if len(parts) != 3:
        return False
    op, date_s, docset = (p.strip() for p in parts)
    from datetime import date
    if not op or date_s != date.today().isoformat():
        return False
    docset = docset.lower()
    if len(docset) < 8 or any(c not in "0123456789abcdef" for c in docset):
        return False
    return bool(source_fp) and source_fp.lower().startswith(docset)
_P0_OBJECT_TYPES = ("product", "sku", "mold", "material")

__all__ = ["ENABLE_ENV", "backfill_snapshot", "main"]


def backfill_snapshot(store, source: Any, *, dry_run: bool = True,
                      tau_table=None, limit: Optional[int] = None,
                      mint_new: bool = False,
                      population_authoritative: Optional[bool] = None) -> SeedReport:
    """跑一轮回填。默认 mention 观测语义（mint_new=False）；决策核心与播种同源。
    P0-05（批次4）：mention 回填**不登记** population 分母（局部观测导出会把 master
    全量分母覆写虚高）；已知全量的源可显式 population_authoritative=True。"""
    return seed_snapshot(store, source, dry_run=dry_run, tau_table=tau_table,
                         limit=limit, mint_new=mint_new, evidence_source="backfill",
                         population_authoritative=population_authoritative)


def _print_coverage(store) -> None:
    """轮末覆盖率播报（M2 门指标）。纯播报：失败不影响退出码。
    P0-05（批次4）：逐行打印分母口径——payload 一直自带 denominator/anomaly 字段，
    此前播报把口径丢掉，approx 处置率会被当成真实覆盖率读。"""
    try:
        total = store.coverage()
        cov = total.get("resolution_coverage")
        if total.get("denominator") == "population":
            basis = f"分母=population({total.get('population_records')})"
        else:
            basis = "分母=approx（处置率口径，非真实覆盖率——无全量源快照）"
        anomaly = f" ⚠️ {total['anomaly']}" if total.get("anomaly") else ""
        print(f"resolution_coverage(总): {cov:.4f}" if cov is not None
              else "resolution_coverage(总): —（尚无观测）",
              f"| active {total.get('active_identifiers')} · open {total.get('open_cases')}"
              f" · auto {total.get('auto_active')} [{basis}]{anomaly}")
        for ot in _P0_OBJECT_TYPES:
            c = store.coverage(ot)
            if not (c.get("active_identifiers") or c.get("open_cases")):
                continue
            cv = c.get("resolution_coverage")
            print(f"  · {ot}: {cv:.4f}" if cv is not None else f"  · {ot}: —",
                  f"(active {c.get('active_identifiers')} / open {c.get('open_cases')}"
                  f" · {c.get('denominator', 'approx')} 口径)")
    except Exception as e:   # noqa: BLE001 — 播报失败不掀翻作业
        print(f"（覆盖率播报失败：{e}）")


def main(argv=None, *, store=None, source=None) -> int:
    p = argparse.ArgumentParser(
        description="本体历史别名回填（观测/快照 → 工作台候选 + 高置信唯一 auto 别名 + case 愈合）")
    p.add_argument("csv_path", nargs="?", help="CSV 快照/观测导出（列契约见 seeding.CsvSnapshotSource）")
    p.add_argument("--commit", action="store_true",
                   help=f"实际写库（缺省 dry-run；另需 {ENABLE_ENV}=true 第二道闸）")
    p.add_argument("--dry-run", action="store_true", help="显式 dry-run（与 --commit 互斥）")
    p.add_argument("--mode", choices=("mention", "master"), default="mention",
                   help="mention=观测语义不铸对象（默认）；master=主数据语义可铸（档案类快照）")
    p.add_argument("--limit", type=int, default=None, help="最多处理前 N 条（安全阀，断点续跑用）")
    p.add_argument("--show-actions", type=int, default=20, help="打印前 N 条动作明细")
    p.add_argument("--population-authoritative", action="store_true",
                   help="声明本源为该 namespace 的**全量**记录，登记 population 分母"
                        "（P0-05：mention 观测默认不登记——局部导出会覆写 master 分母）")
    args = p.parse_args(argv)

    if args.commit and args.dry_run:
        p.error("--commit 与 --dry-run 互斥")
    dry = not args.commit

    if args.commit and os.environ.get(ENABLE_ENV, "").strip().lower() != "true":
        print(f"❌ --commit 需要 {ENABLE_ENV}=true（第二道闸，staged rollout stage-2 才开；"
              "真实回填另须 go/no-go ①③④ 签字）。拒绝执行。", file=sys.stderr)
        return 2

    if source is None:
        if not args.csv_path:
            p.error("缺 csv_path（或经 main(source=...) 注入）")
        source = CsvSnapshotSource(args.csv_path)

    # PR-E（P0-07）：prod 物理指纹硬拒——对齐 ontology_seed.py。此前 backfill 只有
    # DRY_RUN 常量 + ENABLE env 双闸（改源码/设 env 即过 = 可伪造）；现在生产 RDS
    # 的 --commit 还须**当日 + docset-bound** PROD_ACK_ENV=<op>:<YYYY-MM-DD>:<docset_hash>
    # （重审计 §2：第三段 = 快照 sha256 前缀，同日 token 不得复用于另一批数据；token
    # 只能 Sam 设，gate ①③④ 签字后才发）。dry-run 不受限（只读预览）。
    if args.commit:
        from opensearch_pipeline.config import get_config as _gc
        from opensearch_pipeline.config import is_prod_target as _ipt
        from opensearch_pipeline.ontology.seeding import source_fingerprint
        _cfg = _gc()
        if _ipt("rds", _cfg.rds.host):
            _fp = source_fingerprint(source)
            if not _prod_ack_valid(_fp):
                print(f"❌ 目标是生产 RDS——真实回填须 gate ①③④ 签字后由 Sam 设当日 "
                      f"{PROD_ACK_ENV}=<op>:<YYYY-MM-DD>:<docset_hash>"
                      f"（docset_hash=快照 sha256 前 12 位："
                      f"{(_fp or '<指纹不可得>')[:12]}）。拒绝执行。", file=sys.stderr)
                return 2

    if store is None:
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.ontology.store import RDSOntologyStore
        cfg = get_config()
        print(f"目标库：{cfg.rds.host}/{cfg.rds.ontology_database}"
              f"{'（DRY-RUN 零写库）' if dry else ''}")
        store = RDSOntologyStore()

    report = backfill_snapshot(store, source, dry_run=dry,
                               limit=args.limit, mint_new=(args.mode == "master"),
                               population_authoritative=(True if args.population_authoritative
                                                         else None))
    print(report.summary())
    for a in report.actions[: max(0, args.show_actions)]:
        print("  ·", a)
    if len(report.actions) > args.show_actions:
        print(f"  …（其余 {len(report.actions) - args.show_actions} 条动作省略）")
    _print_coverage(store)
    if dry:
        print(f"确认无误后加 --commit + {ENABLE_ENV}=true 实际写库"
              "（staging 先行；真实回填须 gate 签字）。")
    return 2 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
