# =============================================================
# config.py  —  Central configuration for Nexus Portal
# =============================================================
# HOW TO USE:
#   1. Copy this file to your project root.
#   2. Fill in your actual values below.
#   3. NEVER commit this file to version control.
#      Add  config.py  to your .gitignore immediately.
# =============================================================
"""config.py — Central configuration module for the Nexus AI Portal.

Loaded once at process startup (imported early in app.py, right after
``load_dotenv()`` runs) and then imported by nearly every other module
(``auth``, ``app_db``, ``token_limits``, etc.) as the single source of
truth for environment-derived settings. All values are read from
environment variables via ``os.getenv`` with safe development defaults,
so the same code runs unmodified across local dev / staging / production
as long as the .env file (or real environment) supplies the real values.

Settings surface:
    DB_CONFIG                  SQL Server connection parameters (server,
                                port, database, username, password, driver)
    get_auth_db_connection_string()  pyodbc connection string builder
    SECRET_KEY                 Flask session signing key
    SESSION_COOKIE_*           Cookie name/flags for the auth session cookie
    PERMANENT_SESSION_HOURS    Session/OTP-derived TTL (hours)
    GEMINI_API_KEY / OPENROUTER_API_KEY   LLM provider keys (see llm_providers/)
    SMTP_*                      Outbound mail settings for OTP delivery
    OTP_EXPIRY_MINUTES           OTP validity window
    ENV / DEBUG                 Environment flag (development|production)
"""

import os
import urllib


# ─────────────────────────────────────────────────────────────
# DATABASE  (SQL Server / SSMS)
# ─────────────────────────────────────────────────────────────
DB_CONFIG = {
    "server":   os.getenv("DB_SERVER",   "YOUR_SERVER_NAME"),       # e.g. localhost\SQLEXPRESS
    "port":     os.getenv("DB_PORT",     "1433"),
    "database": os.getenv("DB_DATABASE", "YOUR_DATABASE_NAME"),
    "username": os.getenv("DB_USERNAME", "YOUR_SQL_USERNAME"),
    "password": os.getenv("DB_PASSWORD", "YOUR_SQL_PASSWORD"),
    "driver":   os.getenv("DB_DRIVER",   "ODBC Driver 17 for SQL Server"),
}

def get_auth_db_connection_string() -> str:
    """
    Returns a pyodbc connection string for the auth database.
    Uses SQL Server Authentication by default.
    Switch to Trusted_Connection=yes and remove UID/PWD for Windows Auth.
    """
    params = urllib.parse.quote_plus(
        f"DRIVER={{{DB_CONFIG['driver']}}};"
        f"SERVER={DB_CONFIG['server']},{DB_CONFIG['port']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['username']};"
        f"PWD={DB_CONFIG['password']};"
        f"TrustServerCertificate=yes;"   # remove in strict prod environments
    )
    return f"mssql+pyodbc:///?odbc_connect={params}"


# ─────────────────────────────────────────────────────────────
# FLASK / SESSION
# ─────────────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_THIS_TO_A_RANDOM_64_CHAR_STRING")
# Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"

SESSION_COOKIE_NAME     = "nexus_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE   = False   # ← set True when you add HTTPS in production
PERMANENT_SESSION_HOURS = 8       # session expires after 8 hours of inactivity


# ─────────────────────────────────────────────────────────────
# OPENAI / ANTHROPIC — parked for now (see llm_providers/factory.py).
# Gemini + OpenRouter (both free-tier) are the active providers below.
# ─────────────────────────────────────────────────────────────
# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ─────────────────────────────────────────────────────────────
# LLM PROVIDERS  (see llm_providers/ — the provider-agnostic chat abstraction)
# ─────────────────────────────────────────────────────────────
# Which provider backs a call site when nothing more specific overrides it
# (a hub agent's own `provider` column, a workspace conversation's `provider`
# column, etc). Switching this — or an individual agent's provider — never
# requires a code change, only config/DB data.
DEFAULT_LLM_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "gemini")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Amazon Bedrock. AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN
# may be left blank to fall back to boto3's own default credential chain
# (IAM role, instance profile, ~/.aws/credentials) instead of explicit keys.
AWS_REGION            = os.getenv("AWS_REGION", "")
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN     = os.getenv("AWS_SESSION_TOKEN", "")


# ─────────────────────────────────────────────────────────────
# SMTP  (OTP email delivery)
# ─────────────────────────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST",    "smtp.office365.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "25"))
SMTP_USERNAME = os.getenv("SMTP_USER",    "")
SMTP_PASSWORD = os.getenv("SMTP_PASS",    "")
SMTP_FROM     = os.getenv("SMTP_FROM",    SMTP_USERNAME)
SMTP_USE_TLS  = os.getenv("SMTP_USE_TLS", "true").lower() not in ("false", "0", "no")

OTP_EXPIRY_MINUTES = int(os.getenv("OTP_EXPIRY_MINUTES", "10"))


# ─────────────────────────────────────────────────────────────
# EXTERNAL PORTAL SYNC (optional)
# ─────────────────────────────────────────────────────────────
# Name of an external SQL Server database (same instance) to pull user/project
# data from via cross-database views, e.g. for org-sync with an HR/PM portal.
# Leave unset to disable this feature entirely — nothing hardcodes a specific
# external system's name.
PORTAL_DB_NAME = os.getenv("PORTAL_DB_NAME", "")


# ─────────────────────────────────────────────────────────────
# ENVIRONMENT FLAG
# ─────────────────────────────────────────────────────────────
ENV = os.getenv("FLASK_ENV", "development")   # development | production
DEBUG = ENV == "development"