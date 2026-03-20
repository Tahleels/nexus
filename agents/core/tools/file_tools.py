"""File-operation tool implementations: sandboxed read/write access to the
agent file store, and directory report generation.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime
from _shared import _FILE_DIR

def create_text_file(filename: str, content: str, **kwargs) -> dict:
    """Create or overwrite a UTF-8 text file in the agent file store or a configured directory.

    Args:
        filename (str): Target filename (e.g. ``"report.txt"``). Any
            directory component is stripped via ``os.path.basename`` —
            the file is always written directly inside the resolved
            directory, never an arbitrary path.
        content (str): Text content to write (overwrites any existing
            file with the same name).
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            key: ``save_dir`` (str) — absolute directory to write into
            (created if missing). Defaults to the agent file store
            directory when not provided.

    Returns:
        dict: On success, ``{"success": True, "filename": str, "path":
        str, "size": int, "bytes": int}`` (``size`` is character count,
        ``bytes`` is UTF-8 encoded byte count). On failure, ``{"success":
        False, "error": str, "filename": str}``.
    """
    save_dir = (kwargs.get('save_dir') or '').strip()
    if save_dir:
        target_dir = os.path.expandvars(os.path.expanduser(save_dir))
        os.makedirs(target_dir, exist_ok=True)
        safe_name = os.path.basename(filename)
        path = os.path.join(target_dir, safe_name)
    else:
        safe_name = os.path.basename(filename)
        path = os.path.join(_FILE_DIR, safe_name)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return {"success": True, "filename": safe_name, "path": path,
                "size": len(content), "bytes": len(content.encode('utf-8'))}
    except Exception as e:
        return {"success": False, "error": str(e), "filename": filename}


def load_text_file(filename: str, **kwargs) -> dict:
    """Read a UTF-8 text file from the agent file store or configured search directories.

    Searches the agent file store directory first, then any extra
    directories supplied via ``search_dirs``, in order, and returns the
    first match by basename.

    Args:
        filename (str): Filename to read (e.g. ``"report.txt"``). Any
            directory component is stripped via ``os.path.basename``
            before searching.
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            key: ``search_dirs`` (str | list[str]) — comma-separated
            string or list of additional absolute directories to search,
            after the agent file store.

    Returns:
        dict: On success, ``{"success": True, "filename": str, "path":
        str, "content": str, "size": int}``. On failure, ``{"error": str,
        "available_files": list[str], "searched_dirs": list[str]}`` when
        the file isn't found in any searched directory, or
        ``{"success": False, "error": str, "filename": str}`` if found
        but reading raises (decoding errors are replaced rather than
        raised).
    """
    search_dirs_raw = kwargs.get('search_dirs', '')
    if isinstance(search_dirs_raw, list):
        extra_dirs = [d.strip() for d in search_dirs_raw if str(d).strip()]
    else:
        extra_dirs = [d.strip() for d in str(search_dirs_raw).split(',') if d.strip()]

    dirs_to_search = [_FILE_DIR] + [
        os.path.expandvars(os.path.expanduser(d)) for d in extra_dirs
    ]

    safe_name  = os.path.basename(filename)
    found_path = None
    for directory in dirs_to_search:
        candidate = os.path.join(directory, safe_name)
        if os.path.isfile(candidate):
            found_path = candidate
            break

    if not found_path:
        available = []
        for directory in dirs_to_search:
            try:
                available.extend([
                    f for f in os.listdir(directory)
                    if os.path.isfile(os.path.join(directory, f))
                ])
            except Exception:
                pass
        return {"error": f"File not found: {filename}",
                "available_files": available,
                "searched_dirs": dirs_to_search}

    try:
        with open(found_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return {"success": True, "filename": safe_name, "path": found_path,
                "content": content, "size": len(content)}
    except Exception as e:
        return {"success": False, "error": str(e), "filename": filename}


def _single_folder_report(path: str) -> dict:
    """Build a stats report (file/dir counts, sizes, top-20 newest files) for one directory.

    Args:
        path (str): Absolute directory path to inspect.

    Returns:
        dict: On success, ``{"success": True, "path": str, "total_items":
        int, "file_count": int, "dir_count": int, "total_size_kb": float,
        "files": list[dict] (newest-first, capped at 20), "directories":
        list[str] (alphabetical, capped at 20)}``. On failure, ``{"error":
        str, "path": str}`` (e.g. path doesn't exist, isn't a directory,
        or access is denied).
    """
    if not os.path.exists(path):
        return {"error": f"Path does not exist: {path}"}
    if not os.path.isdir(path):
        return {"error": f"Path is not a directory: {path}"}
    try:
        items       = os.listdir(path)
        files       = []
        dirs        = []
        total_bytes = 0
        for item in items:
            full = os.path.join(path, item)
            if os.path.isfile(full):
                size = os.path.getsize(full)
                total_bytes += size
                files.append({"name": item, "size_bytes": size,
                               "modified": datetime.fromtimestamp(
                                   os.path.getmtime(full)).isoformat()})
            elif os.path.isdir(full):
                dirs.append(item)
        return {
            "success":       True,
            "path":          path,
            "total_items":   len(items),
            "file_count":    len(files),
            "dir_count":     len(dirs),
            "total_size_kb": round(total_bytes / 1024, 2),
            "files":         sorted(files, key=lambda x: x["modified"], reverse=True)[:20],
            "directories":   sorted(dirs)[:20],
        }
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e), "path": path}


def folder_report(path: str = None, **kwargs) -> dict:
    """Generate a directory listing with file statistics for one or more directories.

    Args:
        path (str, optional): A specific directory path to report on.
            When supplied, this takes precedence over the configured
            ``paths`` and only this single directory is reported.
        **kwargs: Tool-config / orchestrator-injected context. Recognized
            key: ``paths`` (str | list[str]) — comma-separated string or
            list of directories to report on when ``path`` isn't given.
            Falls back to the agent file store directory if neither is
            set.

    Returns:
        dict: When exactly one target directory is resolved, returns that
        directory's report directly (see :func:`_single_folder_report`
        for the shape). When multiple directories are configured, returns
        ``{"success": True, "path_count": int, "paths": list[dict]}``
        where each entry is a per-directory report (which may itself be
        an ``{"error": str, "path": str}`` dict for directories that
        failed).
    """
    paths_config = kwargs.get('paths', '')
    if isinstance(paths_config, list):
        configured_paths = [p.strip() for p in paths_config if str(p).strip()]
    else:
        configured_paths = [p.strip() for p in str(paths_config).split(',') if p.strip()]

    if path:
        # Model explicitly requested a path — honour it
        targets = [os.path.expandvars(os.path.expanduser(path))]
    elif configured_paths:
        targets = [os.path.expandvars(os.path.expanduser(p)) for p in configured_paths]
    else:
        targets = [_FILE_DIR]

    if len(targets) == 1:
        return _single_folder_report(targets[0])

    reports = [_single_folder_report(t) for t in targets]
    return {"success": True, "path_count": len(reports), "paths": reports}
