"""Per-config SharePoint folder ingestion logic — extracted from
sharepoint_watcher.py in Phase 3 Slice 6. Confirmed linear (not
branch-tangled): `_poll_config` is one top-to-bottom procedure over a
single `_SPWatchConfig` object, so this is a mechanical move like the
rest of this slice's extractions, not a redesign.
"""
from __future__ import annotations

import os
import tempfile
import time
from typing import Dict, List, Optional

from logging_config import get_logger
from sp_graph_client import (
    _TOKEN_CACHE, _get_credentials, _get,
    resolve_site_id, resolve_drive_id, ensure_archive_folder,
    _download, move_to_archive, _GRAPH_BASE,
)

logger = get_logger(__name__)

_SETTLE_SECS  = 10      # skip files modified less than this many seconds ago
_DEFAULT_POLL = 60      # seconds between folder polls

_SUPPORTED = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm",
    ".csv", ".pptx", ".ppt", ".html", ".htm", ".eml",
    ".msg", ".png", ".jpg", ".jpeg", ".tiff", ".bmp",
    ".txt", ".md", ".json",
}


class _SPWatchConfig:
    """In-memory state for one configured SharePoint folder watch."""

    __slots__ = ("watch_id", "site_url", "sp_folder_path", "label",
                 "scope", "scope_id", "target_user_id", "poll_interval",
                 "site_id", "drive_id", "_last_poll", "key_metrics", "user_ids")

    def __init__(self, watch_id: int, site_url: str, sp_folder_path: str,
                 label: str = "", scope: str = "user",
                 scope_id: Optional[int] = None,
                 target_user_id: Optional[int] = None,
                 poll_interval: int = _DEFAULT_POLL,
                 cached_site_id: Optional[str] = None,
                 cached_drive_id: Optional[str] = None,
                 key_metrics: Optional[list] = None,
                 user_ids: Optional[list] = None):
        """
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
        self.watch_id       = watch_id
        self.site_url       = site_url
        self.sp_folder_path = sp_folder_path
        self.label          = label
        self.scope          = scope
        self.scope_id       = scope_id
        self.target_user_id = target_user_id
        self.poll_interval  = poll_interval
        self.site_id        = cached_site_id
        self.drive_id       = cached_drive_id
        self.key_metrics    = key_metrics or []
        self.user_ids       = user_ids or []
        self._last_poll: float = 0.0

    def due(self) -> bool:
        """Return True if at least `poll_interval` seconds have passed since the last poll."""
        return time.time() - self._last_poll >= self.poll_interval

    def mark_polled(self) -> None:
        """Record that this config was just polled (resets the due-for-poll timer)."""
        self._last_poll = time.time()


def _poll_config(cfg: _SPWatchConfig) -> int:
    """
    Poll one SharePoint watch config: list its folder, then for each
    new/settled supported file — download, run through process_document,
    optionally assign to configured users, and move it to archive/ in
    SharePoint.

    Resolves and caches site_id/drive_id on the config (and persists them
    to the DB) the first time they're needed. Skips subfolders, unsupported
    extensions, and files modified within the last `_SETTLE_SECS` seconds.

    Args:
        cfg: The watch configuration to poll.

    Returns:
        Number of files successfully ingested and archived.
    """
    from sp_graph_client import list_folder

    tenant_id, client_id, client_secret = _get_credentials()
    if not all([tenant_id, client_id, client_secret]):
        logger.warning("sharepoint_watcher: SP_TENANT_ID / SP_CLIENT_ID / SP_CLIENT_SECRET not set")
        return 0

    token = _TOKEN_CACHE.get(tenant_id, client_id, client_secret)
    if not token:
        return 0

    # Resolve & cache site / drive IDs
    if not cfg.site_id:
        cfg.site_id = resolve_site_id(token, cfg.site_url)
        if not cfg.site_id:
            logger.warning("sharepoint_watcher: cannot resolve site_id for %s", cfg.site_url)
            return 0
        _cache_ids(cfg)

    if not cfg.drive_id:
        cfg.drive_id = resolve_drive_id(token, cfg.site_id)
        if not cfg.drive_id:
            logger.warning("sharepoint_watcher: cannot resolve drive_id for site %s", cfg.site_id)
            return 0
        _cache_ids(cfg)

    # List folder
    try:
        items = list_folder(token, cfg.drive_id, cfg.sp_folder_path)
    except Exception as exc:
        logger.warning("sharepoint_watcher: list_folder failed: %s", exc)
        return 0

    ingested        = 0
    archive_id: Optional[str] = None

    for item in items:
        if "folder" in item:        # skip all subfolders (incl. archive)
            continue
        filename = item.get("name", "")
        ext      = os.path.splitext(filename)[1].lower()
        if ext not in _SUPPORTED:
            continue

        # Settle delay — skip files modified very recently (still uploading)
        lm_str = item.get("lastModifiedDateTime", "")
        if lm_str:
            from datetime import datetime, timezone
            try:
                lm  = datetime.fromisoformat(lm_str.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - lm).total_seconds()
                if age < _SETTLE_SECS:
                    continue
            except Exception:
                pass

        item_id      = item.get("id")
        download_url = item.get("@microsoft.graph.downloadUrl")
        if not item_id:
            continue

        # Lazy archive folder creation
        if archive_id is None:
            archive_id = ensure_archive_folder(token, cfg.drive_id, cfg.sp_folder_path)
            if not archive_id:
                logger.warning("sharepoint_watcher: could not get/create archive folder in %s",
                               cfg.sp_folder_path)
                break

        # Fresh download URL if not bundled in the listing
        if not download_url:
            item_meta    = _get(token, f"{_GRAPH_BASE}/drives/{cfg.drive_id}/items/{item_id}")
            download_url = item_meta.get("@microsoft.graph.downloadUrl") if item_meta else None

        if not download_url:
            logger.warning("sharepoint_watcher: no download URL for %s", filename)
            continue

        file_bytes = _download(token, download_url)
        if not file_bytes:
            continue

        # Write to a temp file so process_document can read it by path
        suffix = os.path.splitext(filename)[1]
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
        except Exception as exc:
            logger.warning("sharepoint_watcher: temp write failed: %s", exc)
            continue

        try:
            from agents.core.knowledge.document_processor import process_document
            # For user-scoped watches, attribute the doc to the configured target user
            # so that get_visible_doc_ids restricts retrieval to that user only.
            effective_uploader_id = (
                cfg.target_user_id
                if (cfg.scope == "user" and cfg.target_user_id and not cfg.user_ids)
                else 0
            )
            result = process_document(
                file_path=tmp_path,
                source_name=filename,
                uploaded_by_id=effective_uploader_id,
                uploaded_by_name="sharepoint_watcher",
                scope=cfg.scope or "global",
                scope_id=cfg.scope_id,
                source_watch_id=cfg.watch_id,
                source_watch_type="sharepoint",
                key_metrics=cfg.key_metrics or None,
            )
        except Exception as exc:
            logger.warning("sharepoint_watcher: process_document error for %s: %s", filename, exc)
            result = {"success": False, "error": str(exc)}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        if not result.get("success"):
            err = result.get("error", "unknown error")
            logger.warning("sharepoint_watcher: ingest failed for %s: %s", filename, err)
            _write_sp_log(cfg, filename, "error", 0, str(err))
            continue

        # Assign to specific users if configured
        doc_id = result.get("document_id")
        if doc_id and cfg.user_ids:
            try:
                from agents.core.knowledge.document_processor import assign_document
                from app_db import get_app_db
                with get_app_db() as _conn:
                    _cur = _conn.cursor()
                    for uid in cfg.user_ids:
                        _cur.execute("SELECT username FROM users WHERE id = ?", uid)
                        _row = _cur.fetchone()
                        uname = _row[0] if _row else str(uid)
                        assign_document(doc_id, uid, uname, 0, "sharepoint_watcher")
            except Exception as ae:
                logger.warning("sharepoint_watcher: assign_document failed: %s", ae)

        # Move to archive in SharePoint
        chunks = result.get("chunk_count", 0)
        if move_to_archive(token, cfg.drive_id, item_id, archive_id, filename):
            logger.info("sharepoint_watcher: ingested %s -> archive (%d chunks)",
                        filename, chunks)
            ingested += 1
            _bump_counter(cfg.watch_id)
            _write_sp_log(cfg, filename, "success", chunks)
        else:
            logger.warning("sharepoint_watcher: archive move failed for %s — file NOT re-ingested",
                           filename)
            _write_sp_log(cfg, filename, "error", 0, "archive move failed")

    return ingested


def _write_sp_log(cfg: _SPWatchConfig, filename: str, status: str,
                  chunk_count: int = 0, error_msg: str = "") -> None:
    """
    Best-effort write of one ingestion outcome to `connector_logs`.

    Args:
        cfg: The watch config the file came from (provides watch_id/label).
        filename: File that was (attempted to be) ingested.
        status: "success" or "error".
        chunk_count: Number of chunks stored (0 on error).
        error_msg: Error detail when status is "error".

    Never raises — logging failures are swallowed.
    """
    try:
        from database.app_db import log_connector_event
        log_connector_event("sharepoint", cfg.watch_id,
                            cfg.label or cfg.sp_folder_path,
                            filename, status, chunk_count, error_msg)
    except Exception:
        pass


def _cache_ids(cfg: _SPWatchConfig) -> None:
    """Persist resolved site_id / drive_id back to DB so next restart skips resolution."""
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE sharepoint_watch_configs
                SET cached_site_id = ?, cached_drive_id = ?
                WHERE id = ?
            """, cfg.site_id, cfg.drive_id, cfg.watch_id)
            conn.commit()
    except Exception:
        pass


def _bump_counter(watch_id: int) -> None:
    """Increment `sharepoint_watch_configs.files_ingested` and refresh last_scanned for watch_id."""
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE sharepoint_watch_configs
                SET files_ingested = files_ingested + 1, last_scanned = GETUTCDATE()
                WHERE id = ?
            """, watch_id)
            conn.commit()
    except Exception:
        pass


def _update_last_scanned(watch_id: int) -> None:
    """Best-effort update of `sharepoint_watch_configs.last_scanned` to now for watch_id."""
    try:
        from app_db import get_app_db
        with get_app_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE sharepoint_watch_configs SET last_scanned = GETUTCDATE()
                WHERE id = ?
            """, watch_id)
            conn.commit()
    except Exception:
        pass
