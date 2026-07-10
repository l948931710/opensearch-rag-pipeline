# -*- coding: utf-8 -*-
"""
ontology_seed.py — 本体播种 CLI（P0 PR5）。**--dry-run 默认**；--commit 才写库。

用法：
  PYTHONPATH=. RAG_ENV=local python scripts/ontology_seed.py tests/fixtures/ontology_seed_sample.csv
  PYTHONPATH=. RAG_ENV=local python scripts/ontology_seed.py <csv> --commit [--governance-seeds]

纪律（docs/ontology_p0_plan_2026-07-10.md Phase 0）：
- **真实播种（staging/prod 数据）在 go/no-go ①③④ 签字前禁止**——本 CLI 只能技术上兜住
  prod（下面的硬拒），组织门靠签字表；
- prod RDS 目标 → **无条件拒绝 --commit**（P0 无 prod 播种路径；将来经 prod_access 专门开）；
- 写路径另有 GuardedDBConnection 兜底（RAG_READONLY / 非生产→生产 在连接层拦）。
"""
import argparse
import sys


def main(argv=None, *, store=None, source=None) -> int:
    p = argparse.ArgumentParser(description="本体播种（快照 → canonical 对象 + 别名/case）")
    p.add_argument("csv_path", nargs="?", help="CSV 快照（CsvSnapshotSource 列契约见 seeding.py）")
    p.add_argument("--commit", action="store_true", help="实际写库（缺省 dry-run 只预览）")
    p.add_argument("--dry-run", action="store_true", help="显式 dry-run（与 --commit 互斥）")
    p.add_argument("--limit", type=int, default=None, help="最多处理前 N 条（安全阀）")
    p.add_argument("--governance-seeds", action="store_true",
                   help="顺带 upsert attribute_source/stewardship 代码内种子（dry-run 下跳过）")
    p.add_argument("--show-actions", type=int, default=20, help="打印前 N 条动作明细")
    args = p.parse_args(argv)

    if args.commit and args.dry_run:
        p.error("--commit 与 --dry-run 互斥")
    dry = not args.commit

    from opensearch_pipeline.ontology.seeding import CsvSnapshotSource, seed_snapshot

    if source is None:
        if not args.csv_path:
            p.error("缺 csv_path（或经 main(source=...) 注入）")
        source = CsvSnapshotSource(args.csv_path)

    if store is None:
        from opensearch_pipeline.config import get_config, is_prod_target
        from opensearch_pipeline.ontology.store import RDSOntologyStore
        cfg = get_config()
        if args.commit and is_prod_target("rds", cfg.rds.host):
            print("❌ 目标是生产 RDS——P0 无 prod 播种路径（真实播种须 go/no-go ①③④ 签字，"
                  "且届时经 prod_access 专门通道）。拒绝执行。", file=sys.stderr)
            return 2
        print(f"目标库：{cfg.rds.host}/{cfg.rds.ontology_database}"
              f"{'（DRY-RUN 零写库）' if dry else ''}")
        store = RDSOntologyStore()

    if args.governance_seeds:
        if dry:
            print("（dry-run：跳过 governance seeds upsert）")
        else:
            from opensearch_pipeline.ontology import attribute_source, stewardship
            n1 = attribute_source.ensure_seeds(store)
            n2 = stewardship.ensure_seeds(store)
            print(f"governance seeds：attribute_source {n1} 行 · stewardship {n2} 行")

    report = seed_snapshot(store, source, dry_run=dry, limit=args.limit)
    print(report.summary())
    for a in report.actions[: max(0, args.show_actions)]:
        print("  ·", a)
    if len(report.actions) > args.show_actions:
        print(f"  …（其余 {len(report.actions) - args.show_actions} 条动作省略）")
    if dry:
        print("确认无误后加 --commit 实际写库（staging 先行；真实播种须 gate 签字）。")
    return 2 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
