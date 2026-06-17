# -*- coding: utf-8 -*-
"""ops_monitor.py — Phase-3 single entry point for the standing health jobs.

Runs the built cross-store reconcilers (CS3 RDS↔HA3, CS4 OSS↔RDS) + the OBS-5 QA rollup in one
invocation, each fail-open, each firing its own OBS-4 ops alert on drift/breach. Designed to be the
ONE command a scheduler calls — a laptop crontab, a DataWorks shell node, or a SAE timer:

    python -m opensearch_pipeline.ops_monitor            # all jobs, alerts on
    python -m opensearch_pipeline.ops_monitor --no-alert # dry run, no alerts
    python -m opensearch_pipeline.ops_monitor --only reconcile   # subset

Exit code = worst of the sub-jobs (0 ok / 2 drift-or-breach / 3 error). Simulate-safe (each sub-job
no-ops under simulate). This module adds NO new behavior — it only sequences the existing entry
points so scheduling is a one-liner regardless of where it runs.

NOTE: scheduling on DataWorks additionally requires the package + cred injection to be available on
the resource group (not yet set up — the project has only a root no-op and runs laptop-driven). Until
that deployment exists, run this from an environment that already has the code + .env (the laptop),
e.g. a crontab line. See docs for the runbook.
"""
from __future__ import annotations

import argparse
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_JOBS = ("reconcile_ha3", "reconcile_oss", "qa_rollup")


def run_all(*, alert: bool = True, only: Optional[List[str]] = None) -> dict:
    """Run the selected health jobs. Returns {job: report}. Never raises (each sub-job fail-open)."""
    sel = set(only) if only else set(_JOBS)
    out: dict = {}

    if "reconcile_ha3" in sel:
        from opensearch_pipeline.reconcile import run_parity_check
        out["reconcile_ha3"] = run_parity_check(alert=alert)
    if "reconcile_oss" in sel:
        from opensearch_pipeline.reconcile import run_oss_parity_check
        out["reconcile_oss"] = run_oss_parity_check(alert=alert)
    if "qa_rollup" in sel:
        from opensearch_pipeline.qa_rollup import run_rollup
        out["qa_rollup"] = run_rollup(alert=alert)
    return out


def _job_exit(job: str, report: dict) -> int:
    """0 ok/skipped · 2 drift|breach · 3 error/incomplete."""
    if report.get("skipped"):
        return 0
    if report.get("error") or report.get("complete") is False:
        return 3
    if job == "qa_rollup":
        return 0 if report.get("slo_ok", 1) == 1 else 2
    return 0 if report.get("ok") else 2


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="standing health jobs: reconcilers + QA rollup")
    ap.add_argument("--no-alert", action="store_true", help="do not fire OBS-4 ops alerts")
    ap.add_argument("--only", nargs="+", choices=list(_JOBS), default=None,
                    help="run only these jobs (default: all)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    reports = run_all(alert=not args.no_alert, only=args.only)

    if args.json:
        import json
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        for job, rep in reports.items():
            tag = ("skipped" if rep.get("skipped")
                   else "ERROR" if rep.get("error")
                   else "ok" if _job_exit(job, rep) == 0 else "ALERT")
            print(f"[ops_monitor] {job}: {tag}")
    return max((_job_exit(j, r) for j, r in reports.items()), default=0)


if __name__ == "__main__":
    import sys
    sys.exit(main())
