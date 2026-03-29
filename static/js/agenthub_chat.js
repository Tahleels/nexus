// agenthub_chat.js — Agents Hub Chat UI
'use strict';

let _activeAgent          = null;
let _activeConvoId        = null;
let _streaming            = false;
let _allAgents            = [];
let _allConvos            = [];
let _convoVisible         = false;
let _pendingApproval      = null;  // approval_id waiting to be attached after 'final'
let _lastDataAgentResult  = null;  // last communicate_with_data_agent result with data
let _lastQueryDbResult    = null;  // last query_database result with rows/columns
let _docRequested         = null;  // doc type detected in user message — triggered after AI reply
const _hubDataRegistry    = {};    // cardId → dataResult; prevents multi-card data bleed

// ── Current user role (baked in by Jinja in components/sidebar.html) ──────────
// Tool-call chips (and their expandable input/output) are an internal debugging
// trace — only admin/dev should see them. Regular users still see the resulting
// data (viz card / query-result table), which is injected separately.
function _hubUserRole() {
    try {
        const meta = JSON.parse(document.getElementById('sidebarMeta').textContent);
        return meta.role || 'user';
    } catch (e) { return 'user'; }
}
function _hubCanSeeToolTrace() {
    const role = _hubUserRole();
    return role === 'admin' || role === 'dev';
}

// ── Active org scope (dept_id / project_id from URL query string) ────────────
function _getActiveOrgScope() {
    const p = new URLSearchParams(window.location.search);
    const deptId    = p.get('dept_id');
    const projectId = p.get('project_id');
    if (projectId) return { scope_type: 'project',    scope_id: +projectId };
    if (deptId)    return { scope_type: 'department',  scope_id: +deptId   };
    return {};
}

// ── Document type detection ───────────────────────────────────────────────────
const _FILE_DOC_TYPES = new Set(['pdf','docx','csv','xlsx','markdown','txt']);

function _detectDocType(text) {
    if (!/\b(make|create|generate|build|write|produce|prepare|give me|export|convert)\b/i.test(text)) return null;
    if (/\b(ppt|powerpoint|presentation|slides?)\b/i.test(text))   return 'presentation';
    if (/\binfographic\b/i.test(text))                              return 'infographic';
    if (/\bdashboard\b/i.test(text))                                return 'dashboard';
    if (/\b(word|docx?)\b/i.test(text))                             return 'docx';
    if (/\b(excel|xlsx?|spreadsheet)\b/i.test(text))                return 'xlsx';
    if (/\bcsv\b/i.test(text))                                      return 'csv';
    if (/\bmarkdown\b/i.test(text))                                 return 'markdown';
    if (/\b(text file|\.txt|plain text)\b/i.test(text))             return 'txt';
    if (/\bpdf\b/i.test(text))                                      return 'pdf';
    if (/\breport\b/i.test(text))                                   return 'report';
    if (/\bdocument\b/i.test(text))                                 return 'docx';
    return null;
}

// ── Attachment state ──────────────────────────────────────────────────────────
const MAX_ATTACH_FILES = 10;   // cap per selection, for both inline-read and KB-upload modes
let _attachFiles      = [];    // raw File objects — used by both 'inline' and 'kb' modes
let _attachMode       = 'inline'; // 'inline' | 'kb'
let _attachExtracted  = null;  // inline: { mode:'inline', results: [{success, filename, text, truncated, server_path, error?}] }
                                // kb:     { mode:'kb',     results: [{success, filename, doc_id, chunk_count, error?}] }
let _attachProcessing = false;

function _capAttachFiles(files) {
    if (files.length > MAX_ATTACH_FILES) {
        showToast(`Only the first ${MAX_ATTACH_FILES} files were kept (max ${MAX_ATTACH_FILES} per selection)`);
        return files.slice(0, MAX_ATTACH_FILES);
    }
    return files;
}

function _patchAgentToolDocIds(agent, newDocIds) {
    if (!agent || !Array.isArray(agent.tools)) return;
    let found = false;
    agent.tools = agent.tools.map(t => {
        const name = typeof t === 'string' ? t : t.name;
        if (name === 'search_knowledge') {
            found = true;
            const entry = typeof t === 'string' ? { name: t, config: {} } : { ...t };
            entry.config = { ...(entry.config || {}), document_ids: newDocIds };
            return entry;
        }
        return t;
    });
    if (!found) {
        agent.tools.push({ name: 'search_knowledge', config: { document_ids: newDocIds } });
    }
}

// ── In-chat scope dialog ──────────────────────────────────────────────────────
// Returns a promise resolving to { scope, scope_id?, user_ids? } or null on cancel.
function _showChatScopeDialog(filename) {
    return new Promise((resolve) => {
        const id = 'chatScopeModal_' + Date.now();
        const html = `
<div class="modal fade" id="${id}" tabindex="-1" data-bs-backdrop="static">
  <div class="modal-dialog modal-dialog-centered" style="max-width:420px">
    <div class="modal-content">
      <div class="modal-header py-2">
        <h6 class="modal-title mb-0"><i class="fas fa-lock me-2 text-primary"></i>Who can access this document?</h6>
        <button type="button" class="btn-close btn-sm" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body pb-2">
        <p class="text-muted small mb-3">Choose who can retrieve <strong>${filename.replace(/</g,'&lt;')}</strong> when using this agent.</p>
        <div class="d-grid gap-2" id="${id}_opts">
          <button class="btn btn-outline-secondary text-start scope-chat-opt" data-scope="user" data-label="Only me">
            <i class="fas fa-user-lock me-2 text-secondary"></i><strong>Only me</strong>
            <div class="small text-muted">No other user of this agent can fetch it.</div>
          </button>
          <button class="btn btn-outline-primary text-start scope-chat-opt" data-scope="specific" data-label="Specific colleagues">
            <i class="fas fa-user-friends me-2 text-primary"></i><strong>Specific colleagues</strong>
            <div class="small text-muted">Choose exactly who can access it.</div>
          </button>
          <button class="btn btn-outline-warning text-start scope-chat-opt" data-scope="department" data-label="My Department">
            <i class="fas fa-building me-2 text-warning"></i><strong>My Department</strong>
            <div class="small text-muted">All members of your department can access it.</div>
          </button>
          <button class="btn btn-outline-success text-start scope-chat-opt" data-scope="global" data-label="All users of this agent">
            <i class="fas fa-users me-2 text-success"></i><strong>All users of this agent</strong>
            <div class="small text-muted">Anyone assigned to this agent can retrieve it.</div>
          </button>
        </div>
        <div id="${id}_deptRow" class="mt-2" style="display:none">
          <label class="form-label small mb-1">Select your department</label>
          <select class="form-select form-select-sm" id="${id}_deptSel">
            <option value="">Loading…</option>
          </select>
        </div>
        <div id="${id}_usersRow" class="mt-2" style="display:none">
          <label class="form-label small mb-1">Select colleagues</label>
          <input type="text" class="form-control form-control-sm mb-1" id="${id}_userSearch" placeholder="Search colleagues…" autocomplete="off">
          <div id="${id}_userList" class="border rounded p-2" style="max-height:120px;overflow-y:auto;font-size:.82rem;">
            <div class="text-muted text-center py-2"><div class="spinner-border spinner-border-sm"></div></div>
          </div>
        </div>
      </div>
      <div class="modal-footer py-2">
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-sm btn-primary" id="${id}_confirm" disabled>Confirm &amp; Upload</button>
      </div>
    </div>
  </div>
</div>`;
        document.body.insertAdjacentHTML('beforeend', html);
        const el     = document.getElementById(id);
        const modal  = new bootstrap.Modal(el);
        let chosen   = null;
        let colleagues = [];
        const selectedUsers = new Set();

        // Load the user's own department(s) — not the full company directory
        fetch('/api/knowledge/my-departments').then(r => r.json()).then(d => {
            const sel = document.getElementById(`${id}_deptSel`);
            const depts = d.departments || [];
            if (sel) sel.innerHTML = depts.length
                ? '<option value="">— Select —</option>' + depts.map(dep => `<option value="${dep.id}">${dep.name}</option>`).join('')
                : '<option value="">No department membership</option>';
        }).catch(() => {});

        // Load colleagues (scoped server-side to shared dept/project) for the picker
        fetch('/api/knowledge/colleagues').then(r => r.json()).then(d => {
            colleagues = d.users || [];
            renderUserList('');
        }).catch(() => { colleagues = []; renderUserList(''); });

        function renderUserList(query) {
            const ul = document.getElementById(`${id}_userList`);
            if (!ul) return;
            const q = query.toLowerCase();
            const filtered = colleagues.filter(u => !q || u.username.toLowerCase().includes(q));
            ul.innerHTML = filtered.length
                ? filtered.map(u => `
                    <label class="d-flex align-items-center gap-2 py-1 px-1" style="cursor:pointer">
                        <input type="checkbox" value="${u.id}" ${selectedUsers.has(u.id) ? 'checked' : ''}
                               data-chatuser-chk>
                        <span>${u.username.replace(/</g,'&lt;')}</span>
                    </label>`).join('')
                : '<div class="text-muted text-center small py-1">No colleagues found</div>';
            ul.querySelectorAll('[data-chatuser-chk]').forEach(chk => {
                chk.addEventListener('change', () => {
                    const uid = parseInt(chk.value, 10);
                    chk.checked ? selectedUsers.add(uid) : selectedUsers.delete(uid);
                    document.getElementById(`${id}_confirm`).disabled = selectedUsers.size === 0;
                });
            });
        }

        el.addEventListener('input', (e) => {
            if (e.target.id === `${id}_userSearch`) renderUserList(e.target.value);
        });

        el.querySelectorAll('.scope-chat-opt').forEach(btn => {
            btn.addEventListener('click', () => {
                el.querySelectorAll('.scope-chat-opt').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                chosen = btn.dataset.scope;
                document.getElementById(`${id}_deptRow`).style.display  = chosen === 'department' ? '' : 'none';
                document.getElementById(`${id}_usersRow`).style.display = chosen === 'specific'   ? '' : 'none';
                document.getElementById(`${id}_confirm`).disabled = chosen === 'specific' ? selectedUsers.size === 0 : false;
            });
        });

        document.getElementById(`${id}_confirm`).addEventListener('click', () => {
            if (!chosen) return;
            let result = { scope: chosen === 'department' ? 'department' : (chosen === 'global' ? 'global' : 'user') };
            if (chosen === 'department') {
                const sel = document.getElementById(`${id}_deptSel`);
                if (!sel || !sel.value) { alert('Please select a department.'); return; }
                result.scope_id = sel.value;
            } else if (chosen === 'specific') {
                if (!selectedUsers.size) { alert('Please select at least one colleague.'); return; }
                result.user_ids = Array.from(selectedUsers);
            }
            modal.hide();
            resolve(result);
        });

        el.addEventListener('hidden.bs.modal', () => {
            el.remove();
            if (!chosen) resolve(null);
        });

        modal.show();
    });
}

function agentHasProcessDocument() {
    if (!_activeAgent) return false;
    const tools = _activeAgent.tools || [];
    return tools.some(t => (typeof t === 'string' ? t : t.name || t) === 'process_document');
}

// ── SharePoint destination (chat "Add Document") ──────────────────────────────
function agentSharePointConnectorIds(agent) {
    if (!agent || !Array.isArray(agent.tools)) return [];
    const ids = new Set();
    agent.tools.forEach(t => {
        if (typeof t === 'string') return;
        if (!['search_connector_knowledge', 'list_connector_documents'].includes(t.name)) return;
        const keys = (t.config && t.config.connector_keys) || [];
        keys.forEach(k => {
            if (typeof k === 'string' && k.startsWith('sharepoint:')) {
                const id = parseInt(k.split(':')[1], 10);
                if (!isNaN(id)) ids.add(id);
            }
        });
    });
    return Array.from(ids);
}

// Returns a promise resolving to { destination:'individual' } or
// { destination:'sharepoint', sharepoint_watch_id, label }, or null on cancel.
// Skipped entirely (resolves immediately to 'individual') when the agent has
// no SharePoint connector attached — current behavior is unaffected.
function _showDestinationDialog(filename) {
    const connectorIds = agentSharePointConnectorIds(_activeAgent);
    if (!connectorIds.length) return Promise.resolve({ destination: 'individual' });

    return new Promise((resolve) => {
        const id = 'destModal_' + Date.now();
        const html = `
<div class="modal fade" id="${id}" tabindex="-1" data-bs-backdrop="static">
  <div class="modal-dialog modal-dialog-centered" style="max-width:420px">
    <div class="modal-content">
      <div class="modal-header py-2">
        <h6 class="modal-title mb-0"><i class="fas fa-share-square me-2 text-primary"></i>Where should this document go?</h6>
        <button type="button" class="btn-close btn-sm" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body pb-2">
        <p class="text-muted small mb-3">Choose a destination for <strong>${filename.replace(/</g,'&lt;')}</strong>.</p>
        <div class="d-grid gap-2" id="${id}_opts">
          <button class="btn btn-outline-secondary text-start dest-chat-opt" data-dest="individual">
            <i class="fas fa-user me-2 text-secondary"></i><strong>Process as individual document</strong>
            <div class="small text-muted">Uploaded and indexed as usual.</div>
          </button>
          <button class="btn btn-outline-primary text-start dest-chat-opt" data-dest="sharepoint">
            <i class="fas fa-cloud-upload-alt me-2 text-primary"></i><strong>Send to SharePoint</strong>
            <div class="small text-muted">Archives a copy in SharePoint, then indexes it.</div>
          </button>
        </div>
        <div id="${id}_spRow" class="mt-2" style="display:none">
          <label class="form-label small mb-1">Select SharePoint site</label>
          <div id="${id}_spList" class="d-grid gap-1">
            <div class="text-muted text-center py-2"><div class="spinner-border spinner-border-sm"></div></div>
          </div>
        </div>
      </div>
      <div class="modal-footer py-2">
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-sm btn-primary" id="${id}_confirm" disabled>Continue</button>
      </div>
    </div>
  </div>
</div>`;
        document.body.insertAdjacentHTML('beforeend', html);
        const el    = document.getElementById(id);
        const modal = new bootstrap.Modal(el);
        let chosenDest = null;
        let chosenConnector = null; // { id, label }
        let connectors = [];

        function updateConfirmState() {
            const btn = document.getElementById(`${id}_confirm`);
            if (chosenDest === 'individual') { btn.disabled = false; return; }
            if (chosenDest === 'sharepoint') { btn.disabled = !chosenConnector; return; }
            btn.disabled = true;
        }

        el.querySelectorAll('.dest-chat-opt').forEach(btn => {
            btn.addEventListener('click', async () => {
                el.querySelectorAll('.dest-chat-opt').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                chosenDest = btn.dataset.dest;
                const spRow = document.getElementById(`${id}_spRow`);

                if (chosenDest !== 'sharepoint') {
                    spRow.style.display = 'none';
                    chosenConnector = null;
                    updateConfirmState();
                    return;
                }

                spRow.style.display = '';
                if (!connectors.length) {
                    try {
                        const r = await fetch(`/api/agenthub/agents/${_activeAgent.id}/sharepoint-connectors`);
                        const d = await r.json();
                        connectors = d.connectors || [];
                    } catch (_) { connectors = []; }
                }

                const listEl = document.getElementById(`${id}_spList`);
                if (!connectors.length) {
                    listEl.innerHTML = '<div class="text-muted text-center small py-1">No SharePoint connectors available</div>';
                    chosenConnector = null;
                } else if (connectors.length === 1) {
                    chosenConnector = connectors[0];
                    listEl.innerHTML = `<div class="small text-muted">Archiving to: <strong>${chosenConnector.label.replace(/</g,'&lt;')}</strong></div>`;
                } else {
                    listEl.innerHTML = connectors.map(c => `
                        <button class="btn btn-sm btn-outline-primary text-start sp-conn-opt" data-id="${c.id}">
                            ${c.label.replace(/</g,'&lt;')}
                        </button>`).join('');
                    listEl.querySelectorAll('.sp-conn-opt').forEach(cbtn => {
                        cbtn.addEventListener('click', () => {
                            listEl.querySelectorAll('.sp-conn-opt').forEach(b => b.classList.remove('active'));
                            cbtn.classList.add('active');
                            chosenConnector = connectors.find(c => String(c.id) === cbtn.dataset.id) || null;
                            updateConfirmState();
                        });
                    });
                }
                updateConfirmState();
            });
        });

        document.getElementById(`${id}_confirm`).addEventListener('click', () => {
            if (!chosenDest) return;
            modal.hide();
            if (chosenDest === 'sharepoint' && chosenConnector) {
                resolve({ destination: 'sharepoint', sharepoint_watch_id: chosenConnector.id, label: chosenConnector.label });
            } else {
                resolve({ destination: 'individual' });
            }
        });

        el.addEventListener('hidden.bs.modal', () => {
            el.remove();
            if (!chosenDest) resolve(null);
        });

        modal.show();
    });
}

function onAttachFileSelected(e) {
    const files = _capAttachFiles(Array.from(e.target.files || []));
    if (!files.length) return;
    _attachFiles     = files;
    _attachExtracted = null;
    _attachMode      = 'inline';

    const label = files.length === 1 ? files[0].name : `${files.length} files selected`;
    document.getElementById('attachFileName').textContent = label;
    document.getElementById('attachPreviewBar').style.display = 'block';
    document.getElementById('attachProgressWrap').style.display = 'none';

    const badge = document.getElementById('attachModeBadge');
    if (badge) badge.innerHTML = '<span class="badge bg-primary" style="font-size:.7rem;"><i class="fas fa-eye me-1"></i>Read only</span>';

    e.target.value = '';
}

function onAddDocumentFileSelected(e) {
    const files = _capAttachFiles(Array.from(e.target.files || []));
    if (!files.length) return;
    _attachFiles     = files;
    _attachExtracted = null;
    _attachMode      = 'kb';

    const label = files.length === 1 ? files[0].name : `${files.length} files selected`;
    document.getElementById('attachFileName').textContent = label;
    document.getElementById('attachPreviewBar').style.display = 'block';
    document.getElementById('attachProgressWrap').style.display = 'none';

    const badge = document.getElementById('attachModeBadge');
    if (badge) badge.innerHTML = '<span class="badge bg-success" style="font-size:.7rem;"><i class="fas fa-database me-1"></i>Add to Knowledge Base</span>';

    e.target.value = '';
}

function setAttachMode(mode) {
    _attachMode = mode;
    document.getElementById('btnModeInline').className = mode === 'inline'
        ? 'btn btn-xs btn-primary' : 'btn btn-xs btn-outline-primary';
    document.getElementById('btnModeKB').className = mode === 'kb'
        ? 'btn btn-xs btn-success' : 'btn btn-xs btn-outline-success';
    // Reset any previous extraction when switching mode
    _attachExtracted = null;
}

function clearAttachment() {
    _attachExtracted = null;
    _attachFiles = [];
    _attachMode = 'inline';
    _attachProcessing = false;
    document.getElementById('attachPreviewBar').style.display = 'none';
    document.getElementById('attachProgressWrap').style.display = 'none';
    const badge = document.getElementById('attachModeBadge');
    if (badge) badge.innerHTML = '';
}

async function prepareAttachment() {
    if (!_attachFiles.length) return null;
    if (_attachExtracted) return _attachExtracted; // already prepared

    const progWrap  = document.getElementById('attachProgressWrap');
    const progBar   = document.getElementById('attachProgressBar');
    const progLabel = document.getElementById('attachProgressLabel');
    progWrap.style.display = 'block';
    progBar.className = 'progress-bar progress-bar-striped progress-bar-animated';
    progBar.style.width = '40%';

    if (_attachMode === 'kb') {
        // Ask destination (individual vs SharePoint) first — only if the agent
        // has a SharePoint connector attached; otherwise resolves immediately.
        progWrap.style.display = 'none'; // hide progress while dialog is up
        const dialogLabel = _attachFiles.length === 1
            ? _attachFiles[0].name
            : `${_attachFiles.length} files`;
        let destChoice;
        try {
            destChoice = await _showDestinationDialog(dialogLabel);
        } catch (_) {
            return null; // user cancelled
        }
        if (!destChoice) return null;

        // Ask user for access scope before uploading — one choice applies to all files
        let scopeChoice;
        try {
            scopeChoice = await _showChatScopeDialog(dialogLabel);
        } catch (_) {
            return null; // user cancelled
        }
        if (!scopeChoice) return null;

        progWrap.style.display = 'block';
        const results = [];
        for (let i = 0; i < _attachFiles.length; i++) {
            const file = _attachFiles[i];
            progLabel.textContent = `Uploading & indexing ${i + 1}/${_attachFiles.length}: ${file.name}`;
            progBar.style.width = `${Math.round(((i + 0.5) / _attachFiles.length) * 100)}%`;

            const fd = new FormData();
            fd.append('file', file);
            fd.append('source_name', file.name.replace(/\.[^.]+$/, ''));
            fd.append('scope', scopeChoice.scope);
            if (scopeChoice.scope_id) fd.append('scope_id', scopeChoice.scope_id);
            if (scopeChoice.user_ids && scopeChoice.user_ids.length)
                scopeChoice.user_ids.forEach(uid => fd.append('user_ids', uid));
            if (destChoice.destination === 'sharepoint') {
                fd.append('destination', 'sharepoint');
                fd.append('sharepoint_watch_id', destChoice.sharepoint_watch_id);
                fd.append('agent_id', _activeAgent.id);
            }

            try {
                const r    = await fetch('/api/knowledge/upload', { method: 'POST', body: fd });
                const data = await r.json();
                if (data.success) {
                    // ── Auto-add doc to agent's search_knowledge tool config ──────
                    try {
                        const r2 = await fetch(`/api/agenthub/agents/${_activeAgent.id}/add-doc`, {
                            method:  'POST',
                            headers: {'Content-Type': 'application/json'},
                            body:    JSON.stringify({ document_id: data.document_id }),
                        });
                        const d2 = await r2.json();
                        if (d2.success) _patchAgentToolDocIds(_activeAgent, d2.document_ids);
                    } catch (_) { /* non-fatal */ }

                    if (data.sharepoint_archive && !data.sharepoint_archive.success) {
                        showToast(`${file.name} indexed, but SharePoint archive copy failed: ${data.sharepoint_archive.error || 'unknown error'}`, 'warning');
                    }

                    results.push({
                        success:     true,
                        filename:    data.filename,
                        source_name: data.source_name || data.filename,
                        doc_id:      data.document_id,
                        chunk_count: data.chunk_count,
                    });
                } else {
                    results.push({ success: false, filename: file.name, error: data.error || 'Upload failed' });
                }
            } catch (e) {
                results.push({ success: false, filename: file.name, error: e.message });
            }
        }

        progBar.style.width = '100%';
        progBar.classList.remove('progress-bar-animated');
        const okCount = results.filter(r => r.success).length;
        if (okCount === results.length) {
            progLabel.textContent = `Indexed ${okCount} file${okCount !== 1 ? 's' : ''} — added to agent`;
        } else {
            progBar.classList.add('bg-warning');
            progLabel.textContent = `${okCount}/${results.length} indexed — ${results.length - okCount} failed`;
        }

        _attachExtracted = { mode: 'kb', results };
        return _attachExtracted;
    } else {
        // Inline: extract text from each file, no scope dialog needed (read-only, not indexed)
        const results = [];
        for (let i = 0; i < _attachFiles.length; i++) {
            const file = _attachFiles[i];
            progLabel.textContent = `Reading ${i + 1}/${_attachFiles.length}: ${file.name}`;
            progBar.style.width = `${Math.round(((i + 0.5) / _attachFiles.length) * 100)}%`;

            const fd = new FormData();
            fd.append('file', file);
            try {
                const r    = await fetch('/api/knowledge/extract-text', { method: 'POST', body: fd });
                const data = await r.json();
                if (data.success) {
                    results.push({
                        success:     true,
                        filename:    data.filename,
                        text:        data.text,
                        truncated:   data.truncated,
                        server_path: data.server_path,
                    });
                } else {
                    results.push({ success: false, filename: file.name, error: data.error || 'Extraction failed' });
                }
            } catch (e) {
                results.push({ success: false, filename: file.name, error: e.message });
            }
        }

        progBar.style.width = '100%';
        progBar.classList.remove('progress-bar-animated');
        const okCount = results.filter(r => r.success).length;
        if (okCount === results.length) {
            progLabel.textContent = `Extracted ${okCount} file${okCount !== 1 ? 's' : ''}`;
        } else {
            progBar.classList.add('bg-warning');
            progLabel.textContent = `${okCount}/${results.length} extracted — ${results.length - okCount} failed`;
        }

        _attachExtracted = { mode: 'inline', results };
        return _attachExtracted;
    }
}

// ── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Read URL params for dept_id / project_id → auto-select first matching agent
    const _urlParams  = new URLSearchParams(window.location.search);
    const _urlDeptId  = _urlParams.get('dept_id')    ? +_urlParams.get('dept_id')    : null;
    const _urlProjId  = _urlParams.get('project_id') ? +_urlParams.get('project_id') : null;
    const _urlAgentId = _urlParams.get('agent_id')   || null;

    // Set page title if coming from a dept/project context
    if (_urlDeptId || _urlProjId) {
        // Will be updated once agents load with the matched context name
        const sub = document.getElementById('chatPageSubtitle');
        if (sub) sub.textContent = 'Loading context…';
    }

    // Store on window so loadHubAgents can use them
    window._autoSelectDeptId  = _urlDeptId;
    window._autoSelectProjId  = _urlProjId;
    window._autoSelectAgentId = _urlAgentId;

    loadHubAgents();
    loadTokenBadge();
});

async function loadHubAgents() {
    try {
        const res  = await fetch('/api/agenthub/agents');
        _allAgents = await res.json();
        renderAgentList(_allAgents);

        // Auto-select agent from URL params
        const deptId  = window._autoSelectDeptId;
        const projId  = window._autoSelectProjId;
        const agentId = window._autoSelectAgentId;

        let target = null;
        if (agentId) {
            target = _allAgents.find(a => a.id === agentId);
        } else if (deptId) {
            target = _allAgents.find(a => (a.dept_ids || []).includes(deptId));
            if (target) {
                const title = document.getElementById('chatPageTitle');
                const sub   = document.getElementById('chatPageSubtitle');
                const idx   = (target.dept_ids || []).indexOf(deptId);
                const name  = idx >= 0 ? (target.dept_names || [])[idx] : null;
                if (title && name) {
                    title.innerHTML = `<i class="fas fa-building me-2"></i>${esc(name)}`;
                    if (sub) sub.textContent = 'Chat with your AI assistants';
                }
            }
        } else if (projId) {
            target = _allAgents.find(a => (a.project_ids || []).includes(projId));
            if (target) {
                const title = document.getElementById('chatPageTitle');
                const sub   = document.getElementById('chatPageSubtitle');
                const idx   = (target.project_ids || []).indexOf(projId);
                const name  = idx >= 0 ? (target.project_names || [])[idx] : null;
                if (title && name) {
                    title.innerHTML = `<i class="fas fa-folder-open me-2"></i>${esc(name)}`;
                    if (sub) sub.textContent = 'Chat with your AI assistants';
                }
            }
        }

        if (target) {
            // Small delay so renderAgentList DOM is settled
            setTimeout(() => selectAgent(target), 80);
        }
    } catch {
        document.getElementById('hubAgentList').innerHTML =
            '<div class="text-center py-4 text-danger" style="font-size:.82rem;"><i class="fas fa-exclamation-circle me-1"></i>Failed to load agents</div>';
    }
}

function renderAgentList(agents) {
    const el = document.getElementById('hubAgentList');

    // Filter by dept/project from URL param
    const urlDeptId = window._autoSelectDeptId;
    const urlProjId = window._autoSelectProjId;
    const hasFilter = urlDeptId || urlProjId;
    let filtered = agents || [];

    if (urlDeptId) {
        filtered = filtered.filter(a => (a.dept_ids || []).includes(urlDeptId));
    } else if (urlProjId) {
        filtered = filtered.filter(a => (a.project_ids || []).includes(urlProjId));
    }

    if (!filtered.length) {
        const emptyMsg = urlProjId ? 'No agents assigned yet for this project.'
                       : urlDeptId ? 'No agents assigned yet for this department.'
                       : 'No agents assigned yet';
        el.innerHTML = `<div class="text-center py-4 text-muted" style="font-size:.82rem;">
            <i class="fas fa-robot me-1"></i>${emptyMsg}
        </div>`;
        return;
    }
    el.innerHTML = filtered.map(a => {
        const orgDot = a.dept_color
            ? `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${a.dept_color};margin-left:4px;vertical-align:middle;"></span>`
            : '';
        return `
        <div class="hub-agent-item ${_activeAgent?.id === a.id ? 'active' : ''}"
             onclick="selectAgent(${JSON.stringify(a).replace(/"/g, '&quot;')})">
            <div class="hub-agent-avatar" style="background:${a.avatar_color||'#6366f1'}">
                ${(a.name||'?')[0].toUpperCase()}
            </div>
            <div class="hub-agent-info">
                <div class="name">${esc(a.name)}${orgDot}</div>
                <div class="desc">${esc(a.description||'')}</div>
            </div>
        </div>`;
    }).join('');
}

// Re-render agent list when org context changes — re-read URL params first
// so the filter and title reflect the new project/department, not the stale initial values
window.addEventListener('orgContextChange', () => {
    if (!_allAgents) return;
    const p = new URLSearchParams(window.location.search);
    window._autoSelectDeptId = p.get('dept_id')    ? +p.get('dept_id')    : null;
    window._autoSelectProjId = p.get('project_id') ? +p.get('project_id') : null;
    renderAgentList(_allAgents);
});

function selectAgent(agent) {
    _activeAgent   = agent;
    _activeConvoId = null;
    document.querySelectorAll('.hub-agent-item').forEach(el => el.classList.remove('active'));
    event?.currentTarget?.classList.add('active');
    renderAgentList(_allAgents); // re-render to update active state

    document.getElementById('hubEmptyState').style.display       = 'none';
    document.getElementById('hubChatMessages').style.display      = 'flex';
    document.getElementById('hubInputArea').style.display         = 'block';
    document.getElementById('hubChatHeader').style.display        = 'flex';
    document.getElementById('hubChatMessages').innerHTML          = '';

    document.getElementById('activeChatAvatar').textContent       = (agent.name||'?')[0].toUpperCase();
    document.getElementById('activeChatAvatar').style.background  = agent.avatar_color || '#6366f1';
    document.getElementById('activeChatName').textContent         = agent.name;
    document.getElementById('activeChatObj').textContent          = agent.objective || agent.description || '';

    // Show Create dropdown; reset Dashboard item (no data yet for new conversation)
    const createDrop = document.getElementById('hubCreateDropdown');
    if (createDrop) createDrop.style.display = '';
    const dashItem = document.getElementById('hubCreateDashboardItem');
    if (dashItem) dashItem.style.display = 'none';

    // Show "Add Document" button only for agents with process_document tool
    const addDocBtn = document.getElementById('hubAddDocBtn');
    if (addDocBtn) addDocBtn.style.display = agentHasProcessDocument() ? '' : 'none';

    // If KB-mode file(s) are pending but the new agent can't process docs, clear them
    if (_attachMode === 'kb' && _attachFiles.length && !agentHasProcessDocument()) {
        clearAttachment();
    }

    document.getElementById('hubMsgInput').focus();
}

function startNewConvo() {
    _activeConvoId = null;
    document.getElementById('hubChatMessages').innerHTML = '';
    document.getElementById('hubMsgInput').focus();
}

// ── Send message ─────────────────────────────────────────────────────────────
async function sendHubMsg() {
    if (_streaming || !_activeAgent) return;
    const input   = document.getElementById('hubMsgInput');
    let   text    = input.value.trim();
    const hasFile = _attachFiles.length > 0;

    if (!text && !hasFile) return;

    // ── Handle attachment ─────────────────────────────────────────────────────
    let finalMessage   = text;
    let fileStatusText = '';
    if (hasFile) {
        document.getElementById('hubSendBtn').disabled = true;
        const att = await prepareAttachment();
        document.getElementById('hubSendBtn').disabled = false;

        if (!att) return; // extraction failed, let user see the error

        // Build a per-file status line the USER actually sees — never rely on
        // the LLM to relay upload/extraction failures buried in finalMessage.
        fileStatusText = att.results.map(r => r.success
            ? `✅ ${r.filename}${att.mode === 'kb' ? ` — indexed (${r.chunk_count} chunks)` : ''}`
            : `❌ ${r.filename} — ${r.error}`
        ).join('\n');

        if (att.mode === 'kb') {
            // KB mode: tell agent which file(s) were indexed and that it can search them
            const lines = att.results.map(r => r.success
                ? `- ${r.filename} → indexed (${r.chunk_count} chunks, doc_id=${r.doc_id})`
                : `- ${r.filename} → FAILED: ${r.error}`);
            const kbNote = `[File(s) uploaded to Knowledge Base]\n${lines.join('\n')}\n\nIndexed documents have been added. You can now search them with the search_knowledge tool.`;
            finalMessage = text ? `${kbNote}\n\n${text}` : kbNote;
        } else {
            // Inline mode: prepend each file's extracted text as context
            const blocks = att.results.map(r => r.success
                ? `[Attached file: ${r.filename}]${r.truncated ? ' (content truncated to 8000 chars)' : ''}\n${r.text}`
                : `[Attached file: ${r.filename}] FAILED to read: ${r.error}`);
            const header = `IMPORTANT: The full content of each attached file is provided directly below. Read it from this message — do NOT call search_knowledge for these files.\n\n${blocks.join('\n\n---\n\n')}`;
            finalMessage = text ? `${header}\n\n---\n\n${text}` : header;
        }

        clearAttachment();
    }

    if (!finalMessage) return;

    input.value = '';
    autoResize(input);

    // Show the real per-file outcome — never collapse to a generic "file attached"
    // (appendMsg HTML-escapes user messages, so this stays plain text by design)
    const displayText = hasFile
        ? (text ? `${fileStatusText}\n${text}` : fileStatusText)
        : text;
    appendMsg('user', displayText);

    const thinkingEl = appendMsg('assistant', '<span class="hub-streaming-cursor"></span>', true);

    _streaming = true;
    _lastDataAgentResult = null;
    _lastQueryDbResult   = null;
    _docRequested        = !hasFile ? _detectDocType(text) : null;
    document.getElementById('hubSendBtn').disabled = true;

    try {
        const resp = await fetch('/api/agenthub/chat/stream', {
            method:  'POST',
            headers: {'Content-Type': 'application/json'},
            body:    JSON.stringify({
                agent_id:        _activeAgent.id,
                message:         finalMessage,
                conversation_id: _activeConvoId,
                ..._getActiveOrgScope(),
            }),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            thinkingEl.querySelector('.hub-msg-bubble').innerHTML =
                `<span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>${esc(err.error || 'Error')}</span>`;
            return;
        }

        const reader    = resp.body.getReader();
        const decoder   = new TextDecoder();
        let   buffer    = '';
        let   content   = '';
        let   toolCalls = [];

        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const parsed = JSON.parse(line);

                    if (parsed.type === 'conversation_id') {
                        _activeConvoId = parsed.conversation_id;
                    } else if (parsed.type === 'text') {
                        content += parsed.content || '';
                        renderStreamingMsg(thinkingEl, content, toolCalls);
                    } else if (parsed.type === 'tool_start') {
                        if (parsed.tool === 'request_human_approval') {
                            toolCalls.push({name: 'Request Human Approval ✅', status: 'running', input: parsed.input || {}});
                        } else {
                            toolCalls.push({name: parsed.tool, status: 'running', input: parsed.input || {}});
                        }
                        renderStreamingMsg(thinkingEl, content, toolCalls);
                    } else if (parsed.type === 'tool_result') {
                        const tc = toolCalls[toolCalls.length - 1];
                        if (tc) {
                            tc.status        = parsed.result?.success === false ? 'error' : 'done';
                            tc.result        = parsed.result;
                            tc.execution_time = parsed.execution_time;
                        }
                        if (parsed.tool === 'communicate_with_data_agent') {
                            // Executor wraps result: parsed.result = {success, tool, result: {data, ...}}
                            const outer = parsed.result || {};
                            const r = outer.result || outer;
                            if (Array.isArray(r.data) && r.data.length > 0) {
                                _lastDataAgentResult = r;
                            }
                        }
                        if (parsed.tool === 'query_database') {
                            const outer = parsed.result || {};
                            const r = outer.result || outer;
                            if (Array.isArray(r.columns) && Array.isArray(r.rows) && r.rows.length > 0) {
                                _lastQueryDbResult = r;
                            }
                        }
                        if (parsed.tool === 'analyze_csv_files') {
                            const outer = parsed.result || {};
                            const r = outer.result || outer;
                            // DataFrame result: {type:"dataframe", columns, data}
                            const df = r.type === 'dataframe' ? r
                                : (typeof r === 'object' && Object.values(r).find(v => v && v.type === 'dataframe'));
                            if (df && Array.isArray(df.columns) && Array.isArray(df.data) && df.data.length > 0) {
                                _lastQueryDbResult = {
                                    columns:    df.columns,
                                    rows:       df.data.map(row => df.columns.map(c => row[c])),
                                    row_count:  df.total_rows || df.data.length,
                                    sql:        '',
                                    connection: 'CSV/Excel',
                                };
                            }
                        }
                        renderStreamingMsg(thinkingEl, content, toolCalls);
                    } else if (parsed.type === 'approval_requested') {
                        _pendingApproval = parsed.approval_id;
                    } else if (parsed.type === 'final') {
                        content = parsed.content || content;
                        renderStreamingMsg(thinkingEl, content, toolCalls, true);
                        if (_pendingApproval) {
                            appendApprovalCard(thinkingEl, _pendingApproval);
                            startApprovalPolling(_pendingApproval, thinkingEl);
                            _pendingApproval = null;
                        }
                        // Fallback: recover from toolCalls memory if tool_result parse was silent-dropped
                        if (!_lastDataAgentResult) {
                            for (const tc of toolCalls) {
                                if (tc.name === 'communicate_with_data_agent') {
                                    const outer = tc.result || {};
                                    const r = outer.result || outer;
                                    if (Array.isArray(r.data) && r.data.length > 0) {
                                        _lastDataAgentResult = r;
                                        break;
                                    }
                                }
                            }
                        }
                        if (!_lastQueryDbResult) {
                            for (const tc of toolCalls) {
                                if (tc.name === 'query_database') {
                                    const outer = tc.result || {};
                                    const r = outer.result || outer;
                                    if (Array.isArray(r.columns) && Array.isArray(r.rows) && r.rows.length > 0) {
                                        _lastQueryDbResult = r;
                                        break;
                                    }
                                }
                                if (tc.name === 'analyze_csv_files') {
                                    const outer = tc.result || {};
                                    const r = outer.result || outer;
                                    const df = r.type === 'dataframe' ? r
                                        : (typeof r === 'object' && Object.values(r).find(v => v && v.type === 'dataframe'));
                                    if (df && Array.isArray(df.columns) && Array.isArray(df.data) && df.data.length > 0) {
                                        _lastQueryDbResult = {
                                            columns:    df.columns,
                                            rows:       df.data.map(row => df.columns.map(c => row[c])),
                                            row_count:  df.total_rows || df.data.length,
                                            sql:        '',
                                            connection: 'CSV/Excel',
                                        };
                                        break;
                                    }
                                }
                            }
                        }
                        const _hadDataAgent = !!_lastDataAgentResult;
                        const _hadQueryDb   = !!_lastQueryDbResult;
                        if (_lastDataAgentResult) {
                            injectVizCardIntoBubble(thinkingEl, _lastDataAgentResult);
                            _lastDataAgentResult = null;
                        }
                        if (_lastQueryDbResult) {
                            injectQueryDbCard(thinkingEl, _lastQueryDbResult);
                            _lastQueryDbResult = null;
                        }
                        if (!_hadDataAgent && !_hadQueryDb && _docRequested) {
                            const _dtype = _docRequested;
                            setTimeout(() => hubCreateDocument(_dtype), 300);
                        }
                        _docRequested = null;
                        loadTokenBadge();
                    } else if (parsed.type === 'error') {
                        thinkingEl.querySelector('.hub-msg-bubble').innerHTML =
                            `<span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>${esc(parsed.content)}</span>`;
                    }
                } catch(e) { console.warn('[hub stream parse error]', e); }
            }
        }
    } catch (err) {
        thinkingEl.querySelector('.hub-msg-bubble').innerHTML =
            `<span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>Connection error</span>`;
    } finally {
        _streaming = false;
        document.getElementById('hubSendBtn').disabled = false;
    }
}

function renderStreamingMsg(el, content, toolCalls, done = false) {
    const bubble = el.querySelector('.hub-msg-bubble');
    let html = md2html(content) + (done ? '' : '<span class="hub-streaming-cursor"></span>');
    if (toolCalls.length && _hubCanSeeToolTrace()) {
        html += '<div class="hub-tool-trace">' + toolCalls.map((tc, i) => renderToolChip(tc, i)).join('') + '</div>';
    }
    bubble.innerHTML = html;
    scrollToBottom();
}

function renderToolChip(tc, idx) {
    const icon = tc.status === 'running'
        ? 'fas fa-spinner fa-spin'
        : tc.status === 'error'
            ? 'fas fa-times-circle text-danger'
            : 'fas fa-check-circle text-success';
    const timeLabel = tc.execution_time != null ? ` · ${tc.execution_time}s` : '';
    const hasDetail = tc.input || tc.result;
    const detailId  = `tc-detail-${Date.now()}-${idx}`;

    let detailHtml = '';
    if (hasDetail && tc.status !== 'running') {
        const inputStr  = JSON.stringify(tc.input  || {}, null, 2);
        const resultStr = JSON.stringify(tc.result || {}, null, 2);
        const resultSuccess = tc.result?.success !== false;
        detailHtml = `
        <div class="hub-tc-detail" id="${detailId}" style="display:none;">
            <div class="hub-tc-section">
                <span class="hub-tc-label">Input</span>
                <pre class="hub-tc-pre">${esc(inputStr)}</pre>
            </div>
            <div class="hub-tc-section">
                <span class="hub-tc-label" style="color:${resultSuccess?'#22c55e':'#ef4444'};">Output ${resultSuccess?'✓':'✗'}</span>
                <pre class="hub-tc-pre">${esc(resultStr)}</pre>
            </div>
        </div>`;
    }

    const toggleAttr = hasDetail && tc.status !== 'running'
        ? `onclick="hubToggleToolDetail('${detailId}', this)" style="cursor:pointer;" title="Click to expand"`
        : '';

    return `
    <div class="hub-tool-call" ${toggleAttr}>
        <i class="${icon}"></i>
        <span class="hub-tc-name">${esc(tc.name)}</span>
        <span class="hub-tc-time">${timeLabel}</span>
        ${hasDetail && tc.status !== 'running' ? '<i class="fas fa-chevron-down hub-tc-chevron ms-auto"></i>' : ''}
    </div>
    ${detailHtml}`;
}

function hubToggleToolDetail(detailId, headerEl) {
    const panel   = document.getElementById(detailId);
    const chevron = headerEl.querySelector('.hub-tc-chevron');
    if (!panel) return;
    const open = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : '';
    if (chevron) chevron.style.transform = open ? '' : 'rotate(180deg)';
}

function appendMsg(role, html, isStreaming = false) {
    const msgs = document.getElementById('hubChatMessages');
    const div  = document.createElement('div');
    div.className = `hub-msg ${role}`;
    div.innerHTML = `<div class="hub-msg-bubble">${isStreaming ? html : (role === 'user' ? esc(html) : md2html(html))}</div>
                     <div class="hub-msg-meta">${new Date().toLocaleTimeString()}</div>`;
    msgs.appendChild(div);
    scrollToBottom();
    return div;
}

function scrollToBottom() {
    const el = document.getElementById('hubChatMessages');
    el.scrollTop = el.scrollHeight;
}

// ── Approval card (shown in chat when agent requests human approval) ──────────
function appendApprovalCard(msgEl, approvalId) {
    const card = document.createElement('div');
    card.id        = `apr-card-${approvalId}`;
    card.className = 'hub-approval-card mt-2';
    card.innerHTML = `
        <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;padding:12px 14px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="fas fa-pause-circle text-warning fs-5"></i>
                <strong style="font-size:.88rem;">Waiting for Human Approval</strong>
                <span class="spinner-border spinner-border-sm text-warning ms-auto"></span>
            </div>
            <div class="text-muted" style="font-size:.78rem;" id="apr-status-${approvalId}">
                Approval request sent. The designated approver will review and respond.
            </div>
            <div class="mt-2 d-flex gap-2 flex-wrap">
                <a href="/approvals" target="_blank" class="btn btn-sm btn-outline-warning" style="font-size:.75rem;">
                    <i class="fas fa-external-link-alt me-1"></i>View Approvals
                </a>
                <span class="text-muted" style="font-size:.72rem;align-self:center;">
                    ID: <code>${approvalId.slice(0,8)}…</code>
                </span>
            </div>
        </div>`;
    msgEl.appendChild(card);
    scrollToBottom();
}

function startApprovalPolling(approvalId, msgEl) {
    const INTERVAL = 10000; // poll every 10 s
    const timer = setInterval(async () => {
        try {
            const res  = await fetch(`/api/agenthub/approvals/${approvalId}`);
            if (!res.ok) return;
            const data = await res.json();
            if (data.status !== 'pending') {
                clearInterval(timer);
                const card = document.getElementById(`apr-card-${approvalId}`);
                if (!card) return;
                const isApproved = data.status === 'approved';
                const who  = data.approver_username || 'Approver';
                const note = data.approver_note ? ` — "${esc(data.approver_note)}"` : '';
                card.innerHTML = `
                    <div style="background:${isApproved ? '#dcfce7' : '#fee2e2'};
                                border:1px solid ${isApproved ? '#86efac' : '#fca5a5'};
                                border-radius:8px;padding:12px 14px;">
                        <div class="d-flex align-items-center gap-2">
                            <i class="fas fa-${isApproved ? 'check-circle text-success' : 'times-circle text-danger'} fs-5"></i>
                            <strong style="font-size:.88rem;">
                                ${isApproved ? 'Approved' : 'Rejected'} by ${esc(who)}
                            </strong>
                        </div>
                        ${note ? `<div class="text-muted mt-1" style="font-size:.78rem;">${note}</div>` : ''}
                        <div class="text-muted mt-1" style="font-size:.72rem;">
                            ${isApproved
                                ? 'The agent may now proceed. Send a follow-up message to continue.'
                                : 'The action was rejected. Send a follow-up to let the agent know.'}
                        </div>
                    </div>`;
                scrollToBottom();
            }
        } catch { /* ignore */ }
    }, INTERVAL);
}

// ── Conversations panel ──────────────────────────────────────────────────────
async function showConversations() {
    _convoVisible = true;
    document.getElementById('hubConvoPanel').style.display = 'flex';
    document.getElementById('hubConvoPanel').style.flexDirection = 'column';
    await loadConversations();
}

function hideConversations() {
    _convoVisible = false;
    document.getElementById('hubConvoPanel').style.display = 'none';
}

async function loadConversations() {
    const el = document.getElementById('hubConvoList');
    const url = _activeAgent
        ? `/api/agenthub/chat/conversations?agent_id=${_activeAgent.id}`
        : '/api/agenthub/chat/conversations';
    try {
        const res = await fetch(url);
        _allConvos = await res.json();
        if (!_allConvos.length) {
            el.innerHTML = '<div class="text-center py-4 text-muted" style="font-size:.8rem;">No conversations yet</div>';
            return;
        }
        el.innerHTML = _allConvos.map(c => `
            <div class="hub-convo-item ${_activeConvoId === c.id ? 'active' : ''}" onclick="loadConversation('${c.id}')">
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(c.title||'Untitled')}</span>
                <button class="btn btn-sm btn-link text-muted p-0 ms-1" onclick="event.stopPropagation();deleteConvo('${c.id}')" title="Delete">
                    <i class="fas fa-trash" style="font-size:.7rem;"></i>
                </button>
            </div>
        `).join('');
    } catch {
        el.innerHTML = '<div class="text-center py-2 text-danger" style="font-size:.8rem;">Failed to load</div>';
    }
}

async function loadConversation(cid) {
    _activeConvoId = cid;
    hideConversations();
    try {
        const res  = await fetch(`/api/agenthub/chat/conversations/${cid}`);
        const data = await res.json();
        const msgs = document.getElementById('hubChatMessages');
        msgs.innerHTML = '';

        // Set active agent if not already set — restore cid after because selectAgent() clears it
        if (data.agent_id && (!_activeAgent || _activeAgent.id !== data.agent_id)) {
            const ag = _allAgents.find(a => a.id === data.agent_id);
            if (ag) { selectAgent(ag); _activeConvoId = cid; }
        }

        (data.messages || []).forEach(m => {
            const el = appendMsg(m.role, m.content);
            if (m.role === 'assistant' && m.tool_calls?.length && _hubCanSeeToolTrace()) {
                const bubble = el.querySelector('.hub-msg-bubble');
                const traceHtml = '<div class="hub-tool-trace">' +
                    m.tool_calls.map((tc, i) => renderToolChip({
                        name:           tc.tool || tc.name,
                        status:         tc.result?.success === false ? 'error' : 'done',
                        input:          tc.input,
                        result:         tc.result,
                        execution_time: tc.execution_time,
                    }, i)).join('') +
                    '</div>';
                bubble.innerHTML += traceHtml;
            }
        });
        scrollToBottom();
    } catch {
        showToast('Failed to load conversation', 'error');
    }
}

async function deleteConvo(cid) {
    if (!confirm('Delete this conversation?')) return;
    await fetch(`/api/agenthub/chat/conversations/${cid}`, {method: 'DELETE'});
    if (_activeConvoId === cid) {
        _activeConvoId = null;
        document.getElementById('hubChatMessages').innerHTML = '';
    }
    await loadConversations();
}

// ── File document generation (docx / xlsx / csv / markdown / txt) ─────────────
const _DOC_META = {
    pdf:      { icon:'fas fa-file-pdf',    label:'PDF Document',     color:'#dc2626', ext:'pdf'  },
    docx:     { icon:'fas fa-file-word',   label:'Word Document',    color:'#A22E57', ext:'docx' },
    xlsx:     { icon:'fas fa-file-excel',  label:'Excel Spreadsheet',color:'#16a34a', ext:'xlsx' },
    csv:      { icon:'fas fa-file-csv',    label:'CSV File',         color:'#d97706', ext:'csv'  },
    markdown: { icon:'fas fa-file-code',   label:'Markdown',         color:'#7c3aed', ext:'md'   },
    txt:      { icon:'fas fa-file-alt',    label:'Text File',        color:'#6e6265', ext:'txt'  },
};

async function _hubGenerateFileDoc(type, messages) {
    const instrMsg    = [...messages].reverse()
        .find(m => m.startsWith('User:') && _detectDocType(m.replace(/^User:\s*/, '')) !== null)
        || [...messages].reverse().find(m => m.startsWith('User:'))
        || '';
    const instruction = instrMsg.replace(/^User:\s*/, '');
    const meta        = _DOC_META[type] || _DOC_META.txt;

    // Append a "generating" indicator as a new assistant message
    const msgEl  = appendMsg('assistant', '', true);
    const bubble = msgEl.querySelector('.hub-msg-bubble');
    bubble.innerHTML = `
        <div style="display:flex;align-items:center;gap:10px;color:#6e6265;font-size:.85rem;padding:4px 0;">
            <span class="hub-streaming-cursor"></span>
            <span>Generating your ${meta.label}…</span>
        </div>`;
    scrollToBottom();

    try {
        const res  = await fetch('/api/agenthub/generate-document', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ type, messages, instruction, agent_name: _activeAgent?.name || '' }),
        });
        const data = await res.json();

        if (data.status !== 'success') {
            bubble.innerHTML = `<span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>${esc(data.message || 'Generation failed')}</span>`;
            return;
        }

        window._hubLastDocument = data;
        bubble.innerHTML = '';
        appendDocReadyCard(msgEl, data);

    } catch (e) {
        bubble.innerHTML = `<span class="text-danger"><i class="fas fa-exclamation-circle me-1"></i>Failed: ${esc(e.message)}</span>`;
    }
}

function appendDocReadyCard(msgEl, data) {
    const meta  = _DOC_META[data.doc_type] || _DOC_META.txt;
    const kb    = data.size_bytes ? (data.size_bytes / 1024).toFixed(1) : '?';
    const bubble = msgEl.querySelector('.hub-msg-bubble');

    bubble.innerHTML = `
        <div style="background:#f8f6f6;border:1px solid #e6dfe0;border-radius:10px;padding:12px 14px;min-width:240px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="${meta.icon}" style="color:${meta.color};font-size:1.25rem;flex-shrink:0;"></i>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(data.filename)}</div>
                    <div style="font-size:.72rem;color:#a89b9d;">${meta.label} · ${kb} KB</div>
                </div>
                <span style="background:${meta.color}22;color:${meta.color};font-size:.65rem;font-weight:700;text-transform:uppercase;
                             letter-spacing:.08em;padding:2px 8px;border-radius:99px;white-space:nowrap;">${meta.ext.toUpperCase()}</span>
            </div>
            <div class="d-flex gap-2">
                <button class="btn btn-sm btn-primary" onclick="hubDownloadDoc()" style="font-size:.78rem;">
                    <i class="fas fa-download me-1"></i>Download
                </button>
                <button class="btn btn-sm btn-outline-secondary" onclick="hubPreviewDoc()" style="font-size:.78rem;">
                    <i class="fas fa-eye me-1"></i>Preview
                </button>
            </div>
        </div>`;
    scrollToBottom();
}

function hubDownloadDoc() {
    const doc = window._hubLastDocument;
    if (!doc) { showToast('No document available.', 'error'); return; }

    let blob;
    if (doc.content_b64) {
        const bin = atob(doc.content_b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        blob = new Blob([arr], { type: doc.mime_type || 'application/octet-stream' });
    } else {
        blob = new Blob([doc.content_text || ''], { type: doc.mime_type || 'text/plain' });
    }

    const url = URL.createObjectURL(blob);
    const a   = Object.assign(document.createElement('a'), { href: url, download: doc.filename || 'document' });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// ── Hub-specific preview panel ────────────────────────────────────────────────
function _hubShowPreview(title, src) {
    const panel   = document.getElementById('hubPreviewPanel');
    const content = document.getElementById('hubPreviewContent');
    const titleEl = document.getElementById('hubPreviewTitle');
    if (!panel) return null;
    titleEl.textContent = title || 'Preview';
    content.innerHTML = `<iframe src="${src}" frameborder="0"></iframe>`;
    panel.classList.add('open');
    return content.querySelector('iframe');
}
function closeHubPreview() {
    const panel = document.getElementById('hubPreviewPanel');
    if (panel) panel.classList.remove('open');
}

function hubPreviewDoc() {
    const doc = window._hubLastDocument;
    if (!doc) { showToast('No document to preview.', 'error'); return; }

    const iframe = _hubShowPreview(doc.filename || 'Document', '/preview/document');
    if (!iframe) return;
    iframe.onload = function () {
        iframe.contentWindow.postMessage({
            type:         'documentData',
            docType:      doc.doc_type,
            filename:     doc.filename,
            content_text: doc.content_text  || null,
            content_b64:  doc.content_b64   || null,
            preview_html: doc.preview_html  || null,
            mime_type:    doc.mime_type     || null,
        }, '*');
    };
}

// ── Data visualization — injected inside the bubble so it's always visible ────
// ── Shared table builder for hub data results ─────────────────────────────────
const _HUB_TABLE_PREVIEW = 10;  // rows shown before "show all"
const _hubTableStore = {};       // tableId → {rows, columns, isArrayOfArrays}

function _hubFmtCell(v) {
    if (v === null || v === undefined) return '<span class="hub-null-val">—</span>';
    return esc(String(v)).replace(/\n/g, ' ');
}

function _hubBuildRows(rows, columns, isArrayOfArrays) {
    return rows.map(row => {
        const cells = isArrayOfArrays
            ? columns.map((_, i) => `<td>${_hubFmtCell(row[i])}</td>`).join('')
            : columns.map(c => `<td>${_hubFmtCell(row[c])}</td>`).join('');
        return `<tr>${cells}</tr>`;
    }).join('');
}

function _buildHubDataTable(columns, rows, isArrayOfArrays) {
    if (!columns || !columns.length || !rows || !rows.length) return '';
    const total   = rows.length;
    const tableId = 'hdt-' + Math.random().toString(36).slice(2, 8);
    _hubTableStore[tableId] = { rows, columns, isArrayOfArrays };

    const thead        = `<thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead>`;
    const tbodyPreview = `<tbody id="${tableId}-body">${_hubBuildRows(rows.slice(0, _HUB_TABLE_PREVIEW), columns, isArrayOfArrays)}</tbody>`;
    const showAllBtn   = total > _HUB_TABLE_PREVIEW
        ? `<button class="hub-show-all-btn" id="${tableId}-btn" data-expanded="0"
               onclick="_hubToggleTableRows('${tableId}')">Show all ${total} rows</button>`
        : `<span style="font-size:.72rem;color:#a89b9d;">${total} row${total === 1 ? '' : 's'}</span>`;

    return `
        <div class="hub-data-table-wrap" id="${tableId}-wrap">
            <table class="hub-data-table">${thead}${tbodyPreview}</table>
        </div>
        <div style="margin-top:4px;">${showAllBtn}</div>`;
}

function _hubToggleTableRows(tableId) {
    const store  = _hubTableStore[tableId];
    const tbody  = document.getElementById(tableId + '-body');
    const btn    = document.getElementById(tableId + '-btn');
    const wrap   = document.getElementById(tableId + '-wrap');
    if (!store || !tbody || !btn) return;
    const { rows, columns, isArrayOfArrays } = store;
    const expanded = btn.dataset.expanded === '1';
    if (expanded) {
        tbody.innerHTML      = _hubBuildRows(rows.slice(0, _HUB_TABLE_PREVIEW), columns, isArrayOfArrays);
        btn.textContent      = `Show all ${rows.length} rows`;
        btn.dataset.expanded = '0';
        if (wrap) wrap.style.maxHeight = '320px';
    } else {
        tbody.innerHTML      = _hubBuildRows(rows, columns, isArrayOfArrays);
        btn.textContent      = 'Show less';
        btn.dataset.expanded = '1';
        if (wrap) wrap.style.maxHeight = '480px';
    }
}

// ── query_database result card ─────────────────────────────────────────────────
function injectQueryDbCard(msgEl, dbResult) {
    const columns  = dbResult.columns || [];
    const rows     = dbResult.rows    || [];   // array-of-arrays
    const rowCount = rows.length;
    const sqlText  = dbResult.sql     || '';
    const connName = dbResult.connection || '';

    const tableHtml = _buildHubDataTable(columns, rows, true);
    const card = document.createElement('div');
    card.className = 'hub-dataviz-card mt-3';
    card.innerHTML = `
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:12px 14px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="fas fa-table" style="color:#16a34a;font-size:.9rem;"></i>
                <strong style="font-size:.82rem;color:#14532d;">Query result${connName ? ` · ${esc(connName)}` : ''}</strong>
                <span style="background:#16a34a;color:#fff;border-radius:20px;padding:1px 8px;font-size:.68rem;margin-left:auto;white-space:nowrap;">${rowCount} row${rowCount===1?'':'s'}</span>
            </div>
            ${tableHtml}
            ${sqlText ? `<details style="margin-top:8px;">
                <summary style="font-size:.72rem;color:#6e6265;cursor:pointer;">View SQL</summary>
                <pre style="font-size:.72rem;margin-top:4px;padding:6px 8px;background:#150f12;color:#e6dfe0;border-radius:6px;overflow-x:auto;white-space:pre-wrap;">${esc(sqlText)}</pre>
            </details>` : ''}
        </div>`;
    const bubble = msgEl.querySelector('.hub-msg-bubble');
    (bubble || msgEl).appendChild(card);
    scrollToBottom();
}

function injectVizCardIntoBubble(msgEl, dataResult) {
    const cardId = 'hdr-' + Date.now();
    _hubDataRegistry[cardId] = dataResult;
    window._hubLastDataResult = dataResult;  // keep global updated for header dropdown

    const dashItem = document.getElementById('hubCreateDashboardItem');
    if (dashItem) dashItem.style.display = '';

    window.agentManager = window.agentManager || {};
    window.agentManager.lastQueryResult = {
        data:       dataResult.data    || [],
        columns:    dataResult.columns || (dataResult.data[0] ? Object.keys(dataResult.data[0]) : []),
        question:   dataResult.question || '',
        agent_name: dataResult.agent_name || '',
        insights:   dataResult.answer ? [{ message: dataResult.answer, raw_message: dataResult.answer }] : [],
    };
    window.currentAgentName = dataResult.agent_name || '';

    const rowCount = (dataResult.data || []).length;
    const columns  = dataResult.columns || (dataResult.data && dataResult.data[0] ? Object.keys(dataResult.data[0]) : []);
    const sqlText  = dataResult.sql || '';
    const tableHtml = _buildHubDataTable(columns, dataResult.data || [], false);

    const card = document.createElement('div');
    card.className = 'hub-dataviz-card mt-3';
    card.innerHTML = `
        <div style="background:#F8E8EB;border:1px solid #E8B9C4;border-radius:10px;padding:12px 14px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="fas fa-database" style="color:#6B2038;font-size:.9rem;"></i>
                <strong style="font-size:.82rem;color:#4A1C2E;">Data retrieved</strong>
                <span style="background:#6B2038;color:#fff;border-radius:20px;padding:1px 8px;font-size:.68rem;margin-left:auto;white-space:nowrap;">${rowCount} row${rowCount===1?'':'s'}</span>
            </div>
            ${tableHtml}
            ${sqlText ? `<details style="margin-top:8px;">
                <summary style="font-size:.72rem;color:#6e6265;cursor:pointer;">View SQL</summary>
                <pre style="font-size:.72rem;margin-top:4px;padding:6px 8px;background:#150f12;color:#e6dfe0;border-radius:6px;overflow-x:auto;white-space:pre-wrap;">${esc(sqlText)}</pre>
            </details>` : ''}
            <div class="d-flex gap-2 flex-wrap" style="margin-top:10px;padding-top:8px;border-top:1px solid #E8B9C4;">
                <span style="font-size:.72rem;color:#4A1C2E;align-self:center;font-weight:500;">Create:</span>
                <button class="btn btn-sm btn-outline-primary" onclick="hubTriggerViz('dashboard','${cardId}')" style="font-size:.75rem;padding:2px 10px;">
                    <i class="fas fa-tachometer-alt me-1"></i>Dashboard
                </button>
                <button class="btn btn-sm btn-outline-success" onclick="hubTriggerViz('report','${cardId}')" style="font-size:.75rem;padding:2px 10px;">
                    <i class="fas fa-file-alt me-1"></i>Report
                </button>
                <button class="btn btn-sm btn-outline-warning" onclick="hubTriggerViz('presentation','${cardId}')" style="font-size:.75rem;padding:2px 10px;">
                    <i class="fas fa-slideshare me-1"></i>Presentation
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="hubTriggerViz('infographic','${cardId}')" style="font-size:.75rem;padding:2px 10px;">
                    <i class="fas fa-chart-pie me-1"></i>Infographic
                </button>
            </div>
        </div>`;

    // Append inside the bubble so it's always visible regardless of parent CSS
    const bubble = msgEl.querySelector('.hub-msg-bubble');
    if (bubble) {
        bubble.appendChild(card);
    } else {
        msgEl.appendChild(card);
    }
    scrollToBottom();
}

// ── Data visualization prompt ─────────────────────────────────────────────────
function appendDataVizCard(msgEl, dataResult) {
    const cardId = 'hdr-' + Date.now();
    _hubDataRegistry[cardId] = dataResult;
    window._hubLastDataResult = dataResult;  // keep global updated for header dropdown

    // Unlock Dashboard in the persistent Create dropdown
    const dashItem = document.getElementById('hubCreateDashboardItem');
    if (dashItem) dashItem.style.display = '';

    const rowCount = (dataResult.data || []).length;
    const card = document.createElement('div');
    card.className = 'hub-dataviz-card mt-2';
    card.innerHTML = `
        <div style="background:#F8E8EB;border:1px solid #E8B9C4;border-radius:10px;padding:12px 14px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="fas fa-chart-bar" style="color:#6B2038;font-size:1rem;"></i>
                <strong style="font-size:.85rem;color:#4A1C2E;">Data retrieved — create a visualization?</strong>
                <span style="background:#6B2038;color:#fff;border-radius:20px;padding:1px 8px;font-size:.7rem;margin-left:auto;white-space:nowrap;">${rowCount} rows</span>
            </div>
            <div class="d-flex gap-2 flex-wrap">
                <button class="btn btn-sm btn-outline-primary" onclick="hubTriggerViz('dashboard','${cardId}')" style="font-size:.78rem;">
                    <i class="fas fa-tachometer-alt me-1"></i>Dashboard
                </button>
                <button class="btn btn-sm btn-outline-success" onclick="hubTriggerViz('report','${cardId}')" style="font-size:.78rem;">
                    <i class="fas fa-file-alt me-1"></i>Report
                </button>
                <button class="btn btn-sm btn-outline-warning" onclick="hubTriggerViz('presentation','${cardId}')" style="font-size:.78rem;">
                    <i class="fas fa-slideshare me-1"></i>Presentation
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="hubTriggerViz('infographic','${cardId}')" style="font-size:.78rem;">
                    <i class="fas fa-chart-pie me-1"></i>Infographic
                </button>
            </div>
        </div>`;
    msgEl.appendChild(card);
    scrollToBottom();
}

function hubTriggerViz(type, cardId) {
    const dr = (cardId && _hubDataRegistry[cardId]) || window._hubLastDataResult;
    if (!dr || !Array.isArray(dr.data) || !dr.data.length) {
        showToast('No data available — ask a data question first.', 'error');
        return;
    }

    // Build a minimal insights array for infographic (needs at least one entry)
    let insights = [];
    if (dr.answer) {
        insights.push({ message: dr.answer, raw_message: dr.answer });
    } else {
        (dr.data || []).slice(0, 5).forEach(row => {
            const txt = Object.entries(row).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(' · ');
            if (txt) insights.push({ message: txt, raw_message: txt });
        });
    }

    // Expose data so the shared generation functions can read it
    if (!window.agentManager) window.agentManager = {};
    window.agentManager.lastQueryResult = {
        data:       dr.data    || [],
        columns:    dr.columns || (dr.data[0] ? Object.keys(dr.data[0]) : []),
        question:   dr.question || '',
        agent_name: dr.agent_name || '',
        insights,
    };
    window.currentAgentName = dr.agent_name || '';

    if      (type === 'dashboard')    generateDashboard();
    else if (type === 'report')       generateReport();
    else if (type === 'presentation') generatePresentation();
    else if (type === 'infographic')  generateInfographic();
}

// ── Conversation-based document creation ─────────────────────────────────────

function _getConversationContext() {
    const msgs = [];
    document.querySelectorAll('#hubChatMessages .hub-msg').forEach(el => {
        const role   = el.classList.contains('user') ? 'User' : 'Assistant';
        const bubble = el.querySelector('.hub-msg-bubble');
        if (!bubble) return;
        // Clone and strip tool traces / cards so only narrative text remains
        const clone = bubble.cloneNode(true);
        clone.querySelectorAll(
            '.hub-tool-trace, .hub-dataviz-card, .hub-doccreate-card, .hub-approval-card'
        ).forEach(n => n.remove());
        const clean = (clone.innerText || clone.textContent || '').trim();
        if (clean) msgs.push(`${role}: ${clean}`);
    });
    return msgs;
}

function appendDocCreateCard(msgEl, suggestedType) {
    const hasData = !!(window._hubLastDataResult?.data?.length);

    // Rows 1 & 2: visual; Row 3: file formats
    const visualTypes = [
        { type: 'presentation', label: 'Presentation', icon: 'fas fa-slideshare',   cls: 'btn-outline-primary'  },
        { type: 'infographic',  label: 'Infographic',  icon: 'fas fa-chart-pie',    cls: 'btn-outline-danger'   },
        { type: 'pdf',          label: 'PDF',          icon: 'fas fa-file-pdf',     cls: 'btn-outline-secondary'},
    ];
    const fileTypes = [
        { type: 'docx',     label: 'Word',     icon: 'fas fa-file-word',  cls: 'btn-outline-primary'   },
        { type: 'xlsx',     label: 'Excel',    icon: 'fas fa-file-excel', cls: 'btn-outline-success'   },
        { type: 'csv',      label: 'CSV',      icon: 'fas fa-file-csv',   cls: 'btn-outline-warning'   },
        { type: 'markdown', label: 'Markdown', icon: 'fas fa-file-code',  cls: 'btn-outline-secondary' },
        { type: 'txt',      label: 'Text',     icon: 'fas fa-file-alt',   cls: 'btn-outline-secondary' },
    ];
    if (hasData) {
        visualTypes.unshift({ type: 'dashboard', label: 'Dashboard', icon: 'fas fa-tachometer-alt', cls: 'btn-outline-warning' });
    }

    const makeBtns = types => types.map(t => {
        const hi = t.type === suggestedType
            ? ';font-weight:700;box-shadow:0 0 0 2px currentColor;'
            : '';
        return `<button class="btn btn-sm ${t.cls}" onclick="hubCreateDocument('${t.type}')" style="font-size:.75rem${hi}">
            <i class="${t.icon} me-1"></i>${t.label}
        </button>`;
    }).join('');

    const card = document.createElement('div');
    card.className = 'hub-doccreate-card mt-2';
    card.innerHTML = `
        <div style="background:#f5f3ff;border:1px solid #c4b5fd;border-radius:10px;padding:12px 14px;">
            <div class="d-flex align-items-center gap-2 mb-2">
                <i class="fas fa-file-export" style="color:#7c3aed;font-size:1rem;"></i>
                <strong style="font-size:.85rem;color:#4c1d95;">Create a document from this conversation</strong>
            </div>
            <div class="mb-1" style="font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:#a78bfa;font-weight:700;">Visual</div>
            <div class="d-flex gap-2 flex-wrap mb-2">${makeBtns(visualTypes)}</div>
            <div class="mb-1" style="font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:#a78bfa;font-weight:700;">Files</div>
            <div class="d-flex gap-2 flex-wrap">${makeBtns(fileTypes)}</div>
        </div>`;
    msgEl.appendChild(card);
    scrollToBottom();
}

// ── Conversation-based document creation (purple card + header dropdown) ──────
// This path is ALWAYS used by hubCreateDocument — it never calls the BI
// endpoints (those are only called from hubTriggerViz via the blue data card).

function hubCreateDocument(type) {
    const messages = _getConversationContext();
    if (!messages.length) {
        showToast('No conversation context yet — start chatting first.', 'error');
        return;
    }

    // Dashboard truly needs data rows
    if (type === 'dashboard') {
        const rows = window._hubLastDataResult?.data || [];
        if (!rows.length) {
            showToast('Dashboard requires data from a data agent. Try PDF instead.', 'info');
            return;
        }
        hubTriggerViz('dashboard');
        return;
    }

    // File documents (docx, xlsx, csv, markdown, txt) → backend generation + download card
    if (_FILE_DOC_TYPES.has(type)) {
        _hubGenerateFileDoc(type, messages);
        return;
    }

    // Visual documents (presentation, infographic, report) → preview panel
    _hubGenerateFromConversation(type, messages);
}

async function _hubGenerateFromConversation(type, messages) {
    // Resolve the user's specific instruction (most recent doc-request message)
    const instrMsg    = [...messages].reverse()
        .find(m => m.startsWith('User:') && _detectDocType(m.replace(/^User:\s*/, '')) !== null)
        || [...messages].reverse().find(m => m.startsWith('User:'))
        || '';
    const instruction = instrMsg.replace(/^User:\s*/, '');

    // "report" maps to infographic — it's already a beautiful visual document
    const backendType = (type === 'report') ? 'infographic' : type;
    const previewTitle = type === 'presentation' ? 'Presentation'
                       : type === 'report'       ? 'Visual Report'
                       :                           'Infographic Preview';
    const previewPath  = type === 'presentation' ? '/preview/presentation'
                       :                           '/preview/infographic';
    const loadingMsg   = type === 'presentation'
        ? { type: 'presentationLoading', step: 'Crafting slides from your conversation…' }
        : { type: 'infographicLoading' };

    const iframe = _hubShowPreview(previewTitle, previewPath);
    if (!iframe) { showToast('Preview panel not available.', 'error'); return; }

    iframe.onload = async function () {
        iframe.contentWindow.postMessage(loadingMsg, '*');
        try {
            const res  = await fetch('/api/agenthub/generate-document', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    type:       backendType,
                    messages,
                    instruction,
                    agent_name: _activeAgent?.name || '',
                }),
            });
            const data = await res.json();

            if (data.status !== 'success') {
                const errMsg = data.message || 'Generation failed';
                if (type === 'presentation') {
                    iframe.contentWindow.postMessage(
                        { type: 'presentationLoading', step: `Error: ${errMsg}` }, '*');
                } else {
                    iframe.contentWindow.postMessage(
                        { type: 'infographicError', error: errMsg }, '*');
                }
                return;
            }

            if (type === 'presentation') {
                iframe.contentWindow.postMessage({
                    type:        'presentationData',
                    slides:      data.slides,
                    client:      data.client      || {},
                    client_id:   data.client_id   || '',
                    pptx_base64: data.pptx_base64 || null,
                    cached:      false,
                }, '*');
            } else {
                iframe.contentWindow.postMessage(
                    { type: 'infographicData', infographic: data.infographic }, '*');
            }
        } catch (e) {
            if (type === 'presentation') {
                iframe.contentWindow.postMessage(
                    { type: 'presentationLoading', step: `Error: ${e.message}` }, '*');
            } else {
                iframe.contentWindow.postMessage(
                    { type: 'infographicError', error: e.message }, '*');
            }
        }
    };
}

function hubGeneratePDF(messages) {
    const agentName = _activeAgent?.name || 'AI Agent';
    const rows      = window._hubLastDataResult?.data || [];
    const date = new Date().toLocaleDateString('en-US', {
        year: 'numeric', month: 'long', day: 'numeric',
    });

    // Render each message as styled HTML
    const msgsHtml = messages.map(msg => {
        const isUser = msg.startsWith('User:');
        const role   = isUser ? 'You' : agentName;
        const body   = msg.replace(/^(User|Assistant):\s*/, '');
        // Simple markdown: bold, bullets
        const formatted = body
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/^[-•]\s+(.+)/gm, '<li>$1</li>')
            .replace(/\n/g, '<br>');
        return `
        <div class="msg ${isUser ? 'msg-u' : 'msg-a'}">
            <div class="msg-role">${role}</div>
            <div class="msg-body">${formatted}</div>
        </div>`;
    }).join('');

    // Optional data table
    let tableHtml = '';
    if (rows.length) {
        const cols = Object.keys(rows[0]).slice(0, 7);
        const headerCells = cols.map(c => `<th>${c}</th>`).join('');
        const bodyRows    = rows.slice(0, 30).map(row =>
            `<tr>${cols.map(c => `<td>${row[c] ?? ''}</td>`).join('')}</tr>`
        ).join('');
        tableHtml = `
        <div class="section-title">Data</div>
        <table><thead><tr>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table>`;
    }

    const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${agentName} — Export</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:#f2eeee;color:#150f12;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .cover{background:linear-gradient(135deg,#6B2038 0%,#4f46e5 100%);color:#fff;padding:48px 56px 40px}
  .cover h1{font-size:26px;font-weight:700;margin-bottom:6px}
  .cover .meta{font-size:13px;opacity:.75;display:flex;gap:24px;margin-top:10px}
  .meta-chip{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.15);border-radius:99px;padding:4px 14px;font-size:12px;font-weight:500}
  .body{max-width:820px;margin:0 auto;padding:36px 48px 60px}
  .msg{margin-bottom:18px;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07)}
  .msg-u{background:#F8E8EB;border-left:4px solid #A22E57}
  .msg-a{background:#ffffff;border-left:4px solid #6366f1}
  .msg-role{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;padding:10px 16px 0;color:#6e6265}
  .msg-u .msg-role{color:#A22E57}
  .msg-a .msg-role{color:#4f46e5}
  .msg-body{padding:8px 16px 14px;font-size:14px;line-height:1.75;color:#251e21}
  .msg-body strong{color:#150f12}
  .msg-body code{background:#f2eeee;padding:1px 5px;border-radius:4px;font-size:12px;font-family:monospace}
  .msg-body li{margin-left:18px;margin-bottom:3px;list-style:disc}
  .section-title{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#574c4f;margin:28px 0 10px;padding-bottom:6px;border-bottom:2px solid #e6dfe0}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:24px}
  th{background:#6B2038;color:#fff;padding:8px 12px;text-align:left;font-weight:600}
  td{padding:7px 12px;border-bottom:1px solid #e6dfe0;color:#3c3235}
  tr:nth-child(even) td{background:#f8f6f6}
  .footer{text-align:center;font-size:11px;color:#a89b9d;margin-top:40px;padding-top:16px;border-top:1px solid #e6dfe0}
  @media print{
    body{background:#fff}
    .cover{background:linear-gradient(135deg,#6B2038,#4f46e5)!important}
    .msg{page-break-inside:avoid}
    .footer{position:fixed;bottom:0;width:100%}
  }
</style>
</head>
<body>
<div class="cover">
  <h1>${agentName}</h1>
  <div class="meta">
    <span class="meta-chip">📄 Conversation Export</span>
    <span class="meta-chip">📅 ${date}</span>
    <span class="meta-chip">💬 ${messages.length} messages</span>
  </div>
</div>
<div class="body">
  ${msgsHtml}
  ${tableHtml}
  <div class="footer">Generated by ${agentName} · ${date}</div>
</div>
<script>
  window.onload = () => {
    document.title = '${agentName.replace(/'/g, "\\'")} — Export';
    setTimeout(() => window.print(), 600);
  };
</script>
</body>
</html>`;

    const blob = new Blob([html], { type: 'text/html' });
    const url  = URL.createObjectURL(blob);
    window.open(url, '_blank');
    showToast('Opening print dialog — save as PDF from there.', 'info');
}

// ── Token badge ──────────────────────────────────────────────────────────────
async function loadTokenBadge() {
    try {
        const res  = await fetch('/api/auth/token-usage');
        const data = await res.json();

        // New balance card
        const card = document.getElementById('tokenBalanceCard');
        const val  = document.getElementById('tokenBalanceVal');
        const sub  = document.getElementById('tokenBalanceSub');
        if (card && val) {
            card.style.display = 'block';
            if (data.unlimited) {
                val.textContent = 'Unlimited';
                val.style.color = '#22c55e';
                if (sub) sub.textContent = 'No daily limit';
            } else {
                const remaining = data.remaining || 0;
                const limit     = data.limit     || 1;
                const pct       = Math.round(remaining / limit * 100);
                // Show remaining tokens in K/M format
                val.textContent = remaining >= 1_000_000
                    ? `${parseFloat((remaining / 1_000_000).toFixed(3))}M`
                    : remaining >= 1_000
                    ? `${Math.round(remaining / 1_000)}K`
                    : remaining.toLocaleString();
                val.style.color = pct < 15 ? '#ef4444' : pct < 40 ? '#f59e0b' : '#251e21';
                if (sub) {
                    const used = data.used_today || 0;
                    sub.textContent = `${used >= 1000 ? Math.round(used/1000)+'K' : used} used today`;
                }
            }
        }

        // Legacy small badge (keep working if still present)
        const el = document.getElementById('hubTokenBadge');
        if (el) {
            if (data.unlimited) { el.textContent = 'Unlimited'; return; }
            el.textContent = `${(data.remaining||0).toLocaleString()} tokens left`;
        }
    } catch { /* ignore */ }
}

// ── Utilities ────────────────────────────────────────────────────────────────
function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _md2htmlTable(block) {
    const lines = block.trim().split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.length < 2) return null;
    const isSep = l => /^\|[\s\-:|]+\|/.test(l);
    let sepIdx = lines.findIndex(isSep);
    if (sepIdx < 1) return null;
    const parseCells = l => l.replace(/^\||\|$/g,'').split('|').map(c => c.trim());
    const headers  = parseCells(lines[0]);
    const dataRows = lines.slice(sepIdx + 1).filter(l => !isSep(l));
    if (!dataRows.length) return null;
    const thead = `<thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead>`;
    const tbody = `<tbody>${dataRows.map(r=>`<tr>${parseCells(r).map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody>`;
    return `<div class="hub-data-table-wrap" style="margin:6px 0;"><table class="hub-data-table">${thead}${tbody}</table></div>`;
}

function md2html(text) {
    if (!text) return '';
    // Extract markdown tables before HTML-escaping so tags aren't double-encoded
    const slots = {};
    let si = 0;
    text = text.replace(/((?:[ \t]*\|[^\n]+\|\s*\n?){2,})/g, match => {
        const html = _md2htmlTable(match);
        if (!html) return match;
        const key = `\x00TABLE${si++}\x00`;
        slots[key] = html;
        return '\n' + key + '\n';
    });
    let out = text
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
        .replace(/\*(.+?)\*/g,'<em>$1</em>')
        .replace(/`([^`]+)`/g,'<code>$1</code>')
        .replace(/\n/g,'<br>');
    for (const [key, html] of Object.entries(slots)) out = out.replace(key, html);
    return out;
}

function showToast(msg, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `alert alert-${type === 'error' ? 'danger' : 'info'} position-fixed`;
    toast.style.cssText = 'bottom:20px;right:20px;z-index:9999;min-width:220px;';
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}
