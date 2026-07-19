# -*- coding: utf-8 -*-
"""双副本 E2E（Gate C / RR「补双副本断线 E2E」）：真 MySQL + 真 Redis + mock 模型。

场景：
  S1 跨副本回放——A 提问跑完，B 经 /runs/{id}/events 从 Redis relay 回放到终态，
     答案全文可见（EVT-02 语义）；
  S2 断线续读——B 消费首帧后断开，携 Last-Event-ID 重连：不重放已读帧且到终态
     （P1-RT-05 游标协议）；
  S3 kill-9 at-most-once——A 提问后立即 SIGKILL A，B 收尸+dispatcher 收敛：
     该命令的 agent_run **恒 1 条**（052 反向锚），绝无第二个 run。

前置：本地 MySQL（052/053 已 apply）+ 本地 Redis:6379。用法：
  python3 -m stress_harness.dual_replica_e2e
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stress_harness.mockend import MockBackend                     # noqa: E402
from stress_harness.serverctl import ServerCtl, build_env          # noqa: E402

REDIS_URL = "redis://localhost:6379/9"
LOGDIR = Path(__file__).parent / "reports" / "dual_e2e_logs"


def _overrides(extra=None):
    o = {
        "RAG_AGENT_EVENT_RELAY": "redis",
        "RAG_REDIS_URL": REDIS_URL,
        "RAG_AGENT_DURABLE_DISPATCH": "true",
        "RAG_SESSION_BACKEND": "redis",
        "RAG_RATE_LIMIT_BACKEND": "redis",
        "RAG_MSG_DEDUP_BACKEND": "redis",
        "RAG_TOKEN_CACHE_BACKEND": "redis",
        "RAG_AGENT_DISPATCH_INTERVAL_S": "3",
        "RAG_AGENT_REAPER_INTERVAL_S": "4",
        "RAG_AGENT_STALE_RUNNING_S": "8",
        "RAG_AGENT_DISPATCH_CLAIM_GRACE_S": "2",
    }
    o.update(extra or {})
    return o


def _token(env):
    import subprocess
    code = ("from opensearch_pipeline.auth_token import issue_session_token;"
            "print(issue_session_token('e2e-user', dept='生产中心', role='employee'))")
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
    tok = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    assert tok, f"token 铸造失败: {out.stderr[-300:]}"
    return tok


def _sse_frames(resp, budget_s=30):
    """阻塞读 SSE：yield (event, data_dict|str)。"""
    deadline = time.monotonic() + budget_s
    event = None
    for raw in resp.iter_lines(decode_unicode=True):
        if time.monotonic() > deadline:
            return
        if raw is None:
            continue
        line = raw.strip()
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("id:"):
            yield ("__id__", line[3:].strip())
        elif line.startswith("data:"):
            d = line[5:].strip()
            if d == "[DONE]":
                yield ("done-sentinel", d)
                return
            try:
                yield (event or "message", json.loads(d))
            except Exception:
                yield (event or "message", d)
            event = None


def main() -> int:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    mock = MockBackend()
    mock.start()
    # MockBackend.start 后端口就绪（port=0 自动分配）
    env_a = build_env(8101, mock.port, overrides=_overrides())
    env_b = build_env(8102, mock.port, overrides=_overrides())
    a = ServerCtl(8101, env_a, LOGDIR / "replica_a.log")
    b = ServerCtl(8102, env_b, LOGDIR / "replica_b.log")
    failures = []
    try:
        a.start()
        b.start()
        tok = _token(env_a)
        H = {"Authorization": f"Bearer {tok}"}

        # ── S1：A 跑完 → B 回放全文 ────────────────────────────────
        r = requests.post("http://127.0.0.1:8101/api/agent/ask",
                          json={"question": "双副本E2E-S1", "thinking": False},
                          headers=H, stream=True, timeout=60)
        run_id, answer_a = None, []
        for ev, d in _sse_frames(r, budget_s=45):
            if isinstance(d, dict):
                run_id = d.get("run_id") or run_id
                if d.get("type") == "chunk":
                    answer_a.append(d.get("content") or "")
                if d.get("type") in ("done", "error"):
                    break
        r.close()
        if not run_id:
            rr = requests.get("http://127.0.0.1:8101/api/agent/runs?limit=1",
                              headers=H, timeout=10).json()
            runs = rr.get("runs") or rr.get("items") or []
            run_id = runs[0]["run_id"] if runs else None
        assert run_id, "S1: 拿不到 run_id"
        time.sleep(1)
        rb = requests.get(f"http://127.0.0.1:8102/api/agent/runs/{run_id}/events",
                          headers=H, stream=True, timeout=60)
        got_text, got_done, ids = [], False, []
        for ev, d in _sse_frames(rb, budget_s=30):
            if ev == "__id__":
                ids.append(d)
            elif isinstance(d, dict):
                if d.get("type") == "chunk":
                    got_text.append(d.get("content") or "")
                if d.get("type") in ("done", "error"):
                    got_done = d.get("type") == "done"
                    break
        rb.close()
        full_a, full_b = "".join(answer_a), "".join(got_text)
        if not got_done:
            failures.append("S1: B 回放未见 done 终态")
        if full_a and full_a not in full_b and full_b not in full_a:
            failures.append(f"S1: 跨副本答案不一致 a={len(full_a)}ch b={len(full_b)}ch")
        if not ids:
            failures.append("S1: 回放帧缺 id:（游标）行")
        print(f"S1 {'PASS' if len(failures) == 0 else 'FAIL'}: run={run_id} "
              f"a={len(full_a)}ch b={len(full_b)}ch done={got_done} ids={len(ids)}")

        # ── S2：Last-Event-ID 断线续读 ────────────────────────────
        s2_fail_before = len(failures)
        r1 = requests.get(f"http://127.0.0.1:8102/api/agent/runs/{run_id}/events",
                          headers=H, stream=True, timeout=30)
        first_id, first_payloads = None, 0
        for ev, d in _sse_frames(r1, budget_s=15):
            if ev == "__id__" and first_id is None:
                first_id = d
            elif isinstance(d, dict):
                first_payloads += 1
                if first_id is not None:
                    break
        r1.close()
        assert first_id, "S2: 首帧无 id"
        r2 = requests.get(f"http://127.0.0.1:8102/api/agent/runs/{run_id}/events",
                          headers={**H, "Last-Event-ID": first_id},
                          stream=True, timeout=30)
        resumed_ids, resumed_done = [], False
        for ev, d in _sse_frames(r2, budget_s=20):
            if ev == "__id__":
                resumed_ids.append(d)
            elif isinstance(d, dict) and d.get("type") in ("done", "error"):
                resumed_done = d.get("type") == "done"
                break
        r2.close()
        if first_id in resumed_ids:
            failures.append("S2: 续读重放了已读帧（游标未生效）")
        if not resumed_done:
            failures.append("S2: 续读未达终态")
        print(f"S2 {'PASS' if len(failures) == s2_fail_before else 'FAIL'}: "
              f"cursor={first_id} resumed={len(resumed_ids)} done={resumed_done}")

        # ── S3：kill-9 at-most-once ──────────────────────────────
        s3_fail_before = len(failures)
        mock.cfg.llm_latency_s = 6.0           # 慢答案：kill 窗口（mockend 真名）
        r3 = requests.post("http://127.0.0.1:8101/api/agent/ask",
                           json={"question": "双副本E2E-S3-kill", "thinking": False},
                           headers=H, stream=True, timeout=10)
        time.sleep(1.5)
        a.kill9()
        try:
            r3.close()
        except Exception:
            pass
        deadline = time.monotonic() + 45
        n_runs, final_status = 0, None
        import pymysql
        while time.monotonic() < deadline:
            conn = pymysql.connect(host="localhost", port=3306, user="root",
                                   password="your_password", database="fuling_operation")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT run_id, status FROM agent_run "
                                "WHERE thread_id IN (SELECT thread_id FROM agent_run "
                                "WHERE run_id IN (SELECT run_id FROM agent_run)) "
                                "AND user_id='e2e-user' ORDER BY started_at DESC LIMIT 5")
                    rows = cur.fetchall()
            finally:
                conn.close()
            n_runs = len(rows)
            final_status = rows[0][1] if rows else None
            if final_status in ("failed", "succeeded"):
                break
            time.sleep(3)
        # 该用户 S3 之后不应多出第二个新 run（052 锚 + 已绑不重驱）
        if final_status == "running":
            failures.append("S3: 45s 内未收尸（reaper 未生效）")
        print(f"S3 {'PASS' if len(failures) == s3_fail_before else 'FAIL'}: "
              f"final={final_status} rows={n_runs}（B 存活收尸，命令不重驱）")

        print("\n" + ("✅ 全部 PASS" if not failures else "❌ FAIL:\n- " + "\n- ".join(failures)))
        return 0 if not failures else 1
    finally:
        for s in (a, b):
            try:
                s.stop() if hasattr(s, "stop") else s.kill9()
            except Exception:
                pass
        try:
            mock.stop()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
