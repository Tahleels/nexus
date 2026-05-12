"""
teams_crypto.py — Fernet encryption for Teams chat content stored in the DB.

Both agents_hub_bp and workspace_bp import from here so the same key and
prefix are used everywhere.

Key:  TEAMS_MSG_ENCRYPT_KEY env var (Fernet base64-encoded 32-byte key).
      If unset, encrypt/decrypt are no-ops (data stored as plain text).
"""

import os
from logging_config import get_logger

logger = get_logger(__name__)

TEAMS_ENC_PREFIX = "TEAMS_ENC:"


def teams_encrypt(text: str) -> str:
    """Return 'TEAMS_ENC:<token>' or plain text if key is not configured."""
    key = os.environ.get("TEAMS_MSG_ENCRYPT_KEY", "").strip().encode()
    if not key or not text:
        return text
    try:
        from cryptography.fernet import Fernet
        return TEAMS_ENC_PREFIX + Fernet(key).encrypt(text.encode()).decode()
    except Exception as exc:
        logger.warning("[teams-enc] encrypt failed: %s", exc)
        return text


def teams_decrypt(text: str) -> str:
    """Decrypt a 'TEAMS_ENC:<token>' string; return plain text unchanged."""
    if not text or not text.startswith(TEAMS_ENC_PREFIX):
        return text
    key = os.environ.get("TEAMS_MSG_ENCRYPT_KEY", "").strip().encode()
    if not key:
        return text
    try:
        from cryptography.fernet import Fernet
        return Fernet(key).decrypt(text[len(TEAMS_ENC_PREFIX):].encode()).decode()
    except Exception as exc:
        logger.warning("[teams-enc] decrypt failed: %s", exc)
        return text
