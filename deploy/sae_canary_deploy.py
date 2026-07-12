# -*- coding: utf-8 -*-
"""
sae_canary_deploy.py — SAE 分批灰度发布 + 健康验证 + 失败自动回滚（P0-6 重评审计）。

审计缺口：无 deploy workflow / 无 canary / 无自动 rollback（全委托 SAE 原生但没人编排）。
本脚本把「发布 → 灰度批 → 版本指纹+就绪双验证 → 确认放量 → 失败回滚」编排成一条
可审计的命令流。**默认 dry-run 只打印将执行的 aliyun 命令**；真执行须 --commit 且
逐阶段人工确认（部署是 Sam 逐次授权的动作——本脚本存在 ≠ 允许自动部署）。

前置（两道门，缺一不发）：
  1. `make release-gate` 绿（脚本只提醒不代跑——评测门有自己的冻结纪律）；
  2. Sam 明确要求本次部署。

用法：
  python deploy/sae_canary_deploy.py --image <ACR镜像:tag> --git-sha <短SHA>      # dry-run
  python deploy/sae_canary_deploy.py --image ... --git-sha ... --commit           # 真执行

依赖：aliyun CLI 已配置（RAM 需 sae:DeployApplication/DescribeChangeOrder/
AbortAndRollbackChangeOrder）。⚠️ 参数名以 `aliyun sae help` 为准——SAE OpenAPI 偶有
版本差异，首次使用先 dry-run 核对命令行。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

DEFAULT_BASE_URL = os.environ.get("SAE_VERIFY_BASE_URL", "http://120.55.69.9:8000")
DEFAULT_APP_ID = os.environ.get("SAE_APP_ID", "")


def _run_aliyun(args: list, *, commit: bool) -> dict:
    cmd = ["aliyun", "sae", *args]
    print(f"  $ {' '.join(cmd)}")
    if not commit:
        return {}
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"aliyun 命令失败: {out.stderr.strip()[:500]}")
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"raw": out.stdout}


def _http_json(url: str, timeout: int = 8) -> tuple:
    """(status_code, body dict)；网络异常 → (0, {})。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:   # noqa: S310 — 固定内部探针
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as e:   # noqa: BLE001
        print(f"    探针异常: {e}")
        return 0, {}


def verify_health(base_url: str, expect_sha: str, *, rounds: int = 5,
                  interval_s: int = 10) -> bool:
    """灰度批健康双验证：/api/version.git_commit == 目标 SHA 且 /api/ready 200，
    连续 rounds 轮全过才算过（单次抖动不作数）。"""
    ok_rounds = 0
    for i in range(rounds * 3):                     # 容忍前几轮打到旧实例（SLB 轮转）
        st_v, ver = _http_json(f"{base_url}/api/version")
        st_r, _ready = _http_json(f"{base_url}/api/ready")
        sha = str(ver.get("git_commit", ""))
        hit = st_v == 200 and st_r == 200 and (sha.startswith(expect_sha) or expect_sha in sha)
        print(f"    round {i + 1}: version={st_v}({sha or '-'}) ready={st_r} "
              f"{'✓' if hit else '·'}")
        ok_rounds = ok_rounds + 1 if hit else 0
        if ok_rounds >= rounds:
            return True
        time.sleep(interval_s)
    return False


def wait_change_order(order_id: str, *, commit: bool, timeout_s: int = 900) -> str:
    """轮询发布单状态；返回终态（成功 2 / 失败 3 / 中止 6 / 超时 'timeout'）。"""
    if not commit or not order_id:
        print("  [dry-run] 跳过发布单轮询")
        return "2"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        data = _run_aliyun(["DescribeChangeOrder", "--ChangeOrderId", order_id],
                           commit=True)
        status = str(((data.get("Data") or {}).get("Status")) or "")
        print(f"    发布单 {order_id} status={status}")
        if status in ("2", "3", "6", "10"):          # 2=成功 3=失败 6=中止 10=系统失败
            return status
        time.sleep(15)
    return "timeout"


def rollback(app_id: str, order_id: str, *, commit: bool) -> None:
    """失败自动回滚：先中止在途发布单并回滚（AbortAndRollbackChangeOrder）。"""
    print("⛑  触发自动回滚 …")
    _run_aliyun(["AbortAndRollbackChangeOrder", "--ChangeOrderId", order_id],
                commit=commit)
    print("  回滚指令已发出——用 /api/version 确认线上 SHA 已回到旧版，再排查失败原因。")


def _confirm(msg: str, *, yes: bool) -> None:
    if yes:
        return
    ans = input(f"{msg} [yes/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("已取消。")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="SAE 分批灰度发布 + 验证 + 自动回滚")
    ap.add_argument("--app-id", default=DEFAULT_APP_ID, help="SAE AppId（或 SAE_APP_ID env）")
    ap.add_argument("--image", required=True, help="ACR 镜像地址:tag（多阶段 Dockerfile 产物）")
    ap.add_argument("--git-sha", required=True, help="目标短 SHA（须与镜像 --build-arg GIT_SHA 一致）")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help="健康验证入口")
    ap.add_argument("--min-ready", default="1", help="MinReadyInstances（灰度批保底在线数）")
    ap.add_argument("--batch-wait", default="300", help="BatchWaitTime 秒（批间人工观察窗）")
    ap.add_argument("--commit", action="store_true", help="真执行（默认 dry-run 只打印命令）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（仅限演练）")
    args = ap.parse_args()

    if args.commit and not args.app_id:
        ap.error("--commit 需要 --app-id / SAE_APP_ID")

    mode = "COMMIT（真发布）" if args.commit else "DRY-RUN（只打印命令）"
    print(f"══ SAE 灰度发布 ══  app={args.app_id or '<未设>'}  image={args.image}\n"
          f"   sha={args.git_sha}  verify={args.base_url}  模式={mode}\n")
    print("① 前置门自查：make release-gate 是否为绿？本次部署是否 Sam 明确要求？")
    _confirm("   两问都为「是」才继续。继续？", yes=args.yes or not args.commit)

    print("\n② 分批发布（灰度批先行；BatchWaitTime 为批间观察窗，期间可中止）")
    data = _run_aliyun([
        "DeployApplication", "--AppId", args.app_id or "<APP_ID>",
        "--ImageUrl", args.image,
        "--MinReadyInstances", str(args.min_ready),
        "--BatchWaitTime", str(args.batch_wait),
        "--ChangeOrderDesc", f"canary deploy {args.git_sha} via sae_canary_deploy.py",
    ], commit=args.commit)
    order_id = str(((data.get("Data") or {}).get("ChangeOrderId")) or "")
    if args.commit and not order_id:
        print("❌ 未拿到 ChangeOrderId，中止（无法追踪/回滚的发布不做）")
        sys.exit(2)

    print("\n③ 灰度批健康验证（版本指纹 + 深就绪，连续 5 轮）")
    if args.commit:
        time.sleep(30)                               # 等灰度批实例起来再验
        if not verify_health(args.base_url, args.git_sha):
            rollback(args.app_id, order_id, commit=True)
            sys.exit(3)
        print("  ✓ 灰度批验证通过")
    else:
        print("  [dry-run] 将轮询 /api/version（git_commit==目标 SHA）+ /api/ready==200 ×5")

    print("\n④ 放量（等待发布单余批完成；失败即回滚）")
    final = wait_change_order(order_id, commit=args.commit)
    if final != "2":
        print(f"❌ 发布单终态异常（{final}）")
        if args.commit:
            rollback(args.app_id, order_id, commit=True)
        sys.exit(4)

    print("\n⑤ 全量后终验")
    if args.commit and not verify_health(args.base_url, args.git_sha, rounds=3):
        rollback(args.app_id, order_id, commit=True)
        sys.exit(5)
    print("✅ 发布完成且验证通过。别忘了：SAE env 变量若有新 flag，本脚本不改 env——"
          "在控制台核对后再灰度开 flag。")


if __name__ == "__main__":
    main()
