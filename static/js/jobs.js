/* ============================================================
   jobs.js — Nexus AI · Jobs & Scheduler  (v3)

   New in v3:
   - Monitor execution detail has two tabs: Log and Query Output
   - Query Output renders exactly like the chat: table + SQL + insights + AI analysis
   - loadOutput() fetches /api/jobs/logs/<job_id>/<exec_id>/output on demand
   - formatInsightText() ported from agents.js for consistent markdown rendering
   ============================================================ */

'use strict';

// ── Shared helpers ────────────────────────────────────────────────────────────

const JobsUtil = {

    fmt(isoStr) {
        if (!isoStr) return '—';
        const d = new Date(isoStr);
        return isNaN(d) ? isoStr
            : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    },

    smartTime(isoStr) {
        if (!isoStr) return '—';
        const d = new Date(isoStr);
        if (isNaN(d)) return isoStr;
        const diff = Date.now() - d.getTime();
        if (diff < 0) return this.fmt(isoStr);          // future → real datetime
        const m = Math.round(diff / 60000);
        if (m < 1) return 'just now';
        if (m < 60) return `${m}m ago`;
        const h = Math.round(m / 60);
        if (h < 24) return `${h}h ago`;
        return `${Math.round(h / 24)}d ago`;
    },

    duration(ms) {
        if (!ms) return '—';
        if (ms < 1000) return `${ms}ms`;
        if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
        return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
    },

    statusBadge(status, isRunning) {
        if (isRunning)
            return '<span class="sbadge running"><i class="fas fa-spinner fa-spin"></i> Running</span>';
        return ({
            success: '<span class="sbadge success"><i class="fas fa-check"></i> Success</span>',
            failed: '<span class="sbadge failed"><i class="fas fa-times"></i> Failed</span>',
            disabled: '<span class="sbadge disabled"><i class="fas fa-pause"></i> Disabled</span>',
            scheduled: '<span class="sbadge scheduled"><i class="fas fa-clock"></i> Scheduled</span>',
        })[status] || '<span class="sbadge unknown">—</span>';
    },

    toolBadge(tool) {
        return ({
            dashboard: '<span class="tool-badge dashboard"><i class="fas fa-tachometer-alt"></i> Dashboard</span>',
            report: '<span class="tool-badge report"><i class="fas fa-file-alt"></i> Report</span>',
            infographic: '<span class="tool-badge infographic"><i class="fas fa-chart-pie"></i> Infographic</span>',
            ppt: '<span class="tool-badge ppt"><i class="fas fa-slideshare"></i> PPT</span>',
        })[tool] || `<span class="tool-badge">${esc(tool)}</span>`;
    },

    scheduleLabel(schedule) {
        if (!schedule || !schedule.type) return '—';
        const { type, value, unit, hour, minute, cron, day_of_week, start_from } = schedule;
        let label = '';
        if (type === 'interval') {
            label = `Every ${value} ${unit}`;
            if (start_from) {
                const sf = new Date(start_from);
                if (!isNaN(sf))
                    label += ` · from ${sf.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}`;
            }
        } else if (type === 'daily') {
            label = `Daily ${pad(hour)}:${pad(minute || 0)} UTC`;
        } else if (type === 'weekly') {
            label = `Weekly ${day_of_week} ${pad(hour)}:${pad(minute || 0)} UTC`;
        } else if (type === 'cron') {
            label = `Cron: ${cron}`;
        }
        return label || '—';
    },

    notify(msg, type = 'info') {
        const cls = {
            success: 'alert-success', error: 'alert-danger',
            warning: 'alert-warning', info: 'alert-info'
        }[type] || 'alert-info';
        const el = document.createElement('div');
        el.className = `alert ${cls} alert-dismissible fade show position-fixed`;
        el.style.cssText = 'top:20px;right:20px;z-index:9999;min-width:300px;max-width:420px';
        el.innerHTML = `${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 5000);
    },
};

function pad(n) { return String(n).padStart(2, '0'); }
function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}


// ── Chat-style output renderer (mirrors agents.js) ────────────────────────────

const OutputRenderer = {

    /**
     * Render a full query_result as the same HTML the chat UI produces.
     * qr = { sql_query, columns, data, row_count, insights, analysis, truncated }
     */
    render(qr, jobName, startedAt, toolsDone) {
        if (!qr) return '<div class="text-muted text-center py-4">No output data stored.</div>';

        let html = '';

        // ── Header ──
        html += `
      <div class="d-flex align-items-center justify-content-between flex-wrap gap-2 mb-3">
        <div>
          <span class="fw-bold" style="font-size:15px">📊 Query Results</span>
          <span class="text-muted ms-2" style="font-size:13px">${esc(jobName)}</span>
        </div>
        <div class="d-flex gap-2 align-items-center flex-wrap">
          <span class="badge bg-light text-dark">${esc(JobsUtil.fmt(startedAt))}</span>
          <span class="badge bg-light text-dark">${qr.row_count || 0} rows</span>
          ${(toolsDone || []).map(t => JobsUtil.toolBadge(t)).join('')}
        </div>
      </div>`;

        if (qr.truncated) {
            html += `<div class="truncated-note">
        <i class="fas fa-info-circle me-1"></i>
        Showing first 500 of ${qr.row_count} rows. Full data was sent via email / tool outputs.
      </div>`;
        }

        // ── Insights ──
        const insights = (qr.insights || []).filter(i => i.message);
        if (insights.length) {
            html += `<div class="output-section">
        <div class="output-section-title"><i class="fas fa-lightbulb"></i> Quick Insights</div>
        <div class="d-flex flex-column gap-2">
          ${insights.map(i => `
            <div class="insight-box">
              ${i.icon ? `<span class="me-1">${esc(i.icon)}</span>` : ''}
              ${this._formatInsightText(i.message)}
            </div>`).join('')}
        </div>
      </div>`;
        }

        // ── Data table ──
        const cols = qr.columns || [];
        const rows = qr.data || [];
        if (cols.length && rows.length) {
            html += `<div class="output-section">
        <div class="output-section-title">
          <i class="fas fa-table"></i> Results
          <span class="fw-normal text-muted">(${rows.length} of ${qr.row_count} rows)</span>
        </div>
        <div class="output-table-wrap">
          <table class="output-table">
            <thead>
              <tr>${cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr>
            </thead>
            <tbody>
              ${rows.map(row => `
                <tr>${cols.map(c => `<td>${this._fmtCell(row[c])}</td>`).join('')}</tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>`;
        } else if (!rows.length) {
            html += `<div class="output-section">
        <div class="alert alert-secondary py-2 mb-0">
          <i class="fas fa-search me-1"></i>
          Query returned no rows.
        </div>
      </div>`;
        }

        // ── SQL ──
        if (qr.sql_query) {
            html += `<div class="output-section">
        <div class="output-section-title"><i class="fas fa-code"></i> SQL Query</div>
        <div class="output-sql-block">${esc(qr.sql_query)}</div>
      </div>`;
        }

        return html;
    },

    _fmtCell(val) {
        if (val === null || val === undefined)
            return '<span class="null-val">NULL</span>';
        if (typeof val === 'boolean')
            return val ? '✓' : '✗';
        if (typeof val === 'number')
            return `<span class="num-val">${val.toLocaleString()}</span>`;
        const s = String(val);
        const display = s.length > 80 ? s.slice(0, 77) + '…' : s;
        return `<span title="${esc(s)}">${esc(display)}</span>`;
    },

    /** Minimal markdown: **bold**, bullet lines, section headers */
    _formatInsightText(text) {
        if (!text) return '';
        let normalized = text.replace(/\r/g, '').replace(/\n{3,}/g, '\n\n').trim();
        const lines = normalized.split('\n');
        let html = '', inList = false;
        for (let raw of lines) {
            let line = raw.trim();
            if (!line) { if (inList) { html += '</div>'; inList = false; } continue; }

            // Section header: **Title**
            if (/^\*\*(.+?)\*\*$/.test(line)) {
                if (inList) { html += '</div>'; inList = false; }
                const t = line.match(/^\*\*(.+?)\*\*$/)[1];
                html += `<div style="font-size:14px;font-weight:700;margin:14px 0 6px;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px">${esc(t)}</div>`;
                continue;
            }
            // Bullet
            if (line.startsWith('- ') || line.startsWith('• ')) {
                if (!inList) { html += '<div style="display:flex;flex-direction:column;gap:6px">'; inList = true; }
                line = line.replace(/^[-•]\s*/, '').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
                html += `<div style="display:flex;gap:8px"><span>•</span><span>${line}</span></div>`;
                continue;
            }
            if (inList) { html += '</div>'; inList = false; }
            line = line.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            html += `<div style="margin:4px 0">${line}</div>`;
        }
        if (inList) html += '</div>';
        return html;
    },
};


// ══════════════════════════════════════════════════════════════
// JOBS PAGE  (jobsApp)
// ══════════════════════════════════════════════════════════════

/** Read the current org context from the URL query string (?dept_id / ?project_id). */
function getOrgContext() {
    const p = new URLSearchParams(window.location.search);
    const d = p.get('dept_id');
    const r = p.get('project_id');
    return {
        dept_ids:    d ? [+d] : [],
        project_ids: r ? [+r] : [],
    };
}

const jobsApp = (() => {
    let _jobs = [], _agents = [], _clients = [], _modal = null;
    // PPT email state
    let _pptDetectedClientName = '';
    let _pptDetectedClientId   = '';

    function init() {
        _modal = new bootstrap.Modal(document.getElementById('jobModal'));
        loadJobs();
        loadAgents();
        loadClients();
        onScheduleTypeChange();
        ['jSchedType', 'jSchedValue', 'jSchedUnit', 'jSchedTime',
            'jSchedDay', 'jSchedCron', 'jSchedStartFrom'].forEach(id => {
                const el = document.getElementById(id);
                if (el) {
                    el.addEventListener('change', updateSchedulePreview);
                    el.addEventListener('input', updateSchedulePreview);
                }
            });
    }

    async function loadClients() {
        try {
            const res = await fetch('/api/clients');
            _clients = await res.json();
        } catch (_) { }
    }

    // ── PPT Email Intelligence helpers ────────────────────────────────────────

    /** Guess a client name from the free-text NLQ prompt. */
    function _guessClientFromPrompt(prompt) {
        if (!prompt || !_clients.length) return null;
        const q = prompt.toLowerCase();

        // 1. Explicit trigger: "for/about/regarding <client name>"
        const triggerMatch = prompt.match(/\b(?:for|about|regarding|client|on)\s+([A-Z][A-Za-z0-9 &'.-]{1,40})/);
        if (triggerMatch) {
            const candidate = triggerMatch[1].trim().toLowerCase();
            const found = _clients.find(c =>
                candidate.includes(c.name.toLowerCase()) ||
                c.name.toLowerCase().split(/\s+/).some(w => w.length > 3 && candidate.includes(w))
            );
            if (found) return found;
        }

        // 2. Direct name match anywhere in prompt
        const SKIP = new Set(['show','give','me','a','the','ppt','generate','create','build',
                              'report','dashboard','data','last','days','week','month']);
        for (const client of _clients) {
            const words = client.name.toLowerCase().replace(/[&,]/g, ' ').split(/\s+/).filter(w => w.length > 3 && !SKIP.has(w));
            if (words.some(w => q.includes(w))) return client;
            if (client.id && q.includes(client.id.toLowerCase())) return client;
        }
        return null;
    }

    function onPptToggle() {
        const checked = document.getElementById('tPpt').checked;
        document.getElementById('pptEmailSection').classList.toggle('d-none', !checked);
        if (checked) detectClientAndFetchSenders();
    }

    function onIncludeEmailToggle() {
        const include = document.getElementById('jIncludeEmail').checked;
        document.getElementById('pptEmailBody').style.opacity = include ? '' : '0.4';
        document.getElementById('pptEmailBody').style.pointerEvents = include ? '' : 'none';
    }

    async function detectClientAndFetchSenders() {
        const prompt = document.getElementById('jPrompt').value.trim();
        const clientBadge   = document.getElementById('pptDetectedClient');
        const sendersWrap   = document.getElementById('pptSendersWrap');

        _pptDetectedClientName = '';
        _pptDetectedClientId   = '';

        if (!prompt) {
            clientBadge.textContent = '—';
            sendersWrap.innerHTML   = '<span class="text-muted">Enter a prompt above, then click Re-detect.</span>';
            return;
        }

        const client = _guessClientFromPrompt(prompt);
        if (!client) {
            clientBadge.textContent   = 'Not detected';
            clientBadge.className     = 'badge bg-warning text-dark border';
            sendersWrap.innerHTML     = '<span class="text-muted">No matching client found. Email data will be skipped.</span>';
            return;
        }

        _pptDetectedClientName = client.name;
        _pptDetectedClientId   = client.id || '';
        clientBadge.textContent = client.name;
        clientBadge.className   = 'badge bg-primary text-white';

        sendersWrap.innerHTML = '<span class="text-muted"><i class="fas fa-spinner fa-spin me-1"></i>Fetching senders…</span>';

        try {
            const res  = await fetch('/api/bi-agents/email-senders', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_name: client.name, client_id: client.id || '' }),
            });
            const data = await res.json();

            if (data.status !== 'success' || !data.senders?.length) {
                sendersWrap.innerHTML = '<span class="text-warning"><i class="fas fa-exclamation-triangle me-1"></i>No email senders found for this client. Email data will be skipped.</span>';
                return;
            }

            sendersWrap.innerHTML = `
                <div class="mb-1 text-muted" style="font-size:12px">Select senders to include:</div>
                <div class="d-flex flex-wrap gap-2">
                ${data.senders.map((s, i) => `
                    <div class="form-check form-check-inline mb-0">
                        <input class="form-check-input ppt-sender-check" type="checkbox" value="${esc(s)}" id="pptSender_${i}" checked>
                        <label class="form-check-label" for="pptSender_${i}" style="font-size:12px">${esc(s)}</label>
                    </div>`).join('')}
                </div>`;
        } catch (e) {
            sendersWrap.innerHTML = `<span class="text-danger">Failed to fetch senders: ${esc(String(e))}</span>`;
        }
    }

    function _readPptEmailConfig() {
        const pptChecked = document.getElementById('tPpt')?.checked;
        if (!pptChecked) return {};
        return {
            include_email_data:    document.getElementById('jIncludeEmail')?.checked ?? true,
            detected_client_name:  _pptDetectedClientName,
            detected_client_id:    _pptDetectedClientId,
            email_senders: [...document.querySelectorAll('.ppt-sender-check:checked')].map(cb => cb.value),
        };
    }

    function _restorePptEmailSection(job) {
        const del = job.delivery || {};
        _pptDetectedClientName = del.detected_client_name || '';
        _pptDetectedClientId   = del.detected_client_id   || '';

        const pptChecked = (job.tools || []).includes('ppt');
        document.getElementById('pptEmailSection').classList.toggle('d-none', !pptChecked);
        if (!pptChecked) return;

        document.getElementById('jIncludeEmail').checked = del.include_email_data !== false;
        onIncludeEmailToggle();

        const clientBadge = document.getElementById('pptDetectedClient');
        if (_pptDetectedClientName) {
            clientBadge.textContent = _pptDetectedClientName;
            clientBadge.className   = 'badge bg-primary text-white';
        } else {
            clientBadge.textContent = '—';
            clientBadge.className   = 'badge bg-white text-dark border';
        }

        const savedSenders = del.email_senders || [];
        if (savedSenders.length) {
            const sendersWrap = document.getElementById('pptSendersWrap');
            sendersWrap.innerHTML = `
                <div class="mb-1 text-muted" style="font-size:12px">Select senders to include:</div>
                <div class="d-flex flex-wrap gap-2">
                ${savedSenders.map((s, i) => `
                    <div class="form-check form-check-inline mb-0">
                        <input class="form-check-input ppt-sender-check" type="checkbox" value="${esc(s)}" id="pptSender_${i}" checked>
                        <label class="form-check-label" for="pptSender_${i}" style="font-size:12px">${esc(s)}</label>
                    </div>`).join('')}
                </div>`;
        }
    }

    async function loadJobs() {
        try {
            const res = await fetch('/api/jobs');
            _jobs = await res.json();
            renderTable(_jobs);
            updateStats(_jobs);
        } catch (_) {
            document.getElementById('jobsTableBody').innerHTML =
                '<tr><td colspan="8" class="text-center text-danger py-4">Failed to load jobs</td></tr>';
        }
    }

    async function loadAgents() {
        try {
            // Pass org context so the dropdown respects the current department/project filter
            const ctx    = getOrgContext();
            const params = new URLSearchParams();
            if (ctx.dept_ids.length === 1)    params.set('dept_id',    ctx.dept_ids[0]);
            else if (ctx.project_ids.length === 1) params.set('project_id', ctx.project_ids[0]);
            const qs  = params.toString() ? '?' + params.toString() : '';
            const res = await fetch('/api/bi-agents' + qs);
            _agents = await res.json();
            const sel = document.getElementById('jAgent');
            if (!sel) return;
            sel.innerHTML = '<option value="">Select agent…</option>' +
                _agents.map(a => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join('');
        } catch (_) { }
    }

    function updateStats(jobs) {
        document.getElementById('statTotal').textContent = jobs.length;
        document.getElementById('statEnabled').textContent = jobs.filter(j => j.enabled).length;
        document.getElementById('statRunning').textContent = jobs.filter(j => j.is_running).length;
        document.getElementById('statFailed').textContent = jobs.filter(j => j.last_status === 'failed').length;
    }

    function renderTable(jobs) {
        const tbody = document.getElementById('jobsTableBody');

        // Apply org context filter (multi-select)
        const ctx  = (typeof getOrgContext === 'function') ? getOrgContext() : { dept_ids: [], project_ids: [] };
        const dIds = ctx.dept_ids || [];
        const pIds = ctx.project_ids || [];
        const hasFilter = dIds.length || pIds.length;
        let filtered = jobs || [];
        if (hasFilter) {
            filtered = filtered.filter(j => {
                const jDepts = j.dept_ids && j.dept_ids.length ? j.dept_ids
                             : (j.department_id != null ? [j.department_id] : []);
                const jProjs = j.project_ids && j.project_ids.length ? j.project_ids
                             : (j.project_id != null ? [j.project_id] : []);
                return dIds.some(id => jDepts.includes(id)) || pIds.some(id => jProjs.includes(id));
            });
        }

        if (!filtered.length) {
            tbody.innerHTML = `<tr><td colspan="9" class="text-center py-5 text-muted">
                ${hasFilter ? 'No jobs match the selected filter.' : 'No jobs yet. Click <strong>New Job</strong> to get started.'}
            </td></tr>`;
            return;
        }
        tbody.innerHTML = filtered.map(job => {
            const effectiveStatus = !job.enabled ? 'disabled'
                : job.is_running ? 'running' : (job.last_status || 'scheduled');
            const tools = (job.tools || []).map(JobsUtil.toolBadge).join(' ');
            const orgCell = job.dept_name
                ? `<span class="badge" style="background:${job.dept_color||'#6366f1'};font-size:.67rem">${esc(job.dept_name)}</span>`
                  + (job.project_name ? `<br><small class="text-muted">${esc(job.project_name)}</small>` : '')
                : (job.project_name ? `<small class="text-muted">${esc(job.project_name)}</small>` : '<span class="text-muted">—</span>');
            return `<tr data-id="${job.id}" data-name="${esc(job.name)}" data-status="${effectiveStatus}"
                        data-dept-id="${job.department_id||''}" data-project-id="${job.project_id||''}">
        <td>
          <div class="fw-semibold">${esc(job.name)}</div>
          <div class="text-muted" style="font-size:12px">${esc(job.description || '')}</div>
          ${job.created_by_username ? `<div style="font-size:11px;color:#9ca3af">by ${esc(job.created_by_username)}</div>` : ''}
        </td>
        <td><span class="badge bg-secondary">${esc(job.agent_name)}</span></td>
        <td style="font-size:12px">${JobsUtil.scheduleLabel(job.schedule)}</td>
        <td style="font-size:12px">${orgCell}</td>
        <td>${tools || '<span class="text-muted">—</span>'}</td>
        <td style="font-size:12px">${JobsUtil.smartTime(job.last_run)}</td>
        <td style="font-size:12px;font-weight:${job.next_run ? '500' : 'normal'}">${JobsUtil.smartTime(job.next_run)}</td>
        <td>${JobsUtil.statusBadge(effectiveStatus, job.is_running)}</td>
        <td>
          <div class="d-flex gap-1 flex-wrap">
            <button class="btn btn-sm btn-outline-primary"   title="Run now" onclick="jobsApp.runNow('${job.id}','${esc(job.name)}')"><i class="fas fa-play"></i></button>
            <button class="btn btn-sm btn-outline-secondary" title="Edit"    onclick="jobsApp.openEditModal('${job.id}')"><i class="fas fa-edit"></i></button>
            <button class="btn btn-sm ${job.enabled ? 'btn-outline-warning' : 'btn-outline-success'}" title="${job.enabled ? 'Disable' : 'Enable'}" onclick="jobsApp.toggleJob('${job.id}')">
              <i class="fas fa-${job.enabled ? 'pause' : 'play'}"></i>
            </button>
            <button class="btn btn-sm btn-outline-info"  title="Logs"   onclick="jobsApp.openLogs('${job.id}','${esc(job.name)}')"><i class="fas fa-list-alt"></i></button>
            <button class="btn btn-sm btn-outline-danger" title="Delete" onclick="jobsApp.deleteJob('${job.id}','${esc(job.name)}')"><i class="fas fa-trash"></i></button>
          </div>
        </td>
      </tr>`;
        }).join('');
    }

    function filterJobs() {
        const q  = (document.getElementById('jobSearch')?.value || '').toLowerCase();
        const st = (document.getElementById('jobStatusFilter')?.value || '');
        document.querySelectorAll('#jobsTableBody tr[data-id]').forEach(row => {
            const mQ  = !q  || row.dataset.name.toLowerCase().includes(q);
            const mSt = !st || row.dataset.status === st
                || (st === 'enabled' && row.dataset.status !== 'disabled');
            row.style.display = (mQ && mSt) ? '' : 'none';
        });
    }

    // Re-render when org context changes
    window.addEventListener('orgContextChange', () => {
        if (_jobs) renderTable(_jobs);
    });

    function _clearModal() {
        ['jName', 'jDesc', 'jPrompt', 'jEmails', 'jSubject', 'jMessage', 'jSchedCron', 'jSchedStartFrom']
            .forEach(id => { const e = document.getElementById(id); if (e) e.value = ''; });
        document.getElementById('jAgent').value = '';
        document.getElementById('jEnabled').checked = true;
        document.getElementById('jSchedType').value = 'interval';
        document.getElementById('jSchedValue').value = '1';
        document.getElementById('jSchedUnit').value = 'hours';
        document.getElementById('jSchedTime').value = '08:00';
        document.getElementById('jSchedDay').value = 'mon';
        document.querySelectorAll('.tool-check').forEach(cb => cb.checked = false);
        document.getElementById('editJobId').value = '';
        // Reset PPT email state
        _pptDetectedClientName = '';
        _pptDetectedClientId   = '';
        document.getElementById('pptEmailSection').classList.add('d-none');
        document.getElementById('jIncludeEmail').checked = true;
        document.getElementById('pptDetectedClient').textContent = '—';
        document.getElementById('pptDetectedClient').className = 'badge bg-white text-dark border';
        document.getElementById('pptSendersWrap').innerHTML = '<span class="text-muted">Enter a prompt above, then click Re-detect.</span>';
        onIncludeEmailToggle();
    }

    function openCreateModal() {
        document.getElementById('jobModalTitle').textContent = 'New Job';
        _clearModal();
        switchTab('basic', document.querySelector('[data-tab="basic"]'));
        onScheduleTypeChange(); updateSchedulePreview();
        _modal.show();
    }

    async function openEditModal(jobId) {
        const job = _jobs.find(j => j.id === jobId);
        if (!job) return;
        document.getElementById('jobModalTitle').textContent = `Edit: ${job.name}`;
        document.getElementById('editJobId').value = job.id;
        document.getElementById('jName').value = job.name;
        document.getElementById('jDesc').value = job.description || '';
        document.getElementById('jPrompt').value = job.nlq_prompt || '';
        document.getElementById('jAgent').value = job.agent_name || '';
        document.getElementById('jEnabled').checked = !!job.enabled;
        document.querySelectorAll('.tool-check').forEach(cb => cb.checked = (job.tools || []).includes(cb.value));
        const sch = job.schedule || {};
        document.getElementById('jSchedType').value = sch.type || 'interval';
        document.getElementById('jSchedValue').value = sch.value || '1';
        document.getElementById('jSchedUnit').value = sch.unit || 'hours';
        if (sch.start_from) {
            const sf = new Date(sch.start_from);
            if (!isNaN(sf)) {
                const pp = n => String(n).padStart(2, '0');
                document.getElementById('jSchedStartFrom').value =
                    `${sf.getFullYear()}-${pp(sf.getMonth() + 1)}-${pp(sf.getDate())}T${pp(sf.getHours())}:${pp(sf.getMinutes())}`;
            }
        }
        if (sch.hour !== undefined) {
            document.getElementById('jSchedTime').value = `${pad(sch.hour)}:${pad(sch.minute || 0)}`;
        }
        document.getElementById('jSchedDay').value = sch.day_of_week || 'mon';
        document.getElementById('jSchedCron').value = sch.cron || '';
        const del = job.delivery || {};
        document.getElementById('jEmails').value = del.emails || '';
        document.getElementById('jSubject').value = del.subject || '';
        document.getElementById('jMessage').value = del.message || '';
        _restorePptEmailSection(job);
        switchTab('basic', document.querySelector('[data-tab="basic"]'));
        onScheduleTypeChange(); updateSchedulePreview();
        _modal.show();
    }

    async function saveJob() {
        const jobId = document.getElementById('editJobId').value;
        const name = document.getElementById('jName').value.trim();
        const agent = document.getElementById('jAgent').value;
        const prompt = document.getElementById('jPrompt').value.trim();
        if (!name) return JobsUtil.notify('Job name is required', 'error');
        if (!agent) return JobsUtil.notify('Please select an agent', 'error');
        if (!prompt) return JobsUtil.notify('NLQ prompt is required', 'error');

        const _orgCtx = getOrgContext();
        const payload = {
            name, agent_name: agent, nlq_prompt: prompt,
            description: document.getElementById('jDesc').value.trim(),
            enabled: document.getElementById('jEnabled').checked,
            tools: [...document.querySelectorAll('.tool-check:checked')].map(cb => cb.value),
            schedule: _readSchedule(),
            delivery: {
                emails: document.getElementById('jEmails').value.trim(),
                subject: document.getElementById('jSubject').value.trim(),
                message: document.getElementById('jMessage').value.trim(),
                ..._readPptEmailConfig(),
            },
            // Include current org context so the backend can auto-assign dept/project
            dept_ids:    _orgCtx.dept_ids,
            project_ids: _orgCtx.project_ids,
        };
        try {
            const res = await fetch(jobId ? `/api/jobs/${jobId}` : '/api/jobs', {
                method: jobId ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (data.status === 'success') {
                JobsUtil.notify(`Job ${jobId ? 'updated' : 'created'}!`, 'success');
                _modal.hide(); loadJobs();
            } else { JobsUtil.notify(data.message || 'Failed', 'error'); }
        } catch (_) { JobsUtil.notify('Network error', 'error'); }
    }

    function _readSchedule() {
        const type = document.getElementById('jSchedType').value;
        const base = { type };
        if (type === 'interval') {
            base.value = parseInt(document.getElementById('jSchedValue').value) || 1;
            base.unit = document.getElementById('jSchedUnit').value;
            const sf = document.getElementById('jSchedStartFrom').value;
            if (sf) base.start_from = new Date(sf).toISOString();
        } else if (type === 'daily') {
            const [h, m] = (document.getElementById('jSchedTime').value || '08:00').split(':');
            base.hour = parseInt(h); base.minute = parseInt(m);
        } else if (type === 'weekly') {
            const [h, m] = (document.getElementById('jSchedTime').value || '08:00').split(':');
            base.hour = parseInt(h); base.minute = parseInt(m);
            base.day_of_week = document.getElementById('jSchedDay').value;
        } else if (type === 'cron') {
            base.cron = document.getElementById('jSchedCron').value.trim();
        }
        return base;
    }

    function onScheduleTypeChange() {
        const type = document.getElementById('jSchedType')?.value;
        if (!type) return;
        const show = (id, v) => { const e = document.getElementById(id); if (e) e.style.display = v ? '' : 'none'; };
        show('schedIntervalWrap', type === 'interval');
        show('schedStartFromWrap', type === 'interval');
        show('schedTimeWrap', type === 'daily' || type === 'weekly');
        show('schedDayWrap', type === 'weekly');
        show('schedCronWrap', type === 'cron');
        updateSchedulePreview();
    }

    function updateSchedulePreview() {
        const txt = document.getElementById('schedPreviewText');
        if (txt) txt.textContent = JobsUtil.scheduleLabel(_readSchedule()) || '—';
    }

    async function runNow(jobId, jobName) {
        if (!confirm(`Run "${jobName}" now?`)) return;
        try {
            const res = await fetch(`/api/jobs/${jobId}/run`, { method: 'POST' });
            const d = await res.json();
            JobsUtil.notify(d.status === 'success' ? `✅ "${jobName}" triggered` : d.message,
                d.status === 'success' ? 'success' : 'error');
            setTimeout(loadJobs, 1500);
        } catch (_) { JobsUtil.notify('Network error', 'error'); }
    }

    async function toggleJob(jobId) {
        try {
            const res = await fetch(`/api/jobs/${jobId}/toggle`, { method: 'POST' });
            const d = await res.json();
            JobsUtil.notify(d.message, d.status === 'success' ? 'success' : 'error');
            loadJobs();
        } catch (_) { JobsUtil.notify('Network error', 'error'); }
    }

    async function deleteJob(jobId, jobName) {
        if (!confirm(`Delete "${jobName}"? Cannot be undone.`)) return;
        try {
            const res = await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
            const d = await res.json();
            JobsUtil.notify(d.message, d.status === 'success' ? 'success' : 'error');
            loadJobs();
        } catch (_) { JobsUtil.notify('Network error', 'error'); }
    }

    async function openLogs(jobId, jobName) {
        document.getElementById('logsModalJobName').textContent = jobName;
        const body = document.getElementById('logsModalBody');
        body.innerHTML = '<div class="p-4 text-center text-muted">Loading…</div>';
        new bootstrap.Modal(document.getElementById('logsModal')).show();
        try {
            const res = await fetch(`/api/jobs/logs/${jobId}?limit=15`);
            const logs = await res.json();
            body.innerHTML = logs.length ? logs.map(_renderLogEntry).join('')
                : '<div class="p-4 text-center text-muted">No executions recorded yet.</div>';
        } catch (_) { body.innerHTML = '<div class="p-4 text-danger">Failed to load logs.</div>'; }
    }

    function _renderLogEntry(log) {
        const icon = log.status === 'success' ? '✅' : log.status === 'running' ? '⏳' : '❌';
        const lines = (log.log_lines || []).map(l => `<div>${esc(l)}</div>`).join('');
        return `<div class="log-entry">
      <div class="d-flex align-items-center justify-content-between flex-wrap gap-2">
        <div>
          <span class="fw-semibold">${icon} Execution #${log.exec_id}</span>
          <span class="text-muted ms-2" style="font-size:12px">${JobsUtil.fmt(log.started_at)}</span>
        </div>
        <div class="d-flex gap-2 flex-wrap">
          ${log.duration_ms ? `<span class="badge bg-light text-dark">${JobsUtil.duration(log.duration_ms)}</span>` : ''}
          <span class="badge bg-light text-dark">${log.row_count || 0} rows</span>
          ${(log.tools_completed || []).map(t => `<span class="badge bg-secondary">${t}</span>`).join('')}
        </div>
      </div>
      ${log.error ? `<div class="alert alert-danger py-1 px-2 mt-2 mb-0" style="font-size:12px"><i class="fas fa-exclamation-triangle me-1"></i>${esc(log.error)}</div>` : ''}
      ${lines ? `<div class="log-lines mt-2">${lines}</div>` : ''}
    </div>`;
    }

    function switchTab(tabId, linkEl) {
        ['basic', 'schedule', 'delivery'].forEach(t => {
            document.getElementById(`tab-${t}`).style.display = t === tabId ? '' : 'none';
        });
        document.querySelectorAll('#jobTabs .nav-link').forEach(a => a.classList.remove('active'));
        if (linkEl) linkEl.classList.add('active');
    }

    return {
        init, loadJobs, filterJobs, openCreateModal, openEditModal, saveJob,
        switchTab, onScheduleTypeChange, updateSchedulePreview,
        runNow, toggleJob, deleteJob, openLogs,
        onPptToggle, onIncludeEmailToggle, detectClientAndFetchSenders,
    };
})();


// ══════════════════════════════════════════════════════════════
// MONITOR PAGE  (monitor)
// ══════════════════════════════════════════════════════════════

const monitor = (() => {
    let _jobs = [];
    let _allLogs = [];
    let _timer = null;

    // State for the currently open detail modal
    let _activeJobId = null;
    let _activeExecId = null;
    let _activeEntry = null;   // the log entry (without query_result)

    async function init() {
        await refresh();
        _timer = setInterval(refresh, 10000);
    }

    async function refresh() {
        await Promise.all([_loadGrid(), _loadLogs()]);
    }

    async function _loadGrid() {
        try {
            const res = await fetch('/api/jobs');
            _jobs = await res.json();
            document.getElementById('mkTotalVal').textContent = _jobs.length;
            document.getElementById('mkRunningVal').textContent = _jobs.filter(j => j.is_running).length;
            _renderGrid(_jobs);
        } catch (_) { }
    }

    async function _loadLogs() {
        try {
            const res = await fetch('/api/jobs/logs?limit=50');
            _allLogs = await res.json();
            const now = Date.now(), h24 = 86400000;
            const rec = _allLogs.filter(l => now - new Date(l.started_at).getTime() < h24);
            document.getElementById('mkSuccessVal').textContent = rec.filter(l => l.status === 'success').length;
            document.getElementById('mkFailedVal').textContent = rec.filter(l => l.status === 'failed').length;
            filterLogs();
        } catch (_) { }
    }

    function _renderGrid(jobs) {
        const q = (document.getElementById('monSearch')?.value || '').toLowerCase();
        const tbody = document.getElementById('monitorGrid');
        const list = q ? jobs.filter(j => j.name.toLowerCase().includes(q)) : jobs;
        if (!list.length) {
            tbody.innerHTML = '<tr><td colspan="9" class="text-center py-5 text-muted">No jobs found.</td></tr>';
            return;
        }
        tbody.innerHTML = list.map(job => {
            const st = !job.enabled ? 'disabled' : job.is_running ? 'running' : (job.last_status || 'scheduled');
            const dur = (() => {
                const m = _allLogs.find(l => l.job_id === job.id && l.duration_ms);
                return m ? JobsUtil.duration(m.duration_ms) : '—';
            })();
            return `<tr>
        <td><div class="fw-semibold">${esc(job.name)}</div>
            <div class="text-muted" style="font-size:11px">${esc(job.description || '')}</div></td>
        <td><span class="badge bg-secondary">${esc(job.agent_name)}</span></td>
        <td style="font-size:12px">${JobsUtil.scheduleLabel(job.schedule)}</td>
        <td style="font-size:12px">${JobsUtil.smartTime(job.last_run)}</td>
        <td style="font-size:12px">${dur}</td>
        <td style="font-size:12px;font-weight:${job.next_run ? '500' : 'normal'}">${JobsUtil.smartTime(job.next_run)}</td>
        <td>${JobsUtil.statusBadge(st, job.is_running)}</td>
        <td><span class="badge bg-light text-dark">${job.run_count || 0}</span></td>
        <td>
          <button class="btn btn-sm btn-outline-info"
                  onclick="monitor.showJobLogs('${job.id}','${esc(job.name)}')">
            <i class="fas fa-list-alt"></i>
          </button>
        </td>
      </tr>`;
        }).join('');
    }

    function filterGrid() { _renderGrid(_jobs); }

    function filterLogs() {
        const st = document.getElementById('monStatusFilter')?.value || '';
        _renderExecList(st ? _allLogs.filter(l => l.status === st) : _allLogs);
    }

    function _renderExecList(logs) {
        const container = document.getElementById('recentExecList');
        if (!logs.length) {
            container.innerHTML = '<div class="p-4 text-center text-muted">No executions recorded.</div>';
            return;
        }
        container.innerHTML = logs.slice(0, 30).map(log => {
            const icCls = log.status === 'success' ? 'success' : log.status === 'running' ? 'running' : 'failed';
            const ic = log.status === 'success' ? 'fa-check' : log.status === 'running' ? 'fa-spinner fa-spin' : 'fa-times';
            const tools = (log.tools_completed || []).map(t => `<span class="exec-tag">${t}</span>`).join('');
            return `<div class="exec-row" style="cursor:pointer"
                   onclick="monitor.openDetail('${log.exec_id}','${log.job_id}')">
        <div class="exec-icon ${icCls}"><i class="fas ${ic}"></i></div>
        <div style="flex:1;min-width:0">
          <div class="fw-semibold">${esc(log.job_name || '—')}</div>
          <div class="exec-meta">
            ${JobsUtil.fmt(log.started_at)}
            ${log.duration_ms ? ` · ${JobsUtil.duration(log.duration_ms)}` : ''}
            · ${log.row_count || 0} rows
          </div>
          ${tools ? `<div class="exec-tags mt-1">${tools}</div>` : ''}
          ${log.error ? `<div class="text-danger mt-1" style="font-size:12px"><i class="fas fa-exclamation-triangle me-1"></i>${esc(log.error)}</div>` : ''}
        </div>
        <div>${JobsUtil.statusBadge(log.status, log.status === 'running')}</div>
      </div>`;
        }).join('');
    }

    // ── Detail modal ───────────────────────────────────────────────

    async function openDetail(execId, jobId) {
        _activeJobId = jobId;
        _activeExecId = execId;
        _activeEntry = _allLogs.find(l => l.exec_id === execId && l.job_id === jobId) || null;

        const entry = _activeEntry;
        const job = _jobs.find(j => j.id === jobId);

        document.getElementById('execModalTitle').textContent =
            `Execution — ${entry?.job_name || (job?.name || jobId)}`;
        document.getElementById('execModalSubtitle').textContent =
            entry ? `#${execId}  ·  ${JobsUtil.fmt(entry.started_at)}` : '';

        // Reset tabs
        switchExecTab('log');
        _renderLogTab(entry);

        // Reset output tab
        document.getElementById('execOutputBody').innerHTML = `
      <div class="text-center text-muted py-5">
        <i class="fas fa-table fa-2x mb-3 d-block" style="opacity:.3"></i>
        Click <strong>Load Output</strong> to fetch the query results.
      </div>`;
        document.getElementById('execOutputLoadWrap').style.display = '';
        document.getElementById('execOutputLoadBtn').disabled = false;
        document.getElementById('execOutputLoadBtn').textContent = 'Load Output';

        new bootstrap.Modal(document.getElementById('execDetailModal')).show();
    }

    function _renderLogTab(entry) {
        const body = document.getElementById('execDetailBody');
        if (!entry) { body.innerHTML = '<div class="text-muted">No log entry found.</div>'; return; }
        const lines = (entry.log_lines || []).join('\n');
        body.innerHTML = `
      <div class="mb-3 d-flex align-items-center justify-content-between flex-wrap gap-2">
        <div>
          ${JobsUtil.statusBadge(entry.status, entry.status === 'running')}
          <span class="ms-3 text-muted" style="font-size:12px">
            ${JobsUtil.fmt(entry.started_at)} → ${JobsUtil.fmt(entry.finished_at)}
          </span>
        </div>
        <div class="d-flex flex-wrap gap-2">
          <span class="badge bg-light text-dark">${entry.row_count || 0} rows</span>
          <span class="badge bg-light text-dark">${JobsUtil.duration(entry.duration_ms)}</span>
          ${(entry.tools_completed || []).map(t => `<span class="badge bg-secondary">${t}</span>`).join('')}
        </div>
      </div>
      ${entry.error ? `<div class="alert alert-danger py-2 mb-3"><i class="fas fa-exclamation-triangle me-1"></i>${esc(entry.error)}</div>` : ''}
      ${lines ? `<div class="log-block">${esc(lines)}</div>` : '<div class="text-muted">No log lines.</div>'}`;
    }

    async function loadOutput() {
        if (!_activeJobId || !_activeExecId) return;
        const btn = document.getElementById('execOutputLoadBtn');
        const body = document.getElementById('execOutputBody');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Loading…';
        body.innerHTML = '<div class="text-center text-muted py-4"><i class="fas fa-spinner fa-spin me-2"></i>Fetching output…</div>';

        try {
            const res = await fetch(`/api/jobs/logs/${_activeJobId}/${_activeExecId}/output`);
            const data = await res.json();

            if (data.status !== 'success') {
                body.innerHTML = `<div class="alert alert-warning">${esc(data.message || 'No output stored.')}</div>`;
                btn.disabled = false;
                btn.innerHTML = 'Retry';
                return;
            }

            const qr = data.query_result;
            body.innerHTML = OutputRenderer.render(qr, data.job_name, data.started_at, data.tools_completed);
            document.getElementById('execOutputLoadWrap').style.display = 'none';

        } catch (e) {
            body.innerHTML = `<div class="alert alert-danger">Failed to load output: ${esc(String(e))}</div>`;
            btn.disabled = false;
            btn.innerHTML = 'Retry';
        }
    }

    function switchExecTab(tab) {
        document.getElementById('execTabLog').style.display = tab === 'log' ? '' : 'none';
        document.getElementById('execTabOutput').style.display = tab === 'output' ? '' : 'none';
        document.getElementById('tabLogBtn').classList.toggle('active', tab === 'log');
        document.getElementById('tabOutputBtn').classList.toggle('active', tab === 'output');
    }

    async function showJobLogs(jobId, jobName) {
        // Load logs for this job, show first one in detail modal
        try {
            const res = await fetch(`/api/jobs/logs/${jobId}?limit=10`);
            const logs = await res.json();
            if (!logs.length) { JobsUtil.notify('No executions yet', 'info'); return; }
            // Merge into _allLogs cache so openDetail can find them
            logs.forEach(l => { if (!_allLogs.find(e => e.exec_id === l.exec_id)) _allLogs.push(l); });
            openDetail(logs[0].exec_id, jobId);
        } catch (_) { JobsUtil.notify('Failed to load logs', 'error'); }
    }

    return {
        init, refresh, filterGrid, filterLogs,
        openDetail, loadOutput, switchExecTab, showJobLogs
    };
})();


// ── Boot ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('jobsTableBody')) jobsApp.init();
    if (document.getElementById('monitorGrid')) monitor.init();
});