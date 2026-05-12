"""Low-level Microsoft Graph API client for SharePoint access — extracted
from sharepoint_watcher.py in Phase 3 Slice 6. Pure/stateless HTTP helpers
(each takes a bearer ``token`` as a parameter) plus the shared OAuth2 token
cache. Used by both sharepoint_watcher.py (copy_document_to_sharepoint_archive)
and sp_ingest.py (_poll_config).
"""
from __future__ import annotations

import os
import time
import threading
from typing import Optional, Tuple, List

from logging_config import get_logger

logger = get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ── OAuth2 token cache (per-process) ─────────────────────────────────────────

class _TokenCache:
    """Thread-safe cache for a single Azure AD app-only access token, refreshed near expiry."""

    def __init__(self):
        """Initialize with no cached token."""
        self._token:      Optional[str] = None
        self._expires_at: float         = 0.0
        self._lock = threading.Lock()

    def get(self, tenant_id: str, client_id: str, client_secret: str) -> Optional[str]:
        """
        Return a cached, still-valid access token, fetching a new one if
        expired or absent (refreshes 60s before actual expiry).

        Args:
            tenant_id: Azure AD tenant ID.
            client_id: App registration client ID.
            client_secret: App registration client secret.

        Returns:
            A bearer access token, or None if the token request fails.
        """
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            tok, expires_in = _fetch_token(tenant_id, client_id, client_secret)
            if tok:
                self._token      = tok
                self._expires_at = time.time() + (expires_in or 3600)
            return self._token


# Module-level token cache shared by all watch configs
_TOKEN_CACHE = _TokenCache()


def _fetch_token(tenant_id: str, client_id: str, client_secret: str) -> Tuple[Optional[str], int]:
    """Client-credentials OAuth2 flow. Returns (access_token, expires_in)."""
    import requests
    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://graph.microsoft.com/.default",
    }, timeout=30)
    if resp.status_code == 200:
        body = resp.json()
        return body.get("access_token"), int(body.get("expires_in", 3600))
    logger.warning("sharepoint_watcher: token request failed %d: %s",
                   resp.status_code, resp.text[:300])
    return None, 0


def _get_credentials() -> Tuple[str, str, str]:
    """Return (tenant_id, client_id, client_secret) from environment variables."""
    return (
        os.environ.get("SP_TENANT_ID",     "").strip(),
        os.environ.get("SP_CLIENT_ID",     "").strip(),
        os.environ.get("SP_CLIENT_SECRET", "").strip(),
    )


# ── Low-level Graph API helpers ───────────────────────────────────────────────

def _headers(token: str) -> dict:
    """Build the standard Bearer auth + JSON-accept header dict for a Graph API call."""
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get(token: str, url: str, **kwargs) -> Optional[dict]:
    """
    GET a Microsoft Graph API URL.

    Args:
        token: Bearer access token.
        url: Full Graph API URL.
        **kwargs: Extra kwargs forwarded to `requests.get` (e.g. params).

    Returns:
        Parsed JSON body on HTTP 200, else None (logs a warning).
    """
    import requests
    try:
        r = requests.get(url, headers=_headers(token), timeout=30, **kwargs)
        if r.status_code == 200:
            return r.json()
        logger.warning("sharepoint_watcher: GET %s → %d", url.split("?")[0], r.status_code)
    except Exception as exc:
        logger.warning("sharepoint_watcher: GET %s error: %s", url.split("?")[0], exc)
    return None


def _patch(token: str, url: str, body: dict) -> bool:
    """
    PATCH a Microsoft Graph API URL with a JSON body.

    Args:
        token: Bearer access token.
        url: Full Graph API URL.
        body: JSON-serializable request body.

    Returns:
        True if the response status is 200/201/204, else False.
    """
    import requests
    try:
        r = requests.patch(url, headers={**_headers(token), "Content-Type": "application/json"},
                           json=body, timeout=30)
        return r.status_code in (200, 201, 204)
    except Exception as exc:
        logger.warning("sharepoint_watcher: PATCH %s error: %s", url, exc)
        return False


def _post(token: str, url: str, body: dict) -> Optional[dict]:
    """
    POST a Microsoft Graph API URL with a JSON body.

    Args:
        token: Bearer access token.
        url: Full Graph API URL.
        body: JSON-serializable request body.

    Returns:
        Parsed JSON response on HTTP 200/201, else None (logs a warning).
    """
    import requests
    try:
        r = requests.post(url, headers={**_headers(token), "Content-Type": "application/json"},
                          json=body, timeout=30)
        if r.status_code in (200, 201):
            return r.json()
        logger.warning("sharepoint_watcher: POST %s → %d %s",
                       url.split("?")[0], r.status_code, r.text[:200])
    except Exception as exc:
        logger.warning("sharepoint_watcher: POST %s error: %s", url, exc)
    return None


def _download(token: str, url: str) -> Optional[bytes]:
    """
    Download a file's raw bytes from a Graph API download URL.

    Args:
        token: Bearer access token.
        url: Pre-authenticated or Graph download URL.

    Returns:
        The file's bytes on HTTP 200, else None (logs a warning).
    """
    import requests
    try:
        r = requests.get(url, headers=_headers(token), timeout=120)
        if r.status_code == 200:
            return r.content
        logger.warning("sharepoint_watcher: download failed %d", r.status_code)
    except Exception as exc:
        logger.warning("sharepoint_watcher: download error: %s", exc)
    return None


# ── Graph: site / drive resolution ───────────────────────────────────────────

def resolve_site_id(token: str, site_url: str) -> Optional[str]:
    """
    Resolve a SharePoint site URL to its Microsoft Graph site ID.

    Example: https://yourcompany.sharepoint.com/sites/YourSite
    → Graph site ID (e.g. 'yourcompany.sharepoint.com,abc123,...')

    Args:
        token: Bearer access token.
        site_url: Full SharePoint site URL.

    Returns:
        The Graph site ID, or None if resolution fails.
    """
    from urllib.parse import urlparse
    p        = urlparse(site_url.rstrip("/"))
    hostname = p.netloc
    path     = p.path.rstrip("/")
    data     = _get(token, f"{_GRAPH_BASE}/sites/{hostname}:{path}")
    return data.get("id") if data else None


def resolve_drive_id(token: str, site_id: str) -> Optional[str]:
    """
    Return the default document-library drive ID for the site.

    Args:
        token: Bearer access token.
        site_id: Graph site ID from `resolve_site_id`.

    Returns:
        The drive ID, or None if resolution fails.
    """
    data = _get(token, f"{_GRAPH_BASE}/sites/{site_id}/drive")
    return data.get("id") if data else None


# ── Graph: folder / file operations ──────────────────────────────────────────

def list_folder(token: str, drive_id: str, folder_path: str) -> List[dict]:
    """
    List items directly inside <folder_path> on the drive root, following
    pagination (`@odata.nextLink`) to collect all results.

    Args:
        token: Bearer access token.
        drive_id: Graph drive ID from `resolve_drive_id`.
        folder_path: Path relative to the drive root, e.g. 'Test' or
            'HR/Uploads'.

    Returns:
        List of Graph driveItem dicts (files and subfolders); empty list
        on failure.
    """
    path = folder_path.strip("/")
    url  = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{path}:/children"
    data  = _get(token, url, params={"$top": "500"})
    items = data.get("value", []) if data else []
    # Follow pagination
    while data and "@odata.nextLink" in data:
        data = _get(token, data["@odata.nextLink"])
        if data:
            items.extend(data.get("value", []))
    return items


def ensure_archive_folder(token: str, drive_id: str, parent_path: str) -> Optional[str]:
    """
    Return the item-ID of <parent_path>/archive, creating it if needed.

    Tolerates a concurrent creator: if the create call fails (e.g. the
    folder appeared between the initial GET and the create POST), retries
    the GET once before giving up.

    Args:
        token: Bearer access token.
        drive_id: Graph drive ID.
        parent_path: Watched folder's path relative to the drive root.

    Returns:
        The archive folder's item ID, or None if it could not be found or created.
    """
    archive_path = f"{parent_path.strip('/')}/archive"
    data = _get(token, f"{_GRAPH_BASE}/drives/{drive_id}/root:/{archive_path}")
    if data and data.get("id"):
        return data["id"]
    # Create
    parent_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{parent_path.strip('/')}:/children"
    created    = _post(token, parent_url, {
        "name":   "archive",
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    })
    if created and created.get("id"):
        return created["id"]
    # Folder may have been created by a concurrent process; try GET again
    data = _get(token, f"{_GRAPH_BASE}/drives/{drive_id}/root:/{archive_path}")
    return data.get("id") if data else None


def _create_upload_session(token: str, drive_id: str, parent_item_id: str,
                            filename: str) -> Optional[str]:
    """
    Create a Graph resumable upload session for a new file inside a folder.

    Args:
        token: Bearer access token.
        drive_id: Graph drive ID.
        parent_item_id: Item ID of the destination folder.
        filename: Desired filename (renamed on conflict).

    Returns:
        The session's `uploadUrl`, or None if creation failed.
    """
    url  = f"{_GRAPH_BASE}/drives/{drive_id}/items/{parent_item_id}:/{filename}:/createUploadSession"
    body = {"item": {"@microsoft.graph.conflictBehavior": "rename"}}
    data = _post(token, url, body)
    return data.get("uploadUrl") if data else None


def _upload_session_chunks(upload_url: str, file_path: str,
                            chunk_size: int = 5 * 1024 * 1024) -> Optional[dict]:
    """
    Upload a local file to an existing Graph upload session in sequential chunks.

    Per Graph's convention, chunk PUT requests carry no Authorization header —
    the session URL itself is pre-authenticated.

    Args:
        upload_url: `uploadUrl` from `_create_upload_session`.
        file_path: Local path of the file to upload.
        chunk_size: Bytes per chunk (must be a multiple of 320 KiB; default 5 MiB).

    Returns:
        The final driveItem JSON on success, or None on failure.
    """
    import requests
    total = os.path.getsize(file_path)
    if total == 0:
        return None
    try:
        with open(file_path, "rb") as fh:
            start = 0
            while start < total:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range":  f"bytes {start}-{end}/{total}",
                }
                r = requests.put(upload_url, headers=headers, data=chunk, timeout=120)
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code != 202:
                    logger.warning("sharepoint_watcher: upload chunk %d-%d/%d failed %d: %s",
                                   start, end, total, r.status_code, r.text[:200])
                    return None
                start = end + 1
    except Exception as exc:
        logger.warning("sharepoint_watcher: upload_session_chunks error: %s", exc)
        return None
    return None


def move_to_archive(token: str, drive_id: str,
                    item_id: str, archive_folder_id: str, filename: str) -> bool:
    """
    Move a drive item into the archive folder, retrying with a
    timestamp-suffixed name if the original name conflicts.

    Args:
        token: Bearer access token.
        drive_id: Graph drive ID.
        item_id: ID of the item to move.
        archive_folder_id: Destination archive folder's item ID.
        filename: Desired filename in the destination folder.

    Returns:
        True if the move succeeded (with either name), else False.
    """
    url  = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
    body = {"parentReference": {"id": archive_folder_id}, "name": filename}
    if _patch(token, url, body):
        return True
    # Retry with timestamped name to avoid conflicts
    base, ext = os.path.splitext(filename)
    ts_name   = f"{base}_{int(time.time())}{ext}"
    body["name"] = ts_name
    return _patch(token, url, body)
