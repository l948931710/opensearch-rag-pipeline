# -*- coding: utf-8 -*-
"""serverctl — 被测服务进程编排（与 SAE 生产同参：--workers 1 --timeout-keep-alive 65）。

env 注入原则：以「白名单拼装」为唯一事实——先剥离继承环境里所有 RAG_*，再铺场景 env，
杜绝宿主 .env / RAG_ENV 文件遮蔽（config env-loader 有 file-wins 语义）。
/proc/<pid>/status 周期采样 RSS 与线程数（S3 泄漏门的数据源）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_env(api_port: int, mock_port: int, overrides: Optional[Dict[str, str]] = None,
              db_password: str = "your_password") -> Dict[str, str]:
    """本地档场景 env：mock 后端 + 本地 MySQL + agent 开 + 限流关（S7 单独开）。"""
    base = {k: v for k, v in os.environ.items() if not k.startswith("RAG_")}
    base.pop("DASHSCOPE_API_KEY", None)
    mock = f"http://127.0.0.1:{mock_port}"
    env = {
        **base,
        "RAG_ENVIRONMENT": "development",
        # 一切「真」——真库 + 真 HTTP 打 mock。development 环境 simulate* 默认 True，会让
        # 限流器 _persist_rejected（检查 cfg.simulate_db）静默丢弃台账写、plain-RAG 走假答
        # 不打 mock。显式关掉四个 simulate 让配置与「真栈打替身外呼」的实际姿态一致。
        "RAG_SIMULATE": "false",
        "RAG_SIMULATE_API": "false",
        "RAG_SIMULATE_DB": "false",
        "RAG_SIMULATE_OPENSEARCH": "false",
        "RAG_AGENT_ENABLE": "true",
        "RAG_ONTOLOGY_ENABLE": "true",
        "RAG_SESSION_SIGNING_KEY": "stress-local-key",
        "RAG_REQUIRE_AUTH": "true",
        "RAG_DASHSCOPE_API_KEY": "stress-dummy-key",
        # 模型/嵌入基址 → mock（ModelGateway: {base}/chat/completions；
        # embedding: {base}/services/embeddings/...，base 含 /api/v1 时幂等）
        "RAG_LLM_API_BASE_URL": f"{mock}/compatible-mode/v1",
        "RAG_EMBEDDING_API_BASE_URL": f"{mock}/api/v1",
        # 检索走本地 OpenSearch 回退路径（HA3 endpoint 留空）→ mock /_search
        "RAG_OPENSEARCH_HOST": "127.0.0.1",
        "RAG_OPENSEARCH_PORT": str(mock_port),
        "RAG_OPENSEARCH_USE_SSL": "false",
        "RAG_OPENSEARCH_VERIFY_CERTS": "false",
        "RAG_OPENSEARCH_INDEX": "fuling_knowledge_v1",
        # 真库持久化（create_run fail-closed，必须可达）
        "RAG_RDS_HOST": "127.0.0.1",
        "RAG_RDS_PORT": "3306",
        "RAG_RDS_USER": "root",
        "RAG_RDS_PASSWORD": db_password,
        "RAG_RDS_DATABASE": "fuling_knowledge",
        "RAG_RDS_OPERATION_DATABASE": "fuling_operation",
        "RAG_RDS_ONTOLOGY_DATABASE": "fuling_ontology",
        # 压测基线：限流关（S7 场景显式开）；主命中 RDS 复核关
        # （mock 命中在权威表无行，fail-closed 会全丢——诚实范围段已记载）
        "RAG_RATE_LIMIT_ENABLE": "false",
        "RAG_MAIN_HIT_REVALIDATE": "false",
        "RAG_API_PORT": str(api_port),
    }
    env.update(overrides or {})
    # 值为 None 的覆盖 = 显式删除该变量
    return {k: v for k, v in env.items() if v is not None}


class ServerCtl:
    """uvicorn 子进程 + 就绪等待 + /proc 采样 + SIGKILL 钻演。"""

    def __init__(self, api_port: int, env: Dict[str, str], log_path: Path):
        self.api_port = api_port
        self.env = env
        self.log_path = log_path
        self.proc: Optional[subprocess.Popen] = None
        self.samples: List[Dict[str, float]] = []      # {t, rss_kb, threads}
        self._sampler: Optional[threading.Thread] = None
        self._sampling = False

    def start(self, wait_ready_s: float = 30.0) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_path, "ab")
        level = os.environ.get("STRESS_UVICORN_LOG_LEVEL", "warning")
        # sys.executable 而非裸 "python"：与 runner 同解释器（PATH 无 python 的环境照跑）
        cmd = [sys.executable, "-m", "uvicorn", "opensearch_pipeline.api:app",
               "--host", "127.0.0.1", "--port", str(self.api_port),
               "--workers", "1", "--timeout-keep-alive", "65", "--log-level", level]
        self.proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=self.env,
                                     stdout=logf, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + wait_ready_s
        url = f"http://127.0.0.1:{self.api_port}/api/version"
        last = ""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务进程提前退出 rc={self.proc.returncode}，"
                                   f"日志见 {self.log_path}")
            try:
                r = httpx.get(url, timeout=2.0)
                if r.status_code == 200:
                    return
                last = f"HTTP {r.status_code}"
            except Exception as e:   # noqa: BLE001
                last = str(e)[:80]
            time.sleep(0.4)
        raise RuntimeError(f"服务 {wait_ready_s}s 未就绪（最后错误: {last}），日志 {self.log_path}")

    # ── /proc 采样 ──────────────────────────────────────────────
    def start_sampling(self, interval_s: float = 2.0) -> None:
        self._sampling = True

        def _loop():
            t0 = time.monotonic()
            while self._sampling and self.proc and self.proc.poll() is None:
                s = self._read_proc()
                if s:
                    s["t"] = time.monotonic() - t0
                    self.samples.append(s)
                time.sleep(interval_s)

        self._sampler = threading.Thread(target=_loop, name="proc-sampler", daemon=True)
        self._sampler.start()

    def stop_sampling(self) -> None:
        self._sampling = False

    def _read_proc(self) -> Optional[Dict[str, float]]:
        try:
            text = Path(f"/proc/{self.proc.pid}/status").read_text()
        except OSError:
            return None
        rss_kb, threads = 0.0, 0.0
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = float(line.split()[1])
            elif line.startswith("Threads:"):
                threads = float(line.split()[1])
        return {"rss_kb": rss_kb, "threads": threads}

    # ── 停止/钻演 ───────────────────────────────────────────────
    def kill9(self) -> None:
        """S8：SIGKILL 模拟实例崩溃/SAE 强杀。"""
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        self.stop_sampling()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
