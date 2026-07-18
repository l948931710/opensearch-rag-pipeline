# -*- coding: utf-8 -*-
"""alerting.py — Phase-2 OBS-4: one DingTalk ops-alert sink (fail-open, config-gated).

Posts a Markdown alert to a DingTalk CUSTOM-ROBOT webhook (oapi.dingtalk.com/robot/send with
HMAC-SHA256 sign). Distinct from the user-facing batchSend path in dingtalk_card.py — this is an
operator/ops channel for orchestrator non-zero exits, embed-fail spikes, RDS↔HA3 parity drift, and
cost-breaker trips. Config keys:
  RAG_OPS_ALERT_WEBHOOK  — full webhook URL (incl. access_token query param)
  RAG_OPS_ALERT_SECRET   — signing secret (DingTalk addSign option)
Without either, send_ops_alert logs+no-ops (fail-open, never raises, never blocks the caller).

Rate-limited per dedup_key to prevent alert storms (default 60s window). Auxiliary like qa_logger /
audit_log: an alert send failure must never abort the operation that triggered it.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_LAST_SENT: dict = {}  # dedup_key -> last send timestamp (process-local; fine for single-instance)
_DEDUP_WINDOW_S = 60


def _sign_url(webhook: str, secret: str) -> str:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    sig = base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                                    digestmod=hashlib.sha256).digest())
    return f"{webhook}{'&' if '?' in webhook else '?'}timestamp={ts}&sign={urllib.parse.quote_plus(sig)}"


def _webhook_allowed(url: str) -> bool:
    """B5（生产级外审 2026-07-17 P2-08）SSRF 防线：webhook 仅 https + 域白名单
    （默认钉钉官方域；`RAG_OPS_ALERT_WEBHOOK_ALLOW=host1,host2` 追加自建告警网关域）。
    套 dingtalk_bot._is_dingtalk_webhook（#F-dingtalk-ssrf）同款范式——env 被污染时
    file:// 与 VPC 内网/元数据地址不再可达。解析失败一律拒。"""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname:
            return False
        host = p.hostname.lower()
        suffixes = [".dingtalk.com"]
        exacts = {"dingtalk.com"}
        extra = (os.environ.get("RAG_OPS_ALERT_WEBHOOK_ALLOW") or "").strip()
        for e in extra.split(","):
            e = e.strip().lower().lstrip(".")
            if e:
                exacts.add(e)
                suffixes.append("." + e)
        return host in exacts or any(host.endswith(s) for s in suffixes)
    except Exception:   # noqa: BLE001 — 解析不了的 URL 不值得信任
        return False


def send_ops_alert(title: str, text: str, *, severity: str = "warning",
                   dedup_key: Optional[str] = None, timeout: float = 5.0) -> bool:
    """Post a Markdown alert. Returns True on a 2xx HTTP send; False otherwise (or no-op).
    Never raises. severity ∈ {info, warning, critical} (decorative; DingTalk doesn't use it).
    """
    webhook = (os.environ.get("RAG_OPS_ALERT_WEBHOOK") or "").strip()
    secret = (os.environ.get("RAG_OPS_ALERT_SECRET") or "").strip()
    if webhook and not _webhook_allowed(webhook):
        # B5（P2-08）：env 污染 → file:///内网 SSRF 的通道关闭（拒发+响亮留痕）。
        logger.error("ops-alert 拒发：webhook 不在允许域（须 https + *.dingtalk.com；"
                     "自建网关用 RAG_OPS_ALERT_WEBHOOK_ALLOW 追加域名）：%s", webhook[:80])
        return False
    if not webhook:
        # P0-05（报告1）：webhook 未配时不再静默 no-op——critical 升到 logger.error（运维日志
        # 至少能看到"本该告警但发不出去"），其余 severity 保持 info。真正的常在线调度/dead-man
        # 归 DataWorks 部署（代码改不了），但静默这一处代码侧先补上。
        (logger.error if severity == "critical" else logger.info)(
            "ops-alert %s (RAG_OPS_ALERT_WEBHOOK unset): [%s] %s",
            "SUPPRESSED-CRITICAL" if severity == "critical" else "no-op", severity, title)
        return False

    # rate-limit identical alerts to prevent storms
    key = dedup_key or f"{severity}:{title}"
    now = time.time()
    last = _LAST_SENT.get(key, 0)
    if now - last < _DEDUP_WINDOW_S:
        logger.debug("ops-alert deduped within %ds: %s", _DEDUP_WINDOW_S, key)
        return False

    try:
        url = _sign_url(webhook, secret) if secret else webhook
        body = json.dumps({
            "msgtype": "markdown",
            "markdown": {"title": f"[{severity.upper()}] {title}",
                         "text": f"#### [{severity.upper()}] {title}\n\n{text}"},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            status = resp.status
            raw = b""
            try:
                raw = resp.read()
            except Exception:   # noqa: BLE001 — body 读失败按不可解析处理（不新增失败面）
                raw = b""
        _LAST_SENT[key] = now   # 失败也进 dedup 窗：坏 webhook 不该被告警风暴反复打
        if not ok:
            logger.warning("ops-alert HTTP %s: %s", status, title)
            return False
        # 批次7（ultra alerting:73）：钉钉自定义机器人在 **HTTP 200** 里用 errcode 报失败
        # （310000 签名不符/关键词过滤、130101 机器人限流 20 条/分）——只看 HTTP 状态时，
        # RAG_OPS_ALERT_SECRET 配错或突发超限会让全部运维告警（parity 漂移/SLO/熔断，
        # 15+ 调用点）静默蒸发且本函数还返回 True。解析 body，errcode!=0 记 ERROR 返 False。
        try:
            bj = json.loads(raw.decode("utf-8"))
            errcode = int(bj.get("errcode", 0))
        except Exception:   # noqa: BLE001 — body 非 JSON：保守按已送达（旧行为，不误报）
            return True
        if errcode != 0:
            logger.error("ops-alert DingTalk errcode=%s errmsg=%s: %s",
                         errcode, bj.get("errmsg"), title)
            return False
        return True
    except Exception as e:
        # fail-open: an alert send failure must never abort the operation that triggered it
        logger.warning("ops-alert send failed (non-fatal): %s err=%s", title, e)
        return False
