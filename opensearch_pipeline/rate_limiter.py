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
⚠️ A2（审计复核批次1，2026-07-12）：agent 链路另有**逐模型调用计费**
（charge_llm_call——多轮 run/空终轮重试/审批续跑段每次真实模型调用都扣全局熔断与
每用户日计数，准入层的一次性计数只护 ask 边界）。开 RAG_AGENT_ENABLE 后
RAG_GLOBAL_DAILY_LLM_CAP 的语义从「次/ask」趋向「模型调用数」，阈值需按实测
倍率重定（批次 7 运营配套）。

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

from opensearch_pipeline import redis_client

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


# P2-11：全局熔断准入量的持久化「原因」键（与拒绝共表；__ 前缀 = 非拒绝行，
# qa_rollup._fetch_rejected 按前缀排除）。落库频率与拒绝同批（≤1 批/60s）。
_ADMITTED_KEY = "__admitted__"


def _load_admitted_today(day: str) -> Optional[int]:
    """读回当日已持久化的准入量（进程重启回种；盲区审计 P2-11：计数器纯内存，
    SAE 重启/崩溃循环反复清零 2000/日 的账单保护顶）。fail-open：模拟/表未建/失败 → None。"""
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db:
            return None
        op_db = cfg.rds.operation_database
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT reject_count FROM {op_db}.qa_admission_reject"
                    " WHERE stat_date = %s AND reason = %s",
                    (day, _ADMITTED_KEY))
                row = cur.fetchone()
                if not row:
                    return 0
                return int((row["reject_count"] if isinstance(row, dict) else row[0]) or 0)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001
        logger.warning("熔断计数回种读取失败（fail-open，按内存计数）: %s", e)
        return None


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
    # P2-11：深思请求 ~8x token 计费（模块 docstring），对全局熔断按权重计数——
    # 否则同一顶下真实账单差一个数量级（"准入请求"vs"计费调用"的阻抗失配）
    thinking_cap_weight: int = 8


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
        thinking_cap_weight=max(1, _env_int("RAG_GLOBAL_CAP_THINKING_WEIGHT", 8)),
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
        # P2-11：熔断计数回种日界（进程内每北京日至多回种一次）
        self._cap_seed_day = ""

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
            self._cap_seed_day = ""
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

        # P2-11 重启回种（锁外触发：_seed 内部要取 _lock，同步测试模式下锁内直调会死锁；
        # 竞态良性——至多重复派发，回种本身 max() 幂等）：进程首见当日 → 后台读回已持久化
        # 的准入量（纯内存计数会被 SAE 重部署/崩溃循环反复清零，攻击中崩溃 = 反复重置
        # 账单保护顶）。
        if count_llm and lim.global_daily_llm_cap > 0 and self._cap_seed_day != day:
            self._cap_seed_day = day
            self._dispatch_cap_seed(day)

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
                prev = g_cnt if g_day == day else 0
                # P2-11：深思按权重计（~8x token 计费），"准入请求"≈"计费量"不再差一个量级
                weight = lim.thinking_cap_weight if thinking else 1
                new_cnt = prev + weight
                self._global_day = (day, new_cnt)
                # 准入量与拒绝同批持久化（__admitted__ 行，重启回种数据源）
                with self._reject_lock:
                    a_key = (day, _ADMITTED_KEY)
                    self._rejected[a_key] = self._rejected.get(a_key, 0) + weight
                if prev < lim.global_daily_llm_cap <= new_cnt:
                    # 跨越触顶边沿（权重计数可跳过精确等号）：大声记日志 + 运维告警
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

    def charge_llm_call(self, actor: str, *, weight: int = 1, thinking: bool = False) -> bool:
        """A2 逐模型调用计费（审计复核批次1）：**只扣计数、不做准入判定**——每次真实
        模型调用扣全局日熔断 + 该用户日计数（thinking 档按 thinking_cap_weight 加权，
        与准入侧 P2-11 同一"cap≈计费量"口径）。返回 True=仍在帽内；False=已超帽
        （调用方 fail-closed：agent run 以 budget_exceeded 诚实收尾）。

        与 admit_ask 的分工：准入只在 ask 边界计 1 次，多轮 run/空终轮重试/审批续跑段
        的模型调用此前对熔断零扣减（复核 A2）；本方法补上逐调用扣减。超帽判定取
        「累计 > cap」——恰好落在 cap 上的那次仍放行，与准入侧「≥cap 拒绝」衔接后
        总量恒 ≤ cap+权重。"""
        lim = self.limits()
        if not lim.enabled:
            return True
        now = self._now()
        day = _beijing_day(now)
        w = max(1, int(weight)) * (lim.thinking_cap_weight if thinking else 1)
        # 重启回种（同 admit_ask：锁外触发，_seed 内部要取 _lock）
        if lim.global_daily_llm_cap > 0 and self._cap_seed_day != day:
            self._cap_seed_day = day
            self._dispatch_cap_seed(day)
        over = False
        with self._lock:
            if lim.global_daily_llm_cap > 0:
                g_day, g_cnt = self._global_day
                prev = g_cnt if g_day == day else 0
                new_cnt = prev + w
                self._global_day = (day, new_cnt)
                with self._reject_lock:   # __admitted__ 行：SLO offered/admitted + 重启回种数据源
                    a_key = (day, _ADMITTED_KEY)
                    self._rejected[a_key] = self._rejected.get(a_key, 0) + w
                if prev < lim.global_daily_llm_cap <= new_cnt:
                    logger.error("全局日熔断触顶（逐调用计费）：今日 LLM 调用量已达 %d",
                                 lim.global_daily_llm_cap)
                    self._maybe_cap_alert(day, lim.global_daily_llm_cap)
                if new_cnt > lim.global_daily_llm_cap:
                    over = True
            if lim.user_per_day > 0:
                day_key = f"day:{actor}"
                d_day, d_cnt = self._daily.get(day_key, (day, 0))
                if d_day != day:
                    d_cnt = 0
                self._daily[day_key] = (day, d_cnt + w)
                if d_cnt + w > lim.user_per_day:
                    over = True
            self._note_reject(None, now)   # 顺带冲刷到期的聚合批（锁序 _lock→_reject_lock）
        return not over

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

    def _dispatch_cap_seed(self, day: str) -> None:
        """P2-11 重启回种（持 _lock 路径调用，DB 读放后台线程）：把当日已持久化的准入量
        并回内存计数。max() 语义：稳态下内存 ≥ 已落库（落库滞后 ≤60s），回种是 no-op；
        重启后内存从 0 重积，db 值恢复上一进程的累计（丢失上限 = 最后一批未落库的 ≤60s 尾巴）。
        fail-open：读失败仅按内存计数（回到修复前行为）。"""
        def _seed() -> None:
            db_cnt = _load_admitted_today(day)
            if not db_cnt:
                return
            with self._lock:
                g_day, g_cnt = self._global_day
                cur = g_cnt if g_day == day else 0
                if db_cnt > cur:
                    self._global_day = (day, db_cnt)
                    logger.info("全局熔断计数回种：%s 内存 %d → 持久化 %d", day, cur, db_cnt)
        if _DISPATCH_ASYNC:
            threading.Thread(target=_seed, daemon=True, name="cap-seed").start()
        else:
            _seed()

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


# ─────────────────────────────────────────────────────────────────────────────
# Redis 后端（多实例共享计数；Lua 单往返原子四层查-算）
# ─────────────────────────────────────────────────────────────────────────────
# 「查四层 → 仅在全部未超限时才计入」必须原子（否则并发下超额放行）。用一段 Lua
# 在 Redis 服务端一次完成，单次网络往返（性能优先，优于多条 INCR 往返 + WATCH 重试）。
# KEYS: 1=global 2=minute(固定窗) 3=day 4=think
# ARGV: 1=cap 2=per_min 3=per_day 4=think_q 5=weight 6=ttl_min 7=ttl_day 8=count_llm 9=thinking
# 返回统一三元组 {outcome, count, edge}；outcome ∈ admit/global_cap/thinking_quota/per_min/per_day
_ASK_LUA = """
local cap=tonumber(ARGV[1]); local per_min=tonumber(ARGV[2]); local per_day=tonumber(ARGV[3])
local think_q=tonumber(ARGV[4]); local weight=tonumber(ARGV[5])
local ttl_min=tonumber(ARGV[6]); local ttl_day=tonumber(ARGV[7])
local count_llm=tonumber(ARGV[8]); local thinking=tonumber(ARGV[9])
local g=tonumber(redis.call('GET',KEYS[1]) or '0')
if count_llm==1 and cap>0 and g>=cap then return {'global_cap',g,0} end
local t=tonumber(redis.call('GET',KEYS[4]) or '0')
if thinking==1 and think_q>0 and t>=think_q then return {'thinking_quota',t,0} end
local m=tonumber(redis.call('GET',KEYS[2]) or '0')
if per_min>0 and m>=per_min then return {'per_min',m,0} end
local d=tonumber(redis.call('GET',KEYS[3]) or '0')
if per_day>0 and d>=per_day then return {'per_day',d,0} end
local nm=redis.call('INCR',KEYS[2]); if nm==1 then redis.call('EXPIRE',KEYS[2],ttl_min) end
local nd=redis.call('INCR',KEYS[3]); if nd==1 then redis.call('EXPIRE',KEYS[3],ttl_day) end
if thinking==1 then local nt=redis.call('INCR',KEYS[4]); if nt==1 then redis.call('EXPIRE',KEYS[4],ttl_day) end end
local edge=0
if count_llm==1 and cap>0 then
  local ng=redis.call('INCRBY',KEYS[1],weight); if ng==weight then redis.call('EXPIRE',KEYS[1],ttl_day) end
  if g<cap and ng>=cap then edge=1 end
end
return {'admit',0,edge}
"""

# 辅助端点：仅每分钟固定窗。KEYS:1=minute ARGV:1=per_min 2=ttl_min
_AUX_LUA = """
local per_min=tonumber(ARGV[1]); local ttl_min=tonumber(ARGV[2])
local m=tonumber(redis.call('GET',KEYS[1]) or '0')
if per_min>0 and m>=per_min then return {'aux_per_min',m} end
local nm=redis.call('INCR',KEYS[1]); if nm==1 then redis.call('EXPIRE',KEYS[1],ttl_min) end
return {'admit',nm}
"""

# A2 逐模型调用计费：只扣不判（先扣后报超帽，语义与 memory 版 charge_llm_call 一致）。
# KEYS: 1=global 2=day(用户)   ARGV: 1=cap 2=per_day 3=weight 4=ttl_day
# 返回 {over, edge}：over=1 已超帽（累计>上限）；edge=1 本次恰好跨过全局 cap（告警一次用）
_CHARGE_LUA = """
local cap=tonumber(ARGV[1]); local per_day=tonumber(ARGV[2])
local w=tonumber(ARGV[3]); local ttl_day=tonumber(ARGV[4])
local over=0; local edge=0
if cap>0 then
  local ng=redis.call('INCRBY',KEYS[1],w); if ng==w then redis.call('EXPIRE',KEYS[1],ttl_day) end
  if (ng-w)<cap and ng>=cap then edge=1 end
  if ng>cap then over=1 end
end
if per_day>0 then
  local nd=redis.call('INCRBY',KEYS[2],w); if nd==w then redis.call('EXPIRE',KEYS[2],ttl_day) end
  if nd>per_day then over=1 end
end
return {over, edge}
"""


class RedisRateLimiter:
    """Redis 后端：四层计数**跨实例共享**（全局熔断因此才真正全局，而非每实例各 2000/日
    致实际放大 N 倍）；Lua 脚本单往返原子完成「查四层 + 仅全部未超限才计入」。

    与 memory 后端（ServingRateLimiter）的差异（性能优先取舍，已在测试中分别覆盖）：
    - 每分钟层用**固定窗**（bucket=floor(now/60)）而非滑动窗——省 ZSET/额外往返；边界处
      突发略宽，账单保护语义不变。retry_after 在 Python 按窗口边界算（不依赖 Redis 时钟，
      兼容假时钟测试）。
    - 全局计数在 Redis 共享且 EXPIRE 到北京次日零点自清 → **无需** memory 版的重启回种
      （_dispatch_cap_seed）与 _maybe_prune。
    - 触顶告警用 Redis SETNX 每北京日**全局一次**（优于 memory 的每实例一次，顺带缓解评审 E6）。

    fail-closed：ask 成本路径（count_llm）Redis 故障 → 503（plan WS0-3）。⚠️ 评审 C3：这让
    Redis 成为问答新单点，启用前须 Redis 高可用（双副本/自动切换）。aux 路径 fail-open。

    公有接口与 ServingRateLimiter 对齐（admit_ask/admit_aux/limits/reload_limits/
    reset_for_tests/describe/_now 可注入），api.py 无感切换。
    拒绝/准入聚合落库沿用 memory 版同构逻辑（独立实现，刻意不改动 live 的 ServingRateLimiter）。
    """

    def __init__(self, client) -> None:
        self._r = client
        self._now = time.time                       # 假时钟注入点（对齐 memory）
        self._limits: Optional[Limits] = None
        self._ask_script = client.register_script(_ASK_LUA)   # 客户端侧建 Script，不连服务器
        self._aux_script = client.register_script(_AUX_LUA)
        self._charge_script = client.register_script(_CHARGE_LUA)
        # 拒绝聚合（每实例缓冲 → RDS UPSERT 合并；与 memory 版同构）
        self._reject_lock = threading.Lock()
        self._rejected: Dict[Tuple[str, str], int] = {}
        self._reject_flush_ts = 0.0

    # ── 配置（与 memory 版一致）───────────────────────────────
    def limits(self) -> Limits:
        if self._limits is None:
            self._limits = _load_limits()
        return self._limits

    def reload_limits(self) -> Limits:
        self._limits = None
        return self.limits()

    def reset_for_tests(self) -> None:
        with self._reject_lock:
            self._rejected.clear()
            self._reject_flush_ts = 0.0
        self._limits = None
        # 计数键由测试用全新 fakeredis 实例隔离；刻意不 flushdb（防误清生产共享键）

    def describe(self) -> str:
        lim = self.limits()
        if not lim.enabled:
            return "禁用（模拟模式或 RAG_RATE_LIMIT_ENABLE=false）"
        return (
            f"[redis] 用户 {lim.user_per_min}/分·{lim.user_per_day}/日 | "
            f"匿名IP {lim.anon_per_min}/分·{lim.anon_per_day}/日 | "
            f"深思 {lim.thinking_daily_quota}/日 | 全局熔断 {lim.global_daily_llm_cap}/日 | "
            f"辅助 {lim.aux_per_min}/分"
        )

    # ── 准入 ─────────────────────────────────────────────────
    def admit_ask(self, actor: str, *, is_user: bool, thinking: bool = False,
                  count_llm: bool = True) -> Optional[Denial]:
        lim = self.limits()
        if not lim.enabled:
            return None
        now = self._now()
        day = _beijing_day(now)

        # 深思 anon/off 策略在 Python（无需 Redis）
        if thinking:
            if not is_user:
                return self._deny(Denial(
                    403, "深度思考功能需登录后使用，请关闭深度思考或重新登录", 0, "thinking_anon"), now)
            if lim.thinking_daily_quota <= 0:
                return self._deny(Denial(
                    403, "深度思考功能暂未开放，请关闭深度思考后继续提问", 0, "thinking_off"), now)

        bucket = int(now // _MINUTE_WINDOW_S)
        ttl_day = _secs_to_beijing_midnight(now)
        per_min = lim.user_per_min if is_user else lim.anon_per_min
        per_day = lim.user_per_day if is_user else lim.anon_per_day
        weight = lim.thinking_cap_weight if thinking else 1
        keys = [redis_client.key("rl", "g", day),
                redis_client.key("rl", "m", "ask", actor, bucket),
                redis_client.key("rl", "d", "ask", actor, day),
                redis_client.key("rl", "t", actor, day)]
        args = [lim.global_daily_llm_cap, per_min, per_day, lim.thinking_daily_quota,
                weight, int(2 * _MINUTE_WINDOW_S), ttl_day,
                1 if count_llm else 0, 1 if thinking else 0]
        try:
            outcome, _cnt, edge = self._ask_script(keys=keys, args=args)
        except Exception as e:   # noqa: BLE001 — fail-closed（评审 C3）
            logger.error("限流 Redis 后端故障，ask 路径 fail-closed: %s", e)
            if count_llm:
                return Denial(503, "服务暂时不可用，请稍后再试", 5, "ratelimit_backend_down")
            return None          # 非成本路径不因限流器故障拖垮辅助能力

        if outcome == "admit":
            if edge:
                self._maybe_cap_alert_once(day, lim.global_daily_llm_cap, now)
            if count_llm and lim.global_daily_llm_cap > 0:
                with self._reject_lock:   # __admitted__ 准入量随拒绝同批落库（SLO offered/admitted）
                    a_key = (day, _ADMITTED_KEY)
                    self._rejected[a_key] = self._rejected.get(a_key, 0) + weight
            self._note_reject(None, now)
            return None
        if outcome == "global_cap":
            self._maybe_cap_alert_once(day, lim.global_daily_llm_cap, now)
            return self._deny(Denial(
                503, "服务繁忙：今日问答量已达上限，请明天再试或联系管理员",
                _secs_to_beijing_midnight(now), "global_cap"), now)
        if outcome == "thinking_quota":
            return self._deny(Denial(
                429, f"今日深度思考次数已用完（{lim.thinking_daily_quota} 次/天），可关闭深度思考继续提问",
                _secs_to_beijing_midnight(now), "thinking_quota"), now)
        if outcome == "per_min":
            retry = max(1, int((bucket + 1) * _MINUTE_WINDOW_S - now) + 1)
            return self._deny(Denial(429, "提问太频繁了，请稍后再试", retry, "per_min"), now)
        if outcome == "per_day":
            msg = (f"今日提问次数已达上限（{per_day} 次/天），请明天再来"
                   if is_user else "未登录状态提问次数已达今日上限，请登录后继续使用")
            return self._deny(Denial(429, msg, _secs_to_beijing_midnight(now), "per_day"), now)
        return None

    def admit_aux(self, actor: str) -> Optional[Denial]:
        lim = self.limits()
        if not lim.enabled or lim.aux_per_min <= 0:
            return None
        now = self._now()
        bucket = int(now // _MINUTE_WINDOW_S)
        key = redis_client.key("rl", "m", "aux", actor, bucket)
        try:
            outcome, _cnt = self._aux_script(keys=[key], args=[lim.aux_per_min, int(2 * _MINUTE_WINDOW_S)])
        except Exception as e:   # noqa: BLE001 — aux 非成本路径 fail-open
            logger.warning("限流 Redis 后端故障，aux 路径 fail-open: %s", e)
            return None
        if outcome == "aux_per_min":
            retry = max(1, int((bucket + 1) * _MINUTE_WINDOW_S - now) + 1)
            return self._deny(Denial(429, "操作太频繁，请稍后再试", retry, "aux_per_min"), now)
        return None

    def charge_llm_call(self, actor: str, *, weight: int = 1, thinking: bool = False) -> bool:
        """A2 逐模型调用计费（与 memory 版同一契约，见 ServingRateLimiter.charge_llm_call）。
        计数键与准入侧共享（rl:g:<day> / rl:d:ask:<actor>:<day>），跨实例扣的是同一份账。

        fail 姿态：**fail-open**（有别于 admit_ask 的 fail-closed）——run 已通过准入且有
        run 级 max_turns/token 预算兜底，中途 Redis 抖动不该把在跑 run 全体打死（B2
        「限流 Redis 单点」的爆炸半径控制）；响亮 error 日志保可观测。"""
        lim = self.limits()
        if not lim.enabled:
            return True
        now = self._now()
        day = _beijing_day(now)
        w = max(1, int(weight)) * (lim.thinking_cap_weight if thinking else 1)
        keys = [redis_client.key("rl", "g", day),
                redis_client.key("rl", "d", "ask", actor, day)]
        args = [lim.global_daily_llm_cap, lim.user_per_day, w, _secs_to_beijing_midnight(now)]
        try:
            over, edge = self._charge_script(keys=keys, args=args)
        except Exception as e:   # noqa: BLE001
            logger.error("限流 Redis 后端故障，charge_llm_call fail-open（run 级预算兜底）: %s", e)
            return True
        if edge:
            self._maybe_cap_alert_once(day, lim.global_daily_llm_cap, now)
        if lim.global_daily_llm_cap > 0:
            with self._reject_lock:   # __admitted__ 行：SLO offered/admitted 口径同准入
                a_key = (day, _ADMITTED_KEY)
                self._rejected[a_key] = self._rejected.get(a_key, 0) + w
        self._note_reject(None, now)
        return not int(over)

    # ── 内部（拒绝聚合/告警；与 memory 版同构，复用模块级 _persist_rejected/_dispatch_cap_alert）──
    def _deny(self, denial: Denial, now: float) -> Denial:
        self._note_reject(denial.reason, now)
        return denial

    def _maybe_cap_alert_once(self, day: str, cap: int, now: float) -> None:
        """触顶告警：Redis SETNX 每北京日全局一次（跨实例去重，优于 memory 每实例一次）。"""
        try:
            akey = redis_client.key("rl", "g", day, "alerted")
            if self._r.set(akey, "1", nx=True, ex=_secs_to_beijing_midnight(now)):
                _dispatch_cap_alert(cap)
        except Exception:   # noqa: BLE001 — 告警去重失败不影响限流主路径
            logger.warning("触顶告警 SETNX 失败（fail-open）", exc_info=True)

    def _note_reject(self, reason: Optional[str], now: float) -> None:
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
                                 daemon=True, name="reject-flush-redis").start()
            else:
                self._flush_rejected(snapshot)

    def _flush_rejected(self, snapshot: Dict[Tuple[str, str], int]) -> None:
        if _persist_rejected(snapshot):
            return
        with self._reject_lock:
            for key, n in snapshot.items():
                self._rejected[key] = self._rejected.get(key, 0) + n


def _make_limiter():
    """按 RAG_RATE_LIMIT_BACKEND 选后端（默认 memory = 回滚开关）。"""
    backend = os.environ.get("RAG_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend == "redis":
        try:
            return RedisRateLimiter(redis_client.get_client())
        except Exception as e:   # noqa: BLE001
            # Redis 初始化失败 → 回退 memory（单实例安全默认）。⚠️ 多实例下须确保 Redis 可用，
            # 否则各实例独立计数使全局熔断实际放大 N 倍——启用前置见评审 C3。
            logger.error("RAG_RATE_LIMIT_BACKEND=redis 但 Redis 初始化失败，回退 memory: %s", e)
    elif backend not in ("", "memory"):
        logger.warning("未知 RAG_RATE_LIMIT_BACKEND=%r，回退 memory", backend)
    return ServingRateLimiter()


# 模块级单例：api.py 各端点共享（workers=1 → 即全局；redis 后端下跨实例共享）
LIMITER = _make_limiter()


def charge_llm_call(user_id: Optional[str], *, weight: int = 1, thinking: bool = False) -> bool:
    """A2 逐模型调用计费的模块级入口（agent make_model_fn 调用）：actor 键与准入侧
    同构（``u:<user_id>``，agent 入口强制登录故恒有 user_id）。返回 False=超帽，
    调用方 fail-closed 收尾 run；本函数自身异常 **fail-open**（不扣减放行）——
    计费件故障不拖垮在跑 run，上界仍有 run 级 max_turns/token 预算 + ask 准入兜底。"""
    try:
        fn = getattr(LIMITER, "charge_llm_call", None)
        if fn is None:                       # 测试替身/旧单例无此方法 → 不计费
            return True
        return bool(fn(f"u:{user_id or 'unknown'}", weight=weight, thinking=thinking))
    except Exception:   # noqa: BLE001
        logger.error("charge_llm_call 内部异常（fail-open：本次调用未扣减）", exc_info=True)
        return True


def _set_limiter_for_test(limiter) -> None:
    """测试用：切换全局限流器单例。"""
    global LIMITER
    LIMITER = limiter
