"""Email and SMS communication tool implementations.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

def send_email(to: str, subject: str, body: str,
               cc: str = None, html: bool = False, **kwargs) -> dict:
    """Send an email via SMTP using server credentials configured via environment variables.

    Args:
        to (str): Recipient email address.
        subject (str): Email subject line.
        body (str): Email body content (plain text unless ``html=True``).
        cc (str, optional): A single CC email address.
        html (bool): If True, send ``body`` as HTML instead of plain text.
            Defaults to False.
        **kwargs: Orchestrator-injected context (unused directly by this
            tool).

    Returns:
        dict: On success, ``{"success": True, "to": str, "subject": str,
        "message": str}``. If SMTP isn't configured (missing
        ``SMTP_HOST``/``SMTP_USER``), returns ``{"success": False,
        "warning": str, "to": str, "subject": str, "preview": str}``
        without raising. On a send failure, ``{"success": False, "error":
        str, "to": str, "subject": str}``.

    Note:
        Requires environment variables ``SMTP_HOST``, ``SMTP_PORT``
        (default 587), ``SMTP_USER``, ``SMTP_PASS``, and optionally
        ``SMTP_FROM`` (defaults to ``SMTP_USER``). Uses STARTTLS unless
        the port is 465 (implicit TLS).
    """
    host  = os.environ.get("SMTP_HOST", "")
    port  = int(os.environ.get("SMTP_PORT", "587"))
    user  = os.environ.get("SMTP_USER", "")
    pw    = os.environ.get("SMTP_PASS", "")
    from_ = os.environ.get("SMTP_FROM", user)

    if not host or not user:
        return {
            "success": False,
            "warning": "SMTP not configured — set SMTP_HOST, SMTP_USER, SMTP_PASS env vars.",
            "to": to, "subject": subject,
            "preview": body[:200],
        }

    try:
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text      import MIMEText

        msg = MIMEMultipart("alternative")
        msg["From"]    = from_
        msg["To"]      = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        content_type = "html" if html else "plain"
        msg.attach(MIMEText(body, content_type, "utf-8"))

        all_recipients = [to] + ([cc] if cc else [])

        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if port != 465:
                s.starttls()
                s.ehlo()
            s.login(user, pw)
            s.sendmail(from_, all_recipients, msg.as_string())

        return {"success": True, "to": to, "subject": subject,
                "message": f"Email sent successfully to {to}"}
    except Exception as e:
        return {"success": False, "error": str(e), "to": to, "subject": subject}


def send_sms(to: str, message: str, **kwargs) -> dict:
    """Send an SMS via the Twilio REST API using credentials from environment variables.

    Args:
        to (str): Recipient phone number in E.164 format (e.g.
            ``"+14155552671"``).
        message (str): SMS message body.
        **kwargs: Orchestrator-injected context (unused directly by this
            tool).

    Returns:
        dict: On success, ``{"success": True, "sid": str, "to": str,
        "status": str}`` (``sid`` is the Twilio message SID; ``status``
        is typically ``"queued"``). If Twilio isn't configured (missing
        ``TWILIO_SID``/``TWILIO_TOKEN``/``TWILIO_FROM``), returns
        ``{"success": False, "warning": str, "to": str, "preview": str}``
        without raising. On a send failure, ``{"success": False, "error":
        str, "to": str}``.

    Note:
        Requires environment variables ``TWILIO_SID``, ``TWILIO_TOKEN``,
        ``TWILIO_FROM``.
    """
    sid   = os.environ.get("TWILIO_SID", "")
    token = os.environ.get("TWILIO_TOKEN", "")
    from_ = os.environ.get("TWILIO_FROM", "")

    if not sid or not token or not from_:
        return {
            "success": False,
            "warning": "Twilio not configured — set TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM.",
            "to": to, "preview": message[:100],
        }

    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({
            "To":   to, "From": from_, "Body": message
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data,
            headers={"Authorization": "Basic " + __import__("base64").b64encode(
                f"{sid}:{token}".encode()).decode()},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        return {"success": True, "sid": result.get("sid"),
                "to": to, "status": result.get("status", "queued")}
    except Exception as e:
        return {"success": False, "error": str(e), "to": to}
