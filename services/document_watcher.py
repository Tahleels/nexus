"""
document_watcher.py — File-system watcher for automatic document ingestion.

When a file is dropped into a watched directory:
  1. It is ingested by the document processor (process_document)
  2. The original file is moved to <dir>/archive/ (created if missing)
  3. The document is scoped to the configured scope/scope_id/target_user_id

Uses watchdog (pip install watchdog).
Soft-falls back to polling if watchdog is unavailable.

Usage:
    from services.document_watcher import get_watcher
    watcher = get_watcher()
    watcher.start()          # called once at app startup
    watcher.stop()           # called at app teardown
"""

from __future__ import annotations

from logging_config import get_logger
import os
import shutil
import sys
import time
import threading
from typing import Dict, List, Optional

logger = get_logger(__name__)

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# File extensions the processor supports
_SUPPORTED = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm",
    ".csv", ".pptx", ".ppt", ".html", ".htm", ".eml",
    ".msg", ".png", ".jpg", ".jpeg", ".tiff", ".bmp",
    ".txt", ".md", ".json",
}

# Directory names that should never be watched even if a user adds a parent path
_SKIP_DIRS = {
    "venv", ".venv", "env", ".env",
    "__pycache__", ".git", ".svn", ".hg",
    "node_modules", ".tox", "dist", "build",
    "archive",   # our own output folder — never re-ingest archived files
    ".idea", ".vscode",
}

# Allow newly-written files to stabilise before ingesting (seconds)
_SETTLE_DELAY = 3


def _is_safe_watch_path(folder_path: str) -> bool:
    """
    Return False if the path is inside a known non-document directory
    (venv, __pycache__, .git, etc.) that should never be auto-ingested.
    """
    norm = os.path.normpath(folder_path)
    for part in norm.replace("\\", "/").split("/"):
        if part.lower() in _SKIP_DIRS:
            return False
    return True


def _archive_path(folder_path: str) -> str:
    """Return `<folder_path>/archive`, creating it first if it doesn't exist."""
    archive = os.path.join(folder_path, "archive")
    os.makedirs(archive, exist_ok=True)
    return archive


def _ingest_file(file_path: str,
                 scope:         str,
                 scope_id:      Optional[int],
                 target_user_id: Optional[int],
                 uploaded_by_id:   int = 0,
                 uploaded_by_name: str = "watcher",
                 watch_id:         Optional[int] = None,
                 key_metrics:      Optional[list] = None,
                 user_ids:         Optional[list] = None) -> bool:
    """
    Ingest a single file via process_document, assign it to configured
    users, then move it into the directory's archive/ subfolder.

    Args:
        file_path: Absolute path of the file to ingest.
        scope: Document visibility scope ('user'|'global'|'department'|'project').
        scope_id: dept_id/project_id when scope is 'department'/'project'.
        target_user_id: For scope='user' watches, the user the doc is
            attributed to (so get_visible_doc_ids restricts it to them).
        uploaded_by_id: Fallback uploader ID when scope isn't 'user' or
            no target_user_id/user_ids apply.
        uploaded_by_name: Display name recorded as the uploader.
        watch_id: ID of the `document_watch_dirs` row driving this ingestion
            (used for connector_logs / files_ingested bookkeeping).
        key_metrics: Optional list of metric names to extract via the LLM
            at ingest time.
        user_ids: Optional explicit list of user IDs to assign the document
            to after ingestion.

    Returns:
        True if the file was successfully ingested and archived, False if
        the extension is unsupported, ingestion failed, or an error occurred.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _SUPPORTED:
        logger.debug("document_watcher: skipping unsupported file: %s", file_path)
        return False

    try:
        from agents.core.knowledge.document_processor import process_document
        # For user-scoped watches, attribute the doc to the configured target user
        # so that get_visible_doc_ids restricts retrieval to that user only.
        effective_uploader_id = (
            target_user_id if (scope == "user" and target_user_id and not user_ids)
            else uploaded_by_id
        )
        result = process_document(
            file_path=file_path,
            uploaded_by_id=effective_uploader_id,
            uploaded_by_name=uploaded_by_name,
            scope=scope or "global",
            scope_id=scope_id,
            source_watch_id=watch_id,
            source_watch_type="filesystem" if watch_id else "",
            key_metrics=key_metrics or None,
        )
        if not result.get("success"):
            err = result.get("error", "unknown error")
            logger.warning("document_watcher: ingest failed for %s: %s", file_path, err)
            _write_log(watch_id, os.path.dirname(file_path),
                       os.path.basename(file_path), "error", 0, str(err))
            return False

        # Assign to specific users if configured
        doc_id = result.get("document_id")
        if doc_id and user_ids:
            try:
                from agents.core.knowledge.document_processor import assign_document
                from app_db import get_app_db
                with get_app_db() as conn:
                    cur = conn.cursor()
                    for uid in user_ids:
                        cur.execute("SELECT username FROM users WHERE id = ?", uid)
                        row = cur.fetchone()
                        uname = row[0] if row else str(uid)
                        assign_document(doc_id, uid, uname, 0, "watcher")
            except Exception as ae:
                logger.warning("document_watcher: assign_document failed: %s", ae)

        # Move to archive
        archive = _archive_path(os.path.dirname(file_path))
        dest    = os.path.join(archive, os.path.basename(file_path))
        if os.path.exists(dest):
            base, ext2 = os.path.splitext(os.path.basename(file_path))
            dest = os.path.join(archive, f"{base}_{int(time.time())}{ext2}")
        shutil.move(file_path, dest)
        chunks = result.get("chunk_count", 0)
        logger.info("document_watcher: ingested %s -> %s (%d chunks)",
                    file_path, dest, chunks)
        _write_log(watch_id, os.path.dirname(file_path),
                   os.path.basename(file_path), "success", chunks)

        # Update files_ingested counter
        _bump_counter(watch_id, os.path.dirname(file_path))
        return True

    except Exception as exc:
        logger.warning("document_watcher: error ingesting %s: %s", file_path, exc)
        _write_log(watch_id, os.path.dirname(file_path),
                   os.path.basename(file_path), "error", 0, str(exc))
        return False


def _write_log(watch_id: Optional[int], folder_path: str,
               filename: str, status: str,
               chunk_count: int = 0, error_msg: str = "") -> None:
    """
    Best-effort write of one ingestion outcome to `connector_logs`.

    Args:
        watch_id: Owning `document_watch_dirs` row ID (0/None if ad-hoc).
        folder_path: Folder being watched, used as a label fallback.
        filename: File that was (attempted to be) ingested.
        status: "success" or "error".
        chunk_count: Number of chunks stored (0 on error).
        error_msg: Error detail when status is "error".

    Never raises — logging failures are swallowed.
    """
    try:
        from database.app_db import log_connector_event, get_app_db
        label = ""
        if watch_id:
            try:
                with get_app_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT label FROM document_watch_dirs WHERE id = ?", watch_id)
                    row = cur.fetchone()
                    label = row[0] if row else folder_path
            except Exception:
                label = folder_path
        log_connector_event("filesystem", watch_id or 0, label or folder_path,
                            filename, status, chunk_count, error_msg)
    except Exception:
        pass


def _bump_counter(watch_id: Optional[int], folder_path: str = "") -> None:
    """
    Increment `document_watch_dirs.files_ingested` and refresh last_scanned.

    Args:
        watch_id: Row ID to update directly, when known.
        folder_path: Fallback match key (normalized both with/without a
            trailing slash) when watch_id is not available.

    Never raises — failures are swallowed.
    """
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            if watch_id:
                cur.execute("""
                    UPDATE document_watch_dirs
                    SET files_ingested = files_ingested + 1, last_scanned = GETUTCDATE()
                    WHERE id = ?
                """, watch_id)
            else:
                # fallback: match by normalised path
                cur.execute("""
                    UPDATE document_watch_dirs
                    SET files_ingested = files_ingested + 1, last_scanned = GETUTCDATE()
                    WHERE folder_path = ? OR folder_path = ?
                """, folder_path, folder_path.rstrip("\\/"))
            conn.commit()
    except Exception:
        pass


# ── Watchdog-based handler ────────────────────────────────────────────────────

_POLL_INTERVAL = 30   # seconds between periodic folder scans (fallback for network shares)


class _DocEventHandler:
    """Watchdog FileSystemEventHandler that ingests new/moved-in files."""

    def __init__(self, watch_id: int, folder_path: str,
                 scope: str, scope_id: Optional[int],
                 target_user_id: Optional[int],
                 key_metrics: Optional[list] = None,
                 user_ids: Optional[list] = None):
        """
        Args:
            watch_id: Owning `document_watch_dirs` row ID.
            folder_path: Absolute path of the watched directory.
            scope: Document visibility scope for files ingested here.
            scope_id: dept_id/project_id when scope is 'department'/'project'.
            target_user_id: User to attribute documents to for scope='user'.
            key_metrics: Optional metric names to extract via LLM at ingest.
            user_ids: Optional explicit assignment targets after ingestion.
        """
        self.watch_id       = watch_id
        self.folder_path    = folder_path
        self.scope          = scope
        self.scope_id       = scope_id
        self.target_user_id = target_user_id
        self.key_metrics    = key_metrics or []
        self.user_ids       = user_ids or []
        self._pending: Dict[str, float] = {}
        self._lock       = threading.Lock()
        self._last_poll  = 0.0   # tracks periodic scan for network-share fallback

    def _is_excluded(self, path: str) -> bool:
        """Return True if path is inside a directory that should be skipped."""
        if not _is_safe_watch_path(path):
            return True
        # Also block the archive subfolder directly
        archive_prefix = os.path.normpath(os.path.join(self.folder_path, "archive"))
        return os.path.normpath(path).startswith(archive_prefix + os.sep)

    def _schedule(self, path: str) -> None:
        """Queue `path` for ingestion after the settle delay, unless excluded."""
        if self._is_excluded(path):
            return
        with self._lock:
            self._pending[path] = time.time() + _SETTLE_DELAY

    def _poll_folder(self) -> None:
        """
        Periodic fallback scan — queues every supported file in the folder.
        Ensures network shares (where watchdog events don't fire) still get processed.
        """
        try:
            for fname in os.listdir(self.folder_path):
                fpath = os.path.join(self.folder_path, fname)
                if not os.path.isfile(fpath):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _SUPPORTED:
                    continue
                self._schedule(fpath)
        except Exception as exc:
            logger.debug("document_watcher: poll_folder error for %s: %s", self.folder_path, exc)

    def _drain(self) -> None:
        """
        Called repeatedly from the watcher's background drain thread.

        Runs the periodic network-share poll when due, then ingests any
        pending paths whose settle delay has elapsed.
        """
        # Periodic poll — catches files on network shares where watchdog is silent
        now = time.time()
        if now - self._last_poll >= _POLL_INTERVAL:
            self._last_poll = now
            self._poll_folder()

        with self._lock:
            ready = [p for p, t in list(self._pending.items()) if t <= now]
            for p in ready:
                del self._pending[p]
        for path in ready:
            if os.path.isfile(path) and not self._is_excluded(path):
                _ingest_file(path, self.scope, self.scope_id, self.target_user_id,
                             watch_id=self.watch_id,
                             key_metrics=self.key_metrics or None,
                             user_ids=self.user_ids or None)

    # Called by watchdog
    def dispatch(self, event) -> None:
        """
        Handle a raw watchdog filesystem event by scheduling the affected
        file for ingestion (created/modified/moved-in events only).

        Args:
            event: A watchdog FileSystemEvent instance.
        """
        if event.is_directory:
            return
        if hasattr(event, "dest_path"):
            self._schedule(event.dest_path)
        elif hasattr(event, "src_path"):
            etype = type(event).__name__
            if "Created" in etype or "Modified" in etype:
                self._schedule(event.src_path)


# ═══════════════════════════════════════════════════════════════════════════════
# WatcherService
# ═══════════════════════════════════════════════════════════════════════════════

class DocumentWatcherService:
    """
    Manages a set of watched filesystem directories, ingesting new files
    via watchdog events (with a periodic polling fallback for network
    shares / when watchdog isn't installed).
    """

    def __init__(self):
        """Initialize empty handler/observer state; call start() to begin watching."""
        self._handlers:  Dict[str, _DocEventHandler] = {}  # folder_path → handler
        self._observer  = None
        self._drain_thread = None
        self._running   = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog observer and load watched dirs from DB."""
        self._running = True
        self._try_start_observer()
        self._load_from_db()
        self._start_drain_thread()

    def stop(self) -> None:
        """Stop the background drain loop and the watchdog observer (if running)."""
        self._running = False
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception:
                pass

    # ── Directory management ─────────────────────────────────────────────────

    def add_watch(self, watch_id: int, folder_path: str, label: str,
                  scope: str = "user",
                  scope_id: Optional[int] = None,
                  target_user_id: Optional[int] = None,
                  key_metrics: Optional[list] = None,
                  user_ids: Optional[list] = None) -> None:
        """
        Register a directory to watch, scheduling it with the watchdog
        observer if one is running. No-ops if already watched, the folder
        doesn't exist, or the path is excluded (`_is_safe_watch_path`).

        Args:
            watch_id: Owning `document_watch_dirs` row ID.
            folder_path: Absolute path of the directory to watch.
            label: Display label (unused internally, kept for parity with DB row).
            scope: Document visibility scope for files ingested here.
            scope_id: dept_id/project_id when scope is 'department'/'project'.
            target_user_id: User to attribute documents to for scope='user'.
            key_metrics: Optional metric names to extract via LLM at ingest.
            user_ids: Optional explicit assignment targets after ingestion.
        """
        if folder_path in self._handlers:
            return
        if not os.path.isdir(folder_path):
            logger.warning("document_watcher.add_watch: folder not found: %s", folder_path)
            return
        if not _is_safe_watch_path(folder_path):
            logger.warning("document_watcher.add_watch: refused excluded path: %s", folder_path)
            return

        handler = _DocEventHandler(
            watch_id=watch_id, folder_path=folder_path,
            scope=scope, scope_id=scope_id, target_user_id=target_user_id,
            key_metrics=key_metrics or [], user_ids=user_ids or [],
        )
        self._handlers[folder_path] = handler

        if self._observer:
            try:
                self._observer.schedule(
                    self._make_watchdog_handler(handler),
                    folder_path,
                    recursive=False,
                )
                logger.info("document_watcher: watching %s (scope=%s)", folder_path, scope)
            except Exception as exc:
                logger.warning("document_watcher.add_watch: watchdog schedule failed: %s", exc)

    def remove_watch(self, folder_path: str) -> None:
        """
        Stop watching a directory.

        Watchdog has no clean API to unschedule a single path, so this
        drops the handler and restarts the observer against the remaining
        watched directories.

        Args:
            folder_path: Absolute path previously passed to add_watch.
        """
        self._handlers.pop(folder_path, None)
        # Watchdog doesn't easily unschedule individual paths; restart observer
        self._reload_observer()

    def scan_now(self, folder_path: str, scope: str, scope_id: Optional[int],
                 target_user_id: Optional[int],
                 triggered_by_id:   int = 0,
                 triggered_by_name: str = "",
                 watch_id: Optional[int] = None,
                 user_ids: Optional[list] = None) -> int:
        """
        Process all existing supported files currently in the directory
        (used for the "scan now" manual trigger, not the event-driven path).

        Args:
            folder_path: Absolute path of the directory to scan.
            scope: Document visibility scope for files ingested here.
            scope_id: dept_id/project_id when scope is 'department'/'project'.
            target_user_id: User to attribute documents to for scope='user'.
            triggered_by_id: ID of the user who triggered this manual scan.
            triggered_by_name: Display name of the triggering user.
            watch_id: Owning `document_watch_dirs` row ID; resolved from the
                DB by folder_path if not supplied.
            user_ids: Explicit assignment targets; resolved from the DB by
                folder_path if not supplied.

        Returns:
            Number of files successfully ingested.

        Raises:
            FileNotFoundError: If folder_path is not an existing directory.
        """
        if not os.path.isdir(folder_path):
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        # Resolve watch_id and user_ids from DB if not provided
        if watch_id is None or user_ids is None:
            try:
                import json as _json
                from app_db import get_app_db
                with get_app_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, user_ids_json FROM document_watch_dirs
                        WHERE folder_path = ?
                    """, folder_path)
                    row = cur.fetchone()
                    if row:
                        if watch_id is None:
                            watch_id = row[0]
                        if user_ids is None and row[1]:
                            user_ids = _json.loads(row[1])
            except Exception:
                pass

        archive = _archive_path(folder_path)
        ingested = 0
        for fname in os.listdir(folder_path):
            fpath = os.path.join(folder_path, fname)
            # Skip directories (including archive, venv, __pycache__, etc.)
            if os.path.isdir(fpath):
                continue
            if not _is_safe_watch_path(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _SUPPORTED:
                continue
            ok = _ingest_file(
                fpath, scope, scope_id, target_user_id,
                uploaded_by_id=triggered_by_id,
                uploaded_by_name=triggered_by_name or "watcher",
                watch_id=watch_id,
                user_ids=user_ids or None,
            )
            if ok:
                ingested += 1

        # Update last_scanned
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE document_watch_dirs SET last_scanned = GETUTCDATE()
                    WHERE folder_path = ?
                """, folder_path)
                conn.commit()
        except Exception:
            pass

        return ingested

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_from_db(self) -> None:
        """Load all enabled rows from `document_watch_dirs` and register them via add_watch."""
        import json as _json
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, folder_path, label, scope, scope_id,
                           target_user_id, key_metrics, user_ids_json
                    FROM document_watch_dirs WHERE enabled = 1
                """)
                rows = cur.fetchall()
            for row in rows:
                wid, fpath, label, scope, scope_id, target_uid, km_raw, uid_raw = row
                key_metrics = _json.loads(km_raw)  if km_raw  else []
                user_ids    = _json.loads(uid_raw) if uid_raw else []
                self.add_watch(wid, fpath, label or fpath, scope or "user",
                               scope_id, target_uid,
                               key_metrics=key_metrics, user_ids=user_ids)
        except Exception as exc:
            logger.warning("document_watcher: could not load dirs from DB: %s", exc)

    def _try_start_observer(self) -> None:
        """Start a watchdog Observer if the `watchdog` package is installed; logs and no-ops otherwise."""
        try:
            from watchdog.observers import Observer
            self._observer = Observer()
            self._observer.start()
            logger.info("document_watcher: watchdog Observer started")
        except ImportError:
            logger.info("document_watcher: watchdog not installed — "
                        "run 'pip install watchdog'. Using poll-only mode.")
        except Exception as exc:
            logger.warning("document_watcher: Observer start failed: %s", exc)

    def _make_watchdog_handler(self, handler: _DocEventHandler):
        """Wrap our handler into watchdog's expected interface."""
        try:
            from watchdog.events import FileSystemEventHandler
            class _Bridge(FileSystemEventHandler):
                def dispatch(self_bridge, event):
                    handler.dispatch(event)
            return _Bridge()
        except ImportError:
            return None

    def _reload_observer(self) -> None:
        """Stop and recreate the watchdog Observer, re-scheduling all current handlers."""
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
            self._observer = None
        self._try_start_observer()
        for fpath, handler in self._handlers.items():
            if self._observer:
                try:
                    from watchdog.observers import Observer
                    self._observer.schedule(
                        self._make_watchdog_handler(handler),
                        fpath, recursive=False)
                except Exception:
                    pass

    def _start_drain_thread(self) -> None:
        """Background thread that processes settled file events."""
        def _loop():
            while self._running:
                for handler in list(self._handlers.values()):
                    try:
                        handler._drain()
                    except Exception:
                        pass
                time.sleep(1)

        self._drain_thread = threading.Thread(target=_loop, daemon=True)
        self._drain_thread.start()


# ── Singleton ─────────────────────────────────────────────────────────────────

_watcher: Optional[DocumentWatcherService] = None


def get_watcher() -> DocumentWatcherService:
    """Return the process-wide singleton DocumentWatcherService, creating it on first call."""
    global _watcher
    if _watcher is None:
        _watcher = DocumentWatcherService()
    return _watcher
