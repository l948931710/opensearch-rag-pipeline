# -*- coding: utf-8 -*-
"""
rate_limiter.py — serving 公网防刷（进程内四层准入）

背景：SAE 公网 EIP（HTTP 测试期形态）已被扫描器探到端口，/api/ask 匿名直打
照样调 DashScope（embedding + LLM + rerank）= 刷百炼账单。本模块在应用层
做四层准入（Dockerfile 钉死 --workers 1，进程内计数即权威，无需 Redis）：

  1. 每用户限频 + 日配额      —— 已登录（Bearer 令牌验证过）按 user_id 计
  2. 匿名按 IP 严格限额        —— 无令牌按客户端 IP 计（阈值远低于登录用户）
  3. 深思（thinking）日配额    —— 仅登录用户可用；~8x token 计费，单独限量
  4. 全局日熔断               —— 全服务每日 LLM 问答总量帽，护住百炼账单底线

设计要点：
  - 框架无关（不 import FastAPI）：api.py 把 Denial 翻成 HTTPException。
  - 线程安全：FastAPI 把 def 处理器放线程池并发执行，所有计数在一把锁内
    "检查全部通过 → 原子计入"，深思配额耗尽的拒绝不消耗常规预算（关掉
    深思可立即重问）。
  - 日界 = 北京时间（UTC+8 固定偏移，中国无夏令时）；SAE 容器时区是 UTC，
    不能用本地日期。
  - 客户端 IP 自适应两种部署形态（见 resolve_client_ip）。
  - 默认启用条件 = not simulate_api：模拟模式不打真实 API、没有账单可保护
    （单测/make sim 天然不受限）。RAG_RATE_LIMIT_ENABLE 显式设置时优先。
  - fail open：限流器自身异常由调用方兜住放行——辅助防护绝不拖垮回答主链路。

环境变量（全部可选，代码默认值即生产推荐值；0 或负数 = 关闭该层，
仅 RAG_THINKING_DAILY_QUOTA=0 例外 = 深思功能整体关闭）：
  RAG_RATE_LIMIT_ENABLE       总开关（未设时 = not simulate_api）
  RAG_RATE_USER_PER_MIN=6     登录用户 每分钟提问上限（滑动窗）
  RAG_RATE_USER_PER_DAY=300   登录用户 每日提问上限
  RAG_RATE_ANON_PER_MIN=3     匿名 IP  每分钟提问上限
  RAG_RATE_ANON_PER_DAY=30    匿名 IP  每日提问上限
  RAG_RATE_AUX_PER_MIN=30     辅助端点（auth/feedback/重签/历史/热门）每分钟上限
  RAG_THINKING_DAILY_QUOTA=10 登录用户 深思每日配额（匿名一律拒绝）
  RAG_GLOBAL_DAILY_LLM_CAP=2000  全局每日问答熔断阈值（/api/ask + /api/ask/stream）

注意：全局熔断按"准入的问答请求"计数（含最终 NO_RESULT 的），因为 embedding/
HA3/rerank 开销在准入后即发生——这正是熔断要兜的上游调用总量。

可观测性（盲区审计 P1-1，2026-07-05）：被拒请求在 log_qa_session 之前就被拒，
从不落 qa_session_log——此前触顶=静默全站宕机且 SLO 反而全绿。现在：
  * 触顶边沿（admit 侧 new_cnt==cap）与 global_cap 拒绝侧（进程重启/中途调低 cap
    不经过边沿）各有一个每日至多一次的 critical 运维告警（OBS-4 send_ops_alert）；
  * 全部拒绝按 (北京日, 原因) 聚合，~60s 批量 UPSERT 到 qa_admission_reject
    （schema/017），夜间 qa_rollup 读它把 offered vs admitted 补进 SLO 判定。
"""

import ipaddress
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 北京时间固定偏移（中国无夏令时，安全）
_BJ_OFFSET_S = 8 * 3600
_MINUTE_WINDOW_S = 60.0
# 同一 key 的拒绝日志节流间隔（扫描器打满限额时避免刷爆日志）
_WARN_THROTTLE_S = 300.0
# 计数字典软上限：超过即清理过期项（扫描器轮换 IP 不至于撑爆内存）
_PRUNE_THRESHOLD = 20000
# perf#94：全量重建式清理的节流间隔——超阈值期间不再每个放行请求都重建，
# 代价是过期条目最多多存活 ~30s（阈值以下路径零变化，设计内让步）
_PRUNE_INTERVAL_S = 30.0
# 拒绝聚合落库间隔（盲区审计 P1-1）：60s 一批 → 拒绝风暴期间 RDS 写放大有界（≤1 批/分钟）
_REJECT_FLUSH_INTERVAL_S = 60.0
# tests 置 False：触顶告警/拒绝落库同步直调，便于断言；生产恒 True（热路径零阻塞）
_DISPATCH_ASYNC = True


def _dispatch_cap_alert(cap: int) -> None:
    """全局日熔断触顶 → OBS-4 运维告警（盲区审计 P1-1：此前触顶只有 logger.error，
    无人被通知，且被拒请求不落 qa_session_log——全站宕机日在 SLO 看板上反而全绿）。
    fail-open：告警发送失败绝不影响限流主路径。"""
    def _send() -> None:
        try:
            from opensearch_pipeline.alerting import send_ops_alert
            send_ops_alert(
                "全局日熔断触顶：问答服务对外全拒",
                f"今日准入问答量已达 RAG_GLOBAL_DAILY_LLM_CAP={cap}，"
                "此后所有 /api/ask 将被 503 拒绝至北京时间次日零点。\n\n"
                "- 正常业务增长 → 调高上限并重启 serving；\n"
                "- 扫描/重试风暴 → 排查来源 IP（限流拒绝日志 reason=global_cap）。",
                severity="critical", dedup_key="global-cap")
        except Exception:   # noqa: BLE001
            logger.warning("熔断触顶告警发送失败（fail-open）", exc_info=True)
    if _DISPATCH_ASYNC:
        threading.Thread(target=_send, daemon=True, name="cap-alert").start()
    else:
        _send()


def _persist_rejected(snapshot: Dict[Tuple[str, str], int]) -> bool:
    """拒绝聚合批量落库（qa_admission_reject，schema/017）：(北京日, 原因) → 计数累加。
    夜间 qa_rollup 读它把「offered vs admitted」补进 SLO——否则被拒请求在唯一的
    serving 指标里不存在。fail-open：模拟模式丢弃即成功；表未建/连接失败返回 False
    让调用方把计数并回内存，下一批重试。"""
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return True
        op_db = cfg.rds.operation_database
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                for (day, reason), n in snapshot.items():
                    cur.execute(
                        f"INSERT INTO {op_db}.qa_admission_reject (stat_date, reason, reject_count)"
                        " VALUES (%s, %s, %s)"
                        " ON DUPLICATE KEY UPDATE reject_count = reject_count + VALUES(reject_count)",
                        (day, reason, int(n)))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001
        logger.warning("qa_admission_reject 落库失败（fail-open，计数并回内存待重试）: %s", e)
        return False


def _beijing_day(now: float) -> str:
    """北京时间日期串 YYYY-MM-DD（日配额/熔断的日界）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now + _BJ_OFFSET_S))


def _secs_to_beijing_midnight(now: float) -> int:
    """距北京时间次日零点的秒数（日配额类拒绝的 Retry-After）。"""
    return int(86400 - ((now + _BJ_OFFSET_S) % 86400)) + 1


def _try_ip(text: str):
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def resolve_client_ip(client_host: Optional[str], xff: Optional[str]) -> str:
    """解析真实客户端 IP，自适应两种部署形态（零配置）：

    - 当前形态（EIP 直绑实例）：socket 对端就是真实公网客户端 →
      client_host 是公网 IP，直接用；此时 X-Forwarded-For 是客户端
      可伪造的请求头（没有可信代理会改写它），必须忽略，否则攻击者
      每请求换个假 XFF 就能旋转限流 key。
    - 备案后形态（SAE 绑域名 / 经 SLB 网关）：socket 对端是网关的
      私网地址 → 取 XFF 最右一跳（最近的可信代理追加的真实客户端），
      左侧条目仍是客户端可伪造的，不可用。
    """
    host = (client_host or "").strip()
    parsed = _try_ip(host)
    if parsed is not None and parsed.is_global:
        return host
    if xff:
        for part in reversed(xff.split(",")):
            cand = part.strip()
            if cand and _try_ip(cand) is not None:
                return cand
    return host or "unknown"


@dataclass
class Denial:
    """一次被拒的准入。api.py 翻译成 HTTPException(status_code, message) + Retry-After。"""
    status_code: int        # 429 限频/配额 | 403 策略拒绝 | 503 全局熔断
    message: str            # 用户可见中文文案（小程序错误卡直接展示）
    retry_after: int = 0    # 秒；0 = 不带 Retry-After 头
    reason: str = ""        # 机器码，仅用于日志/排查


@dataclass
class Limits:
    """生效限额快照（懒加载自环境变量，见模块 docstring）。"""
    enabled: bool = True
    user_per_min: int = 6
    user_per_day: int = 300
    anon_per_min: int = 3
    anon_per_day: int = 30
    aux_per_min: int = 30
    thinking_daily_quota: int = 10
    global_daily_llm_cap: int = 2000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，使用默认值 %d", name, raw, default)
        return default


def _load_limits() -> Limits:
    """读取生效限额。先触发 get_config()——确保 RAG_ENV 的 .env 覆盖层已载入
    os.environ（dotenv override=True），再读环境变量，否则本地起服会读到裸 shell 值。
    """
    simulate_api = True
    try:
        from opensearch_pipeline.config import get_config
        simulate_api = bool(getattr(get_config(), "simulate_api", True))
    except Exception:
        # 配置异常时宁可启用限流（防护开着不影响正常回答，限额本身宽松）
        simulate_api = False
        logger.warning("加载配置失败，限流按非模拟模式启用", exc_info=True)

    raw_enable = os.environ.get("RAG_RATE_LIMIT_ENABLE", "").strip().lower()
    if raw_enable in ("true", "1", "yes", "on"):
        enabled = True
    elif raw_enable in ("false", "0", "no", "off"):
        enabled = False
    else:
        enabled = not simulate_api

    return Limits(
        enabled=enabled,
        user_per_min=_env_int("RAG_RATE_USER_PER_MIN", 6),
        user_per_day=_env_int("RAG_RATE_USER_PER_DAY", 300),
        anon_per_min=_env_int("RAG_RATE_ANON_PER_MIN", 3),
        anon_per_day=_env_int("RAG_RATE_ANON_PER_DAY", 30),
        aux_per_min=_env_int("RAG_RATE_AUX_PER_MIN", 30),
        thinking_daily_quota=_env_int("RAG_THINKING_DAILY_QUOTA", 10),
        global_daily_llm_cap=_env_int("RAG_GLOBAL_DAILY_LLM_CAP", 2000),
    )


class ServingRateLimiter:
    """进程内限流器（workers=1 下即全局权威）。所有 admit_* 返回 None=放行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 时钟钩子（测试注入假时钟推进窗口/日界，不猴补丁全局 time.time）
        self._now = time.time
        self._limits: Optional[Limits] = None
        # key -> 60s 滑动窗内的请求时间戳
        self._minute: Dict[str, Deque[float]] = {}
        # key -> (北京日期, 当日计数)；含 "day:" 日配额与 "think:" 深思配额
        self._daily: Dict[str, Tuple[str, int]] = {}
        # 全局日熔断计数 (北京日期, 当日问答数)
        self._global_day: Tuple[str, int] = ("", 0)
        # 拒绝日志节流：key -> 上次告警时间戳
        self._last_warn: Dict[str, float] = {}
        # perf#94：上次全量清理时间戳（_maybe_prune 节流用）
        self._last_prune = 0.0
        # P1-1：拒绝聚合 (北京日, 原因) -> 计数，独立小锁（_deny 持主锁时嵌套获取，
        # 顺序恒 _lock → _reject_lock，flush 线程只取 _reject_lock，无死锁环）
        self._reject_lock = threading.Lock()
        self._rejected: Dict[Tuple[str, str], int] = {}
        self._reject_flush_ts = 0.0
        # P1-1：触顶告警日界（每北京日至多发一次 critical 告警）
        self._cap_alert_day = ""

    # ── 配置 ─────────────────────────────────────────────────

    def limits(self) -> Limits:
        if self._limits is None:
            self._limits = _load_limits()
        return self._limits

    def reload_limits(self) -> Limits:
        """重读环境变量（测试/运维热调用）。"""
        self._limits = None
        return self.limits()

    def reset_for_tests(self) -> None:
        """清空全部计数并重读限额（仅测试使用）。"""
        with self._lock:
            self._minute.clear()
            self._daily.clear()
            self._global_day = ("", 0)
            self._last_warn.clear()
            self._last_prune = 0.0
            self._cap_alert_day = ""
        with self._reject_lock:
            self._rejected.clear()
            self._reject_flush_ts = 0.0
        self._limits = None

    def describe(self) -> str:
        """启动 banner 用的一行描述。"""
        lim = self.limits()
        if not lim.enabled:
            return "禁用（模拟模式或 RAG_RATE_LIMIT_ENABLE=false）"
        return (
            f"用户 {lim.user_per_min}/分·{lim.user_per_day}/日 | "
            f"匿名IP {lim.anon_per_min}/分·{lim.anon_per_day}/日 | "
            f"深思 {lim.thinking_daily_quota}/日(匿名拒绝) | "
            f"全局熔断 {lim.global_daily_llm_cap}/日 | 辅助 {lim.aux_per_min}/分"
        )

    # ── 准入 ─────────────────────────────────────────────────

    def admit_ask(self, actor: str, *, is_user: bool, thinking: bool = False,
                  count_llm: bool = True) -> Optional[Denial]:
        """问答类端点准入（/api/ask、/api/ask/stream；/api/search 以 count_llm=False
        共享限频/日配额但不计入全局熔断——它不调 LLM）。

        检查顺序：全局熔断 → 深思策略 → 每分钟限频 → 日配额；全部通过才原子计入。
        """
        lim = self.limits()
        if not lim.enabled:
            return None
        now = self._now()
        day = _beijing_day(now)

        with self._lock:
            # 1) 全局日熔断（503：服务层面停摆，与用户行为无关）
            if count_llm and lim.global_daily_llm_cap > 0:
                g_day, g_cnt = self._global_day
                if g_day == day and g_cnt >= lim.global_daily_llm_cap:
                    # P1-1 兜底告警点：进程重启后计数重积/运维中途调低 cap 等场景不经过
                    # 下方的 new_cnt==cap 边沿，首个 global_cap 拒绝同样要拉响告警。
                    self._maybe_cap_alert(day, lim.global_daily_llm_cap)
                    return self._deny(actor, Denial(
                        503, "服务繁忙：今日问答量已达上限，请明天再试或联系管理员",
                        _secs_to_beijing_midnight(now), "global_cap"))

            # 2) 深思策略（只检查不计数：被拒后关掉深思可立即重问，不烧常规预算）
            think_key = ""
            if thinking:
                if not is_user:
                    return self._deny(actor, Denial(
                        403, "深度思考功能需登录后使用，请关闭深度思考或重新登录",
                        0, "thinking_anon"))
                if lim.thinking_daily_quota <= 0:
                    return self._deny(actor, Denial(
                        403, "深度思考功能暂未开放，请关闭深度思考后继续提问",
                        0, "thinking_off"))
                think_key = f"think:{actor}"
                t_day, t_cnt = self._daily.get(think_key, (day, 0))
                if t_day == day and t_cnt >= lim.thinking_daily_quota:
                    return self._deny(actor, Denial(
                        429,
                        f"今日深度思考次数已用完（{lim.thinking_daily_quota} 次/天），"
                        "可关闭深度思考继续提问",
                        _secs_to_beijing_midnight(now), "thinking_quota"))

            # 3) 每分钟滑动窗
            per_min = lim.user_per_min if is_user else lim.anon_per_min
            dq = self._minute.setdefault(f"ask:{actor}", deque())
            while dq and now - dq[0] >= _MINUTE_WINDOW_S:
                dq.popleft()
            if per_min > 0 and len(dq) >= per_min:
                retry = max(1, int(_MINUTE_WINDOW_S - (now - dq[0])) + 1)
                return self._deny(actor, Denial(
                    429, "提问太频繁了，请稍后再试", retry, "per_min"))

            # 4) 日配额（匿名文案引导登录：登录后限额更宽且部门权限生效）
            per_day = lim.user_per_day if is_user else lim.anon_per_day
            day_key = f"day:{actor}"
            d_day, d_cnt = self._daily.get(day_key, (day, 0))
            if d_day != day:
                d_cnt = 0
            if per_day > 0 and d_cnt >= per_day:
                msg = (f"今日提问次数已达上限（{per_day} 次/天），请明天再来"
                       if is_user else "未登录状态提问次数已达今日上限，请登录后继续使用")
                return self._deny(actor, Denial(
                    429, msg, _secs_to_beijing_midnight(now), "per_day"))

            # 5) 全部通过 → 原子计入
            dq.append(now)
            self._daily[day_key] = (day, d_cnt + 1)
            if think_key:
                t_day, t_cnt = self._daily.get(think_key, (day, 0))
                self._daily[think_key] = (day, (t_cnt if t_day == day else 0) + 1)
            if count_llm and lim.global_daily_llm_cap > 0:
                g_day, g_cnt = self._global_day
                new_cnt = (g_cnt if g_day == day else 0) + 1
                self._global_day = (day, new_cnt)
                if new_cnt == lim.global_daily_llm_cap:
                    # 最后一个放行的请求触顶：大声记日志 + 运维告警（下一个请求开始全拒）
                    logger.error("全局日熔断触顶：今日问答量已达 %d，后续请求将被拒绝至次日",
                                 lim.global_daily_llm_cap)
                    self._maybe_cap_alert(day, lim.global_daily_llm_cap)
            self._maybe_prune(now, day)
            # 放行路径顺带检查拒绝聚合是否到期（风暴结束后残留计数靠正常流量冲出去）
            self._note_reject(None, now)
        return None

    def admit_aux(self, actor: str) -> Optional[Denial]:
        """辅助端点准入（auth/feedback/resign-images/history/hot-questions/session-clear）：
        仅每分钟滑动窗——它们不调 LLM，防的是连接洪泛与 DB/OSS/钉钉 API 滥用。
        """
        lim = self.limits()
        if not lim.enabled or lim.aux_per_min <= 0:
            return None
        now = self._now()
        with self._lock:
            dq = self._minute.setdefault(f"aux:{actor}", deque())
            while dq and now - dq[0] >= _MINUTE_WINDOW_S:
                dq.popleft()
            if len(dq) >= lim.aux_per_min:
                retry = max(1, int(_MINUTE_WINDOW_S - (now - dq[0])) + 1)
                return self._deny(actor, Denial(
                    429, "操作太频繁，请稍后再试", retry, "aux_per_min"))
            dq.append(now)
            self._maybe_prune(now, _beijing_day(now))
        return None

    # ── 内部 ─────────────────────────────────────────────────

    def _deny(self, actor: str, denial: Denial) -> Denial:
        """拒绝出口：按 (actor, reason) 节流记日志 + 拒绝聚合计数（持锁路径，字典操作
        与至多每 60s 一次的 flush 线程派生）。"""
        now = self._now()
        warn_key = f"{denial.reason}|{actor}"
        last = self._last_warn.get(warn_key, 0.0)
        if now - last >= _WARN_THROTTLE_S:
            self._last_warn[warn_key] = now
            logger.warning("限流拒绝 reason=%s actor=%s status=%d retry_after=%ds",
                           denial.reason, actor, denial.status_code, denial.retry_after)
        self._note_reject(denial.reason, now)
        return denial

    def _maybe_cap_alert(self, day: str, cap: int) -> None:
        """触顶告警（持 _lock 路径）：每北京日至多派发一次；发送在后台线程，不阻塞热路径。"""
        if self._cap_alert_day == day:
            return
        self._cap_alert_day = day
        _dispatch_cap_alert(cap)

    def _note_reject(self, reason: Optional[str], now: float) -> None:
        """拒绝聚合：reason 非空则 +1；到期（≥60s 且有存量）则快照清零并派发落库。
        独立 _reject_lock（调用方可能持有 _lock，见 __init__ 的锁序注释）；落库失败由
        _flush_rejected 把快照并回，下一批重试。"""
        snapshot = None
        with self._reject_lock:
            if reason:
                key = (_beijing_day(now), reason)
                self._rejected[key] = self._rejected.get(key, 0) + 1
            if self._rejected and now - self._reject_flush_ts >= _REJECT_FLUSH_INTERVAL_S:
                self._reject_flush_ts = now
                snapshot = self._rejected
                self._rejected = {}
        if snapshot:
            if _DISPATCH_ASYNC:
                threading.Thread(target=self._flush_rejected, args=(snapshot,),
                                 daemon=True, name="reject-flush").start()
            else:
                self._flush_rejected(snapshot)

    def _flush_rejected(self, snapshot: Dict[Tuple[str, str], int]) -> None:
        """落库一批拒绝聚合；失败并回内存（键按日聚合，重试无重复计数风险）。"""
        if _persist_rejected(snapshot):
            return
        with self._reject_lock:
            for key, n in snapshot.items():
                self._rejected[key] = self._rejected.get(key, 0) + n

    def _maybe_prune(self, now: float, day: str) -> None:
        """计数字典超软上限时清理过期项（窗口外/隔日），防扫描器轮换 key 撑爆内存。

        perf#94：重建节流 ≥30s 一次——超阈值期间原本每个放行请求都在持锁路径做
        全量 dict 重建（O(n) x2）；节流后过期条目最多多存活 ~30s。阈值以下只做
        三次 len 比较，路径零变化。时间源沿用调用方传入的 self._now()（测试假时钟兼容）。
        """
        if now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        pruned = False
        if len(self._minute) > _PRUNE_THRESHOLD:
            self._minute = {k: dq for k, dq in self._minute.items()
                            if dq and now - dq[-1] < _MINUTE_WINDOW_S}
            pruned = True
        if len(self._daily) > _PRUNE_THRESHOLD:
            self._daily = {k: v for k, v in self._daily.items() if v[0] == day}
            pruned = True
        if len(self._last_warn) > _PRUNE_THRESHOLD:
            self._last_warn.clear()
            pruned = True
        if pruned:
            self._last_prune = now


# 模块级单例：api.py 各端点共享（workers=1 → 即全局）
LIMITER = ServingRateLimiter()
