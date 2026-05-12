"""Email delivery helpers for scheduled job output — extracted from
scheduler_service.py in Phase 3 Slice 6. Fully self-contained (env vars +
stdlib smtplib/email only), used by scheduler_service.py's job pipeline to
deliver generated artifacts as attachments.
"""
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from email.mime.base      import MIMEBase
from email                import encoders
from typing import List, Optional, Dict

from logging_config import get_logger

logger = get_logger(__name__)


def _fix_b64_padding(s: str) -> str:
    """Pad a base64 string with trailing ``=`` to a multiple of 4 chars, if needed."""
    return s + "=" * (-len(s) % 4)


def _to_bytes(data, is_base64: bool = False) -> bytes:
    """Coerce attachment ``data`` (bytes, base64 str, or plain str) to raw bytes."""
    if isinstance(data, bytes):
        return data
    if is_base64 and isinstance(data, str):
        import base64
        return base64.b64decode(_fix_b64_padding(data))
    return data.encode("utf-8") if isinstance(data, str) else str(data).encode("utf-8")


def _send_email(recipients: List[str], subject: str, body: str,
                attachments: Optional[List[Dict]] = None) -> None:
    """Send an HTML email with optional attachments via SMTP.

    Reads SMTP host/port/credentials from environment variables
    (``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USER``, ``SMTP_PASS``, ``SMTP_FROM``).
    No-ops (with a warning log) if ``SMTP_USER``/``SMTP_PASS`` are not set.

    Args:
        recipients: Destination email addresses.
        subject: Email subject line.
        body: HTML body content.
        attachments: List of ``{filename, data, is_base64}`` dicts; ``data``
            is converted to bytes via `_to_bytes` before attaching.

    Raises:
        Exception: Any SMTP connection/auth/send error propagates to the
            caller (callers in `_execute_job` catch and log it).
    """
    host  = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port  = int(os.getenv("SMTP_PORT", "587"))
    user  = os.getenv("SMTP_USER", "")
    pw    = os.getenv("SMTP_PASS", "")
    from_ = os.getenv("SMTP_FROM", user)

    if not user or not pw:
        logger.warning("  SMTP not configured — skipping email")
        return

    msg = MIMEMultipart()
    msg["From"]    = from_
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    for att in (attachments or []):
        raw  = _to_bytes(att["data"], is_base64=att.get("is_base64", False))
        part = MIMEBase("application", "octet-stream")
        part.set_payload(raw)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{att["filename"]}"',
        )
        msg.attach(part)

    with smtplib.SMTP(host, port) as s:
        s.ehlo(); s.starttls(); s.login(user, pw)
        s.sendmail(from_, recipients, msg.as_string())
    logger.info(f" Email sent to {recipients}")
