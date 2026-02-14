"""
logging_config.py — Feature-separated logging for Nexus AI Portal.

Each feature writes to its own rotating log file under logs/ AND propagates
to the combined logs/app.log so you always have a single unified view too.

Log directory layout:
    logs/
        app.log         combined — every message from every feature
        auth.log        login, logout, sessions, token limits
        nlq.log         NLQ engine, schema graph, SQL generation
        agents.log      agents hub, agent manager, orchestrator, tools
        knowledge.log   knowledge base, RAG pipeline, vector store, doc processor
        jobs.log        scheduled jobs, job manager, execution logger
        workspace.log   workspace blueprint + services
        services.log    document watcher, SharePoint watcher, email intelligence
        database.log    database_manager, app_db, org_db, clients_db
        training.log    training blueprint, training_db, bi_training_db
        generators.log  PPT, dashboard, report, infographic, chart generators
        requests.log    HTTP request / response trace (method, path, status, rid)

Usage
-----
Startup (once, in app.py):
    from logging_config import setup_logging
    logger = setup_logging(env="production")

Any module:
    from logging_config import get_logger
    logger = get_logger(__name__)
"""
import logging
import os
from logging.handlers import RotatingFileHandler

try:
    from pythonjsonlogger import jsonlogger as _jsonlogger
    _HAS_JSON = True
except ImportError:
    _HAS_JSON = False

_ROOT    = "app"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR  = os.path.join(_BASE_DIR, "logs")

# fmt: off
_PLAIN_FMT  = "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s"
_JSON_FIELDS = "%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d %(funcName)s"
# fmt: on

# ── Feature → (log filename, [logger names]) ─────────────────
_FEATURES: dict = {
    "auth":       ("auth.log",       ["app.auth",       "app.token_limits"]),
    "nlq":        ("nlq.log",        ["app.nlq_engine", "app.schema_graph"]),
    "agents":     ("agents.log",     ["app.agents_hub_bp", "app.agent_manager",
                                      "app.engine",        "app.registry"]),
    "knowledge":  ("knowledge.log",  ["app.knowledge_bp",       "app.rag_pipeline",
                                      "app.vector_store",        "app.document_processor"]),
    "jobs":       ("jobs.log",       ["app.app_jobs_routes", "app.job_manager",
                                      "app.scheduler_service", "app.execution_logger"]),
    "workspace":  ("workspace.log",  ["app.workspace_bp",             "app.workspace_db",
                                      "app.workspace_export_service",  "app.workspace_openai_service"]),
    "services":   ("services.log",   ["app.document_watcher", "app.sharepoint_watcher",
                                      "app.email_intelligence"]),
    "database":   ("database.log",   ["app.database_manager", "app.app_db",
                                      "app.org_db",            "app.clients_db"]),
    "training":   ("training.log",   ["app.training_bp", "app.training_db",
                                      "app.bi_training_db"]),
    "generators": ("generators.log", ["app.ppt_generator",       "app.dashboard_generator",
                                      "app.reportgenerator",      "app.infographicgenerator",
                                      "app.chartselector",        "app.artifact_builder"]),
    "requests":   ("requests.log",   ["app.requests"]),
}

# Third-party loggers that flood the output
_NOISY = (
    "urllib3", "httpx", "httpcore",
    "openai", "anthropic", "langchain", "langsmith",
    "transformers", "sentence_transformers",
    "chromadb", "lancedb",
    "sqlalchemy.engine", "sqlalchemy.pool",
    "APScheduler",
)


def _make_rotating_handler(
    filepath: str,
    env: str,
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> RotatingFileHandler:
    h = RotatingFileHandler(filepath, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    if env == "production" and _HAS_JSON:
        h.setFormatter(_jsonlogger.JsonFormatter(_JSON_FIELDS))
    else:
        h.setFormatter(logging.Formatter(_PLAIN_FMT))
    h.setLevel(logging.INFO)
    return h


def setup_logging(
    env: str = "development",
    max_bytes: int = 5_000_000,
    backup_count: int = 3,
) -> logging.Logger:
    """
    Initialise the 'app' root logger plus one handler per feature.
    Idempotent — safe to call multiple times.
    Returns the root 'app' logger (writes to logs/app.log + console).
    """
    root = logging.getLogger(_ROOT)
    if root.handlers:
        return root

    os.makedirs(_LOG_DIR, exist_ok=True)

    root.setLevel(logging.DEBUG if env == "development" else logging.INFO)

    # ── Combined log (every message) ─────────────────────────
    combined_handler = _make_rotating_handler(
        os.path.join(_LOG_DIR, "app.log"), env, max_bytes, backup_count
    )
    root.addHandler(combined_handler)

    # ── Console (dev: DEBUG, prod: WARNING) ───────────────────
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(_PLAIN_FMT))
    console_handler.setLevel(logging.DEBUG if env == "development" else logging.WARNING)
    root.addHandler(console_handler)

    # ── Per-feature log files ─────────────────────────────────
    for _feature, (log_file, logger_names) in _FEATURES.items():
        feature_handler = _make_rotating_handler(
            os.path.join(_LOG_DIR, log_file), env, max_bytes, backup_count
        )
        for name in logger_names:
            lg = logging.getLogger(name)
            lg.addHandler(feature_handler)
            lg.propagate = True   # still reaches root → logs/app.log

    # ── Silence chatty third-party libs ──────────────────────
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root.info("Logging initialised (env=%s, log_dir=%s)", env, _LOG_DIR)
    return root


def get_logger(name: str) -> logging.Logger:
    """
    Return a namespaced child logger under 'app'.
    Always pass __name__ from the calling module.

    Examples:
        get_logger("nlq_engine")            → app.nlq_engine  → logs to nlq.log + app.log
        get_logger("agents.core.knowledge") → app.knowledge   → logs to knowledge.log + app.log
        get_logger("app.auth")              → app.auth        → logs to auth.log + app.log
    """
    if name == _ROOT or name.startswith(_ROOT + "."):
        return logging.getLogger(name)
    # Use only the last component so nested package paths collapse to the module name
    short = name.rsplit(".", 1)[-1] if "." in name else name
    return logging.getLogger(f"{_ROOT}.{short}")
