/* knowledge_base.js — Knowledge Base page logic */

const kb = (() => {
    let _allDocs   = [];
    let _uploadFile = null;
    let _versionFile = null;
    let _currentDocId = null;

    // Source filter state
    let _activeSource = 'all';   // 'all' | 'upload' | 'filesystem' | 'sharepoint'
    let _activeWatchId = null;   // numeric watch id or null (all)
    let _sourceWatches = { filesystem: [], sharepoint: [] };

    // ── Helpers ──────────────────────────────────────────────────────────────

    function fmt(date) {
        if (!date) return '—';
        const d = new Date(date);
        if (isNaN(d)) return date;
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    }

    function ftBadge(type) {
        const t = (type || 'other').toLowerCase();
        return `<span class="badge-filetype ft-${t}">${t}</span>`;
    }

    function sourceBadge(doc) {
        const type = doc.source_watch_type;
        if (!type) {
            return `<span class="src-badge src-upload"><i class="fas fa-upload"></i> Upload</span>`;
        }
        if (type === 'filesystem') {
            const w = _sourceWatches.filesystem.find(w => w.id === doc.source_watch_id);
            const lbl = w ? escHtml(w.label) : 'Directory';
            return `<span class="src-badge src-directory" title="${lbl}"><i class="fas fa-folder-open"></i> ${lbl.length > 18 ? lbl.slice(0,16)+'…' : lbl}</span>`;
        }
        if (type === 'sharepoint') {
            const w = _sourceWatches.sharepoint.find(w => w.id === doc.source_watch_id);
            const lbl = w ? escHtml(w.label) : 'SharePoint';
            return `<span class="src-badge src-sharepoint" title="${lbl}"><i class="fas fa-share-alt"></i> ${lbl.length > 18 ? lbl.slice(0,16)+'…' : lbl}</span>`;
        }
        return `<span class="src-badge src-upload">${escHtml(type)}</span>`;
    }

    function statusBadge(status) {
        const map = {
            ready:      'bg-success',
            processing: 'bg-warning text-dark',
            error:      'bg-danger',
        };
        const cls = map[status] || 'bg-secondary';
        return `<span class="badge ${cls}">${status || 'ready'}</span>`;
    }

    function isThisWeek(dateStr) {
        if (!dateStr) return false;
        const d   = new Date(dateStr);
        const now = new Date();
        const wk  = 7 * 24 * 60 * 60 * 1000;
        return (now - d) < wk;
    }

    function showToast(msg, type = 'success') {
        const el = document.createElement('div');
        el.className = `alert alert-${type} position-fixed top-0 end-0 m-3 shadow`;
        el.style.cssText = 'z-index:9999;min-width:260px;animation:fadeIn .2s';
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 3500);
    }

    // ── Load & render documents ───────────────────────────────────────────────

    async function loadDocs() {
        const tbody = document.getElementById('docsTableBody');
        tbody.innerHTML = '<tr><td colspan="9" class="text-center py-4 text-muted"><i class="fas fa-spinner fa-spin me-2"></i>Loading…</td></tr>';
        try {
            const res  = await fetch('/api/knowledge/documents');
            const data = await res.json();
            _allDocs = data.documents || [];
            updateStats(_allDocs);
            filterDocs();
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4 text-danger">${e.message}</td></tr>`;
        }
    }

    function renderDocs(docs) {
        const tbody = document.getElementById('docsTableBody');
        if (!docs.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="text-center py-4 text-muted">No documents yet — upload your first file above.</td></tr>';
            updateFooter(0, 0);
            return;
        }
        tbody.innerHTML = docs.map(d => `
            <tr style="cursor:pointer" onclick="kb.openDetail('${d.id}')">
                <td>
                    <div class="fw-medium" title="${d.filename || ''}">${(d.source_name || d.filename || '—').slice(0, 55)}</div>
                    <small class="text-muted">${(d.filename || '').slice(0, 40)}</small>
                    ${d.assigned_to ? `<br><small class="text-muted"><i class="fas fa-users me-1"></i>${d.assigned_to}</small>` : ''}
                </td>
                <td>${ftBadge(d.file_type)}</td>
                <td>${sourceBadge(d)}</td>
                <td><span class="badge bg-light text-dark">${d.chunk_count ?? '—'}</span></td>
                <td><small>${d.uploaded_by_name || '—'}</small></td>
                <td><small>v${d.version || 1}</small></td>
                <td><small>${fmt(d.created_at)}</small></td>
                <td>${statusBadge(d.status)}</td>
                <td onclick="event.stopPropagation()">
                    <div class="d-flex gap-1 justify-content-end">
                        <button class="btn btn-xs btn-outline-secondary" title="View details"
                                onclick="kb.openDetail('${d.id}')">
                            <i class="fas fa-eye"></i>
                        </button>
                    </div>
                </td>
            </tr>`).join('');
        updateFooter(docs.length, docs.reduce((s, d) => s + (d.chunk_count || 0), 0));
    }

    function updateFooter(count, chunks) {
        const el = document.getElementById('docCount');
        const cl = document.getElementById('docChunkTotal');
        if (el) el.textContent = `${count} document${count !== 1 ? 's' : ''}`;
        if (cl) cl.textContent = `${chunks.toLocaleString()} total chunks`;
    }

    function updateStats(docs) {
        const total   = docs.length;
        const chunks  = docs.reduce((s, d) => s + (d.chunk_count || 0), 0);
        const shared  = docs.filter(d => d.assigned_to).length;
        const recent  = docs.filter(d => isThisWeek(d.created_at)).length;

        document.getElementById('statTotal').textContent  = total;
        document.getElementById('statChunks').textContent = chunks > 999 ? (chunks/1000).toFixed(1) + 'k' : chunks;
        document.getElementById('statShared').textContent = shared;
        document.getElementById('statRecent').textContent = recent;

        // Update tab counts
        const countAll = docs.length;
        const countUpload = docs.filter(d => !d.source_watch_type).length;
        const countFs  = docs.filter(d => d.source_watch_type === 'filesystem').length;
        const countSp  = docs.filter(d => d.source_watch_type === 'sharepoint').length;
        const setCount = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        setCount('tabCountAll',        countAll);
        setCount('tabCountUpload',     countUpload);
        setCount('tabCountFilesystem', countFs);
        setCount('tabCountSharepoint', countSp);
    }

    function filterDocs() {
        const q    = (document.getElementById('docSearch')?.value || '').toLowerCase();
        const type = (document.getElementById('docTypeFilter')?.value || '').toLowerCase();
        const filtered = _allDocs.filter(d => {
            const name = (d.source_name || d.filename || '').toLowerCase();
            const ft   = (d.file_type || '').toLowerCase();
            if (q    && !name.includes(q)) return false;
            if (type && !ft.startsWith(type.replace('x', ''))) return false;
            // Source tab filter
            if (_activeSource === 'upload'     && d.source_watch_type) return false;
            if (_activeSource === 'filesystem' && d.source_watch_type !== 'filesystem') return false;
            if (_activeSource === 'sharepoint' && d.source_watch_type !== 'sharepoint') return false;
            // Watch sub-filter
            if (_activeWatchId !== null && d.source_watch_id !== _activeWatchId) return false;
            return true;
        });
        renderDocs(filtered);
    }

    // ── Source tabs ───────────────────────────────────────────────────────────

    async function loadSourceWatches() {
        try {
            const r    = await fetch('/api/knowledge/source-watches');
            const data = await r.json();
            if (data.success) {
                _sourceWatches = { filesystem: data.filesystem || [], sharepoint: data.sharepoint || [] };
            }
        } catch (_) {}
    }

    function setSourceTab(source) {
        _activeSource  = source;
        _activeWatchId = null;

        // Update tab active class
        document.querySelectorAll('.source-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.source === source);
        });

        // Show/hide watch sub-selector
        const row = document.getElementById('watchSelectorRow');
        const container = document.getElementById('watchPillsContainer');
        const needSub = (source === 'filesystem' || source === 'sharepoint');
        row.classList.toggle('visible', needSub);

        if (needSub) {
            const watches = source === 'filesystem' ? _sourceWatches.filesystem : _sourceWatches.sharepoint;
            if (watches.length) {
                container.innerHTML = watches.map(w => `
                    <span class="watch-pill ${w.enabled ? 'watch-pill-enabled' : ''}"
                          data-watch-id="${w.id}"
                          onclick="kb.setWatchFilter(${w.id})"
                          title="${escHtml(source === 'filesystem' ? (w.folder_path||'') : (w.sp_folder_path||w.site_url||''))}">
                        <span class="pill-dot"></span>
                        ${escHtml(w.label)}
                        <small style="opacity:.6">${w.files_ingested || 0}</small>
                    </span>`).join('');
            } else {
                container.innerHTML = '<small class="text-muted">No watch sources configured</small>';
            }
        }

        // Reset "All" pill to active
        document.querySelectorAll('#watchSelectorRow .watch-pill').forEach(p => {
            p.classList.toggle('active', !p.dataset.watchId);
        });

        filterDocs();
    }

    function setWatchFilter(watchId) {
        _activeWatchId = watchId;

        document.querySelectorAll('#watchSelectorRow .watch-pill').forEach(p => {
            const pid = p.dataset.watchId ? Number(p.dataset.watchId) : null;
            p.classList.toggle('active', pid === watchId);
        });

        filterDocs();
    }

    // ── Upload modal ──────────────────────────────────────────────────────────

    function openUploadModal() {
        _uploadFile = null;
        document.getElementById('uploadModalFileName').textContent = 'Click or drag file here';
        document.getElementById('uploadSourceName').value = '';
        document.getElementById('btnUpload').disabled = true;
        document.getElementById('modalUploadProgress').style.display = 'none';
        document.getElementById('modalUploadResult').innerHTML = '';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('uploadModal')).show();
    }

    function handleModalFileSelect(e) {
        const f = e.target.files[0];
        if (f) setUploadFile(f);
    }

    function onDropModal(e) {
        e.preventDefault();
        e.currentTarget.classList.remove('dragover');
        const f = e.dataTransfer.files[0];
        if (f) setUploadFile(f);
    }

    function setUploadFile(f) {
        _uploadFile = f;
        document.getElementById('uploadModalFileName').textContent = f.name;
        document.getElementById('btnUpload').disabled = false;
        if (!document.getElementById('uploadSourceName').value) {
            document.getElementById('uploadSourceName').value = f.name.replace(/\.[^.]+$/, '');
        }
    }

    async function doUpload() {
        if (!_uploadFile) return;
        const btn = document.getElementById('btnUpload');
        btn.disabled = true;
        const prog = document.getElementById('modalUploadProgress');
        const bar  = document.getElementById('modalUploadBar');
        const stat = document.getElementById('modalUploadStatus');
        const res  = document.getElementById('modalUploadResult');

        prog.style.display = 'block';
        res.innerHTML = '';
        bar.style.width = '30%';
        stat.textContent = 'Uploading…';

        const fd = new FormData();
        fd.append('file', _uploadFile);
        const sn = document.getElementById('uploadSourceName').value.trim();
        if (sn) fd.append('source_name', sn);

        const scope   = (document.getElementById('uploadScope') || {}).value || 'user';
        const scopeId = (document.getElementById('uploadScopeId') || {}).value || '';
        fd.append('scope', scope);
        if (scopeId) fd.append('scope_id', scopeId);

        bar.style.width = '60%';
        stat.textContent = 'Processing document…';

        try {
            const r    = await fetch('/api/knowledge/upload', { method: 'POST', body: fd });
            const data = await r.json();
            bar.style.width = '100%';
            bar.classList.remove('progress-bar-animated');

            if (data.success) {
                stat.textContent = 'Done!';
                res.innerHTML = `<div class="alert alert-success py-2 mb-0 mt-2">
                    <i class="fas fa-check-circle me-1"></i>
                    <strong>${data.filename}</strong> processed — ${data.chunk_count} chunks indexed.
                    ${data.extraction_warning ? `<br><small class="text-warning">${data.extraction_warning}</small>` : ''}
                </div>`;
                loadDocs();
                setTimeout(() => bootstrap.Modal.getInstance(document.getElementById('uploadModal'))?.hide(), 2000);
            } else {
                stat.textContent = 'Failed';
                bar.classList.add('bg-danger');
                res.innerHTML = `<div class="alert alert-danger py-2 mb-0 mt-2">${data.error}</div>`;
                btn.disabled = false;
            }
        } catch (e) {
            stat.textContent = 'Error';
            bar.classList.add('bg-danger');
            res.innerHTML = `<div class="alert alert-danger py-2 mb-0 mt-2">${e.message}</div>`;
            btn.disabled = false;
        }
    }

    // ── Drop zone (quick upload) ──────────────────────────────────────────────

    function onDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('dragover'); }
    function onDragLeave(e) { e.currentTarget.classList.remove('dragover'); }
    function onDrop(e) {
        e.preventDefault();
        e.currentTarget.classList.remove('dragover');
        const files = Array.from(e.dataTransfer.files);
        if (files.length) quickUpload(files[0]);
    }
    function handleFileSelect(e) { quickUpload(e.target.files[0]); }

    async function quickUpload(file) {
        if (!file) return;
        const wrap = document.getElementById('uploadProgressWrap');
        const bar  = document.getElementById('uploadProgressBar');
        const name = document.getElementById('uploadFileName');
        const stat = document.getElementById('uploadStatus');
        wrap.style.display = 'block';
        name.textContent = file.name;
        stat.textContent = 'Uploading…';
        bar.style.width  = '40%';

        const fd = new FormData();
        fd.append('file', file);
        fd.append('source_name', file.name.replace(/\.[^.]+$/, ''));
        stat.textContent = 'Processing…';
        bar.style.width  = '70%';

        try {
            const r    = await fetch('/api/knowledge/upload', { method: 'POST', body: fd });
            const data = await r.json();
            bar.style.width = '100%';
            bar.classList.remove('progress-bar-animated');
            if (data.success) {
                stat.textContent = `✓ ${data.chunk_count} chunks indexed`;
                bar.classList.add('bg-success');
                loadDocs();
            } else {
                stat.textContent = `Error: ${data.error}`;
                bar.classList.add('bg-danger');
            }
        } catch (e) {
            stat.textContent = `Error: ${e.message}`;
            bar.classList.add('bg-danger');
        }
        setTimeout(() => { wrap.style.display = 'none'; bar.className = 'progress-bar progress-bar-striped progress-bar-animated'; bar.style.width = '0%'; }, 4000);
    }

    // ── Document detail modal ─────────────────────────────────────────────────

    async function openDetail(docId) {
        _currentDocId = docId;
        const modal   = bootstrap.Modal.getOrCreateInstance(document.getElementById('docDetailModal'));
        document.getElementById('detailModalTitle').textContent  = 'Document Details';
        document.getElementById('detailModalBody').innerHTML      = '<div class="text-center py-4"><i class="fas fa-spinner fa-spin"></i></div>';
        document.getElementById('detailModalActions').innerHTML   = '';
        modal.show();

        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}`);
            const data = await r.json();
            if (!data.success) {
                document.getElementById('detailModalBody').innerHTML = `<div class="alert alert-danger">${data.error}</div>`;
                return;
            }
            const doc = data.document;
            document.getElementById('detailModalTitle').textContent = doc.source_name || doc.filename;

            const chunks = (doc.chunks || []).map((c, i) => `
                <div class="chunk-card">
                    <div class="chunk-label">Chunk ${c.chunk_index + 1}</div>
                    ${escHtml(c.text)}
                </div>`).join('');

            const assignments = (doc.assignments || []).length
                ? doc.assignments.map(a => `<span class="badge bg-secondary me-1">${a.user_name}</span>`).join('')
                : '<small class="text-muted">No assignments</small>';

            document.getElementById('detailModalBody').innerHTML = `
                <div class="row g-3 mb-3">
                    <div class="col-sm-6">
                        <small class="text-muted d-block">File</small>
                        <strong>${escHtml(doc.filename || '—')}</strong>
                    </div>
                    <div class="col-sm-3">
                        <small class="text-muted d-block">Type</small>
                        ${ftBadge(doc.file_type)}
                    </div>
                    <div class="col-sm-3">
                        <small class="text-muted d-block">Version</small>
                        v${doc.version || 1}
                    </div>
                    <div class="col-sm-6">
                        <small class="text-muted d-block">Uploaded By</small>
                        ${escHtml(doc.uploaded_by_name || '—')}
                    </div>
                    <div class="col-sm-3">
                        <small class="text-muted d-block">Date</small>
                        ${fmt(doc.created_at)}
                    </div>
                    <div class="col-sm-3">
                        <small class="text-muted d-block">Chunks</small>
                        ${doc.chunk_count ?? '—'}
                    </div>
                    <div class="col-12">
                        <small class="text-muted d-block mb-1">Assigned To</small>
                        ${assignments}
                    </div>
                </div>
                <hr>
                <h6 class="mb-2"><i class="fas fa-layer-group me-1"></i>Content Preview (first ${doc.chunks?.length || 0} chunks)</h6>
                ${chunks || '<small class="text-muted">No chunks available</small>'}
            `;

            const actions = document.getElementById('detailModalActions');
            if (doc.can_assign) {
                actions.innerHTML += `<button class="btn btn-sm btn-outline-secondary" onclick="kb.openAssign('${docId}')">
                    <i class="fas fa-user-plus me-1"></i>Assign</button>`;
            }
            if (doc.can_delete) {
                actions.innerHTML += `<button class="btn btn-sm btn-outline-warning" onclick="kb.openVersion('${docId}', '${escHtml(doc.source_name || doc.filename)}')">
                    <i class="fas fa-code-branch me-1"></i>New Version</button>`;
                actions.innerHTML += `<button class="btn btn-sm btn-outline-danger" onclick="kb.confirmDelete('${docId}', '${escHtml(doc.source_name || doc.filename)}')">
                    <i class="fas fa-trash me-1"></i>Delete</button>`;
            }
        } catch (e) {
            document.getElementById('detailModalBody').innerHTML = `<div class="alert alert-danger">${e.message}</div>`;
        }
    }

    // ── Delete ────────────────────────────────────────────────────────────────

    async function confirmDelete(docId, name) {
        if (!confirm(`Delete "${name}"?\n\nThis removes all chunks and cannot be undone.`)) return;
        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}`, { method: 'DELETE' });
            const data = await r.json();
            if (data.success) {
                bootstrap.Modal.getInstance(document.getElementById('docDetailModal'))?.hide();
                showToast('Document deleted');
                loadDocs();
            } else {
                showToast(data.error || 'Delete failed', 'danger');
            }
        } catch (e) {
            showToast(e.message, 'danger');
        }
    }

    // ── New version ───────────────────────────────────────────────────────────

    function openVersion(docId, currentName) {
        _currentDocId = docId;
        _versionFile  = null;
        document.getElementById('versionDocId').value = docId;
        document.getElementById('versionFileName').textContent = 'Click to select new file';
        document.getElementById('versionSourceName').value = currentName || '';
        document.getElementById('versionProgress').style.display = 'none';
        document.getElementById('versionResult').innerHTML = '';
        document.getElementById('btnVersion').disabled = true;
        bootstrap.Modal.getOrCreateInstance(document.getElementById('versionModal')).show();
    }

    function handleVersionFileSelect(e) {
        const f = e.target.files[0];
        if (f) { _versionFile = f; document.getElementById('versionFileName').textContent = f.name; document.getElementById('btnVersion').disabled = false; }
    }

    async function doVersion() {
        if (!_versionFile) return;
        const docId = document.getElementById('versionDocId').value;
        const btn   = document.getElementById('btnVersion');
        btn.disabled = true;
        document.getElementById('versionProgress').style.display = 'block';
        document.getElementById('versionBar').style.width = '50%';

        const fd = new FormData();
        fd.append('file', _versionFile);
        const sn = document.getElementById('versionSourceName').value.trim();
        if (sn) fd.append('source_name', sn);

        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}/version`, { method: 'POST', body: fd });
            const data = await r.json();
            document.getElementById('versionBar').style.width = '100%';
            if (data.success) {
                document.getElementById('versionResult').innerHTML = `<div class="alert alert-success py-2 mb-0 mt-2">
                    <i class="fas fa-check-circle me-1"></i>New version uploaded — ${data.chunk_count} chunks indexed.</div>`;
                loadDocs();
                setTimeout(() => bootstrap.Modal.getInstance(document.getElementById('versionModal'))?.hide(), 2000);
            } else {
                document.getElementById('versionResult').innerHTML = `<div class="alert alert-danger py-2 mb-0 mt-2">${data.error}</div>`;
                btn.disabled = false;
            }
        } catch (e) {
            document.getElementById('versionResult').innerHTML = `<div class="alert alert-danger py-2 mb-0 mt-2">${e.message}</div>`;
            btn.disabled = false;
        }
    }

    // ── Assignments ───────────────────────────────────────────────────────────

    async function openAssign(docId) {
        _currentDocId = docId;
        document.getElementById('assignDocId').value = docId;
        document.getElementById('currentAssignments').innerHTML = '<small class="text-muted">Loading…</small>';
        document.getElementById('userCheckboxList').innerHTML    = '<small class="text-muted">Loading…</small>';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('assignModal')).show();

        // Load current assignments
        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}/assignments`);
            const data = await r.json();
            const asgn = data.assignments || [];
            if (asgn.length) {
                document.getElementById('currentAssignments').innerHTML = asgn.map(a => `
                    <span class="badge bg-secondary me-1 mb-1" style="font-size:.8rem">
                        ${escHtml(a.user_name)}
                        <button type="button" class="btn-close btn-close-white ms-1" style="font-size:.55rem"
                                onclick="kb.removeAssignment('${docId}', ${a.user_id}, this)"></button>
                    </span>`).join('');
            } else {
                document.getElementById('currentAssignments').innerHTML = '<small class="text-muted">None</small>';
            }
        } catch (e) {
            document.getElementById('currentAssignments').innerHTML = `<small class="text-danger">${e.message}</small>`;
        }

        // Load all users
        try {
            const r    = await fetch('/api/knowledge/users');
            const data = await r.json();
            if (data.success) {
                document.getElementById('userCheckboxList').innerHTML = data.users.map(u => `
                    <div class="form-check">
                        <input class="form-check-input assign-cb" type="checkbox"
                               value="${u.id}" id="ucb_${u.id}">
                        <label class="form-check-label" for="ucb_${u.id}">
                            ${escHtml(u.username)}
                            <small class="text-muted">(${u.role})</small>
                        </label>
                    </div>`).join('') || '<small class="text-muted">No other users</small>';
            } else {
                document.getElementById('userCheckboxList').innerHTML = `<small class="text-muted">${data.error || 'Could not load users'}</small>`;
            }
        } catch (e) {
            document.getElementById('userCheckboxList').innerHTML = `<small class="text-danger">${e.message}</small>`;
        }
    }

    async function saveAssignments() {
        const docId  = document.getElementById('assignDocId').value;
        const checks = document.querySelectorAll('.assign-cb:checked');
        const ids    = Array.from(checks).map(c => c.value);
        if (!ids.length) { showToast('No users selected', 'warning'); return; }
        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}/assign`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_ids: ids }),
            });
            const data = await r.json();
            if (data.success) {
                showToast(`Assigned to ${data.assigned.length} user(s)`);
                loadDocs();
                openAssign(docId); // refresh the modal
            } else {
                showToast(data.error || 'Assign failed', 'danger');
            }
        } catch (e) { showToast(e.message, 'danger'); }
    }

    async function removeAssignment(docId, userId, btn) {
        btn.disabled = true;
        try {
            const r    = await fetch(`/api/knowledge/documents/${docId}/assign/${userId}`, { method: 'DELETE' });
            const data = await r.json();
            if (data.success) {
                btn.closest('.badge').remove();
                loadDocs();
            } else {
                showToast(data.error || 'Could not remove', 'danger');
                btn.disabled = false;
            }
        } catch (e) { showToast(e.message, 'danger'); btn.disabled = false; }
    }

    // ── Semantic search ───────────────────────────────────────────────────────

    function openSearch() {
        document.getElementById('searchQuery').value = '';
        document.getElementById('searchResults').innerHTML = '';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('searchModal')).show();
        setTimeout(() => document.getElementById('searchQuery').focus(), 300);
    }

    async function doSearch() {
        const q = document.getElementById('searchQuery').value.trim();
        if (!q) return;
        const el = document.getElementById('searchResults');
        el.innerHTML = '<div class="text-center py-3"><i class="fas fa-spinner fa-spin"></i> Searching…</div>';
        try {
            const r    = await fetch('/api/knowledge/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: q, n_results: 8 }),
            });
            const data = await r.json();
            if (!data.success) { el.innerHTML = `<div class="alert alert-danger">${data.error}</div>`; return; }

            const fallback = data.fallback;
            const conf     = Math.round((data.confidence || 0) * 100);
            const cached   = data.from_cache;
            const latency  = data.latency_ms || 0;

            // Meta bar
            const metaHtml = `
                <div class="d-flex align-items-center gap-3 mb-3 px-1" style="font-size:.75rem; color:#6e6265;">
                    <span><i class="fas fa-tachometer-alt me-1"></i>${latency}ms</span>
                    <span class="badge ${fallback ? 'bg-danger' : conf >= 50 ? 'bg-success' : 'bg-warning'}"
                          style="font-size:.65rem;">
                        ${fallback ? 'Insufficient Evidence' : `Confidence: ${conf}%`}
                    </span>
                    ${cached ? '<span class="badge bg-info" style="font-size:.65rem;">Cached</span>' : ''}
                    ${(data.citations||[]).length ? `<span>${data.citations.length} source(s) cited</span>` : ''}
                </div>`;

            if (!data.results.length) {
                el.innerHTML = metaHtml + '<div class="text-muted text-center py-3">No results found</div>';
                return;
            }

            const resultsHtml = data.results.map(r => {
                const rConf = Math.round((r.confidence || r.score || 0) * 100);
                const confColor = rConf >= 50 ? '#22c55e' : rConf >= 25 ? '#f59e0b' : '#ef4444';
                return `
                <div class="search-result-card">
                    <div class="d-flex justify-content-between align-items-start mb-1">
                        <small class="fw-medium">${escHtml(r.source_name || r.filename || r.document_id)}</small>
                        <small style="color:${confColor}; font-size:.75rem;">${rConf}%</small>
                    </div>
                    <div class="search-score-bar">
                        <div class="search-score-fill" style="width:${rConf}%; background:${confColor};"></div>
                    </div>
                    <p class="mb-0 mt-2 small text-muted">${escHtml((r.text || '').slice(0, 300))}${(r.text||'').length > 300 ? '…' : ''}</p>
                </div>`;
            }).join('');

            let citationsHtml = '';
            if ((data.citations||[]).length) {
                citationsHtml = `
                    <div class="mt-3 p-2 rounded" style="background:rgba(162,46,87,.08); border:1px solid rgba(162,46,87,.2);">
                        <div style="font-size:.75rem; color:#a89b9d; font-weight:600; margin-bottom:.4rem;">
                            SOURCES
                        </div>
                        ${data.citations.map((c,i) =>
                            `<div style="font-size:.75rem; color:#a89b9d;">${escHtml(c)}</div>`
                        ).join('')}
                    </div>`;
            }

            el.innerHTML = metaHtml + resultsHtml + citationsHtml;
        } catch (e) { el.innerHTML = `<div class="alert alert-danger">${e.message}</div>`; }
    }

    // ── Utils ─────────────────────────────────────────────────────────────────

    function escHtml(s) {
        return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // ── Init ──────────────────────────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', async () => {
        await loadSourceWatches();
        loadDocs();
    });

    function onScopeChange() {
        const scope = (document.getElementById('uploadScope') || {}).value || 'user';
        const wrap  = document.getElementById('uploadScopeIdWrap');
        const label = document.getElementById('uploadScopeIdLabel');
        if (!wrap) return;
        if (scope === 'department') {
            wrap.style.display = '';
            label.textContent  = 'Department ID';
        } else if (scope === 'project') {
            wrap.style.display = '';
            label.textContent  = 'Project ID';
        } else {
            wrap.style.display = 'none';
        }
    }

    return {
        loadDocs, filterDocs,
        openUploadModal, handleModalFileSelect, onDropModal, doUpload,
        onDragOver, onDragLeave, onDrop, handleFileSelect,
        openDetail, confirmDelete,
        openVersion, handleVersionFileSelect, doVersion,
        openAssign, saveAssignments, removeAssignment,
        openSearch, doSearch,
        onScopeChange,
        setSourceTab, setWatchFilter,
    };
})();
