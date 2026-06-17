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


def send_ops_alert(title: str, text: str, *, severity: str = "warning",
                   dedup_key: Optional[str] = None, timeout: float = 5.0) -> bool:
    """Post a Markdown alert. Returns True on a 2xx HTTP send; False otherwise (or no-op).
    Never raises. severity ∈ {info, warning, critical} (decorative; DingTalk doesn't use it).
    """
    webhook = (os.environ.get("RAG_OPS_ALERT_WEBHOOK") or "").strip()
    secret = (os.environ.get("RAG_OPS_ALERT_SECRET") or "").strip()
    if not webhook:
        # config-gated no-op: the alert path is shipped, activates on first env var set
        logger.info("ops-alert no-op (RAG_OPS_ALERT_WEBHOOK unset): [%s] %s", severity, title)
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
        _LAST_SENT[key] = now
        if not ok:
            logger.warning("ops-alert HTTP %s: %s", resp.status, title)
        return ok
    except Exception as e:
        # fail-open: an alert send failure must never abort the operation that triggered it
        logger.warning("ops-alert send failed (non-fatal): %s err=%s", title, e)
        return False
