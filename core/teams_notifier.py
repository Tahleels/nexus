"""
teams_notifier.py — Microsoft Teams Incoming Webhook alerting for Nexus AI Portal.

Set in .env:
    TEAMS_WEBHOOK_URL   The "Incoming Webhook" connector URL for your Teams channel

Usage — direct call:
    from teams_notifier import send_teams_alert
    send_teams_alert("DB connection lost", str(exc), severity="error")

Usage — as a logging handler (attach once at startup):
    from teams_notifier import TeamsAlertHandler
    import logging
    logging.getLogger("app").addHandler(TeamsAlertHandler())

How to get the webhook URL:
    Teams channel → ··· → Connectors → Incoming Webhook → Configure → copy URL
"""
import json
import logging
import os
import socket
import time
import traceback
from datetime import datetime, timezone

import requests

logger = logging.getLogger("app.teams")

_COLORS = {"error": "FF0000", "warning": "FFA500", "info": "0078D4", "success": "00B050"}
_ICONS  = {"error": "🚨",     "warning": "⚠️",      "info": "ℹ️",     "success": "✅"}


def send_teams_alert(
    title: str,
    message: str,
    severity: str = "error",
    extra: dict = None,
) -> bool:
    """
    Send a MessageCard to the configured Teams channel.
    Returns True on success, False when the webhook is not set or the request fails.
    """
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False

    color = _COLORS.get(severity, _COLORS["error"])
    icon  = _ICONS.get(severity, _ICONS["error"])
    env   = os.getenv("FLASK_ENV", "development")

    facts = [
        {"name": "Environment", "value": env},
        {"name": "Host",        "value": socket.gethostname()},
        {"name": "Time (UTC)",  "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")},
    ]
    if extra:
        facts += [{"name": k, "value": str(v)[:300]} for k, v in extra.items()]

    payload = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": color,
        "summary": title,
        "sections": [
            {
                "activityTitle": f"{icon} **{title}**",
                "activitySubtitle": f"Nexus AI Portal · `{env}`",
                "activityText": f"<pre>{message[:2000]}</pre>",
                "facts": facts,
            }
        ],
    }

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("Teams webhook delivery failed: %s", exc, exc_info=False)
        return False


class TeamsAlertHandler(logging.Handler):
    """
    Logging handler that forwards ERROR+ records to the Teams channel.

    Includes a per-message cooldown (default 60 s) to prevent flood-sending
    the same repeated error.

    Attach to the root 'app' logger so all modules are covered:
        logging.getLogger("app").addHandler(TeamsAlertHandler())
    """

    _COOLDOWN_SECS = 60

    def __init__(self, level: int = logging.ERROR):
        super().__init__(level)
        self._last_sent: dict[str, float] = {}
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d):\n%(message)s"
        ))

    def emit(self, record: logging.LogRecord) -> None:
        key = f"{record.name}:{record.getMessage()[:120]}"
        now = time.monotonic()
        if now - self._last_sent.get(key, 0.0) < self._COOLDOWN_SECS:
            return
        self._last_sent[key] = now

        try:
            severity = "error" if record.levelno >= logging.ERROR else "warning"
            title    = f"[{record.name}] {record.levelname}"
            body     = self.format(record)

            extra = {
                "Module":   record.module,
                "Function": record.funcName,
                "Line":     record.lineno,
            }
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info))
                extra["Traceback"] = tb[:600]

            send_teams_alert(title, body, severity=severity, extra=extra)
        except Exception:
            self.handleError(record)
