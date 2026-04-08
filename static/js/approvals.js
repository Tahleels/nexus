// approvals.js — Approvals page logic
'use strict';

let _allApprovals  = [];
let _activeFilter  = 'all';

// Pass dept/project context from URL so approvals are scoped to the current dept
const _aprParams  = new URLSearchParams(window.location.search);
const _aprDeptId  = _aprParams.get('dept_id');
const _aprProjId  = _aprParams.get('project_id');
const _aprApiUrl  = '/api/agenthub/approvals'
    + (_aprDeptId ? `?dept_id=${_aprDeptId}` : _aprProjId ? `?project_id=${_aprProjId}` : '');

document.addEventListener('DOMContentLoaded', () => {
    loadApprovals();
    // Auto-refresh pending every 30 s
    setInterval(() => { if (_activeFilter === 'pending' || _activeFilter === 'all') loadApprovals(false); }, 30000);
});

// ── Load + render ─────────────────────────────────────────────────────────────
async function loadApprovals(showSpinner = true) {
    if (showSpinner) {
        document.getElementById('aprList').innerHTML = `
            <div class="text-center py-5 text-muted">
                <div class="spinner-border mb-2"></div><p>Loading approvals…</p>
            </div>`;
    }
    try {
        const res = await fetch(_aprApiUrl);
        _allApprovals = await res.json();
        updateStats();
        renderApprovals(_activeFilter);
    } catch {
        document.getElementById('aprList').innerHTML =
            '<div class="text-center py-4 text-danger"><i class="fas fa-exclamation-circle me-1"></i>Failed to load approvals.</div>';
    }
}

function updateStats() {
    const pending  = _allApprovals.filter(a => a.status === 'pending').length;
    const approved = _allApprovals.filter(a => a.status === 'approved').length;
    const rejected = _allApprovals.filter(a => a.status === 'rejected').length;
    const total    = _allApprovals.length;

    setText('aStat_pending',  pending);
    setText('aStat_approved', approved);
    setText('aStat_rejected', rejected);
    setText('aStat_total',    total);
    setText('tabAll',     total);
    setText('tabPending', pending);
    setText('tabApproved', approved);
    setText('tabRejected', rejected);

    // Update sidebar badge
    const badge = document.getElementById('sidebarApprovalBadge');
    if (badge) {
        if (pending > 0) { badge.textContent = pending > 99 ? '99+' : pending; badge.style.display = ''; }
        else badge.style.display = 'none';
    }
}

function filterApprovals(status, btn) {
    _activeFilter = status;
    document.querySelectorAll('#aprTabs .nav-link').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    renderApprovals(status);
}

function renderApprovals(status) {
    const list = status === 'all'
        ? _allApprovals
        : _allApprovals.filter(a => a.status === status);

    const container = document.getElementById('aprList');
    if (!list.length) {
        container.innerHTML = `<div class="text-center py-5 text-muted">
            <i class="fas fa-check-double fa-2x mb-3 opacity-25"></i>
            <p>No ${status === 'all' ? '' : status} approvals found.</p></div>`;
        return;
    }
    container.innerHTML = list.map(a => renderCard(a)).join('');
}

function renderCard(a) {
    const ctx   = a.context || {};
    const isPending = a.status === 'pending';

    const assignedTo = a.assigned_to_username
        ? `<strong>${esc(a.assigned_to_username)}</strong>`
        : '<em class="text-muted">Any admin</em>';

    const sourceLine = a.request_type === 'workflow'
        ? `<i class="fas fa-project-diagram me-1"></i>${esc(a.workflow_name || '')} → node <code>${esc(a.node_id || '')}</code>`
        : `<i class="fas fa-robot me-1"></i>${esc(a.agent_name || '')}`;

    const resolutionHtml = !isPending ? `
        <div class="apr-resolution">
            <i class="fas fa-${a.status === 'approved' ? 'check-circle text-success' : 'times-circle text-danger'} me-1"></i>
            <strong>${a.status === 'approved' ? 'Approved' : 'Rejected'}</strong>
            by ${esc(a.approver_username || '—')}
            ${a.approver_note ? `<span class="text-muted ms-1">— "${esc(a.approver_note)}"</span>` : ''}
            <span class="text-muted ms-2" style="font-size:.75rem;">${fmtDate(a.resolved_at)}</span>
        </div>` : '';

    const actionHtml = isPending ? `
        <button class="btn btn-sm btn-success" onclick="openResolve('${a.approval_id}', 'approved')">
            <i class="fas fa-check me-1"></i>Approve
        </button>
        <button class="btn btn-sm btn-danger" onclick="openResolve('${a.approval_id}', 'rejected')">
            <i class="fas fa-times me-1"></i>Reject
        </button>
        <button class="btn btn-sm btn-outline-secondary" onclick="openCtxModal(${JSON.stringify(a).replace(/"/g,'&quot;')})">
            <i class="fas fa-info-circle me-1"></i>Context
        </button>` : `
        <button class="btn btn-sm btn-outline-secondary" onclick="openCtxModal(${JSON.stringify(a).replace(/"/g,'&quot;')})">
            <i class="fas fa-info-circle me-1"></i>Context
        </button>`;

    return `
    <div class="apr-card ${a.status}" id="aprCard-${a.approval_id}">
        <div class="apr-card-body">
            <div class="apr-card-header">
                <div>
                    <div class="apr-title">${esc(a.title)}</div>
                    <div class="apr-meta">
                        ${sourceLine}
                        &nbsp;·&nbsp; Requested by <strong>${esc(a.requested_by_username || '—')}</strong>
                        &nbsp;·&nbsp; Assigned to ${assignedTo}
                        &nbsp;·&nbsp; ${fmtDate(a.created_at)}
                    </div>
                </div>
                <div class="d-flex gap-2 align-items-center flex-shrink-0">
                    <span class="apr-type-badge ${a.request_type}">${a.request_type}</span>
                    <span class="apr-status-badge ${a.status}">${a.status}</span>
                </div>
            </div>
            ${ctx.context ? `<div class="text-muted" style="font-size:.82rem;margin-top:6px;">${esc(ctx.context.slice(0, 200))}${ctx.context.length > 200 ? '…' : ''}</div>` : ''}
            ${resolutionHtml}
            <div class="apr-action-bar">${actionHtml}</div>
        </div>
    </div>`;
}

// ── Resolve modal ─────────────────────────────────────────────────────────────
function openResolve(approvalId, decision) {
    document.getElementById('resolveApprovalId').value = approvalId;
    document.getElementById('resolveDecision').value   = decision;
    document.getElementById('resolveNote').value       = '';

    const isApprove = decision === 'approved';
    document.getElementById('resolveModalTitle').textContent =
        isApprove ? '✅ Approve Request' : '❌ Reject Request';
    const btn = document.getElementById('resolveConfirmBtn');
    btn.className = isApprove ? 'btn btn-success' : 'btn btn-danger';
    btn.textContent = isApprove ? 'Approve' : 'Reject';

    document.getElementById('resolveNoteRequired').style.display = isApprove ? 'none' : '';

    // Show context preview
    const appr = _allApprovals.find(a => a.approval_id === approvalId);
    const ctx  = appr?.context || {};
    document.getElementById('resolveCtxPreview').textContent =
        ctx.context || ctx.custom_context || '(no additional context provided)';

    new bootstrap.Modal(document.getElementById('resolveModal')).show();
}

async function confirmResolve() {
    const approvalId = document.getElementById('resolveApprovalId').value;
    const decision   = document.getElementById('resolveDecision').value;
    const note       = document.getElementById('resolveNote').value.trim();

    if (decision === 'rejected' && !note) {
        alert('Please add a note when rejecting an approval.');
        return;
    }

    const btn = document.getElementById('resolveConfirmBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

    try {
        const res  = await fetch(`/api/agenthub/approvals/${approvalId}/resolve`, {
            method:  'POST',
            headers: {'Content-Type': 'application/json'},
            body:    JSON.stringify({decision, note}),
        });
        const data = await res.json();
        if (res.ok) {
            bootstrap.Modal.getInstance(document.getElementById('resolveModal')).hide();
            await loadApprovals(false);
        } else {
            alert(data.error || 'Failed to resolve approval.');
        }
    } catch {
        alert('Network error.');
    } finally {
        btn.disabled = false;
    }
}

// ── Context modal ─────────────────────────────────────────────────────────────
function openCtxModal(appr) {
    const ctx = appr.context || {};
    const rows = [
        ['Status',       `<span class="apr-status-badge ${appr.status}">${appr.status}</span>`],
        ['Type',         `<span class="apr-type-badge ${appr.request_type}">${appr.request_type}</span>`],
        ['Title',        esc(appr.title)],
        ['Requested by', esc(appr.requested_by_username || '—')],
        ['Assigned to',  esc(appr.assigned_to_username || 'Any admin')],
        ['Created',      fmtDate(appr.created_at)],
    ];

    if (appr.request_type === 'agent') {
        rows.push(['Agent',           esc(appr.agent_name || '—')]);
        rows.push(['Conversation ID', `<code>${esc(appr.conversation_id || '—')}</code>`]);
    } else {
        rows.push(['Workflow',  esc(appr.workflow_name || '—')]);
        rows.push(['Run ID',    `<code>${esc(appr.run_id || '—')}</code>`]);
        rows.push(['Node',      `<code>${esc(appr.node_id || '—')}</code>`]);
    }

    const tableHtml = `<table class="table table-sm table-bordered mb-3">
        <tbody>${rows.map(([k, v]) => `<tr><td class="text-muted" style="width:35%;font-size:.8rem;">${k}</td><td style="font-size:.82rem;">${v}</td></tr>`).join('')}
        </tbody></table>`;

    const contextHtml = ctx.context || ctx.custom_context
        ? `<h6 class="fw-600 mt-3">Context</h6>
           <div style="background:#f8f6f6;border-radius:8px;padding:12px;font-size:.83rem;white-space:pre-wrap;border:1px solid #e6dfe0;">${esc(ctx.context || ctx.custom_context)}</div>`
        : '';

    const currentOutputHtml = ctx.current_output
        ? `<h6 class="fw-600 mt-3">Output at approval point</h6>
           <div style="background:#f8f6f6;border-radius:8px;padding:12px;font-size:.83rem;white-space:pre-wrap;max-height:200px;overflow-y:auto;border:1px solid #e6dfe0;">${esc(ctx.current_output)}</div>`
        : '';

    const resolutionHtml = appr.status !== 'pending' ? `
        <h6 class="fw-600 mt-3">Resolution</h6>
        <div class="d-flex align-items-center gap-2 p-2" style="background:#f8f6f6;border-radius:8px;border:1px solid #e6dfe0;">
            <i class="fas fa-${appr.status === 'approved' ? 'check-circle text-success' : 'times-circle text-danger'} fs-5"></i>
            <div>
                <strong>${appr.status === 'approved' ? 'Approved' : 'Rejected'}</strong>
                by ${esc(appr.approver_username || '—')} on ${fmtDate(appr.resolved_at)}
                ${appr.approver_note ? `<br><span class="text-muted" style="font-size:.82rem;">"${esc(appr.approver_note)}"</span>` : ''}
            </div>
        </div>` : '';

    document.getElementById('ctxModalBody').innerHTML =
        tableHtml + contextHtml + currentOutputHtml + resolutionHtml;

    new bootstrap.Modal(document.getElementById('ctxModal')).show();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function fmtDate(str) {
    if (!str) return '—';
    const d = new Date(str);
    return isNaN(d) ? str : d.toLocaleString('en-IN', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}
