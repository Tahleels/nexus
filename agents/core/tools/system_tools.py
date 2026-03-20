"""System/hub health monitoring tool implementation.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime
from _shared import _hub_db_query

def check_system_status(**kwargs) -> dict:
    """Return real-time system health metrics for the host running this app.

    Collects CPU/memory/disk usage via ``psutil`` (falling back to
    ``os.statvfs`` for disk usage only, with CPU usage left as None, if
    ``psutil`` isn't installed), counts of active hub agents and total
    hub conversations from the database, and this Python process's
    uptime. Derives an overall ``status`` flag from the resource
    thresholds.

    Args:
        **kwargs: Orchestrator-injected context (unused — this tool takes
            no parameters).

    Returns:
        dict: Always returns a dict (does not raise) with at least
        ``{"status": "healthy" | "warning" | "critical", "timestamp":
        str}``, plus best-effort fields among ``cpu_usage``,
        ``memory_usage``, ``memory_total_gb``, ``memory_used_gb``,
        ``disk_usage``, ``disk_total_gb``, ``disk_free_gb``,
        ``active_agents``, ``total_conversations``, ``process_uptime``.
        ``status`` becomes ``"warning"`` above 70% CPU / 80% memory / 85%
        disk, and ``"critical"`` above 90% CPU / 95% memory / 95% disk.
        Any sub-metric that fails to collect is simply omitted or set to
        None/"unknown" rather than causing the whole call to fail.
    """
    status: dict = {
        "status":       "healthy",
        "timestamp":    datetime.utcnow().isoformat() + "Z",
    }

    # CPU / memory / disk via psutil (optional dependency)
    try:
        import psutil
        status["cpu_usage"]    = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        status["memory_usage"] = round(mem.percent, 1)
        status["memory_total_gb"] = round(mem.total / 1e9, 2)
        status["memory_used_gb"]  = round(mem.used  / 1e9, 2)
        disk = psutil.disk_usage('/')
        status["disk_usage"]   = round(disk.percent, 1)
        status["disk_total_gb"]= round(disk.total / 1e9, 2)
        status["disk_free_gb"] = round(disk.free  / 1e9, 2)
    except ImportError:
        # Fallback: use os module
        import os as _os
        status["cpu_usage"]   = None
        try:
            statvfs = _os.statvfs('/') if hasattr(_os, 'statvfs') else None
            if statvfs:
                total = statvfs.f_frsize * statvfs.f_blocks
                free  = statvfs.f_frsize * statvfs.f_bfree
                status["disk_usage"]    = round((1 - free / total) * 100, 1)
                status["disk_total_gb"] = round(total / 1e9, 2)
                status["disk_free_gb"]  = round(free  / 1e9, 2)
        except Exception:
            pass

    # Active hub agents and conversations
    try:
        a_row = _hub_db_query(
            "SELECT COUNT(*) AS c FROM hub_agents WHERE status='active'", fetchone=True)
        c_row = _hub_db_query(
            "SELECT COUNT(*) AS c FROM hub_conversations", fetchone=True)
        status["active_agents"]      = (a_row or {}).get('c', 0)
        status["total_conversations"] = (c_row or {}).get('c', 0)
    except Exception:
        pass

    # Python process uptime
    try:
        import psutil, os as _os
        proc   = psutil.Process(_os.getpid())
        uptime = time.time() - proc.create_time()
        h, rem = divmod(int(uptime), 3600)
        m, s   = divmod(rem, 60)
        status["process_uptime"] = f"{h}h {m}m {s}s"
    except Exception:
        status["process_uptime"] = "unknown"

    # Overall health flag
    cpu = status.get("cpu_usage") or 0
    mem = status.get("memory_usage") or 0
    disk = status.get("disk_usage") or 0
    if cpu > 90 or mem > 95 or disk > 95:
        status["status"] = "critical"
    elif cpu > 70 or mem > 80 or disk > 85:
        status["status"] = "warning"

    return status
