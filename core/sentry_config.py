"""
sentry_config.py — Sentry error-tracking integration for Nexus AI Portal.

Set in .env:
    SENTRY_DSN          Your Sentry project DSN (required to activate)
    SENTRY_TRACES_RATE  Performance tracing sample rate (default: 0.1 prod / 1.0 dev)

Integrations:
  FlaskIntegration      — captures request context and 5xx responses
  SqlalchemyIntegration — captures slow / failing DB queries
  LoggingIntegration    — WARNING+ → breadcrumbs, ERROR+ → Sentry events
"""
import logging
import os

logger = logging.getLogger("app.sentry")


def init_sentry(env: str = "development") -> bool:
    """
    Initialise the Sentry SDK. Returns True when active, False when DSN is absent.
    Safe to call with no DSN set — simply becomes a no-op.
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN not configured — Sentry disabled")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration

        default_rate = "0.1" if env == "production" else "1.0"
        traces_rate = float(os.getenv("SENTRY_TRACES_RATE", default_rate))

        sentry_sdk.init(
            dsn=dsn,
            environment=env,
            release=os.getenv("APP_VERSION", "unknown"),
            integrations=[
                FlaskIntegration(transaction_style="url"),
                SqlalchemyIntegration(),
                LoggingIntegration(
                    level=logging.WARNING,    # WARNING+ → breadcrumbs
                    event_level=logging.ERROR,  # ERROR+ → Sentry events
                ),
            ],
            traces_sample_rate=traces_rate,
            send_default_pii=False,
            before_send=_before_send,
        )

        logger.info("Sentry active (env=%s, traces=%.2f)", env, traces_rate)
        return True

    except ImportError:
        logger.warning("sentry-sdk not installed — pip install 'sentry-sdk[flask]'")
        return False
    except Exception as exc:
        logger.warning("Sentry init failed: %s", exc)
        return False


def set_user_context(user_id: int, username: str, role: str) -> None:
    """Attach the authenticated user to the current Sentry scope."""
    try:
        import sentry_sdk
        sentry_sdk.set_user({"id": user_id, "username": username, "role": role})
    except Exception:
        pass


def capture_exception(exc: Exception, extra: dict = None) -> None:
    """Manually push an exception to Sentry with optional key-value extras."""
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            if extra:
                for k, v in extra.items():
                    scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _before_send(event, hint):
    """Drop system-exit signals — they are not application errors."""
    exc_info = hint.get("exc_info")
    if exc_info:
        exc_type = exc_info[0]
        if exc_type and exc_type.__name__ in ("KeyboardInterrupt", "SystemExit"):
            return None
    return event
