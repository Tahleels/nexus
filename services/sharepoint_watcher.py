"""
sharepoint_watcher.py — SharePoint Online document ingestion via Microsoft Graph API.

Workflow per watch config:
  1. Poll a SharePoint folder at a configurable interval (default 60 s)
  2. Download new files (skip the 'archive' subfolder and recently-modified items)
  3. Process each file through process_document (vector + SQL metadata)
  4. Move the processed file to <watched-folder>/archive/ in SharePoint
  5. Update DB counters

Authentication uses Azure AD app-only client credentials
(client_id + client_secret + tenant_id) stored in environment variables:
  SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET

No extra libraries required — uses only the built-in 'requests' package.

Phase 3 Slice 6 split this file's Graph HTTP client and per-config
ingestion logic out into sp_graph_client.py and sp_ingest.py — this file
now keeps only the public entry points (copy_document_to_sharepoint_archive,
get_sp_watcher) and the SharePointWatcherService lifecycle/polling-loop
class.
"""
from __future__ import annotations

from logging_config import get_logger
import os
import sys
import threading
import time
from typing import Dict, Optional

logger = get_logger(__name__)

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sp_graph_client import (
    _TOKEN_CACHE, _get_credentials,
    resolve_site_id, resolve_drive_id, ensure_archive_folder,
    _create_upload_session, _upload_session_chunks,
)
from sp_ingest import _SPWatchConfig, _poll_config, _update_last_scanned, _DEFAULT_POLL


def copy_document_to_sharepoint_archive(watch_id: int, file_path: str,
                                         filename: str):
    """
    Upload a copy of a locally-processed document into a SharePoint watch
    config's archive folder (`<sp_folder_path>/archive`), via a resumable
    upload session so any file size is supported.

    Used by the chat "Add Document → SharePoint" flow to place an
    independently-uploaded document in the same archive location the
    background watcher uses for files it ingests itself.

    Args:
        watch_id: `sharepoint_watch_configs` row ID (must be enabled).
        file_path: Local path of the already-saved file to copy.
        filename: Filename to use in SharePoint.

    Returns:
        (True, "") on success, or (False, reason) on any failure. Never raises.
    """
    tenant_id, client_id, client_secret = _get_credentials()
    if not all([tenant_id, client_id, client_secret]):
        return False, "SharePoint credentials not configured"

    token = _TOKEN_CACHE.get(tenant_id, client_id, client_secret)
    if not token:
        return False, "Could not authenticate with SharePoint"

    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT site_url, sp_folder_path, cached_site_id, cached_drive_id
                FROM sharepoint_watch_configs WHERE id = ? AND enabled = 1
            """, watch_id)
            row = cur.fetchone()
    except Exception as exc:
        return False, f"DB error loading connector: {exc}"

    if not row:
        return False, "SharePoint connector not found or disabled"
    site_url, folder_path, site_id, drive_id = row
    folder_path = folder_path or ""

    if not site_id:
        site_id = resolve_site_id(token, site_url)
        if not site_id:
            return False, "Could not resolve SharePoint site"
    if not drive_id:
        drive_id = resolve_drive_id(token, site_id)
        if not drive_id:
            return False, "Could not resolve SharePoint drive"

    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE sharepoint_watch_configs
                SET cached_site_id = ?, cached_drive_id = ?
                WHERE id = ?
            """, site_id, drive_id, watch_id)
            conn.commit()
    except Exception:
        pass

    archive_id = ensure_archive_folder(token, drive_id, folder_path)
    if not archive_id:
        return False, "Could not create/access archive folder"

    upload_url = _create_upload_session(token, drive_id, archive_id, filename)
    if not upload_url:
        return False, "Could not create SharePoint upload session"

    result = _upload_session_chunks(upload_url, file_path)
    if result is None:
        return False, "Upload to SharePoint archive folder failed"
    return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════════════════

class SharePointWatcherService:
    """Manages a set of SharePoint folder watches, polling each on its interval."""

    def __init__(self):
        """Initialize empty config state; call start() to begin the polling loop."""
        self._configs: Dict[int, _SPWatchConfig] = {}
        self._lock    = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Load enabled watch configs from the DB and start the background polling thread."""
        self._running = True
        self._load_from_db()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sharepoint_watcher")
        self._thread.start()
        logger.info("sharepoint_watcher: started (%d watch configs)", len(self._configs))

    def stop(self) -> None:
        """Signal the background polling loop to exit after its current iteration."""
        self._running = False

    # ── Watch management ──────────────────────────────────────────────────────

    def add_watch(self, watch_id: int, site_url: str, sp_folder_path: str,
                  label: str = "", scope: str = "user",
                  scope_id: Optional[int] = None,
                  target_user_id: Optional[int] = None,
                  poll_interval: int = _DEFAULT_POLL,
                  cached_site_id: Optional[str] = None,
                  cached_drive_id: Optional[str] = None,
                  key_metrics: Optional[list] = None,
                  user_ids: Optional[list] = None) -> None:
        """
        Register (or replace) a SharePoint folder watch configuration.

        Args:
            watch_id: Owning `sharepoint_watch_configs` row ID.
            site_url: Full SharePoint site URL.
            sp_folder_path: Folder path relative to the site's drive root.
            label: Display label for logs/UI.
            scope: Document visibility scope for files ingested here.
            scope_id: dept_id/project_id when scope is 'department'/'project'.
            target_user_id: User to attribute documents to for scope='user'.
            poll_interval: Seconds between polls of this folder.
            cached_site_id: Previously resolved Graph site ID, if any.
            cached_drive_id: Previously resolved Graph drive ID, if any.
            key_metrics: Optional metric names to extract via LLM at ingest.
            user_ids: Optional explicit assignment targets after ingestion.
        """
        with self._lock:
            self._configs[watch_id] = _SPWatchConfig(
                watch_id=watch_id, site_url=site_url,
                sp_folder_path=sp_folder_path, label=label,
                scope=scope, scope_id=scope_id,
                target_user_id=target_user_id,
                poll_interval=poll_interval,
                cached_site_id=cached_site_id,
                cached_drive_id=cached_drive_id,
                key_metrics=key_metrics or [],
                user_ids=user_ids or [],
            )

    def remove_watch(self, watch_id: int) -> None:
        """Stop polling a watch config (no-op if not currently registered)."""
        with self._lock:
            self._configs.pop(watch_id, None)

    def scan_now(self, watch_id: int) -> int:
        """
        Immediately poll a watch config, loading it from the DB first if
        not already cached in memory.

        Args:
            watch_id: `sharepoint_watch_configs` row ID to poll.

        Returns:
            Number of files ingested during this poll.

        Raises:
            ValueError: If the watch config does not exist (or is disabled).
        """
        with self._lock:
            cfg = self._configs.get(watch_id)
        if cfg is None:
            cfg = self._load_one_from_db(watch_id)
        if cfg is None:
            raise ValueError(f"SharePoint watch {watch_id} not found or disabled")
        count = _poll_config(cfg)
        _update_last_scanned(watch_id)
        return count

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        """
        Background thread body: every 5s, poll any configs whose
        `poll_interval` has elapsed, then update their last_scanned timestamp.
        Runs until `self._running` is set False by `stop()`.
        """
        while self._running:
            with self._lock:
                due = [c for c in self._configs.values() if c.due()]
            for cfg in due:
                if not self._running:
                    break
                cfg.mark_polled()
                try:
                    _poll_config(cfg)
                except Exception as exc:
                    logger.warning("sharepoint_watcher: poll error watch %d: %s",
                                   cfg.watch_id, exc)
                _update_last_scanned(cfg.watch_id)
            time.sleep(5)

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _load_from_db(self) -> None:
        """Load all enabled rows from `sharepoint_watch_configs` and register them via add_watch."""
        import json as _json
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, site_url, sp_folder_path, label,
                           scope, scope_id, target_user_id, poll_interval,
                           cached_site_id, cached_drive_id, key_metrics, user_ids_json
                    FROM sharepoint_watch_configs WHERE enabled = 1
                """)
                rows = cur.fetchall()
            for row in rows:
                (wid, site_url, sp_path, label,
                 scope, scope_id, target_uid, poll_interval,
                 cached_site_id, cached_drive_id, km_raw, uid_raw) = row
                key_metrics = _json.loads(km_raw)  if km_raw  else []
                user_ids    = _json.loads(uid_raw) if uid_raw else []
                self.add_watch(
                    wid, site_url, sp_path or "",
                    label or "", scope or "user",
                    scope_id, target_uid,
                    poll_interval or _DEFAULT_POLL,
                    cached_site_id, cached_drive_id,
                    key_metrics=key_metrics, user_ids=user_ids,
                )
        except Exception as exc:
            logger.warning("sharepoint_watcher: load from DB failed: %s", exc)

    def _load_one_from_db(self, watch_id: int) -> Optional[_SPWatchConfig]:
        """
        Load a single watch config row by ID, cache it in `self._configs`,
        and return it. Used by `scan_now` when the config isn't already
        in memory (e.g. service just started, or config was added by
        another process).

        Args:
            watch_id: `sharepoint_watch_configs` row ID.

        Returns:
            The loaded _SPWatchConfig, or None if the row doesn't exist or
            loading failed.
        """
        import json as _json
        try:
            from app_db import get_app_db
            with get_app_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, site_url, sp_folder_path, label,
                           scope, scope_id, target_user_id, poll_interval,
                           cached_site_id, cached_drive_id, key_metrics, user_ids_json
                    FROM sharepoint_watch_configs WHERE id = ?
                """, watch_id)
                row = cur.fetchone()
            if not row:
                return None
            (wid, site_url, sp_path, label,
             scope, scope_id, target_uid, poll_interval,
             cached_site_id, cached_drive_id, km_raw, uid_raw) = row
            key_metrics = _json.loads(km_raw)  if km_raw  else []
            user_ids    = _json.loads(uid_raw) if uid_raw else []
            cfg = _SPWatchConfig(
                watch_id=wid, site_url=site_url, sp_folder_path=sp_path or "",
                label=label or "", scope=scope or "user",
                scope_id=scope_id, target_user_id=target_uid,
                poll_interval=poll_interval or _DEFAULT_POLL,
                cached_site_id=cached_site_id,
                cached_drive_id=cached_drive_id,
                key_metrics=key_metrics, user_ids=user_ids,
            )
            with self._lock:
                self._configs[wid] = cfg
            return cfg
        except Exception as exc:
            logger.warning("sharepoint_watcher: _load_one_from_db failed: %s", exc)
            return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_sp_watcher: Optional[SharePointWatcherService] = None


def get_sp_watcher() -> SharePointWatcherService:
    """Return the process-wide singleton SharePointWatcherService, creating it on first call."""
    global _sp_watcher
    if _sp_watcher is None:
        _sp_watcher = SharePointWatcherService()
    return _sp_watcher
