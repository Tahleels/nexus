// agenthub_hub.js — Dev/Admin Hub management pages
'use strict';

// ── Department/Project context from URL params ────────────────────────────────
const _hubParams  = new URLSearchParams(window.location.search);
const _hubDeptId  = _hubParams.get('dept_id')    ? +_hubParams.get('dept_id')    : null;
const _hubProjId  = _hubParams.get('project_id') ? +_hubParams.get('project_id') : null;

/** Filter an array of resources by the current dept/project URL context. */
function _orgFilter(list) {
    if (_hubDeptId) return list.filter(r => (r.dept_ids     || []).includes(_hubDeptId));
    if (_hubProjId) return list.filter(r => (r.project_ids  || []).includes(_hubProjId));
    return list;
}

/** Return the org payload to append to create/update bodies. */
function _orgPayload() {
    return {
        dept_ids:    _hubDeptId  ? [_hubDeptId]  : [],
        project_ids: _hubProjId  ? [_hubProjId]  : [],
    };
}
// ─────────────────────────────────────────────────────────────────────────────

let hubPage = '';
let _hubAgents       = [];
let _hubWorkflows    = [];
let _hubTools        = [];
let _hubUsers        = [];
let _hubConnections  = [];
let _hubKnowledgeDocs = [];   // old hub_knowledge_bases (kept for legacy)
let _hubUserDocs      = [];   // new app_documents (user-visible only)
let _hubBiAgents      = [];
let _hubConnectors    = [];   // filesystem + sharepoint watch configs
let _currentTestToolId = null;

// ── Per-tool configuration definitions ────────────────────────────────────────
const TOOL_CONFIGS = {
    query_database: {
        label:       'Database Connection',
        field:       'connection_name',
        type:        'select',
        placeholder: 'Use first available',
        options:     () => _hubConnections.map(c => ({ value: c.name, label: c.name })),
    },
    create_text_file: {
        label:       'Save Directory (optional)',
        field:       'save_dir',
        type:        'text',
        placeholder: 'e.g. C:\\Reports\\  (blank = agent store)',
    },
    load_text_file: {
        label:       'Search Directories (comma-separated)',
        field:       'search_dirs',
        type:        'text',
        placeholder: 'e.g. C:\\Data\\, D:\\Docs\\  (blank = agent store only)',
    },
    search_documents: {
        label:       'Limit to Documents',
        field:       'document_ids',
        type:        'multiselect',
        placeholder: 'All documents',
        options:     () => _hubKnowledgeDocs.map(d => ({ value: d.id, label: d.name })),
    },
    search_knowledge: {
        label:       'Restrict to Documents',
        field:       'document_ids',
        type:        'multiselect',
        placeholder: 'All my documents',
        options:     () => _hubUserDocs.map(d => ({ value: d.id, label: d.source_name || d.filename || d.id })),
    },
    communicate_with_agent: {
        label:       'Allowed Agents',
        field:       'agent_ids',
        type:        'multiselect',
        placeholder: 'All agents',
        options:     () => _hubAgents.map(a => ({ value: a.id, label: a.name })),
    },
    communicate_with_data_agent: {
        label:       'Allowed BI Agents',
        field:       'agent_names',
        type:        'multiselect',
        placeholder: 'All BI agents',
        options:     () => _hubBiAgents.map(a => ({ value: a.name, label: a.name })),
    },
    folder_report: {
        label:       'Folder Paths (comma-separated)',
        field:       'paths',
        type:        'text',
        placeholder: 'e.g. C:\\Reports\\, D:\\Data\\  (blank = agent store)',
    },
    search_connector_knowledge: {
        label:       'Connectors',
        field:       'connector_keys',
        type:        'multiselect',
        placeholder: 'All connectors',
        options:     () => _hubConnectors.map(c => ({ value: c.key, label: c.label })),
    },
    list_connector_documents: {
        label:       'Connectors',
        field:       'connector_keys',
        type:        'multiselect',
        placeholder: 'All connectors',
        options:     () => _hubConnectors.map(c => ({ value: c.key, label: c.label })),
    },
    list_knowledge_documents: {
        label:       'Restrict to Documents',
        field:       'document_ids',
        type:        'multiselect',
        placeholder: 'All my documents',
        options:     () => _hubUserDocs.map(d => ({ value: d.id, label: d.source_name || d.filename || d.id })),
    },
    analyze_csv_files: {
        label:       'CSV/Excel Directories (comma-separated)',
        field:       'directories',
        type:        'text',
        placeholder: 'e.g. C:\\GA_Reports\\, D:\\Sales\\  (blank = agent store only)',
    },
};

function initHubPage() {
    switch (hubPage) {
        case 'dashboard':     initDashboard();    break;
        case 'agents':        initAgents();       break;
        case 'workflows':     initWorkflows();    break;
        case 'tools':         initTools();        break;
        case 'knowledge':     initKnowledge();    break;
        case 'jobs':          initJobs();         break;
        case 'analytics':     initAnalytics();    break;
        case 'assignments':   initAssignments();  break;
        case 'custom-tools':  initCustomTools();  break;
        case 'tool-jobs':     initToolJobs();     break;
    }
}

// ══════════════════════════════════════════════════════════════
// DASHBOARD
// ══════════════════════════════════════════════════════════════
async function initDashboard() {
    try {
        const res  = await fetch('/api/agenthub/dashboard/stats');
        const data = await res.json();
        const s    = data.stats || {};

        setText('hStat_agents',  fmtNum(s.agents));
        setText('hStat_convos',  fmtNum(s.conversations));
        setText('hStat_wf',      fmtNum(s.workflows));
        setText('hStat_tokens',  fmtNum(s.total_tokens));
        setText('hStat_tools',   fmtNum(s.tools));
        setText('hStat_jobs',    fmtNum(s.jobs));
        setText('hStat_runs',    fmtNum(s.total_runs));
        setText('hStat_status',  data.system_status?.status || 'online');

        // Recent conversations
        const convEl = document.getElementById('hubRecentConvos');
        convEl.innerHTML = (data.recent_conversations || []).map(c => `
            <div class="convo-row">
                <span>${esc(c.title||'Untitled')}</span>
                <span class="text-muted" style="font-size:.75rem;">${c.message_count||0} msgs · ${fmtDate(c.updated_at)}</span>
            </div>
        `).join('') || '<p class="text-muted mb-0" style="font-size:.85rem;">No conversations yet</p>';

        // Top agents by runs
        const maxRuns = Math.max(...(data.agents||[]).map(a => a.total_runs||0), 1);
        document.getElementById('hubTopAgents').innerHTML = (data.agents||[]).map(a => `
            <div style="margin-bottom:10px;">
                <div class="d-flex justify-content-between" style="font-size:.82rem;margin-bottom:3px;">
                    <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${a.avatar_color||'#6366f1'};margin-right:6px;"></span>${esc(a.name)}</span>
                    <span class="text-muted">${a.total_runs||0} runs</span>
                </div>
                <div class="agent-run-bar"><div class="agent-run-fill" style="width:${Math.round((a.total_runs||0)/maxRuns*100)}%;background:${a.avatar_color||'#6366f1'};"></div></div>
            </div>
        `).join('') || '<p class="text-muted mb-0" style="font-size:.85rem;">No agents yet</p>';

        // Recent workflow runs
        document.getElementById('hubRecentWfRuns').innerHTML = (data.recent_workflow_runs||[]).map(r => `
            <div class="convo-row">
                <span class="text-truncate" style="max-width:200px;">${esc(r.workflow_id?.slice(-8)||'—')}</span>
                <span class="badge bg-${r.status==='completed'?'success':'secondary'}">${r.status}</span>
                <span class="text-muted" style="font-size:.75rem;">${fmtDate(r.started_at)}</span>
            </div>
        `).join('') || '<p class="text-muted mb-0" style="font-size:.85rem;">No workflow runs yet</p>';

        // Top tools
        document.getElementById('hubTopTools').innerHTML = (data.top_tools||[]).map(t => `
            <div class="convo-row">
                <span>${esc(t.display_name||t.name)}</span>
                <span class="text-muted">${t.total_calls||0} calls</span>
            </div>
        `).join('') || '<p class="text-muted mb-0" style="font-size:.85rem;">No tool usage yet</p>';

    } catch (e) {
        console.error('Dashboard load failed', e);
    }
}

// ══════════════════════════════════════════════════════════════
// AGENTS MANAGEMENT
// ══════════════════════════════════════════════════════════════
async function initAgents() {
    await Promise.all([
        loadAllAgents(),
        loadToolsForCheckboxes(),
        loadConnectionsForTools(),
        loadKnowledgeDocsForTools(),
        loadUserDocsForTools(),
        loadBiAgentsForTools(),
        loadConnectorsForTools(),
    ]);
}

async function loadConnectionsForTools() {
    try {
        const res    = await fetch('/api/connections');
        _hubConnections = await res.json();
    } catch { _hubConnections = []; }
}

async function loadKnowledgeDocsForTools() {
    try {
        const res     = await fetch('/api/agenthub/knowledge');
        _hubKnowledgeDocs = await res.json();
    } catch { _hubKnowledgeDocs = []; }
}

async function loadUserDocsForTools() {
    try {
        const res  = await fetch('/api/knowledge/documents');
        const data = await res.json();
        _hubUserDocs = data.documents || [];
    } catch { _hubUserDocs = []; }
}

async function loadAllAgents() {
    try {
        const res = await fetch('/api/agenthub/agents/all');
        _hubAgents = await res.json();
        renderHubAgents(_hubAgents);
    } catch {
        document.getElementById('hubAgentsList').innerHTML =
            '<div class="text-danger text-center py-4">Failed to load agents</div>';
    }
}

function renderHubAgents(agents) {
    const el = document.getElementById('hubAgentsList');
    if (!agents.length) {
        el.innerHTML = '<div class="text-center py-5 text-muted"><i class="fas fa-robot fa-2x mb-2"></i><p>No agents yet. Create your first agent.</p></div>';
        return;
    }
    el.innerHTML = agents.map(a => {
        const deptBadges = (a.dept_ids||[]).map((did, i) => {
            const name  = (a.dept_names||[])[i]  || '';
            const color = (a.dept_colors||[])[i] || '#6366f1';
            return name ? `<span class="badge ms-1" style="background:${color};font-size:.67rem;">${esc(name)}</span>` : '';
        }).join('');
        const projBadges = (a.project_ids||[]).map((pid, i) => {
            const name = (a.project_names||[])[i] || '';
            return name ? `<span class="badge bg-secondary ms-1" style="font-size:.67rem;">${esc(name)}</span>` : '';
        }).join('');
        const orgBadge = deptBadges + projBadges;
        const deptDataIds = (a.dept_ids||[]).join(',');
        const projDataIds = (a.project_ids||[]).join(',');
        return `
        <div class="agent-card-hub" data-dept-ids="${deptDataIds}" data-project-ids="${projDataIds}">
            <div class="avatar" style="background:${a.avatar_color||'#6366f1'}">${(a.name||'?')[0].toUpperCase()}</div>
            <div class="info flex-grow-1">
                <div class="d-flex align-items-center gap-2 flex-wrap">
                    <span class="name">${esc(a.name)}</span>
                    <span class="status-badge ${a.status==='active'?'active':'inactive'}">${a.status||'active'}</span>
                    <span class="badge bg-light text-dark border" style="font-size:.7rem;">${esc(a.model||'gpt-4o')}</span>
                    ${orgBadge}
                </div>
                <div class="desc">${esc(a.description||'')}</div>
                <div class="meta">
                    <span><i class="fas fa-play-circle me-1"></i>${a.total_runs||0} runs</span>
                    <span><i class="fas fa-coins me-1"></i>${fmtNum(a.total_tokens||0)} tokens</span>
                    <span><i class="fas fa-calendar me-1"></i>${fmtDate(a.created_at)}</span>
                </div>
                <div class="mt-2">${(a.tools||[]).map(t => `<span class="tool-chip">${esc(typeof t === 'string' ? t : (t.name || ''))}</span>`).join('')}</div>
            </div>
            <div class="d-flex flex-column gap-2 align-items-end">
                <button class="btn btn-sm btn-outline-primary" onclick="editAgent(${JSON.stringify(a).replace(/"/g,'&quot;')})">
                    <i class="fas fa-edit"></i>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="deleteAgent('${a.id}','${esc(a.name)}')">
                    <i class="fas fa-trash"></i>
                </button>
                <a class="btn btn-sm btn-outline-success" href="/agents-hub?agent=${a.id}" title="Chat with this agent">
                    <i class="fas fa-comments"></i>
                </a>
            </div>
        </div>`;
    }).join('');
}

function filterAgents() {
    const q      = document.getElementById('agentSearch')?.value.toLowerCase() || '';
    const status = document.getElementById('agentStatusFilter')?.value || '';
    // URL param context takes priority when on a dept/project page
    const filtered = _orgFilter(_hubAgents).filter(a => {
        if (!(a.name||'').toLowerCase().includes(q)) return false;
        if (status && a.status !== status) return false;
        return true;
    });
    renderHubAgents(filtered);
}

// Re-filter when org context changes
window.addEventListener('orgContextChange', () => {
    if (hubPage === 'agents') filterAgents();
});

async function loadToolsForCheckboxes() {
    try {
        const res  = await fetch('/api/agenthub/tools');
        _hubTools  = await res.json();
    } catch { _hubTools = []; }
}

async function loadBiAgentsForTools() {
    try {
        const res    = await fetch('/api/bi-agents');
        _hubBiAgents = await res.json();
    } catch { _hubBiAgents = []; }
}

async function loadConnectorsForTools() {
    try {
        const [fsRes, spRes] = await Promise.all([
            fetch('/api/knowledge/watch-dirs'),
            fetch('/api/knowledge/sharepoint-watches'),
        ]);
        const fsDirs = fsRes.ok ? (await fsRes.json()).watch_dirs || [] : [];
        const spWatches = spRes.ok ? (await spRes.json()).watches || [] : [];
        _hubConnectors = [
            ...fsDirs.map(d => ({ key: `filesystem:${d.id}`, label: `Directory: ${d.label || d.folder_path}` })),
            ...spWatches.map(w => ({ key: `sharepoint:${w.id}`, label: `SharePoint: ${w.label || w.sp_folder_path}` })),
        ];
    } catch { _hubConnectors = []; }
}

// The model <select> tags each <option> with data-provider — this reads the
// selected option's provider into the hidden agentProvider field so saveAgent()
// sends both without a separate, redundant provider dropdown.
function syncAgentProviderFromModel() {
    const modelEl = document.getElementById('agentModel');
    const opt     = modelEl.selectedOptions[0];
    document.getElementById('agentProvider').value = (opt && opt.dataset.provider) || 'openai';
}

function showCreateAgentModal() {
    document.getElementById('agentModalTitle').textContent = 'Create Agent';
    document.getElementById('editAgentId').value           = '';
    document.getElementById('agentName').value             = '';
    document.getElementById('agentDesc').value             = '';
    document.getElementById('agentObjective').value        = '';
    document.getElementById('agentSystemPrompt').value     = '';
    document.getElementById('agentModel').value            = 'gpt-4o';
    syncAgentProviderFromModel();
    document.getElementById('agentTemp').value             = '0.7';
    document.getElementById('tempVal').textContent         = '0.7';
    document.getElementById('agentColor').value            = '#6366f1';
    document.getElementById('agentEnvVarRows').innerHTML   = '';
    renderToolCheckboxes([]);
    new bootstrap.Modal(document.getElementById('agentModal')).show();
}

function editAgent(a) {
    document.getElementById('agentModalTitle').textContent  = 'Edit Agent';
    document.getElementById('editAgentId').value            = a.id;
    document.getElementById('agentName').value              = a.name || '';
    document.getElementById('agentDesc').value              = a.description || '';
    document.getElementById('agentObjective').value         = a.objective || '';
    document.getElementById('agentSystemPrompt').value      = a.system_prompt || '';
    document.getElementById('agentModel').value             = a.model || 'gpt-4o';
    if (a.provider) {
        document.getElementById('agentProvider').value = a.provider;
    } else {
        syncAgentProviderFromModel();  // legacy agent record predating the provider column
    }
    document.getElementById('agentTemp').value              = a.temperature || 0.7;
    document.getElementById('tempVal').textContent          = a.temperature || 0.7;
    document.getElementById('agentColor').value             = a.avatar_color || '#6366f1';
    renderToolCheckboxes(a.tools || []);
    // Populate env vars
    const envEl = document.getElementById('agentEnvVarRows');
    envEl.innerHTML = '';
    Object.entries(a.env_vars || {}).forEach(([k, v]) => agentAddEnvVar(k, v));
    new bootstrap.Modal(document.getElementById('agentModal')).show();
}

function agentAddEnvVar(key = '', value = '') {
    const el  = document.getElementById('agentEnvVarRows');
    const row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = `
        <input type="text" class="form-control form-control-sm agent-env-key"
               placeholder="VARIABLE_NAME" value="${esc(key)}">
        <input type="password" class="form-control form-control-sm agent-env-val"
               placeholder="value" value="${esc(value)}" autocomplete="new-password">
        <button type="button" class="btn btn-sm btn-outline-danger px-2"
                onclick="this.closest('.env-row').remove()">
            <i class="fas fa-times"></i>
        </button>`;
    el.appendChild(row);
}

function agentCollectEnvVars() {
    const result = {};
    document.querySelectorAll('#agentEnvVarRows .env-row').forEach(row => {
        const k = row.querySelector('.agent-env-key').value.trim();
        const v = row.querySelector('.agent-env-val').value;
        if (k) result[k] = v;
    });
    return result;
}

function renderToolCheckboxes(selectedTools) {
    const el = document.getElementById('toolCheckboxes');
    if (!_hubTools.length) {
        el.innerHTML = '<span class="text-muted" style="font-size:.82rem;padding:4px;">No tools available</span>';
        return;
    }
    // Build map: tool name → config object (handles both old string format and new {name,config})
    const selectedMap = {};
    for (const t of (selectedTools || [])) {
        if (typeof t === 'string') selectedMap[t] = {};
        else if (t && t.name) selectedMap[t.name] = t.config || {};
    }

    el.innerHTML = _hubTools.map(t => {
        const isChecked = t.name in selectedMap;
        const cfgDef    = TOOL_CONFIGS[t.name];
        const cfgPanel  = cfgDef ? _buildConfigPanel(t.name, cfgDef, selectedMap[t.name] || {}) : '';
        return `
            <div class="tool-item">
                <label class="tool-item-label">
                    <input type="checkbox" class="form-check-input" id="tool_${t.name}" value="${t.name}"
                        ${isChecked ? 'checked' : ''}
                        onchange="toggleToolConfig('${t.name}', this.checked)">
                    <span style="font-size:.85rem;font-weight:500;">${esc(t.display_name || t.name)}</span>
                    <span class="badge text-bg-secondary" style="font-size:.68rem;font-weight:500;">${esc(t.category || '')}</span>
                </label>
                ${cfgPanel ? `
                    <div id="toolcfg_${t.name}" class="tool-cfg-panel mt-1"
                         style="display:${isChecked ? 'flex' : 'none'};align-items:center;gap:8px;flex-wrap:wrap;">
                        <span style="font-size:.75rem;color:var(--text-2,#6e6265);min-width:140px;">${esc(cfgDef.label)}:</span>
                        ${cfgPanel}
                    </div>
                ` : ''}
            </div>
        `;
    }).join('');
}

function _buildConfigPanel(toolName, cfgDef, currentConfig) {
    const val     = currentConfig[cfgDef.field];
    const inputId = `tcfg_${toolName}_${cfgDef.field}`;

    if (cfgDef.type === 'select') {
        const opts = cfgDef.options();
        return `
            <select class="form-select form-select-sm" id="${inputId}" style="max-width:220px;">
                <option value="">${esc(cfgDef.placeholder)}</option>
                ${opts.map(o => `<option value="${esc(o.value)}" ${val === o.value ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
            </select>`;
    }
    if (cfgDef.type === 'text') {
        const displayVal = Array.isArray(val) ? val.join(', ') : (val || '');
        return `
            <input type="text" class="form-control form-control-sm" id="${inputId}"
                placeholder="${esc(cfgDef.placeholder)}" value="${esc(displayVal)}"
                style="max-width:300px;">`;
    }
    if (cfgDef.type === 'multiselect') {
        const opts        = cfgDef.options();
        const selectedVals = Array.isArray(val) ? val : (val ? [val] : []);
        return `
            <select class="form-select form-select-sm" id="${inputId}"
                multiple style="max-width:220px;max-height:90px;">
                ${opts.map(o => `<option value="${esc(o.value)}" ${selectedVals.includes(o.value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
            </select>
            <span style="font-size:.7rem;color:var(--text-3,#a89b9d);">Ctrl+click to multi-select</span>`;
    }
    return '';
}

function toggleToolConfig(toolName, checked) {
    const panel = document.getElementById(`toolcfg_${toolName}`);
    if (panel) panel.style.display = checked ? 'flex' : 'none';
}

async function saveAgent() {
    const aid = document.getElementById('editAgentId').value;

    // Collect tools as [{name, config?}] objects
    const tools = [];
    document.querySelectorAll('#toolCheckboxes input[type=checkbox]:checked').forEach(cb => {
        const name   = cb.value;
        const cfgDef = TOOL_CONFIGS[name];
        const tool   = { name };
        if (cfgDef) {
            const el = document.getElementById(`tcfg_${name}_${cfgDef.field}`);
            if (el) {
                let configVal;
                if (cfgDef.type === 'multiselect') {
                    configVal = Array.from(el.selectedOptions).map(o => o.value).filter(Boolean);
                } else {
                    configVal = el.value.trim();
                }
                if (configVal && (Array.isArray(configVal) ? configVal.length : configVal)) {
                    tool.config = { [cfgDef.field]: configVal };
                }
            }
        }
        tools.push(tool);
    });

    const body = {
        name:          document.getElementById('agentName').value,
        description:   document.getElementById('agentDesc').value,
        objective:     document.getElementById('agentObjective').value,
        system_prompt: document.getElementById('agentSystemPrompt').value,
        model:         document.getElementById('agentModel').value,
        provider:      document.getElementById('agentProvider').value || 'openai',
        temperature:   parseFloat(document.getElementById('agentTemp').value),
        avatar_color:  document.getElementById('agentColor').value,
        tools,
        env_vars:      agentCollectEnvVars(),
        // auto-assign dept/project from URL context on new agents
        ...(!aid ? _orgPayload() : {}),
    };
    const url    = aid ? `/api/agenthub/agents/${aid}` : '/api/agenthub/agents';
    const method = aid ? 'PUT' : 'POST';
    try {
        const res = await fetch(url, {method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
        if (!res.ok) throw new Error(await res.text());
        bootstrap.Modal.getInstance(document.getElementById('agentModal'))?.hide();
        showToast('Agent saved', 'success');
        await loadAllAgents();
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

async function deleteAgent(id, name) {
    if (!confirm(`Delete agent "${name}"?`)) return;
    await fetch(`/api/agenthub/agents/${id}`, {method: 'DELETE'});
    showToast('Agent deleted', 'info');
    await loadAllAgents();
}

// ══════════════════════════════════════════════════════════════
// WORKFLOWS
// ══════════════════════════════════════════════════════════════
async function initWorkflows() {
    await loadHubWorkflows();
    await loadWfRunHistory();
}

async function loadHubWorkflows() {
    try {
        const res  = await fetch('/api/agenthub/workflows');
        _hubWorkflows = await res.json();
        renderWorkflows(_orgFilter(_hubWorkflows));
    } catch {
        document.getElementById('hubWorkflowsList').innerHTML = '<div class="text-danger text-center py-4">Failed to load workflows</div>';
    }
}

function renderWorkflows(wfs) {
    const el = document.getElementById('hubWorkflowsList');
    if (!wfs.length) {
        el.innerHTML = '<div class="text-center py-5 text-muted"><i class="fas fa-project-diagram fa-2x mb-2"></i><p>No workflows yet.</p></div>';
        return;
    }
    el.innerHTML = wfs.map(w => `
        <div class="wf-card">
            <div class="d-flex justify-content-between align-items-start">
                <div>
                    <div class="wf-name">${esc(w.name)}</div>
                    <div class="wf-desc">${esc(w.description||'')}</div>
                    <div class="wf-meta">
                        <span class="mode-badge ${w.execution_mode||'sequential'}">${w.execution_mode||'sequential'}</span>
                        <span class="ms-2">${w.nodes?.length||0} nodes · ${w.total_runs||0} runs</span>
                        <span class="ms-2">${fmtDate(w.created_at)}</span>
                    </div>
                </div>
                <div class="d-flex gap-2">
                    <button class="btn btn-sm btn-success" onclick="showRunWorkflow('${w.id}','${esc(w.name)}')">
                        <i class="fas fa-play me-1"></i>Run
                    </button>
                    <button class="btn btn-sm btn-outline-primary" onclick="editWorkflow(${JSON.stringify(w).replace(/"/g,'&quot;')})">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteWorkflow('${w.id}','${esc(w.name)}')">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>
            <div class="run-input-area" id="run_${w.id}">
                <label class="form-label form-label-sm">Input</label>
                <input type="text" class="form-control form-control-sm mb-2" id="runInput_${w.id}" placeholder="Workflow input…">
                <button class="btn btn-sm btn-success" onclick="executeWorkflow('${w.id}')"><i class="fas fa-play me-1"></i>Execute</button>
                <div class="wf-run-output" id="runOut_${w.id}" style="display:none;"></div>
            </div>
        </div>
    `).join('');
}

function showRunWorkflow(wid, name) {
    const area = document.getElementById(`run_${wid}`);
    area.style.display = area.style.display === 'none' ? 'block' : 'none';
}

async function executeWorkflow(wid) {
    const inputEl = document.getElementById(`runInput_${wid}`);
    const outEl   = document.getElementById(`runOut_${wid}`);
    const input   = inputEl.value.trim() || 'Start workflow';
    outEl.style.display = 'block';
    outEl.textContent   = 'Running…\n';

    const resp = await fetch(`/api/agenthub/workflows/${wid}/run`, {
        method:  'POST',
        headers: {'Content-Type':'application/json'},
        body:    JSON.stringify({input}),
    });
    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buf     = '';

    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
            if (!line.trim()) continue;
            try {
                const p = JSON.parse(line);
                if (p.type === 'node_start')    outEl.textContent += `▶ ${p.node_name || 'node'}\n`;
                if (p.type === 'node_complete')  outEl.textContent += `✓ ${p.output?.slice(0,80)||''}\n`;
                if (p.type === 'wf_complete')    outEl.textContent += `\n✅ Done: ${p.final_output?.slice(0,200)||''}\n`;
                if (p.type === 'error')          outEl.textContent += `❌ ${p.message}\n`;
            } catch { /* ignore */ }
            outEl.scrollTop = outEl.scrollHeight;
        }
    }
}

async function loadWfRunHistory() {
    try {
        const res  = await fetch('/api/agenthub/workflows/runs');
        const runs = await res.json();
        const el   = document.getElementById('hubWfRunsList');
        if (!runs.length) { el.innerHTML = '<div class="text-muted text-center py-4" style="font-size:.85rem;">No workflow runs yet</div>'; return; }
        el.innerHTML = `<table class="table table-sm table-hover"><thead class="table-light"><tr><th>Workflow</th><th>Status</th><th>Tokens</th><th>Started</th></tr></thead><tbody>
            ${runs.map(r => `<tr>
                <td class="text-truncate" style="max-width:200px;">${esc(r.workflow_id?.slice(-8)||'—')}</td>
                <td><span class="badge bg-${r.status==='completed'?'success':'secondary'}">${r.status}</span></td>
                <td>${r.tokens_used||0}</td>
                <td>${fmtDate(r.started_at)}</td>
            </tr>`).join('')}
        </tbody></table>`;
    } catch { /* ignore */ }
}

function showCreateWorkflowModal() {
    document.getElementById('editWfId').value  = '';
    document.getElementById('wfName').value    = '';
    document.getElementById('wfDesc').value    = '';
    document.getElementById('wfMode').value    = 'sequential';
    document.getElementById('wfNodes').value   = '[]';
    new bootstrap.Modal(document.getElementById('wfModal')).show();
}

function editWorkflow(w) {
    document.getElementById('editWfId').value  = w.id;
    document.getElementById('wfName').value    = w.name || '';
    document.getElementById('wfDesc').value    = w.description || '';
    document.getElementById('wfMode').value    = w.execution_mode || 'sequential';
    document.getElementById('wfNodes').value   = JSON.stringify(w.nodes || [], null, 2);
    new bootstrap.Modal(document.getElementById('wfModal')).show();
}

async function saveWorkflow() {
    const wid  = document.getElementById('editWfId').value;
    let   nodes;
    try { nodes = JSON.parse(document.getElementById('wfNodes').value || '[]'); }
    catch { showToast('Invalid JSON in nodes', 'error'); return; }

    const body = {
        name:           document.getElementById('wfName').value,
        description:    document.getElementById('wfDesc').value,
        execution_mode: document.getElementById('wfMode').value,
        nodes,
        ..._orgPayload(),   // auto-assign dept/project from URL context
    };
    const url    = wid ? `/api/agenthub/workflows/${wid}` : '/api/agenthub/workflows';
    const method = wid ? 'PUT' : 'POST';
    try {
        const res = await fetch(url, {method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
        if (!res.ok) throw new Error(await res.text());
        bootstrap.Modal.getInstance(document.getElementById('wfModal'))?.hide();
        showToast('Workflow saved', 'success');
        await loadHubWorkflows();
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

async function deleteWorkflow(id, name) {
    if (!confirm(`Delete workflow "${name}"?`)) return;
    await fetch(`/api/agenthub/workflows/${id}`, {method: 'DELETE'});
    showToast('Workflow deleted', 'info');
    await loadHubWorkflows();
}

// ══════════════════════════════════════════════════════════════
// TOOLS
// ══════════════════════════════════════════════════════════════
async function initTools() {
    try {
        const res = await fetch('/api/agenthub/tools');
        _hubTools = await res.json();
        renderTools(_hubTools);
        populateCatFilter();
    } catch {
        document.getElementById('hubToolsList').innerHTML = '<div class="text-danger text-center py-4">Failed to load tools</div>';
    }
}

function populateCatFilter() {
    const cats = [...new Set(_hubTools.map(t => t.category).filter(Boolean))];
    const sel  = document.getElementById('toolCatFilter');
    if (!sel) return;
    cats.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c; opt.textContent = c;
        sel.appendChild(opt);
    });
}

const CAT_ICONS = {
    data: 'database', integration: 'plug', communication: 'envelope',
    files: 'file-alt', knowledge: 'book', collaboration: 'users', system: 'cog',
};

function renderTools(tools) {
    const el = document.getElementById('hubToolsList');
    const cats = [...new Set(tools.map(t => t.category || 'other'))];
    el.innerHTML = cats.map(cat => `
        <div class="cat-header">${cat}</div>
        ${tools.filter(t => (t.category||'other') === cat).map(t => `
            <div class="tool-card">
                <div class="tool-icon ${cat}"><i class="fas fa-${CAT_ICONS[cat]||'tools'}"></i></div>
                <div class="flex-grow-1">
                    <div class="tool-name">${esc(t.display_name||t.name)}</div>
                    <div class="tool-desc">${esc(t.description||'')}</div>
                </div>
                <div class="tool-stats">
                    <div>${t.total_calls||0} calls</div>
                    <div class="text-success">${t.success_calls||0} ok</div>
                </div>
                <div class="d-flex flex-column gap-1 ms-3">
                    <button class="btn btn-sm btn-outline-secondary" onclick="openTestTool('${t.id}','${esc(t.display_name||t.name)}')">
                        <i class="fas fa-play"></i>
                    </button>
                    <button class="btn btn-sm btn-${t.enabled?'success':'secondary'}" onclick="toggleTool('${t.id}',this)"
                            title="${t.enabled?'Disable':'Enable'}" style="font-size:.7rem;padding:2px 8px;">
                        ${t.enabled ? 'ON' : 'OFF'}
                    </button>
                </div>
            </div>
        `).join('')}
    `).join('');
}

function filterTools() {
    const q   = document.getElementById('toolSearch')?.value.toLowerCase() || '';
    const cat = document.getElementById('toolCatFilter')?.value || '';
    renderTools(_hubTools.filter(t =>
        (t.display_name||t.name||'').toLowerCase().includes(q) &&
        (cat === '' || t.category === cat)
    ));
}

async function toggleTool(tid, btn) {
    const res  = await fetch(`/api/agenthub/tools/${tid}/toggle`, {method: 'POST'});
    const data = await res.json();
    btn.textContent = data.enabled ? 'ON' : 'OFF';
    btn.className   = `btn btn-sm btn-${data.enabled?'success':'secondary'}`;
    const t = _hubTools.find(x => x.id === tid);
    if (t) t.enabled = data.enabled;
}

function openTestTool(tid, name) {
    _currentTestToolId = tid;
    document.getElementById('testToolName').textContent = name;
    document.getElementById('testToolParams').value     = '{}';
    document.getElementById('testToolResult').style.display = 'none';
    new bootstrap.Modal(document.getElementById('testToolModal')).show();
}

async function runToolTest() {
    let params;
    try { params = JSON.parse(document.getElementById('testToolParams').value || '{}'); }
    catch { showToast('Invalid JSON', 'error'); return; }
    const tool = _hubTools.find(t => t.id === _currentTestToolId);
    if (!tool) return;
    try {
        const res  = await fetch('/api/agenthub/tools/test', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({tool: tool.name, params}),
        });
        const data = await res.json();
        document.getElementById('testToolResultPre').textContent = JSON.stringify(data, null, 2);
        document.getElementById('testToolResult').style.display   = 'block';
    } catch (e) {
        showToast('Test failed: ' + e.message, 'error');
    }
}

// ══════════════════════════════════════════════════════════════
// KNOWLEDGE BASE
// ══════════════════════════════════════════════════════════════
async function initKnowledge() {
    try {
        const res  = await fetch('/api/agenthub/knowledge');
        const docs = await res.json();
        renderKnowledge(docs);
    } catch {
        document.getElementById('hubKnowledgeList').innerHTML = '<div class="text-danger text-center py-4">Failed to load</div>';
    }
}

function renderKnowledge(docs) {
    const el = document.getElementById('hubKnowledgeList');
    if (!docs.length) {
        el.innerHTML = '<div class="text-center py-5 text-muted"><i class="fas fa-book fa-2x mb-2"></i><p>No documents yet.</p></div>';
        return;
    }
    el.innerHTML = `<table class="table table-hover"><thead class="table-light"><tr><th>Name</th><th>Type</th><th>Size</th><th>Chunks</th><th>Created</th><th></th></tr></thead><tbody>
        ${docs.map(d => `<tr>
            <td><strong>${esc(d.name)}</strong><div class="text-muted" style="font-size:.75rem;">${esc(d.description||'')}</div></td>
            <td><span class="badge bg-light text-dark border">${esc(d.file_type)}</span></td>
            <td>${fmtSize(d.file_size)}</td>
            <td>${d.chunk_count||0}</td>
            <td>${fmtDate(d.created_at)}</td>
            <td><button class="btn btn-sm btn-outline-danger" onclick="deleteKnowledge('${d.id}','${esc(d.name)}')"><i class="fas fa-trash"></i></button></td>
        </tr>`).join('')}
    </tbody></table>`;
}

function showUploadKnowledge() {
    new bootstrap.Modal(document.getElementById('kbModal')).show();
}

async function uploadKnowledge() {
    const body = {
        name:        document.getElementById('kbName').value,
        description: document.getElementById('kbDesc').value,
        file_type:   document.getElementById('kbType').value,
        content:     document.getElementById('kbContent').value,
    };
    try {
        const res = await fetch('/api/agenthub/knowledge', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        bootstrap.Modal.getInstance(document.getElementById('kbModal'))?.hide();
        showToast('Document uploaded', 'success');
        initKnowledge();
    } catch (e) {
        showToast('Upload failed: ' + e.message, 'error');
    }
}

async function deleteKnowledge(kid, name) {
    if (!confirm(`Delete "${name}"?`)) return;
    await fetch(`/api/agenthub/knowledge/${kid}`, {method: 'DELETE'});
    showToast('Document deleted', 'info');
    initKnowledge();
}

// ══════════════════════════════════════════════════════════════
// JOBS
// ══════════════════════════════════════════════════════════════
async function initJobs() {
    await loadHubJobs();
    updateJobTargets();
}

async function loadHubJobs() {
    try {
        const res  = await fetch('/api/agenthub/jobs');
        const jobs = await res.json();
        renderJobs(_orgFilter(jobs));
    } catch {
        document.getElementById('hubJobsList').innerHTML = '<div class="text-danger text-center py-4">Failed to load</div>';
    }
}

function renderJobs(jobs) {
    const el = document.getElementById('hubJobsList');
    if (!jobs.length) {
        el.innerHTML = '<div class="text-center py-5 text-muted"><i class="fas fa-clock fa-2x mb-2"></i><p>No scheduled jobs yet.</p></div>';
        return;
    }
    el.innerHTML = jobs.map(j => `
        <div class="job-card">
            <div class="job-icon"><i class="fas fa-${j.job_type==='workflow'?'project-diagram':'robot'}"></i></div>
            <div class="flex-grow-1">
                <div class="job-name">${esc(j.name)}</div>
                <div class="job-desc">${esc(j.description||'')}</div>
                <div class="mt-2 d-flex gap-2 align-items-center">
                    <span class="job-type-badge ${j.job_type}">${j.job_type}</span>
                    <span class="job-schedule">${esc(j.schedule)}</span>
                    <span class="badge bg-${j.status==='active'?'success':'secondary'}">${j.status}</span>
                    <span class="text-muted" style="font-size:.75rem;">${j.run_count||0} runs</span>
                </div>
            </div>
            <div class="d-flex flex-column gap-2">
                <button class="btn btn-sm btn-outline-${j.status==='active'?'warning':'success'}" onclick="toggleJob('${j.id}',this)">
                    <i class="fas fa-${j.status==='active'?'pause':'play'}"></i>
                </button>
                <button class="btn btn-sm btn-outline-danger" onclick="deleteJob('${j.id}','${esc(j.name)}')">
                    <i class="fas fa-trash"></i>
                </button>
            </div>
        </div>
    `).join('');
}

async function updateJobTargets() {
    const type = document.getElementById('jobType')?.value;
    const sel  = document.getElementById('jobTarget');
    if (!sel) return;
    try {
        if (type === 'agent') {
            const res  = await fetch('/api/agenthub/agents/all');
            const data = await res.json();
            sel.innerHTML = data.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
        } else {
            const res  = await fetch('/api/agenthub/workflows');
            const data = await res.json();
            sel.innerHTML = data.map(w => `<option value="${w.id}">${esc(w.name)}</option>`).join('');
        }
    } catch { sel.innerHTML = '<option value="">Failed to load</option>'; }
}

function showCreateJobModal() {
    new bootstrap.Modal(document.getElementById('jobModal')).show();
}

async function createJob() {
    const body = {
        name:        document.getElementById('jobName').value,
        description: document.getElementById('jobDesc').value,
        job_type:    document.getElementById('jobType').value,
        target_id:   document.getElementById('jobTarget').value,
        schedule:    document.getElementById('jobSchedule').value,
        ..._orgPayload(),
    };
    try {
        const res = await fetch('/api/agenthub/jobs', {
            method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        bootstrap.Modal.getInstance(document.getElementById('jobModal'))?.hide();
        showToast('Job created', 'success');
        loadHubJobs();
    } catch (e) {
        showToast('Failed: ' + e.message, 'error');
    }
}

async function toggleJob(jid, btn) {
    const res  = await fetch(`/api/agenthub/jobs/${jid}/toggle`, {method: 'POST'});
    const data = await res.json();
    await loadHubJobs();
}

async function deleteJob(jid, name) {
    if (!confirm(`Delete job "${name}"?`)) return;
    await fetch(`/api/agenthub/jobs/${jid}`, {method: 'DELETE'});
    showToast('Job deleted', 'info');
    loadHubJobs();
}

// ══════════════════════════════════════════════════════════════
// ANALYTICS
// ══════════════════════════════════════════════════════════════
function _fmtCost(usd) {
    if (usd == null || usd === 0) return '$0.00';
    if (usd < 0.01) return '$' + usd.toFixed(4);
    return '$' + usd.toFixed(2);
}

const ANALYT_RANGE_LABELS = { today: 'Today', '7d': 'Last 7 Days', all: 'All Time' };
const ANALYT_MONTH_LABELS = ['January','February','March','April','May','June',
                              'July','August','September','October','November','December'];

// Selecting a month overrides the quick range and vice-versa — only one
// filter is ever "active" at a time so the numbers on the page are unambiguous.
function onAnalytRangeChange() {
    const monthInput = document.getElementById('analytMonthFilter');
    if (monthInput) monthInput.value = '';
    initAnalytics();
}

function onAnalytMonthChange() {
    const monthInput = document.getElementById('analytMonthFilter');
    const rangeSel   = document.getElementById('analytRangeFilter');
    if (rangeSel) rangeSel.disabled = !!(monthInput && monthInput.value);
    initAnalytics();
}

function _analytMonthLabel(monthVal) {
    const [y, m] = monthVal.split('-').map(Number);
    return `${ANALYT_MONTH_LABELS[m - 1]} ${y}`;
}

async function initAnalytics() {
    try {
        const deptSel    = document.getElementById('analytDeptFilter');
        const deptId     = deptSel ? deptSel.value : '';
        const rangeSel   = document.getElementById('analytRangeFilter');
        const monthInput = document.getElementById('analytMonthFilter');
        const monthVal   = monthInput ? monthInput.value : '';
        const range      = rangeSel ? rangeSel.value : '7d';

        const params = new URLSearchParams();
        if (monthVal) params.set('month', monthVal);
        else          params.set('range', range);
        if (deptId) params.set('dept_id', deptId);

        const url  = `/api/agenthub/analytics?${params.toString()}`;
        const res  = await fetch(url);
        const data = await res.json();
        const t    = data.totals || {};

        // Update filter label
        const lbl = document.getElementById('analytFilterLabel');
        if (lbl) {
            if (deptId && deptSel) {
                const txt = deptSel.options[deptSel.selectedIndex]?.text || '';
                lbl.textContent = 'Dept: ' + txt;
                lbl.style.display = '';
            } else {
                lbl.style.display = 'none';
            }
        }

        const rangeLbl = document.getElementById('analytUserUsageRange');
        if (rangeLbl) {
            rangeLbl.textContent = '(' + (monthVal ? _analytMonthLabel(monthVal)
                                                    : (ANALYT_RANGE_LABELS[range] || '')) + ')';
        }

        document.getElementById('hubAnalyticsTotals').innerHTML = `
            <div class="tot-item"><div class="n">${fmtNum(t.tokens)}</div><div class="l">Tokens</div></div>
            <div class="tot-item"><div class="n">${fmtNum(t.agent_runs)}</div><div class="l">Agent Runs</div></div>
            <div class="tot-item"><div class="n">${fmtNum(t.workflow_runs)}</div><div class="l">WF Runs</div></div>
            <div class="tot-item cost-item"><div class="n">${_fmtCost(t.cost_usd)}</div><div class="l">Est. Cost</div></div>
        `;

        // Agent stats — bar chart with run count + cost
        const maxRuns = Math.max(...(data.agent_stats||[]).map(a => a.runs||0), 1);
        document.getElementById('hubAgentStats').innerHTML = (data.agent_stats||[]).map(a => `
            <div class="bar-row" title="${esc(a.name)} · model: ${esc(a.model||'gpt-4o')} · ${fmtNum(a.tokens)} tokens · ${_fmtCost(a.cost_usd)}">
                <div class="label">${esc(a.name)}</div>
                <div class="bar-track"><div class="bar-fill" style="width:${Math.round((a.runs||0)/maxRuns*100)}%;background:${a.color||'#6366f1'};"></div></div>
                <div class="val">${a.runs||0}</div>
                <div class="cost">${_fmtCost(a.cost_usd)}</div>
            </div>
        `).join('') || '<p class="text-muted" style="font-size:.82rem;">No data</p>';

        // Tool stats
        const maxCalls = Math.max(...(data.tool_stats||[]).map(ts => ts.calls||0), 1);
        document.getElementById('hubToolStats').innerHTML = (data.tool_stats||[]).slice(0,10).map(ts => `
            <div class="bar-row">
                <div class="label">${esc(ts.name)}</div>
                <div class="bar-track"><div class="bar-fill" style="width:${Math.round((ts.calls||0)/maxCalls*100)}%;background:#14b8a6;"></div></div>
                <div class="val">${ts.calls||0}</div>
            </div>
        `).join('') || '<p class="text-muted" style="font-size:.82rem;">No data</p>';

        // User usage — table with per-agent breakdown and costs
        if ((data.user_usage||[]).length === 0) {
            document.getElementById('hubUserUsage').innerHTML =
                '<p class="text-muted" style="font-size:.82rem;">No hub usage tracked</p>';
        } else {
            document.getElementById('hubUserUsage').innerHTML = `
                <table class="uu-table">
                    <thead><tr>
                        <th>User</th>
                        <th style="text-align:right;">Tokens</th>
                        <th style="text-align:right;">Cost</th>
                    </tr></thead>
                    <tbody>
                    ${(data.user_usage||[]).map(u => `
                        <tr>
                            <td>
                                <div class="uu-user">${esc(u.username)}</div>
                                ${(u.agents||[]).length ? `<div class="uu-agents">${
                                    u.agents.map(ag => `<span class="uu-agent-tag" title="Model: ${esc(ag.model||'gpt-4o')}">${esc(ag.name)}&nbsp;<span>${fmtNum(ag.tokens)}&nbsp;·&nbsp;${_fmtCost(ag.cost_usd)}</span></span>`).join('')
                                }</div>` : ''}
                            </td>
                            <td style="text-align:right;" class="uu-tokens">${fmtNum(u.tokens)}</td>
                            <td style="text-align:right;" class="uu-cost">${_fmtCost(u.cost_usd)}</td>
                        </tr>
                    `).join('')}
                    </tbody>
                </table>`;
        }

        // Workflow run table
        document.getElementById('hubWfRunsTable').innerHTML = (data.workflow_runs||[]).map(r => `
            <tr>
                <td class="text-truncate" style="max-width:200px;">${esc(r.workflow_id?.slice(-8)||'—')}</td>
                <td><span class="badge bg-${r.status==='completed'?'success':'secondary'}">${r.status}</span></td>
                <td>${r.tokens_used||0}</td>
                <td>${fmtDate(r.started_at)}</td>
                <td>${fmtDate(r.completed_at)}</td>
            </tr>
        `).join('') || '<tr><td colspan="5" class="text-center text-muted py-3">No runs yet</td></tr>';

    } catch (e) {
        console.error('Analytics failed', e);
    }
}

// ══════════════════════════════════════════════════════════════
// ASSIGNMENTS
// ══════════════════════════════════════════════════════════════
async function initAssignments() {
    await Promise.all([
        loadUsersForAssign(),
        loadAgentsForAssign(),
        loadWorkflowsForAssign(),
        loadHubAgentAssignmentPanel(),
        loadWorkflowAssignments(),
    ]);
}

async function loadUsersForAssign() {
    try {
        const res  = await fetch('/api/admin/users');
        _hubUsers  = await res.json();
        const users = _hubUsers.filter(u => u.role === 'user' || u.role === 'dev');
        const opts  = users.map(u => `<option value="${u.id}">${esc(u.username)} (${u.role})</option>`).join('');
        document.getElementById('assignWfUser').innerHTML = opts;
    } catch { /* ignore */ }
}

async function loadAgentsForAssign() {
    try {
        const res = await fetch('/api/agenthub/agents/all');
        _hubAllAgents = await res.json();
    } catch { /* ignore */ }
}

async function loadWorkflowsForAssign() {
    try {
        const res = await fetch('/api/agenthub/workflows');
        const wfs = await res.json();
        document.getElementById('assignWfSelect').innerHTML =
            wfs.map(w => `<option value="${w.id}" data-name="${esc(w.name)}">${esc(w.name)}</option>`).join('');
    } catch { /* ignore */ }
}

// ── Agent Assignments — two-pane (user list + agent checkboxes) ───────────
const HUB_ROLE_BADGE = {
    admin: '<span class="badge bg-danger">Admin</span>',
    dev:   '<span class="badge bg-warning text-dark">Dev</span>',
    user:  '<span class="badge bg-secondary">User</span>',
};

let _hubAllAgents          = [];
let _hubAgentAssignments   = [];
let _hubAssignSelectedUser = null;

async function loadHubAgentAssignmentPanel() {
    try {
        const res  = await fetch('/api/agenthub/assignments/agents');
        _hubAgentAssignments = await res.json();
    } catch { _hubAgentAssignments = []; }

    const nonAdmins = _hubUsers.filter(u => u.role !== 'admin');
    const container = document.getElementById('hubAssignUserList');
    if (!container) return;

    if (!nonAdmins.length) {
        container.innerHTML = '<div class="text-muted small p-3">No non-admin users yet.</div>';
        return;
    }

    container.innerHTML = nonAdmins.map(u => `
        <div class="hub-assign-user-row px-3 py-2 d-flex align-items-center gap-2"
             id="hubUserRow-${u.id}"
             onclick="selectHubAssignUser(${u.id})"
             style="cursor:pointer; border-bottom:1px solid #f2eeee; transition:background .15s;">
            <div style="width:30px;height:30px;border-radius:50%;background:#e6dfe0;
                        display:flex;align-items:center;justify-content:center;
                        font-weight:700;font-size:.8rem;flex-shrink:0;">
                ${esc(u.username[0].toUpperCase())}
            </div>
            <div>
                <div style="font-weight:600;font-size:.875rem;">${esc(u.username)}</div>
                <div style="font-size:.75rem;color:#a89b9d;">${HUB_ROLE_BADGE[u.role] || u.role}</div>
            </div>
        </div>`).join('');
}

function selectHubAssignUser(userId) {
    _hubAssignSelectedUser = userId;

    document.querySelectorAll('.hub-assign-user-row').forEach(el => {
        el.style.background = el.id === 'hubUserRow-' + userId ? '#F8E8EB' : '';
    });

    const user = _hubUsers.find(u => u.id === userId);
    document.getElementById('hubAssignPanelTitle').textContent =
        `Hub Agents for: ${user ? user.username : userId}`;
    document.getElementById('hubSaveAssignBtn').classList.remove('d-none');

    renderHubAgentCheckboxes();
}

function renderHubAgentCheckboxes() {
    const container = document.getElementById('hubAssignAgentList');
    if (!_hubAllAgents.length) {
        container.innerHTML = '<div class="text-muted small">No Hub agents found. Create agents first.</div>';
        return;
    }
    const assignedIds = _hubAgentAssignments
        .filter(a => a.user_id === _hubAssignSelectedUser)
        .map(a => a.agent_id);

    container.innerHTML = _hubAllAgents.map(agent => {
        const checked = assignedIds.includes(agent.id) ? 'checked' : '';
        return `
        <div class="d-flex align-items-center gap-2 mb-2">
            <div class="form-check mb-0 flex-grow-1">
                <input class="form-check-input" type="checkbox" value="${esc(agent.id)}"
                       data-name="${esc(agent.name)}" id="hubChk-${esc(agent.id)}" ${checked}>
                <label class="form-check-label" for="hubChk-${esc(agent.id)}">
                    <strong>${esc(agent.name)}</strong>
                    ${agent.description ? `<span class="text-muted small ms-1">— ${esc(agent.description)}</span>` : ''}
                </label>
            </div>
            <button class="btn btn-sm btn-outline-warning py-0 px-2" style="font-size:.72rem;"
                    title="Configure guardrails for this user+agent"
                    onclick="openGuardrailModal(${_hubAssignSelectedUser}, ${JSON.stringify(agent.id)}, ${JSON.stringify(agent.name)}, 'hub')">
                <i class="fas fa-shield-alt me-1"></i>Guardrails
            </button>
        </div>`;
    }).join('');
}

async function saveHubAgentAssignments() {
    if (!_hubAssignSelectedUser) return;
    const checkboxes = document.querySelectorAll('#hubAssignAgentList .form-check-input');
    const checkedAgentIds = Array.from(checkboxes).filter(c => c.checked)
        .map(c => ({ id: c.value, name: c.dataset.name }));

    const currentAssignments = _hubAgentAssignments.filter(a => a.user_id === _hubAssignSelectedUser);
    const currentIds = currentAssignments.map(a => a.agent_id);
    const checkedIds  = checkedAgentIds.map(a => a.id);

    const toAdd    = checkedAgentIds.filter(a => !currentIds.includes(a.id));
    const toRemove = currentAssignments.filter(a => !checkedIds.includes(a.agent_id));

    const btn = document.getElementById('hubSaveAssignBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

    try {
        await Promise.all([
            ...toAdd.map(a => fetch('/api/agenthub/assignments/agents', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: _hubAssignSelectedUser, agent_id: a.id, agent_name: a.name }),
            })),
            ...toRemove.map(a => fetch(`/api/agenthub/assignments/agents/${a.id}`, { method: 'DELETE' })),
        ]);

        const res  = await fetch('/api/agenthub/assignments/agents');
        _hubAgentAssignments = await res.json();

        btn.innerHTML = '<i class="fas fa-check me-1"></i>Saved!';
        setTimeout(() => {
            btn.innerHTML = '<i class="fas fa-save me-1"></i>Save';
            btn.disabled  = false;
        }, 1500);
    } catch (e) {
        showToast('Failed to save assignments', 'error');
        btn.innerHTML = '<i class="fas fa-save me-1"></i>Save';
        btn.disabled  = false;
    }
}

async function loadWorkflowAssignments() {
    try {
        const res  = await fetch('/api/agenthub/assignments/workflows');
        const rows = await res.json();
        renderAssignList('wfAssignList', rows, 'workflow_name', 'username', (id) => removeWfAssignment(id));
    } catch { /* ignore */ }
}

function renderAssignList(elId, rows, nameKey, userKey, onRemove, agentType = 'hub') {
    const el = document.getElementById(elId);
    if (!rows.length) { el.innerHTML = '<div class="text-muted" style="font-size:.82rem;">No assignments yet</div>'; return; }
    el.innerHTML = rows.map(r => `
        <div class="assign-row">
            <span class="u-chip"><i class="fas fa-user me-1"></i>${esc(r[userKey])}</span>
            <i class="fas fa-arrow-right text-muted mx-1"></i>
            <span class="a-chip"><i class="fas fa-robot me-1"></i>${esc(r[nameKey])}</span>
            <span class="text-muted ms-auto" style="font-size:.72rem;">${fmtDate(r.assigned_at)}</span>
            ${agentType === 'hub' ? `<button class="btn btn-sm btn-link text-warning p-0 ms-2" title="Configure Guardrails"
                onclick="openGuardrailModal(${r.user_id}, '${esc(r.agent_id)}', '${esc(r.agent_name || r[nameKey])}', 'hub')">
                <i class="fas fa-shield-alt"></i>
            </button>` : ''}
            <button class="btn btn-sm btn-link text-danger p-0 ms-1" onclick="(${onRemove.toString()})(${r.id})">
                <i class="fas fa-times"></i>
            </button>
        </div>
    `).join('');
}

async function addWorkflowAssignment() {
    const user_id      = document.getElementById('assignWfUser').value;
    const sel          = document.getElementById('assignWfSelect');
    const workflow_id  = sel.value;
    const workflow_name = sel.selectedOptions[0]?.dataset.name || '';
    if (!user_id || !workflow_id) { showToast('Select user and workflow', 'error'); return; }
    try {
        const res = await fetch('/api/agenthub/assignments/workflows', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({user_id: parseInt(user_id), workflow_id, workflow_name}),
        });
        if (!res.ok) throw new Error(await res.text());
        showToast('Workflow assigned', 'success');
        loadWorkflowAssignments();
    } catch (e) {
        showToast('Failed: ' + e.message, 'error');
    }
}

async function removeWfAssignment(id) {
    await fetch(`/api/agenthub/assignments/workflows/${id}`, {method: 'DELETE'});
    showToast('Assignment removed', 'info');
    loadWorkflowAssignments();
}

// ══════════════════════════════════════════════════════════════
// GUARDRAILS MODAL
// ══════════════════════════════════════════════════════════════
let _grailCtx = { scopeType: 'user', scopeId: null, agentId: null, agentName: '', agentType: 'hub' };

async function openGuardrailModal(userId, agentId, agentName, agentType = 'hub') {
    await _loadGuardrailModal('user', userId, agentId, agentName, agentType, userId, '');
}

async function _loadGuardrailModal(scopeType, scopeId, agentId, agentName, agentType, scopeLabel, scopeNote) {
    _grailCtx = { scopeType, scopeId, agentId, agentName, agentType };
    document.getElementById('grailModalTitle').textContent =
        `Guardrails — ${agentName}`;
    document.getElementById('grailUserId').textContent     = scopeLabel;
    document.getElementById('grailAgentId').textContent    = agentId;
    document.getElementById('grailScopeNote').innerHTML    = scopeNote;
    document.getElementById('grailCustomInstruction').value = '';
    document.getElementById('grailFilterRules').innerHTML  = '';

    // Load existing config
    try {
        const res = await fetch(
            `/api/agenthub/admin/guardrails?scope_type=${scopeType}&scope_id=${scopeId}&agent_id=${encodeURIComponent(agentId)}&agent_type=${agentType}`
        );
        const data = await res.json();
        const g = data.guardrail || {};
        document.getElementById('grailCustomInstruction').value = g.custom_instruction || '';
        (g.filter_rules || []).forEach(r => addFilterRow(r.column, r.operator, r.value));
    } catch { /* start fresh */ }

    const modal = new bootstrap.Modal(document.getElementById('guardrailModal'));
    modal.show();
}

async function openOrgGuardrailModal(scopeType) {
    const isDept     = scopeType === 'department';
    const scopeSel   = document.getElementById(isDept ? 'grailDeptSelect'          : 'grailProjectSelect');
    const agentSel   = document.getElementById(isDept ? 'grailDeptAgentSelect'     : 'grailProjectAgentSelect');
    const typeSel    = document.getElementById(isDept ? 'grailDeptAgentType'        : 'grailProjectAgentType');
    const scopeId    = scopeSel.value;
    const scopeName  = scopeSel.selectedOptions[0]?.textContent || '';
    const agentId    = agentSel.value;
    const agentName  = agentSel.selectedOptions[0]?.textContent || agentId;
    const agentType  = typeSel?.value || 'bi';
    if (!scopeId || !agentId) { showToast(`Select a ${scopeType} and an agent`, 'error'); return; }

    const typeLabel = agentType === 'hub' ? 'Hub Agent' : 'BI Agent';
    const note = ` These apply to <strong>every member</strong> of ${isDept ? 'department' : 'project'} "${esc(scopeName)}" for ${typeLabel} <strong>${esc(agentName)}</strong>.`;
    await _loadGuardrailModal(scopeType, parseInt(scopeId), agentId, agentName, agentType, `${scopeName} (${scopeType})`, note);
}

function addFilterRow(col = '', op = '=', val = '') {
    const container = document.getElementById('grailFilterRules');
    const idx = container.children.length;
    const div = document.createElement('div');
    div.className = 'd-flex gap-2 mb-2 align-items-center grail-filter-row';
    div.innerHTML = `
        <input type="text" class="form-control form-control-sm grail-col"
               placeholder="Column (e.g. project_name)" value="${esc(col)}" style="flex:2">
        <select class="form-select form-select-sm grail-op" style="flex:1">
            ${['=','!=','LIKE','NOT LIKE','IN','NOT IN','>','<','>=','<='].map(o =>
                `<option value="${o}"${o===op?' selected':''}>${o}</option>`
            ).join('')}
        </select>
        <input type="text" class="form-control form-control-sm grail-val"
               placeholder="Value (e.g. AAI)" value="${esc(val)}" style="flex:2">
        <button class="btn btn-sm btn-outline-danger" onclick="this.parentElement.remove()">
            <i class="fas fa-times"></i>
        </button>`;
    container.appendChild(div);
}

async function saveGuardrail() {
    const rows = [...document.querySelectorAll('.grail-filter-row')].map(r => ({
        column:   r.querySelector('.grail-col').value.trim(),
        operator: r.querySelector('.grail-op').value,
        value:    r.querySelector('.grail-val').value.trim(),
    })).filter(r => r.column);

    const payload = {
        scope_type:         _grailCtx.scopeType,
        scope_id:           _grailCtx.scopeId,
        agent_id:           _grailCtx.agentId,
        agent_type:         _grailCtx.agentType,
        filter_rules:       rows,
        restrict_tables:    null,
        custom_instruction: document.getElementById('grailCustomInstruction').value.trim(),
    };

    try {
        const res = await fetch('/api/agenthub/admin/guardrails', {
            method: 'PUT', headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.success) {
            showToast('Guardrail saved', 'success');
            bootstrap.Modal.getInstance(document.getElementById('guardrailModal')).hide();
            if (_grailCtx.scopeType !== 'user') loadOrgGuardrailLists();
        } else {
            showToast('Save failed: ' + (data.message || ''), 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function clearGuardrail() {
    if (!confirm('Remove all guardrails for this assignment?')) return;
    try {
        await fetch('/api/agenthub/admin/guardrails', {
            method: 'DELETE', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({
                scope_type: _grailCtx.scopeType,
                scope_id:   _grailCtx.scopeId,
                agent_id:   _grailCtx.agentId,
                agent_type: _grailCtx.agentType,
            }),
        });
        showToast('Guardrail removed', 'info');
        bootstrap.Modal.getInstance(document.getElementById('guardrailModal')).hide();
        if (_grailCtx.scopeType !== 'user') loadOrgGuardrailLists();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

// ══════════════════════════════════════════════════════════════
// ORG GUARDRAILS TAB (department / project)
// ══════════════════════════════════════════════════════════════
let _orgGuardrailsInited = false;

// Cached agent lists by type for the org guardrail dropdowns
let _orgBiAgents  = [];
let _orgHubAgents = [];

function _orgAgentOpts(type) {
    const list = type === 'hub' ? _orgHubAgents : _orgBiAgents;
    // Hub agents are identified by UUID (a.id) everywhere in the chat system.
    // BI agents are identified by name. Use the right key so the saved agent_id
    // matches what the chat endpoint sends when querying guardrails.
    return list.map(a => {
        const val = type === 'hub' ? a.id : a.name;
        return `<option value="${esc(val)}">${esc(a.name)}</option>`;
    }).join('');
}

/** Repopulate the agent dropdown when admin switches between BI / Hub agent type. */
function refreshOrgAgentList(scopeType) {
    const isDept  = scopeType === 'department';
    const typeSel = document.getElementById(isDept ? 'grailDeptAgentType' : 'grailProjectAgentType');
    const agentEl = document.getElementById(isDept ? 'grailDeptAgentSelect' : 'grailProjectAgentSelect');
    if (agentEl && typeSel) agentEl.innerHTML = _orgAgentOpts(typeSel.value);
}

async function initOrgGuardrails() {
    if (_orgGuardrailsInited) { loadOrgGuardrailLists(); return; }
    _orgGuardrailsInited = true;
    try {
        const [deptRes, projRes, biRes, hubRes] = await Promise.all([
            fetch('/api/admin/departments'),
            fetch('/api/admin/projects'),
            fetch('/api/bi-agents'),
            fetch('/api/agenthub/agents/all'),
        ]);
        const depts = await deptRes.json();
        const projs = await projRes.json();
        _orgBiAgents  = await biRes.json()  || [];
        _orgHubAgents = await hubRes.json() || [];

        document.getElementById('grailDeptSelect').innerHTML =
            (depts || []).map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
        document.getElementById('grailProjectSelect').innerHTML =
            (projs || []).map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');

        // Populate agent dropdowns (default: BI agents)
        document.getElementById('grailDeptAgentSelect').innerHTML    = _orgAgentOpts('bi');
        document.getElementById('grailProjectAgentSelect').innerHTML = _orgAgentOpts('bi');

        // Pre-select the active dept/project from URL context
        if (_hubDeptId) {
            const el = document.getElementById('grailDeptSelect');
            if (el) el.value = String(_hubDeptId);
        }
        if (_hubProjId) {
            const el = document.getElementById('grailProjectSelect');
            if (el) el.value = String(_hubProjId);
        }
    } catch { /* ignore */ }
    loadOrgGuardrailLists();
}

async function loadOrgGuardrailLists() {
    try {
        const res  = await fetch('/api/agenthub/admin/guardrails');
        const data = await res.json();
        const rows = data.guardrails || [];

        // When inside a dept/project context, show only that scope's guardrails
        const deptRows = rows.filter(r => r.scope_type === 'department' &&
            (!_hubDeptId || r.scope_id === _hubDeptId));
        const projRows = rows.filter(r => r.scope_type === 'project' &&
            (!_hubProjId || r.scope_id === _hubProjId));

        renderOrgGuardrailList('deptGuardrailList', deptRows);
        renderOrgGuardrailList('projectGuardrailList', projRows);
    } catch { /* ignore */ }
}

function renderOrgGuardrailList(elId, rows) {
    const el = document.getElementById(elId);
    if (!rows.length) { el.innerHTML = '<div class="text-muted" style="font-size:.82rem;">None configured</div>'; return; }
    el.innerHTML = rows.map(r => `
        <div class="assign-row">
            <span class="u-chip"><i class="fas fa-sitemap me-1"></i>${esc(r.scope_name)}</span>
            <i class="fas fa-arrow-right text-muted mx-1"></i>
            <span class="a-chip"><i class="fas fa-robot me-1"></i>${esc(r.agent_display_name || r.agent_id)}</span>
            <span class="badge bg-secondary ms-1" style="font-size:.65rem;">${esc(r.agent_type)}</span>
            <span class="text-muted ms-auto" style="font-size:.72rem;">${(r.filter_rules && JSON.parse(r.filter_rules).length) || 0} filter(s)</span>
            <button class="btn btn-sm btn-link text-warning p-0 ms-2" title="Edit Guardrail"
                onclick="_editOrgGuardrail('${r.scope_type}', ${r.scope_id}, '${esc(r.agent_id)}', '${esc(r.scope_name)}', '${esc(r.agent_display_name || r.agent_id)}', '${esc(r.agent_type)}')">
                <i class="fas fa-shield-alt"></i>
            </button>
        </div>
    `).join('');
}

async function _editOrgGuardrail(scopeType, scopeId, agentId, scopeName, agentName, agentType = 'bi') {
    const isDept    = scopeType === 'department';
    const typeLabel = agentType === 'hub' ? 'Hub Agent' : 'BI Agent';
    const note = ` These apply to <strong>every member</strong> of ${isDept ? 'department' : 'project'} "${esc(scopeName)}" for ${typeLabel} <strong>${esc(agentName)}</strong>.`;
    await _loadGuardrailModal(scopeType, scopeId, agentId, agentName, agentType, `${scopeName} (${scopeType})`, note);
}

// ══════════════════════════════════════════════════════════════
// SHARED UTILITIES
// ══════════════════════════════════════════════════════════════
function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function md2html(text) {
    if (!text) return '';
    return text
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
        .replace(/\*(.+?)\*/g,'<em>$1</em>')
        .replace(/`([^`]+)`/g,'<code>$1</code>')
        .replace(/\n/g,'<br>');
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString();
}

function fmtDate(s) {
    if (!s) return '—';
    try { return new Date(s).toLocaleDateString(); } catch { return s; }
}

function fmtSize(bytes) {
    if (!bytes) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/1048576).toFixed(1) + ' MB';
}

function showToast(msg, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `alert alert-${type === 'error' ? 'danger' : type === 'success' ? 'success' : 'info'} position-fixed shadow`;
    toast.style.cssText = 'bottom:20px;right:20px;z-index:9999;min-width:220px;font-size:.85rem;';
    toast.innerHTML = `<i class="fas fa-${type==='success'?'check-circle':type==='error'?'exclamation-circle':'info-circle'} me-2"></i>${esc(msg)}`;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3500);
}


// ══════════════════════════════════════════════════════════════
// CUSTOM TOOLS
// ══════════════════════════════════════════════════════════════

let _ctTools        = [];
let _ctActiveTool   = null;  // full tool object currently in the form
let _ctEditMode     = false; // true = editing existing, false = creating new

function initCustomTools() {
    ctLoadTools();
    // Tab-key support for code editors
    document.querySelectorAll('.code-editor').forEach(el => {
        el.addEventListener('keydown', e => {
            if (e.key === 'Tab') {
                e.preventDefault();
                const s = el.selectionStart, end = el.selectionEnd;
                el.value = el.value.substring(0, s) + '    ' + el.value.substring(end);
                el.selectionStart = el.selectionEnd = s + 4;
            }
        });
    });
}

async function ctLoadTools() {
    try {
        const res  = await fetch('/api/agenthub/custom-tools');
        _ctTools   = await res.json();
        ctRenderList();
    } catch(e) {
        document.getElementById('ctToolList').innerHTML =
            '<div class="ct-empty text-danger"><i class="fas fa-exclamation-circle me-1"></i>Failed to load</div>';
    }
}

function ctRenderList() {
    const el = document.getElementById('ctToolList');
    document.getElementById('ctToolCount').textContent = _ctTools.length;
    if (!_ctTools.length) {
        el.innerHTML = '<div class="ct-empty"><i class="fas fa-wrench mb-2 d-block"></i>No tools yet.<br>Click <strong>+ New Tool</strong> to start.</div>';
        return;
    }
    el.innerHTML = _ctTools.map(t => `
        <div class="ct-tool-item ${_ctActiveTool && _ctActiveTool.id === t.id ? 'active' : ''}"
             onclick="ctSelectTool(${t.id})">
            <div class="ct-icon">🔧</div>
            <div class="ct-meta">
                <div class="ct-name">${esc(t.display_name || t.name)}</div>
                <div class="ct-cat">${esc(t.category || 'Custom')}</div>
            </div>
            <span class="${t.enabled ? 'badge-enabled' : 'badge-disabled'}">${t.enabled ? 'On' : 'Off'}</span>
        </div>
    `).join('');
}

async function ctSelectTool(id) {
    try {
        const res = await fetch(`/api/agenthub/custom-tools/${id}`);
        if (!res.ok) { showToast('Failed to load tool', 'error'); return; }
        const tool = await res.json();
        _ctActiveTool = tool;
        _ctEditMode   = true;
        ctPopulateForm(tool);
        ctRenderList();
    } catch(e) {
        showToast('Error loading tool', 'error');
    }
}

function ctNewTool() {
    _ctActiveTool = null;
    _ctEditMode   = false;
    ctPopulateForm(null);
    ctRenderList();
}

function ctPopulateForm(tool) {
    document.getElementById('ctPlaceholder').style.display = 'none';
    document.getElementById('ctForm').style.display        = 'block';

    const v = (id, val) => { const el = document.getElementById(id); if(el) el.value = val || ''; };
    v('ctName',         tool ? tool.name : '');
    v('ctDisplayName',  tool ? tool.display_name : '');
    v('ctCategory',     tool ? (tool.category || 'Custom') : 'Custom');
    v('ctDescription',  tool ? (tool.description || '') : '');
    v('ctPipPackages',  tool ? (Array.isArray(tool.pip_packages) ? tool.pip_packages.join('\n') : '') : '');
    v('ctImports',      tool ? (tool.imports_code || '') : '');
    v('ctFunctionCode', tool ? (tool.function_code || 'def run(**kwargs):\n    return {}') : 'def run(**kwargs):\n    return {}');
    v('ctOutputDesc',   tool ? (tool.output_desc || '') : '');

    // Schema rows
    const schema = tool ? (tool.input_schema || []) : [];
    document.getElementById('ctSchemaRows').innerHTML = '';
    schema.forEach(p => ctAddSchemaRow(p));

    // Env var rows
    const envEl = document.getElementById('ctEnvVarRows');
    if (envEl) {
        envEl.innerHTML = '';
        Object.entries(tool ? (tool.env_vars || {}) : {}).forEach(([k, v]) => ctAddEnvVar(k, v));
    }

    // Venv status (only when editing an existing tool)
    const venvStatusEl = document.getElementById('ctVenvStatus');
    if (venvStatusEl) venvStatusEl.innerHTML = '';
    if (tool) ctLoadVenvStatus(tool.id);

    // Buttons visibility
    const isEdit = !!tool;
    document.getElementById('ctTestBtn').style.display   = isEdit ? '' : 'none';
    document.getElementById('ctToggleBtn').style.display = isEdit ? '' : 'none';
    document.getElementById('ctDeleteBtn').style.display = isEdit ? '' : 'none';

    if (isEdit) {
        const toggleBtn = document.getElementById('ctToggleBtn');
        toggleBtn.innerHTML = tool.enabled
            ? '<i class="fas fa-toggle-off me-2"></i>Disable'
            : '<i class="fas fa-toggle-on me-2"></i>Enable';
    }

    document.getElementById('ctSaveMsg').style.display = 'none';
    document.getElementById('ctInstallOutput').style.display = 'none';
    document.getElementById('ctName').disabled = isEdit; // name is immutable after creation
}

function ctSlugify(input) {
    input.value = input.value.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
}

function ctAddSchemaRow(param) {
    const p = param || { name: '', type: 'string', required: false, description: '' };
    const row = document.createElement('div');
    row.className = 'schema-row';
    row.innerHTML = `
        <input type="text" class="form-control form-control-sm ct-param-name"
               placeholder="param_name" value="${esc(p.name)}">
        <select class="form-select form-select-sm ct-param-type">
            ${['string','integer','float','boolean','object','array'].map(t =>
                `<option value="${t}" ${p.type === t ? 'selected' : ''}>${t}</option>`
            ).join('')}
        </select>
        <div class="form-check d-flex justify-content-center align-items-center">
            <input class="form-check-input ct-param-req" type="checkbox" ${p.required ? 'checked' : ''}>
        </div>
        <input type="text" class="form-control form-control-sm ct-param-desc"
               placeholder="Description" value="${esc(p.description || '')}">
        <button class="btn btn-sm btn-outline-danger px-2" onclick="ctRemoveSchemaRow(this)">
            <i class="fas fa-times"></i>
        </button>`;
    document.getElementById('ctSchemaRows').appendChild(row);
}

function ctRemoveSchemaRow(btn) {
    btn.closest('.schema-row').remove();
}

function ctCollectInputSchema() {
    const rows = document.querySelectorAll('#ctSchemaRows .schema-row');
    return Array.from(rows).map(row => ({
        name:        row.querySelector('.ct-param-name').value.trim(),
        type:        row.querySelector('.ct-param-type').value,
        required:    row.querySelector('.ct-param-req').checked,
        description: row.querySelector('.ct-param-desc').value.trim(),
    })).filter(p => p.name);
}

function ctCollectFormData() {
    return {
        name:          document.getElementById('ctName').value.trim(),
        display_name:  document.getElementById('ctDisplayName').value.trim(),
        category:      document.getElementById('ctCategory').value.trim() || 'Custom',
        description:   document.getElementById('ctDescription').value.trim(),
        pip_packages:  document.getElementById('ctPipPackages').value
                           .split('\n').map(s => s.trim()).filter(Boolean),
        imports_code:  document.getElementById('ctImports').value,
        function_code: document.getElementById('ctFunctionCode').value,
        input_schema:  ctCollectInputSchema(),
        output_desc:   document.getElementById('ctOutputDesc').value.trim(),
        env_vars:      ctCollectEnvVars(),
    };
}

function ctAddEnvVar(key = '', value = '') {
    const el  = document.getElementById('ctEnvVarRows');
    if (!el) return;
    const row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = `
        <input type="text" class="form-control form-control-sm ct-env-key"
               placeholder="VAR_NAME" value="${esc(key)}">
        <input type="password" class="form-control form-control-sm ct-env-val"
               placeholder="default value" value="${esc(value)}" autocomplete="new-password">
        <button type="button" class="btn btn-sm btn-outline-danger px-2"
                onclick="this.closest('.env-row').remove()">
            <i class="fas fa-times"></i>
        </button>`;
    el.appendChild(row);
}

function ctCollectEnvVars() {
    const result = {};
    document.querySelectorAll('#ctEnvVarRows .env-row').forEach(row => {
        const k = row.querySelector('.ct-env-key').value.trim();
        const v = row.querySelector('.ct-env-val').value;
        if (k) result[k] = v;
    });
    return result;
}

async function ctLoadVenvStatus(tid) {
    const statusEl = document.getElementById('ctVenvStatus');
    if (!statusEl) return;
    statusEl.innerHTML = '<span class="text-muted" style="font-size:.75rem;">Checking venv…</span>';
    try {
        const res  = await fetch(`/api/agenthub/custom-tools/${tid}/venv-status`);
        const data = await res.json();
        if (data.created) {
            statusEl.innerHTML = `
                <span class="badge-enabled">Venv Active</span>
                <span class="text-muted ms-2" style="font-size:.75rem;">${esc(data.py_version)} &middot; ${data.size_mb} MB</span>
                <button class="btn btn-link text-danger ms-2 p-0" style="font-size:.75rem;"
                        onclick="ctDeleteVenv()">Delete Venv</button>`;
            document.getElementById('ctInstallBtn').innerHTML =
                '<i class="fas fa-sync-alt me-1"></i>Reinstall Packages';
        } else {
            statusEl.innerHTML = `
                <span class="badge bg-secondary" style="font-size:.7rem;font-weight:500;">No Venv</span>
                <span class="text-muted ms-2" style="font-size:.75rem;">Click Install to create an isolated environment</span>`;
            document.getElementById('ctInstallBtn').innerHTML =
                '<i class="fas fa-plus me-1"></i>Create Venv + Install';
        }
    } catch { statusEl.innerHTML = ''; }
}

async function ctDeleteVenv() {
    if (!_ctActiveTool) return;
    if (!confirm('Delete the virtual environment? Packages will need to be reinstalled.')) return;
    const res = await fetch(`/api/agenthub/custom-tools/${_ctActiveTool.id}/venv`, { method: 'DELETE' });
    const d   = await res.json();
    if (d.success) {
        showToast('Venv deleted', 'info');
        ctLoadVenvStatus(_ctActiveTool.id);
    } else {
        showToast('Delete failed: ' + d.error, 'error');
    }
}

async function ctSaveTool() {
    const data = ctCollectFormData();
    if (!data.name)         { showToast('Tool name is required', 'error'); return; }
    if (!data.display_name) { data.display_name = data.name; }

    const btn = document.getElementById('ctSaveBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving…';

    try {
        const url    = _ctEditMode ? `/api/agenthub/custom-tools/${_ctActiveTool.id}` : '/api/agenthub/custom-tools';
        const method = _ctEditMode ? 'PUT' : 'POST';
        const res    = await fetch(url, {
            method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
        });
        const result = await res.json();
        if (!res.ok) { showToast(result.error || 'Save failed', 'error'); return; }

        _ctActiveTool = result;
        _ctEditMode   = true;
        showToast('Tool saved!', 'success');
        ctPopulateForm(result);
        await ctLoadTools();
        ctRenderList();
    } catch(e) {
        showToast('Network error', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-save me-2"></i>Save Tool';
    }
}

async function ctToggleTool() {
    if (!_ctActiveTool) return;
    const res  = await fetch(`/api/agenthub/custom-tools/${_ctActiveTool.id}/toggle`, { method: 'POST' });
    const tool = await res.json();
    _ctActiveTool = tool;
    ctPopulateForm(tool);
    await ctLoadTools();
    ctRenderList();
    showToast(tool.enabled ? 'Tool enabled' : 'Tool disabled', 'info');
}

async function ctDeleteTool() {
    if (!_ctActiveTool) return;
    if (!confirm(`Delete tool "${_ctActiveTool.display_name}"? This cannot be undone.`)) return;
    const res = await fetch(`/api/agenthub/custom-tools/${_ctActiveTool.id}`, { method: 'DELETE' });
    if (res.ok) {
        showToast('Tool deleted', 'info');
        _ctActiveTool = null;
        _ctEditMode   = false;
        document.getElementById('ctPlaceholder').style.display = '';
        document.getElementById('ctForm').style.display        = 'none';
        await ctLoadTools();
        ctRenderList();
    } else {
        showToast('Delete failed', 'error');
    }
}

async function ctInstallPackages() {
    if (!_ctActiveTool) { showToast('Save the tool first', 'error'); return; }
    const btn    = document.getElementById('ctInstallBtn');
    const output = document.getElementById('ctInstallOutput');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Installing…';
    output.style.display = 'block';
    output.textContent   = 'Running pip install…';

    try {
        const res    = await fetch(`/api/agenthub/custom-tools/${_ctActiveTool.id}/install`, { method: 'POST' });
        const result = await res.json();
        output.textContent = result.output || '(no output)';
        if (result.success) {
            showToast('Packages installed!', 'success');
            if (_ctActiveTool) ctLoadVenvStatus(_ctActiveTool.id);
        } else {
            showToast('Some packages failed — see output above', 'error');
        }
    } catch(e) {
        output.textContent = 'Network error: ' + e;
        showToast('Install error', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-download me-1"></i>Install Packages';
    }
}

function ctOpenTestModal() {
    if (!_ctActiveTool) return;
    const schema = _ctActiveTool.input_schema || [];
    document.getElementById('ctTestModalName').textContent = _ctActiveTool.display_name || _ctActiveTool.name;
    document.getElementById('ctTestResultWrap').style.display = 'none';

    const inputsEl = document.getElementById('ctTestInputs');
    if (!schema.length) {
        inputsEl.innerHTML = '<p class="text-muted small">This tool has no input parameters.</p>';
    } else {
        inputsEl.innerHTML = schema.map(p => `
            <div class="mb-2">
                <label class="form-label fw-semibold" style="font-size:.83rem;">
                    ${esc(p.name)}
                    <span class="text-muted fw-normal">(${esc(p.type)}${p.required ? ', required' : ''})</span>
                </label>
                ${p.description ? `<div class="form-text mb-1">${esc(p.description)}</div>` : ''}
                <input type="text" class="form-control form-control-sm ct-test-input"
                       data-param="${esc(p.name)}"
                       placeholder="${esc(p.type === 'boolean' ? 'true / false' : p.type === 'integer' ? '0' : '')}">
            </div>
        `).join('');
    }

    new bootstrap.Modal(document.getElementById('ctTestModal')).show();
}

async function ctRunTest() {
    if (!_ctActiveTool) return;
    const schema = _ctActiveTool.input_schema || [];
    const params = {};

    document.querySelectorAll('.ct-test-input').forEach(inp => {
        const key  = inp.dataset.param;
        let   val  = inp.value.trim();
        const pDef = schema.find(p => p.name === key);
        const type = pDef ? pDef.type : 'string';
        if (type === 'integer')       val = parseInt(val, 10);
        else if (type === 'float')    val = parseFloat(val);
        else if (type === 'boolean')  val = val.toLowerCase() === 'true';
        else {
            try { val = JSON.parse(val); } catch { /* keep as string */ }
        }
        if (key) params[key] = val;
    });

    const btn  = document.getElementById('ctRunTestBtn');
    const wrap = document.getElementById('ctTestResultWrap');
    const pre  = document.getElementById('ctTestResult');
    const badge = document.getElementById('ctTestBadge');

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Running…';
    wrap.style.display = 'none';

    try {
        const res    = await fetch(`/api/agenthub/custom-tools/${_ctActiveTool.id}/test`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params)
        });
        const result = await res.json();
        pre.textContent    = JSON.stringify(result, null, 2);
        badge.className    = result.success ? 'badge-enabled' : 'badge-disabled';
        badge.textContent  = result.success ? 'Success' : 'Failed';
        wrap.style.display = '';
    } catch(e) {
        pre.textContent    = 'Network error: ' + e;
        badge.className    = 'badge-disabled';
        badge.textContent  = 'Error';
        wrap.style.display = '';
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-play me-2"></i>Run Test';
    }
}

// ══════════════════════════════════════════════════════════════
// TOOL JOBS
// ══════════════════════════════════════════════════════════════
let _tjJobs        = [];
let _tjActive      = null;
let _tjEditMode    = false;
let _tjCustomTools = [];

async function initToolJobs() {
    await Promise.all([loadToolJobs(), _tjLoadCustomTools()]);
    tjPopulateToolSelect();
}

async function _tjLoadCustomTools() {
    try {
        const res    = await fetch('/api/agenthub/custom-tools');
        const data   = await res.json();
        _tjCustomTools = (data.tools || data || []).filter(t => t.enabled !== false);
    } catch { _tjCustomTools = []; }
}

async function loadToolJobs() {
    try {
        const res  = await fetch('/api/agenthub/tool-jobs');
        const data = await res.json();
        _tjJobs = Array.isArray(data) ? data : (data.jobs || []);
        tjRenderList();
    } catch (e) {
        console.error('loadToolJobs', e);
    }
}

function tjPopulateToolSelect() {
    const sel = document.getElementById('tjTool');
    if (!sel) return;
    while (sel.options.length > 1) sel.remove(1);
    if (!_tjCustomTools.length) {
        const opt = document.createElement('option');
        opt.disabled = true;
        opt.textContent = '— no custom tools found —';
        sel.appendChild(opt);
        return;
    }
    _tjCustomTools.forEach(t => {
        const opt = document.createElement('option');
        opt.value       = t.name;
        opt.textContent = t.display_name || t.name;
        sel.appendChild(opt);
    });
}

function tjRenderList() {
    const el    = document.getElementById('tjJobList');
    const count = document.getElementById('tjJobCount');
    if (!el) return;
    count.textContent = _tjJobs.length;
    if (!_tjJobs.length) {
        el.innerHTML = '<div class="tj-empty"><i class="fas fa-tools mb-2 d-block"></i>No jobs yet.<br>Click <strong>+ New Job</strong> to create one.</div>';
        return;
    }
    el.innerHTML = _tjJobs.map(j => {
        const active  = _tjActive && _tjActive.id === j.id;
        const icon    = j.status === 'active' ? '🟢' : '⏸️';
        const lastRun = j.last_run_at ? _tjRelTime(j.last_run_at) : 'never run';
        return `
        <div class="tj-job-item${active ? ' active' : ''}" onclick="tjSelect('${j.id}')">
            <div class="tj-job-icon">${icon}</div>
            <div class="tj-job-meta">
                <div class="tj-job-name">${esc(j.name)}</div>
                <div class="tj-job-sub">${esc(j.tool_name || '—')} · ${lastRun}</div>
            </div>
        </div>`;
    }).join('');
}

function _tjRelTime(dtStr) {
    if (!dtStr) return 'never';
    const diff = Math.round((Date.now() - new Date(dtStr + 'Z')) / 1000);
    if (diff < 60)   return diff + 's ago';
    if (diff < 3600) return Math.round(diff/60) + 'm ago';
    if (diff < 86400)return Math.round(diff/3600) + 'h ago';
    return Math.round(diff/86400) + 'd ago';
}

async function tjSelect(jid) {
    try {
        const res = await fetch(`/api/agenthub/tool-jobs/${jid}`);
        const job = await res.json();
        _tjActive  = job;
        _tjEditMode = true;
        tjRenderList();
        tjPopulateForm(job);
    } catch(e) {
        showToast('Failed to load job', 'error');
    }
}

function tjNew() {
    _tjActive  = null;
    _tjEditMode = false;
    tjRenderList();
    document.getElementById('tjPlaceholder').style.display = 'none';
    document.getElementById('tjForm').style.display        = '';
    document.getElementById('tjName').value     = '';
    document.getElementById('tjTool').value     = '';
    document.getElementById('tjSchedule').value = '0 9 * * *';
    document.getElementById('tjEnvVarRows').innerHTML = '';
    document.getElementById('tjParamSection').style.display = 'none';
    document.getElementById('tjParamFields').innerHTML = '';
    document.getElementById('tjLastRunSection').style.display = 'none';
    document.getElementById('tjRunNowBtn').style.display  = 'none';
    document.getElementById('tjToggleBtn').style.display  = 'none';
    document.getElementById('tjDeleteBtn').style.display  = 'none';
}

function tjPopulateForm(job) {
    document.getElementById('tjPlaceholder').style.display = 'none';
    document.getElementById('tjForm').style.display        = '';
    document.getElementById('tjName').value     = job.name || '';
    document.getElementById('tjTool').value     = job.tool_name || '';
    document.getElementById('tjSchedule').value = job.schedule || '0 9 * * *';

    // Env vars
    const envEl = document.getElementById('tjEnvVarRows');
    envEl.innerHTML = '';
    const envVars = job.tool_env_vars || {};
    Object.entries(envVars).forEach(([k, v]) => tjAddEnvVar(k, v));

    // Param fields
    tjOnToolChange(job.tool_params || {});

    // Last run panel
    if (job.last_run_at || job.run_count) {
        document.getElementById('tjLastRunSection').style.display = '';
        const badge = document.getElementById('tjLastRunStatusBadge');
        const st    = job.last_run_status || '';
        badge.className = `badge-${st || 'secondary'}`;
        badge.textContent = st || 'Unknown';

        document.getElementById('tjLastRunAt').textContent   = job.last_run_at ? _tjRelTime(job.last_run_at) : '';
        document.getElementById('tjRunCount').textContent    = `${job.run_count || 0} total run${job.run_count === 1 ? '' : 's'}`;

        const outputWrap = document.getElementById('tjLastRunOutputWrap');
        if (job.last_run_output) {
            document.getElementById('tjLastRunOutput').textContent = job.last_run_output;
            outputWrap.style.display = '';
        } else {
            outputWrap.style.display = 'none';
        }
    } else {
        document.getElementById('tjLastRunSection').style.display = 'none';
    }

    // Action buttons
    document.getElementById('tjRunNowBtn').style.display  = '';
    document.getElementById('tjDeleteBtn').style.display  = '';
    const toggleBtn = document.getElementById('tjToggleBtn');
    toggleBtn.style.display = '';
    if (job.status === 'active') {
        toggleBtn.innerHTML = '<i class="fas fa-pause me-2"></i>Pause';
        toggleBtn.className = 'btn btn-outline-warning';
    } else {
        toggleBtn.innerHTML = '<i class="fas fa-play me-2"></i>Resume';
        toggleBtn.className = 'btn btn-outline-success';
    }
}

function tjOnToolChange(existingParams) {
    const toolName = document.getElementById('tjTool').value;
    const section  = document.getElementById('tjParamSection');
    const fields   = document.getElementById('tjParamFields');
    if (!toolName) { section.style.display = 'none'; fields.innerHTML = ''; return; }

    const tool = _tjCustomTools.find(t => t.name === toolName);
    const rawSchema = tool ? (tool.input_schema || tool.schema || []) : [];
    // input_schema is [{name, type, required, description}], convert to {name: def} map
    const schema = Array.isArray(rawSchema)
        ? Object.fromEntries(rawSchema.map(p => [p.name, { type: p.type || 'string', required: p.required || false, description: p.description || '' }]))
        : rawSchema;
    const paramList = Object.entries(schema).filter(([k]) => !['api_key','_db_query','_hub_ctx'].includes(k));

    if (!paramList.length) { section.style.display = 'none'; fields.innerHTML = ''; return; }

    section.style.display = '';
    const vals = existingParams || {};
    fields.innerHTML = paramList.map(([pName, pDef]) => {
        const type = pDef.type || 'string';
        const req  = pDef.required ? '<span class="text-danger">*</span>' : '';
        const desc = pDef.description ? `<div class="form-text mb-1">${esc(pDef.description)}</div>` : '';
        const val  = vals[pName] !== undefined ? vals[pName] : '';

        let input;
        if (type === 'boolean') {
            input = `<select class="form-select form-select-sm tj-param-input" data-param="${esc(pName)}" data-type="${type}">
                <option value="true"${val === true || val === 'true' ? ' selected' : ''}>true</option>
                <option value="false"${val === false || val === 'false' ? ' selected' : ''}>false</option>
            </select>`;
        } else if (type === 'object' || type === 'array') {
            const strVal = val ? (typeof val === 'string' ? val : JSON.stringify(val, null, 2)) : '';
            input = `<textarea class="form-control form-control-sm tj-param-input" rows="3"
                        data-param="${esc(pName)}" data-type="${type}"
                        placeholder="JSON">${esc(strVal)}</textarea>`;
        } else if (type === 'integer' || type === 'float' || type === 'number') {
            input = `<input type="number" class="form-control form-control-sm tj-param-input"
                        data-param="${esc(pName)}" data-type="${type}"
                        step="${type === 'integer' ? '1' : 'any'}" value="${esc(String(val))}">`;
        } else {
            input = `<input type="text" class="form-control form-control-sm tj-param-input"
                        data-param="${esc(pName)}" data-type="${type}" value="${esc(String(val))}">`;
        }

        return `<div class="mb-2">
            <label class="form-label fw-semibold" style="font-size:.83rem;">${esc(pName)} ${req}
                <span class="text-muted fw-normal" style="font-size:.78rem;">(${type})</span>
            </label>
            ${desc}${input}
        </div>`;
    }).join('');
}

function tjCollectParams() {
    const params = {};
    document.querySelectorAll('.tj-param-input').forEach(el => {
        const key  = el.dataset.param;
        const type = el.dataset.type;
        let   val  = el.value.trim();
        if (!key) return;
        if (type === 'integer')          val = parseInt(val, 10);
        else if (type === 'float' || type === 'number') val = parseFloat(val);
        else if (type === 'boolean')     val = val === 'true';
        else if (type === 'object' || type === 'array') {
            try { val = JSON.parse(val); } catch { /* keep string */ }
        }
        params[key] = val;
    });
    return params;
}

function tjAddEnvVar(key, value) {
    const row = document.createElement('div');
    row.className = 'env-row';
    row.innerHTML = `
        <input type="text"     class="form-control form-control-sm tj-env-key"   placeholder="VAR_NAME"  value="${esc(key   || '')}">
        <input type="password" class="form-control form-control-sm tj-env-val"   placeholder="value"     value="${esc(value || '')}">
        <button type="button" class="btn btn-sm btn-outline-danger" onclick="this.closest('.env-row').remove()">
            <i class="fas fa-times"></i>
        </button>`;
    document.getElementById('tjEnvVarRows').appendChild(row);
}

function tjCollectEnvVars() {
    const vars = {};
    document.querySelectorAll('.tj-env-key').forEach(keyEl => {
        const k = keyEl.value.trim();
        const v = keyEl.nextElementSibling.value;
        if (k) vars[k] = v;
    });
    return vars;
}

function tjCollectFormData() {
    return {
        name:             document.getElementById('tjName').value.trim(),
        tool_name:        document.getElementById('tjTool').value,
        tool_params:      tjCollectParams(),
        tool_env_vars:    tjCollectEnvVars(),
        schedule:         document.getElementById('tjSchedule').value.trim() || '0 9 * * *',
    };
}

async function tjSave() {
    const data = tjCollectFormData();
    if (!data.name)      { showToast('Job name is required', 'error'); return; }
    if (!data.tool_name) { showToast('Please select a tool', 'error'); return; }

    const btn = document.getElementById('tjSaveBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Saving…';

    try {
        const url    = _tjEditMode ? `/api/agenthub/tool-jobs/${_tjActive.id}` : '/api/agenthub/tool-jobs';
        const method = _tjEditMode ? 'PUT' : 'POST';
        const res    = await fetch(url, {
            method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
        });
        const result = await res.json();
        if (!res.ok) { showToast(result.error || 'Save failed', 'error'); return; }
        _tjActive   = result;
        _tjEditMode = true;
        showToast('Job saved!', 'success');
        tjPopulateForm(result);
        await loadToolJobs();
    } catch(e) {
        showToast('Network error', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-save me-2"></i>Save Job';
    }
}

async function tjToggle() {
    if (!_tjActive) return;
    const res = await fetch(`/api/agenthub/tool-jobs/${_tjActive.id}/toggle`, { method: 'POST' });
    const job = await res.json();
    _tjActive = job;
    tjPopulateForm(job);
    await loadToolJobs();
    showToast(job.status === 'active' ? 'Job resumed' : 'Job paused', 'info');
}

async function tjDelete() {
    if (!_tjActive) return;
    if (!confirm(`Delete job "${_tjActive.name}"? This cannot be undone.`)) return;
    const res = await fetch(`/api/agenthub/tool-jobs/${_tjActive.id}`, { method: 'DELETE' });
    if (res.ok) {
        showToast('Job deleted', 'info');
        _tjActive   = null;
        _tjEditMode = false;
        document.getElementById('tjPlaceholder').style.display = '';
        document.getElementById('tjForm').style.display        = 'none';
        await loadToolJobs();
    } else {
        showToast('Delete failed', 'error');
    }
}

async function tjRunNow() {
    if (!_tjActive) return;
    const btn = document.getElementById('tjRunNowBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Running…';
    try {
        const res  = await fetch(`/api/agenthub/tool-jobs/${_tjActive.id}/run`, { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
            showToast('Job started — refresh in a moment to see results', 'success');
            // poll status after 3 seconds
            setTimeout(async () => {
                try {
                    const sr   = await fetch(`/api/agenthub/tool-jobs/${_tjActive.id}/status`);
                    const info = await sr.json();
                    if (_tjActive && info.id === _tjActive.id) {
                        _tjActive = info;
                        tjPopulateForm(info);
                        tjRenderList();
                    }
                } catch { /* silent */ }
            }, 3500);
        } else {
            showToast(data.error || 'Run failed', 'error');
        }
    } catch(e) {
        showToast('Network error', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-play me-2"></i>Run Now';
    }
}

function tjSetCronPreset(cron) {
    document.getElementById('tjSchedule').value = cron;
}
