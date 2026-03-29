"""Custom-tool CRUD/install/venv/test routes for agents_hub_bp. The
execution machinery itself (_exec_custom_tool and its helpers) stays in
agents_hub_bp.py's core since it's reached externally via
sys.modules['agents_hub_bp']._exec_custom_tool and reused by the
tool-jobs scheduler. Split out of agents_hub_bp.py in Phase 3 Slice 4.
"""

import sys, os, uuid, json, logging, threading, re, calendar
from datetime import datetime, timedelta, date
from flask import (render_template, jsonify, request,
                   Response, stream_with_context, redirect, url_for, abort)
import auth
import token_limits
from agents_hub_bp import agents_hub_bp
from agents_hub_bp import (
    _ss_exec, _fix_row, _fix_rows, _is_dev_or_admin, _is_admin, logger,
    _get_venv_python, _venv_dir, _load_custom_tools_into_registry,
    _exec_custom_tool, _sync_custom_tool_to_hub_tools, _custom_tool_dict,
    _TOOL_ENVS_DIR,
)


@agents_hub_bp.route('/api/agenthub/custom-tools', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def list_custom_tools():
    _load_custom_tools_into_registry()
    rows = _ss_exec(
        "SELECT * FROM hub_custom_tools ORDER BY created_at DESC",
        fetchall=True) or []
    return jsonify([_custom_tool_dict(r) for r in rows])


@agents_hub_bp.route('/api/agenthub/custom-tools', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def create_custom_tool():
    from core.tools.registry import register_custom_tool
    user = auth.current_user()
    data = request.json or {}

    name = (data.get("name") or "").strip().lower().replace(" ", "_")
    if not name:
        return jsonify({"error": "name is required"}), 400

    existing = _ss_exec("SELECT id FROM hub_custom_tools WHERE name=?", (name,), fetchone=True)
    if existing:
        return jsonify({"error": f"A tool named '{name}' already exists"}), 409

    pip_json      = json.dumps(data.get("pip_packages") or [])
    schema_json   = json.dumps(data.get("input_schema") or [])
    env_vars_json = json.dumps(data.get("env_vars") or {})

    _ss_exec("""
        INSERT INTO hub_custom_tools
            (name, display_name, description, category,
             pip_packages, imports_code, function_code,
             input_schema, output_desc, enabled, created_by, env_vars_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (name,
          data.get("display_name") or name,
          data.get("description", ""),
          data.get("category") or "Custom",
          pip_json,
          data.get("imports_code", ""),
          data.get("function_code", "def run(**kwargs):\n    return {}"),
          schema_json,
          data.get("output_desc", ""),
          user["id"],
          env_vars_json))

    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE name=?", (name,), fetchone=True)
    d   = _custom_tool_dict(row)
    try:
        register_custom_tool(_fix_row(row))
        _sync_custom_tool_to_hub_tools(_fix_row(row))
    except Exception as e:
        logger.warning(f"[custom-tools] Registry sync error: {e}")
    return jsonify(d), 201


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def get_custom_tool(tid):
    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_custom_tool_dict(row))


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>', methods=['PUT'])
@auth.login_required
@auth.dev_or_admin_required
def update_custom_tool(tid):
    from core.tools.registry import register_custom_tool, unregister_custom_tool
    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404

    data          = request.json or {}
    old_name      = row["name"]
    new_name      = (data.get("name") or old_name).strip().lower().replace(" ", "_")
    pip_json      = json.dumps(data.get("pip_packages") or [])
    schema_json   = json.dumps(data.get("input_schema") or [])
    env_vars_json = json.dumps(data.get("env_vars") or {})

    _ss_exec("""
        UPDATE hub_custom_tools SET
            name=?, display_name=?, description=?, category=?,
            pip_packages=?, imports_code=?, function_code=?,
            input_schema=?, output_desc=?, env_vars_json=?, updated_at=GETUTCDATE()
        WHERE id=?
    """, (new_name,
          data.get("display_name") or new_name,
          data.get("description", ""),
          data.get("category") or "Custom",
          pip_json,
          data.get("imports_code", ""),
          data.get("function_code", ""),
          schema_json,
          data.get("output_desc", ""),
          env_vars_json,
          tid))

    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    d   = _custom_tool_dict(row)
    try:
        if old_name != new_name:
            unregister_custom_tool(old_name)
        register_custom_tool(_fix_row(row))
        _sync_custom_tool_to_hub_tools(_fix_row(row))
    except Exception as e:
        logger.warning(f"[custom-tools] Registry sync error: {e}")
    return jsonify(d)


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def delete_custom_tool(tid):
    from core.tools.registry import unregister_custom_tool
    row = _ss_exec("SELECT name FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    name = row["name"]
    _ss_exec("DELETE FROM hub_custom_tools WHERE id=?", (tid,))
    _ss_exec("DELETE FROM hub_tools WHERE id=?", (f"custom_{name}",))
    unregister_custom_tool(name)
    return jsonify({"ok": True})


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>/toggle', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def toggle_custom_tool(tid):
    from core.tools.registry import register_custom_tool, unregister_custom_tool, TOOL_REGISTRY
    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    new_state = 0 if row["enabled"] else 1
    _ss_exec("UPDATE hub_custom_tools SET enabled=?, updated_at=GETUTCDATE() WHERE id=?",
             (new_state, tid))
    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    try:
        if new_state:
            register_custom_tool(_fix_row(row))
            _sync_custom_tool_to_hub_tools(_fix_row(row))
        else:
            unregister_custom_tool(row["name"])
            _ss_exec("UPDATE hub_tools SET enabled=0 WHERE id=?", (f"custom_{row['name']}",))
    except Exception as e:
        logger.warning(f"[custom-tools] Toggle registry error: {e}")
    return jsonify(_custom_tool_dict(row))


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>/install', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def install_custom_tool_packages(tid):
    import subprocess, venv as _venv_mod
    row = _ss_exec("SELECT name, pip_packages FROM hub_custom_tools WHERE id=?",
                   (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404

    tool_name = row["name"]
    packages  = json.loads(row.get("pip_packages") or "[]")
    vdir      = _venv_dir(tool_name)
    os.makedirs(_TOOL_ENVS_DIR, exist_ok=True)

    output_lines = []

    # Step 1 — create venv if it doesn't exist
    venv_py = _get_venv_python(tool_name)
    if not venv_py:
        output_lines.append(f"Creating isolated venv at {vdir} …")
        try:
            _venv_mod.create(vdir, with_pip=True, clear=False)
            venv_py = _get_venv_python(tool_name)
            output_lines.append("Venv created successfully.")
            _ss_exec("UPDATE hub_custom_tools SET venv_path=?, updated_at=GETUTCDATE() WHERE id=?",
                     (vdir, tid))
        except Exception as e:
            return jsonify({"success": False,
                            "output": f"Venv creation failed: {e}", "failed": []})
    else:
        output_lines.append(f"Using existing venv: {vdir}")
        _ss_exec("UPDATE hub_custom_tools SET venv_path=?, updated_at=GETUTCDATE() WHERE id=?",
                 (vdir, tid))

    # Step 2 — upgrade pip inside the venv
    try:
        subprocess.run([venv_py, "-m", "pip", "install", "--upgrade", "pip"],
                       capture_output=True, text=True, timeout=60)
    except Exception:
        pass

    if not packages:
        return jsonify({"output": "\n".join(output_lines) + "\nNo packages to install.",
                        "success": True, "failed": [], "venv_dir": vdir})

    # Step 3 — install each package into the venv
    errors = []
    for pkg in packages:
        try:
            result = subprocess.run(
                [venv_py, "-m", "pip", "install", pkg],
                capture_output=True, text=True, timeout=120)
            output_lines.append(f"$ pip install {pkg}")
            output_lines.append(result.stdout.strip() or result.stderr.strip())
            if result.returncode != 0:
                errors.append(pkg)
        except Exception as e:
            output_lines.append(f"Error installing {pkg}: {e}")
            errors.append(pkg)

    return jsonify({
        "success":  len(errors) == 0,
        "output":   "\n".join(output_lines),
        "failed":   errors,
        "venv_dir": vdir,
    })


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>/venv-status', methods=['GET'])
@auth.login_required
@auth.dev_or_admin_required
def custom_tool_venv_status(tid):
    import subprocess
    row = _ss_exec("SELECT name FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404

    tool_name = row["name"]
    venv_py   = _get_venv_python(tool_name)

    if not venv_py:
        return jsonify({"created": False, "tool_name": tool_name})

    py_version = "unknown"
    try:
        proc = subprocess.run([venv_py, "--version"],
                              capture_output=True, text=True, timeout=10)
        py_version = (proc.stdout or proc.stderr).strip()
    except Exception:
        pass

    vdir       = _venv_dir(tool_name)
    size_bytes = 0
    try:
        for dirpath, _, filenames in os.walk(vdir):
            for fname in filenames:
                try:
                    size_bytes += os.path.getsize(os.path.join(dirpath, fname))
                except Exception:
                    pass
    except Exception:
        pass

    return jsonify({
        "created":    True,
        "tool_name":  tool_name,
        "venv_dir":   vdir,
        "venv_py":    venv_py,
        "py_version": py_version,
        "size_mb":    round(size_bytes / 1_048_576, 1),
    })


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>/venv', methods=['DELETE'])
@auth.login_required
@auth.dev_or_admin_required
def delete_custom_tool_venv(tid):
    import shutil
    row = _ss_exec("SELECT name FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404
    vdir = _venv_dir(row["name"])
    try:
        if os.path.isdir(vdir):
            shutil.rmtree(vdir)
        _ss_exec("UPDATE hub_custom_tools SET venv_path=NULL, updated_at=GETUTCDATE() WHERE id=?",
                 (tid,))
        return jsonify({"success": True, "deleted": vdir})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@agents_hub_bp.route('/api/agenthub/custom-tools/<int:tid>/test', methods=['POST'])
@auth.login_required
@auth.dev_or_admin_required
def test_custom_tool(tid):
    row = _ss_exec("SELECT * FROM hub_custom_tools WHERE id=?", (tid,), fetchone=True)
    if not row:
        return jsonify({"error": "Not found"}), 404

    params = request.json or {}
    result = _exec_custom_tool(_fix_row(row), params)

    success_inc = 1 if result.get("success") else 0
    _ss_exec("""
        UPDATE hub_custom_tools
        SET total_calls=total_calls+1, success_calls=success_calls+?,
            updated_at=GETUTCDATE()
        WHERE id=?
    """, (success_inc, tid))
    return jsonify(result)
