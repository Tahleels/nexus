// static/js/agents.js - FINAL OPTIMIZED VERSION
console.log("🔥 agents.js FILE LOADED");

function _getBiActiveOrgScope() {
    const p = new URLSearchParams(window.location.search);
    const deptId    = p.get('dept_id');
    const projectId = p.get('project_id');
    if (projectId) return { scope_type: 'project',    scope_id: +projectId };
    if (deptId)    return { scope_type: 'department',  scope_id: +deptId   };
    return {};
}
class AgentManager {

    constructor() {
        this.currentConnection = null;
        this.selectedTables = new Set();
        this.selectedColumns = {};
        this.currentChatAgent = null;
        this.currentAgentType = 'bi';
        this.chatHistory = [];
        this.currentChatType = null;
        this.currentAgentId = null;
        this.currentSessionId      = null;   // persists session across messages for follow-up context
        this.currentConversationId = null;   // persists DB conversation across messages
        this._biHistoryVisible     = false;  // history panel toggle state

        this.initializeEventListeners();
    }

    initializeEventListeners() {
        document.getElementById('agentConnections')?.addEventListener('change', (e) => {
            this.onConnectionSelect(e.target.value);
        });

        document.getElementById('generateSchemaBtn')?.addEventListener('click', () => {
            this.generateSchemaContext();
        });

        document.getElementById('createAgentBtn')?.addEventListener('click', () => {
            this.createAgent();
        });

        document.getElementById('agentName')?.addEventListener('input', () => {
            this.updateCreateAgentButton();
        });

        document.getElementById('agentDescription')?.addEventListener('input', () => {
            this.updateCreateAgentButton();
        });

        document.getElementById('sendChatMessageBtn')?.addEventListener('click', () => {
            this.sendChatMessage();
        });

        document.getElementById('clearChatBtn')?.addEventListener('click', () => {
            this.clearChat();
        });

        document.getElementById('chatInput')?.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.sendChatMessage();
        });

        if (document.getElementById('agents-tbody')) {
            this.loadBIAgents();
            // Re-render when org context changes (sidebar selector)
            window.addEventListener('orgContextChange', () => {
                if (this._cachedAgents) this.renderAgents(this._cachedAgents);
            });
        }
    }

    initializeContainerEvents() {
        console.log("initializeContainerEvents() placeholder");
    }

    // ========== SIDEBAR CHAT FUNCTIONS ==========

    openAgentChat(type, agentId, agentName) {
        this.currentChatType = type;
        this.currentAgentId = agentId;
        this.currentAgentType = type;

        const agentList = document.getElementById(type + '-agents-list');
        const chatDiv = document.getElementById(type + '-agent-chat');
        const chatTitle = document.getElementById(type + '-chat-title');
        const messagesDiv = document.getElementById(type + '-chat-messages');

        if (agentList) agentList.classList.add('hidden');
        if (chatDiv) chatDiv.classList.remove('hidden');
        if (chatTitle) chatTitle.textContent = `Chat with ${agentName}`;

        const welcomeMessage = type === 'bi' ?
            "Hello! I'm your BI Agent. Ask me anything about your business data using natural language." :
            "Hello! I'm your Reasoning Agent. I can help you with complex analysis and decision-making.";

        if (messagesDiv) {
            messagesDiv.innerHTML = `
                <div class="message">
                    <div class="message-content">
                        <div class="welcome-message">
                            <strong>🤖 ${type === 'bi' ? 'BI' : 'Reasoning'} Assistant</strong>
                            <p class="mb-0">${welcomeMessage}</p>
                        </div>
                    </div>
                </div>
            `;
        }

        const input = document.getElementById(type + '-message-input');
        if (input) input.focus();
    }

    closeAgentChat() {
        if (this.currentChatType) {
            const agentList = document.getElementById(this.currentChatType + '-agents-list');
            const chatDiv = document.getElementById(this.currentChatType + '-agent-chat');

            if (agentList) agentList.classList.remove('hidden');
            if (chatDiv) chatDiv.classList.add('hidden');

            this.currentChatType = null;
            this.currentAgentId = null;
        }
    }

    sendSidebarMessage(type) {
        const input = document.getElementById(type + '-message-input');
        if (!input) return;

        const message = input.value.trim();
        if (message === '') return;

        const messagesDiv = document.getElementById(type + '-chat-messages');
        if (messagesDiv) {
            const userMessageDiv = document.createElement('div');
            userMessageDiv.className = 'message user';
            userMessageDiv.innerHTML = `<div class="message-content">${message}</div>`;
            messagesDiv.appendChild(userMessageDiv);

            input.value = '';
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
            this.sendChatMessageToBackend(this.currentAgentId, message, type);
        }
    }

    displaySidebarResponse(type, response) {
        const messagesDiv = document.getElementById(type + '-chat-messages');
        if (messagesDiv) {
            const agentMessageDiv = document.createElement('div');
            agentMessageDiv.className = 'message';

            if (typeof response === 'object' && response !== null) {
                agentMessageDiv.innerHTML = this.formatStructuredResponse(response);
            } else {
                agentMessageDiv.innerHTML = `<div class="message-content">${response}</div>`;
            }

            messagesDiv.appendChild(agentMessageDiv);
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }
    }

    // ========== MODAL CHAT FUNCTIONS ==========

    openChatWithAgent(agentName, agentType = 'bi') {
        this.currentChatAgent      = agentName;
        this.currentAgentType      = agentType;
        this.chatHistory           = [];
        this.currentSessionId      = null;   // fresh session for each new agent conversation
        this.currentConversationId = null;   // fresh DB conversation
        this._biHistoryVisible     = false;
        // close history panel if open from a previous agent
        document.getElementById('biConvoPanel')?.classList.add('bi-convo-hidden');

        const title = agentType === 'bi' ? `BI Agent: ${agentName}` : `Reasoning Agent: ${agentName}`;
        document.getElementById('chatModalTitle').textContent = title;

        const welcomeMessage = agentType === 'bi' ?
            "Hello! I'm your BI Agent. Ask me anything about your business data using natural language." :
            "Hello! I'm your Reasoning Agent. I can help you with complex analysis and decision-making.";

        document.getElementById('chatMessages').innerHTML = `
            <div class="message">
                <div class="message-content">
                    <div class="welcome-message">
                        <strong>🤖 ${agentType === 'bi' ? 'BI' : 'Reasoning'} Assistant</strong>
                        <p class="mb-0">${welcomeMessage}</p>
                    </div>
                </div>
            </div>
        `;

        new bootstrap.Modal(document.getElementById('chatModal')).show();
        // Pre-load conversation history list in background
        this._biLoadConversations();
    }

    async sendChatMessage() {
        const input = document.getElementById('chatInput');
        const message = input.value.trim();
        if (!message || !this.currentChatAgent) return;

        this.addChatMessage('user', message);
        input.value = '';

        try {
            const response = await fetch('/api/bi-agents/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    agent_name:      this.currentChatAgent,
                    question:        message,
                    chat_history:    this.chatHistory,
                    session_id:      this.currentSessionId,
                    conversation_id: this.currentConversationId,
                    ..._getBiActiveOrgScope(),
                })
            });

            const result = await response.json();

            if (response.status === 429 || result.error_type === 'token_limit_exceeded') {
                // Token limit hit — show a friendly in-chat message
                const usage = result.token_usage || {};
                const used = (usage.used_today || 0).toLocaleString();
                const limit = (usage.limit || 0).toLocaleString();
                this.addChatMessage('agent', `
                    <div style="
                        background:#fef2f2; border:1px solid #fecaca;
                        border-left:4px solid #ef4444;
                        border-radius:8px; padding:14px 16px;">
                        <div style="font-weight:700;color:#b91c1c;margin-bottom:6px;">
                            🚫 Daily token limit reached
                        </div>
                        <div style="font-size:13px;color:#7f1d1d;">
                            You have used <strong>${used}</strong> of your
                            <strong>${limit}</strong> daily tokens.
                            Your limit resets at <strong>midnight UTC</strong>.
                        </div>
                    </div>`);
                return;
            }

            if (response.ok) {
                this.addChatMessage('agent', this.formatStructuredResponse(result));
                // Only overwrite lastQueryResult when there is actual data — conversational/follow-up
                // replies have no rows, so we preserve the previous data result for the presentation button.
                if (!result.is_conversational && result.data && result.data.length > 0) {
                    window.agentManager.lastQueryResult = result;
                }

                // Persist session_id so subsequent messages share context (follow-ups, etc.)
                if (result.session_id) this.currentSessionId = result.session_id;
                // Persist conversation_id for DB history
                if (result.conversation_id) {
                    const isNew = !this.currentConversationId;
                    this.currentConversationId = result.conversation_id;
                    if (isNew) this._biLoadConversations();  // refresh list on first message
                }

                this.chatHistory.push({ role: 'user', content: message });
                this.chatHistory.push({
                    role: 'assistant',
                    content: result.is_conversational ? (result.analysis || '') : (result.sql_query || '')
                });

            } else {
                this.addChatMessage('error', `Error: ${result.message || 'Unknown error'}`);
            }
        } catch (error) {
            this.addChatMessage('error', 'Sorry, I encountered an error. Please try again.');
        }
    }

    async sendChatMessageToBackend(agentId, message, type) {
        try {
            const response = await fetch('/api/bi-agents/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    agent_id: agentId,
                    question: message,
                    agent_config: this.getAgentConfig(agentId),
                    connection_name: this.getConnectionName(agentId),
                    session_id: this.getSessionId(agentId)
                })
            });

            const data = await response.json();

            if (response.ok) {
                this.displaySidebarResponse(type, data);
            } else {
                this.displaySidebarResponse(type, {
                    success: false,
                    error: data.error || 'Unknown error occurred',
                    response: data.response || 'Failed to process query'
                });
            }
        } catch (error) {
            this.displaySidebarResponse(type, {
                success: false,
                error: 'Network error',
                response: 'Failed to send message. Please check your connection.'
            });
        }
    }

    addChatMessage(role, content) {
        const chatMessages = document.getElementById('chatMessages');
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role === 'user' ? 'user' : ''}`;
        messageDiv.innerHTML = `<div class="message-content">${content}</div>`;

        chatMessages.appendChild(messageDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    clearChat() {
        document.getElementById('chatMessages').innerHTML = `
            <div class="message">
                <div class="message-content">
                    <div class="welcome-message">
                        <strong>🤖 ${this.currentAgentType === 'bi' ? 'BI' : 'Reasoning'} Assistant</strong>
                        <p class="mb-0">Ask natural language questions about your data.</p>
                    </div>
                </div>
            </div>
        `;
        this.chatHistory           = [];
        this.currentSessionId      = null;  // reset server session so next query fetches fresh data
        this.currentConversationId = null;  // start a new DB conversation on next send
    }

    // ========== BI CONVERSATION HISTORY ==========

    async _biLoadConversations() {
        const el = document.getElementById('biConvoList');
        if (!el) return;
        const agent = this.currentChatAgent || '';
        try {
            const res  = await fetch(`/api/bi-agents/conversations?agent_name=${encodeURIComponent(agent)}`);
            const rows = await res.json();
            if (!rows.length) {
                el.innerHTML = '<div class="text-center text-muted py-3" style="font-size:.78rem;">No conversations yet</div>';
                return;
            }
            const esc = s => String(s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
            el.innerHTML = rows.map(c => `
                <div class="bi-convo-item ${this.currentConversationId===c.id?'active':''}" data-cid="${c.id}">
                    <span class="bi-convo-title" onclick="window._biLoadConversation('${c.id}')" title="${esc(c.title)}">${esc(c.title||'Untitled')}</span>
                    <button class="bi-convo-del" onclick="window._biDeleteConvo('${c.id}')" title="Delete"><i class="fas fa-trash"></i></button>
                </div>`).join('');
        } catch {
            el.innerHTML = '<div class="text-center text-danger py-2" style="font-size:.78rem;">Failed to load</div>';
        }
    }

    /**
     * Pre-seed window._dashboardCache / _reportCache / _infographicCache (see
     * modals.html) from server-persisted artifacts so this conversation's
     * dashboard/report/infographic buttons hit cache instead of regenerating
     * via the LLM. `artifacts` is `{dashboard|report|infographic: {cache_key,
     * payload}}` as returned by GET /api/bi-agents/conversations/<id> — payload
     * is each generate endpoint's own raw response, stored verbatim.
     */
    _seedGeneratedArtifactCaches(artifacts) {
        if (!artifacts) return;
        window._dashboardCache   = window._dashboardCache   || new Map();
        window._reportCache      = window._reportCache      || {};
        window._infographicCache = window._infographicCache || {};

        if (artifacts.dashboard) {
            window._dashboardCache.set(artifacts.dashboard.cache_key, {
                config:  artifacts.dashboard.payload.config,
                rawData: artifacts.dashboard.payload.rawData,
            });
        }
        if (artifacts.report) {
            // Mirror the {type, config, columns, rows} shape generateReport()'s
            // cache-miss branch derives from a fresh response, so a cache hit
            // renders identically either way.
            const rows = artifacts.report.payload.raw_data || [];
            window._reportCache[artifacts.report.cache_key] = {
                type:    "reportData",
                config:  artifacts.report.payload.report_config?.header || {},
                columns: rows.length > 0 ? Object.keys(rows[0]) : [],
                rows:    rows,
            };
        }
        if (artifacts.infographic) {
            window._infographicCache[artifacts.infographic.cache_key] = {
                infographic: artifacts.infographic.payload.infographic,
                timestamp:   new Date(),
            };
        }
    }

    async _biLoadConversationById(cid) {
        try {
            const res  = await fetch(`/api/bi-agents/conversations/${cid}`);
            const data = await res.json();
            if (!data || !data.messages) return;

            // Set active agent if different
            if (data.agent_name && data.agent_name !== this.currentChatAgent) {
                this.currentChatAgent = data.agent_name;
            }
            this.currentConversationId = cid;
            this.currentSessionId      = null;  // fresh NLQ session for this loaded conversation
            this.chatHistory           = [];

            const msgs = document.getElementById('chatMessages');
            msgs.innerHTML = '';

            // Pre-seed the dashboard/report/infographic caches (see modals.html)
            // with anything already generated for this conversation, so clicking
            // Generate again reuses it instead of re-calling the LLM. Uses the
            // literal cache_key stored server-side — see _seedGeneratedArtifactCaches.
            this._seedGeneratedArtifactCaches(data.artifacts);

            // Find the last SQL-query message — its re-executed result becomes
            // window.agentManager.lastQueryResult, matching "the most recent
            // query" semantics the Generate buttons already rely on.
            const sqlMsgIndexes = (data.messages || [])
                .map((m, i) => (m.role !== 'user' && m.sql_query ? i : -1))
                .filter(i => i >= 0);
            const lastSqlMsgIndex = sqlMsgIndexes.length ? sqlMsgIndexes[sqlMsgIndexes.length - 1] : -1;

            let lastUserQuestion = '';
            data.messages.forEach((m, idx) => {
                const isLastSqlMsg = idx === lastSqlMsgIndex;
                if (m.role === 'user') {
                    lastUserQuestion = m.content;
                    this.addChatMessage('user', m.content);
                    this.chatHistory.push({ role: 'user', content: m.content });
                } else if (m.sql_query) {
                    // Re-execute the stored SQL to get fresh data
                    const placeholder = document.createElement('div');
                    placeholder.className = 'message';
                    placeholder.innerHTML = `<div class="message-content"><span class="text-muted" style="font-size:.85rem;">⟳ Re-running query…</span></div>`;
                    document.getElementById('chatMessages').appendChild(placeholder);

                    const questionForThisMsg = lastUserQuestion;
                    fetch('/api/bi-agents/execute-sql', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ agent_name: data.agent_name, sql_query: m.sql_query }),
                    })
                    .then(r => r.json())
                    .then(execResult => {
                        const fakeResult = {
                            success:    execResult.success !== false,
                            analysis:   m.content,
                            sql_query:  m.sql_query,
                            columns:    execResult.columns || [],
                            data:       execResult.data    || [],
                            row_count:  execResult.row_count || 0,
                            error:      execResult.error   || '',
                            is_conversational: false,
                            question:   questionForThisMsg,
                            agent_name: data.agent_name || '',
                            // Synthetic single-entry insights (not the original structured
                            // list) — enough to satisfy generateInfographic()'s "has insights"
                            // guard and to compute a matching cache key without an LLM call.
                            // Only used as real generation input on a cache MISS.
                            insights:   m.content ? [{ message: m.content }] : [],
                            execution_time_ms: 0,
                        };
                        const div = document.createElement('div');
                        div.className = 'message';
                        div.innerHTML = `<div class="message-content">${this.formatStructuredResponse(fakeResult)}</div>`;
                        placeholder.replaceWith(div);
                        document.getElementById('chatMessages').scrollTop = 9999;

                        if (isLastSqlMsg && fakeResult.data.length > 0) {
                            window.agentManager.lastQueryResult = fakeResult;
                        }
                    })
                    .catch(err => {
                        console.error('execute-sql failed:', err);
                        const mc = placeholder.querySelector('.message-content');
                        if (mc) mc.innerHTML = `<span class="text-danger" style="font-size:.85rem;">⚠ Could not re-run query: ${err.message || err}</span><br><code style="font-size:.75rem;">${m.sql_query}</code>`;
                    });
                    this.chatHistory.push({ role: 'assistant', content: m.content });
                } else {
                    // Conversational reply — render stored text directly
                    const fakeResult = { success: true, analysis: m.content, is_conversational: true, sql_query: '' };
                    this.addChatMessage('agent', this.formatStructuredResponse(fakeResult));
                    this.chatHistory.push({ role: 'assistant', content: m.content });
                }
            });

            // Highlight active conversation in list
            document.querySelectorAll('.bi-convo-item').forEach(el => {
                el.classList.toggle('active', el.dataset.cid === cid);
            });

            // Close history panel after loading
            this._biSetHistory(false);
        } catch (e) {
            console.error('Failed to load BI conversation', e);
        }
    }

    async _biDeleteConvo(cid) {
        if (!confirm('Delete this conversation?')) return;
        await fetch(`/api/bi-agents/conversations/${cid}`, { method: 'DELETE' });
        if (this.currentConversationId === cid) {
            this.currentConversationId = null;
            this.currentSessionId      = null;
            this.chatHistory           = [];
            document.getElementById('chatMessages').innerHTML =
                '<div class="alert alert-info"><small>Conversation deleted. Ask a new question to start fresh.</small></div>';
        }
        this._biLoadConversations();
    }

    _biSetHistory(visible) {
        this._biHistoryVisible = visible;
        const panel = document.getElementById('biConvoPanel');
        if (!panel) return;
        if (visible) {
            panel.classList.remove('bi-convo-hidden');
            this._biLoadConversations();
        } else {
            panel.classList.add('bi-convo-hidden');
        }
    }

    // ========== STRUCTURED RESPONSE FORMATTING ==========

    _parseMdTable(lines) {
        if (lines.length < 2) return null;
        const isSep = l => /^\|[\s\-:|]+\|/.test(l.trim());
        const sepIdx = lines.findIndex(isSep);
        if (sepIdx < 1) return null;
        const parseCells = l => l.trim().replace(/^\||\|$/g,'').split('|').map(c => c.trim());
        const headers  = parseCells(lines[0]);
        const dataRows = lines.slice(sepIdx + 1).filter(l => !isSep(l.trim()));
        if (!dataRows.length) return null;
        const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const thead = `<thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead>`;
        const tbody = `<tbody>${dataRows.map(r=>`<tr>${parseCells(r).map(c=>`<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody>`;
        return `<div class="table-container" style="margin:10px 0;"><table class="results-table">${thead}${tbody}</table></div>`;
    }

    formatInsightText(text) {
        if (!text) return "";

        let normalized = text
            .replace(/\r/g, '')
            .replace(/\n{3,}/g, '\n\n')
            .trim();

        const lines = normalized.split('\n');
        let html = '';
        let inList = false;
        let tableBuffer = [];

        const flushTable = () => {
            if (!tableBuffer.length) return;
            const tableHtml = this._parseMdTable(tableBuffer);
            if (tableHtml) {
                if (inList) { html += `</div>`; inList = false; }
                html += tableHtml;
            } else {
                // Not a valid table — render lines normally
                for (const tl of tableBuffer) {
                    if (inList) { html += `</div>`; inList = false; }
                    html += `<div style="font-size:13px;line-height:1.7;color:var(--color-text-secondary);margin:6px 0;">${tl}</div>`;
                }
            }
            tableBuffer = [];
        };

        for (let rawLine of lines) {
            let line = rawLine.trim();

            // Accumulate markdown table lines
            if (line.startsWith('|')) {
                tableBuffer.push(line);
                continue;
            }

            // Non-table line — flush any pending table first
            flushTable();

            if (!line) {
                if (inList) { html += `</div>`; inList = false; }
                continue;
            }

            if (/^\*\*(.+?)\*\*$/.test(line)) {
                if (inList) { html += `</div>`; inList = false; }
                const title = line.match(/^\*\*(.+?)\*\*$/)[1];
                html += `
                <div style="font-size:15px;font-weight:700;margin:18px 0 10px;color:var(--color-text-primary);border-bottom:1px solid var(--color-border-tertiary);padding-bottom:6px;">
                    ${title}
                </div>`;
                continue;
            }

            if (line.startsWith('- ') || line.startsWith('• ')) {
                if (!inList) { html += `<div style="display:flex;flex-direction:column;gap:8px;">`; inList = true; }
                line = line.replace(/^[-•]\s*/, '').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
                html += `
                <div style="display:flex;gap:10px;line-height:1.6;font-size:13px;color:var(--color-text-secondary);">
                    <span style="margin-top:1px;">•</span>
                    <span>${line}</span>
                </div>`;
                continue;
            }

            if (inList) { html += `</div>`; inList = false; }
            line = line.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
            html += `<div style="font-size:13px;line-height:1.7;color:var(--color-text-secondary);margin:6px 0;">${line}</div>`;
        }

        flushTable();
        if (inList) html += `</div>`;
        return html;
    }

    formatStructuredResponse(response) {
        if (response.is_conversational) {
            const prefix = response.is_followup
                ? `<div style="font-size:11px;color:var(--color-text-secondary);margin-bottom:6px;">Based on previous results</div>`
                : '';
            return `<div class="conversational-response" style="line-height:1.6;">${prefix}${this.formatInsightText(response.analysis || '')}</div>`;
        }
        if (!response.success) {
            return `
                <div class="error-message">
                    <div class="error-header">
                        <span class="error-icon">❌</span>
                        <strong>Query Execution Failed</strong>
                    </div>
                    <div class="error-details">
                        <p><strong>Error:</strong> ${response.error}</p>
                        ${response.suggestions ? `
                            <div class="suggestions">
                                <strong>Suggestions:</strong>
                                <ul>${response.suggestions.map(s => `<li>${s}</li>`).join('')}</ul>
                            </div>` : ''}
                        ${response.sql_query ? `
                            <div class="sql-section">
                                <details class="sql-details">
                                    <summary class="sql-summary">
                                        <span class="sql-icon">⚡</span> SQL Query
                                        <span class="toggle-arrow">▼</span>
                                    </summary>
                                    <pre class="sql-code"><code>${response.sql_query}</code></pre>
                                </details>
                            </div>` : ''}
                    </div>
                </div>`;
        }

        const executionTime = response.execution_time_ms < 1000 ?
            `${Math.round(response.execution_time_ms)}ms` :
            `${(response.execution_time_ms / 1000).toFixed(1)}s`;

        let html = `
            <div class="structured-response">
                <div class="response-header">
                    <div class="response-title">
                        <span class="success-icon">📊</span>
                        <strong>Query Results</strong>
                    </div>
                    <div class="response-meta">
                        <span class="execution-time">${executionTime}</span>
                        <span class="record-count">${response.row_count} records</span>
                    </div>
                </div>
                <div class="question-context">
                    <strong>Your Question:</strong> "${response.question}"
                </div>`;

        if (response.insights && response.insights.length > 0) {
            html += `
                <div class="insights-section">
                    <div class="section-title">
                        <span class="section-icon">💡</span>
                        <strong>Quick Insights</strong>
                    </div>
                    <div class="insights-list">
                        ${response.insights.map(insight => {
                insight.raw_message = insight.message;
                return `
                            <div class="insight-item ${insight.type}">
                                <span class="insight-icon">${insight.icon}</span>
                                <span class="insight-text">${this.formatInsightText(insight.message)}</span>
                                ${insight.value ? `<span class="insight-value">${insight.value}</span>` : ''}
                            </div>`;
            }).join('')}
                    </div>
                </div>`;
        }

        if (response.data && response.data.length > 0) {
            const displayData = response.row_count > 10 ? response.data.slice(0, 5) : response.data;
            html += `
                <div class="results-section">
                    <div class="section-title">
                        <span class="section-icon">📋</span>
                        <strong>Results</strong>
                        ${response.row_count > 10 ?
                    `<span class="results-count">(showing first 5 of ${response.row_count})</span>` :
                    `<span class="results-count">(${response.row_count} records)</span>`}
                    </div>
                    <div class="table-container">
                        <table class="results-table">
                            <thead>
                                <tr>${response.columns.map(col => `<th>${col}</th>`).join('')}</tr>
                            </thead>
                            <tbody>
                                ${displayData.map(row => `
                                    <tr>${response.columns.map(col => `<td>${this.formatCellValue(row[col])}</td>`).join('')}</tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                    ${response.row_count > 10 ?
                    `<div class="more-records">
                            <button onclick="window.agentManager.showAllRows(this)" data-expanded="false">
                                Show all ${response.row_count} records
                            </button>
                        </div>` : ''}
                </div>`;
        } else if (response.row_count === 0) {
            html += `
                <div class="no-results">
                    <div class="no-results-icon">🔍</div>
                    <strong>No Results Found</strong>
                    <p>The query executed successfully but returned no matching records.</p>
                </div>`;
        }

        if (response.sql_query) {
            html += `
                <div class="sql-section">
                    <details class="sql-details">
                        <summary class="sql-summary">
                            <span class="sql-icon">⚡</span> SQL Query
                            <span class="toggle-arrow">▼</span>
                        </summary>
                        <pre class="sql-code"><code>${response.sql_query}</code></pre>
                    </details>
                </div>`;
        }

        if (response.column_metadata && response.column_metadata.length > 0) {
            html += `
                <div class="metadata-section">
                    <details class="metadata-details">
                        <summary class="metadata-summary">
                            <span class="metadata-icon">📊</span> Column Metadata
                            <span class="toggle-arrow">▼</span>
                        </summary>
                        <div class="metadata-list">
                            ${response.column_metadata.map(meta => `
                                <div class="column-meta">
                                    <div class="column-header">
                                        <strong class="column-name">${meta.name}</strong>
                                        <span class="column-type">${meta.type}</span>
                                    </div>
                                    <div class="column-tags">
                                        ${meta.is_numeric ? '<span class="meta-tag numeric">numeric</span>' : ''}
                                        ${meta.is_date ? '<span class="meta-tag date">date</span>' : ''}
                                        ${meta.has_nulls ? '<span class="meta-tag nulls">has nulls</span>' : ''}
                                        ${meta.unique_count ? `<span class="meta-tag unique">${meta.unique_count} unique</span>` : ''}
                                    </div>
                                </div>`).join('')}
                        </div>
                    </details>
                </div>`;
        }

        html += `</div>`;
        return html;
    }

    formatCellValue(value) {
        if (value === null || value === undefined) return '<span class="null-value">NULL</span>';
        if (typeof value === 'boolean') return value ? '<span class="bool-true">✓</span>' : '<span class="bool-false">✗</span>';
        if (typeof value === 'number') return `<span class="number-value">${value.toLocaleString()}</span>`;
        const strValue = String(value);
        return strValue.length > 50 ?
            `<span title="${strValue}">${strValue.substring(0, 47)}...</span>` : strValue;
    }

    // ========== AGENT CREATION METHODS ==========

    showCreateAgentModal() {
        this.selectedTables.clear();
        this.selectedColumns = {};
        this.currentConnection = null;

        document.getElementById('createAgentForm').reset();
        document.getElementById('schemaSelection').style.display = 'none';
        document.getElementById('createAgentBtn').disabled = true;
        document.getElementById('generateSchemaBtn').disabled = true;
        document.getElementById('schemaContext').value = '';

        this.updatePreview();
        this.loadDatabaseConnections();
        new bootstrap.Modal(document.getElementById('createAgentModal')).show();
    }

    async loadDatabaseConnections() {
        try {
            const response = await fetch('/api/connections');
            const connections = await response.json();
            const select = document.getElementById('agentConnections');
            select.innerHTML = '<option value="">Select a database connection</option>';
            connections.forEach(conn => {
                const option = document.createElement('option');
                option.value = conn.name;
                option.textContent = `${conn.name} (${conn.type})`;
                select.appendChild(option);
            });
        } catch (error) {
            this.showNotification('Error loading database connections', 'error');
        }
    }

    async onConnectionSelect(connectionName) {
        if (!connectionName) {
            document.getElementById('schemaSelection').style.display = 'none';
            document.getElementById('createAgentBtn').disabled = true;
            return;
        }

        this.currentConnection = connectionName;
        this.showNotification(`Loading tables for ${connectionName}...`, 'info');

        try {
            const response = await fetch('/api/bi-agents/schema', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ connection_name: connectionName, tables: [] })
            });

            const result = await response.json();

            if (result.status === 'success') {
                this.displayTables(result.schema);
                document.getElementById('schemaSelection').style.display = 'block';
                this.showNotification(`Found ${result.schema.length} tables! Click on tables to load columns.`, 'success');
            } else {
                this.showNotification('Error loading schema: ' + result.message, 'error');
                document.getElementById('schemaSelection').style.display = 'none';
            }
        } catch (error) {
            this.showNotification('Error loading database schema', 'error');
            document.getElementById('schemaSelection').style.display = 'none';
        }
    }

    displayTables(tables) {
        const container = document.getElementById('schemaTables');
        container.innerHTML = '';

        if (!tables || tables.length === 0) {
            container.innerHTML = `<div class="alert alert-warning">No tables found in this database.</div>`;
            return;
        }

        tables.forEach(table => {
            const tableCard = this.createTableCard(table);
            container.appendChild(tableCard);
        });

        this.updatePreview();
    }

    createTableCard(table) {
        const card = document.createElement('div');
        card.className = 'card mb-3';

        const tableName = table.name || table.table_name || 'unknown';
        const columns = table.columns || [];
        const hasColumns = columns.length > 0;
        const hasError = table.error;

        card.innerHTML = `
        <div class="card-header">
            <div class="form-check">
                <input class="form-check-input table-checkbox" type="checkbox"
                       value="${tableName}" id="table-${tableName}"
                       ${hasError ? 'disabled' : ''}>
                <label class="form-check-label fw-bold" for="table-${tableName}">
                    ${tableName}
                    ${hasError ? '<span class="badge bg-danger ms-2">Error</span>' : ''}
                    ${!hasColumns && !hasError ? '<span class="badge bg-secondary ms-2">Click to load columns</span>' : ''}
                </label>
            </div>
        </div>
        <div class="card-body">
            <div class="table-columns" id="columns-${tableName}" style="display: none;">
                ${hasError ?
                `<div class="alert alert-warning py-2"><small>Error loading columns: ${table.error}</small></div>` :
                hasColumns ?
                    columns.map(col => {
                        const colName = col.name || 'unknown';
                        const colType = col.type || 'unknown';
                        return `
                            <div class="form-check">
                                <input class="form-check-input column-checkbox" type="checkbox"
                                       value="${colName}" id="col-${tableName}-${colName}"
                                       data-table="${tableName}">
                                <label class="form-check-label" for="col-${tableName}-${colName}">
                                    ${colName} <small class="text-muted">(${colType})</small>
                                    ${col.nullable ? '<span class="badge bg-secondary ms-1">nullable</span>' : ''}
                                </label>
                            </div>`;
                    }).join('')
                    : '<div class="text-muted"><small>Click the table to load columns</small></div>'
            }
            </div>
        </div>`;

        if (!hasError) {
            const tableCheckbox = card.querySelector('.table-checkbox');
            tableCheckbox.addEventListener('change', (e) => {
                this.onTableSelect(tableName, e.target.checked);
            });

            if (hasColumns) {
                card.querySelectorAll('.column-checkbox').forEach(checkbox => {
                    checkbox.addEventListener('change', (e) => {
                        this.onColumnSelect(tableName, e.target.value, e.target.checked);
                    });
                });
            }
        }

        return card;
    }

    async onTableSelect(tableName, isSelected) {
        const columnsDiv = document.getElementById(`columns-${tableName}`);
        const tableCheckbox = document.getElementById(`table-${tableName}`);

        if (isSelected) {
            this.selectedTables.add(tableName);
            columnsDiv.style.display = 'block';

            const hasColumns = columnsDiv.querySelector('.column-checkbox');
            const hasError = columnsDiv.querySelector('.alert-warning');

            if (!hasColumns && !hasError) {
                await this.loadTableColumns(tableName, columnsDiv, tableCheckbox);
            }
        } else {
            this.selectedTables.delete(tableName);
            delete this.selectedColumns[tableName];
            columnsDiv.style.display = 'none';
            columnsDiv.querySelectorAll('.column-checkbox').forEach(cb => { cb.checked = false; });
        }

        this.updateGenerateSchemaButton();
        this.updatePreview();
        this.updateCreateAgentButton();
    }

    async loadTableColumns(tableName, columnsDiv, tableCheckbox) {
        try {
            columnsDiv.innerHTML = '<div class="text-center"><div class="spinner-border spinner-border-sm" role="status"></div> Loading columns...</div>';
            tableCheckbox.disabled = true;

            const response = await fetch('/api/bi-agents/schema', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ connection_name: this.currentConnection, tables: [tableName] })
            });

            const result = await response.json();
            tableCheckbox.disabled = false;

            if (result.status === 'success' && result.schema && result.schema.length > 0) {
                const columns = result.schema[0].columns || [];
                if (columns.length > 0) {
                    columnsDiv.innerHTML = columns.map(col => {
                        const colName = col.name || 'unknown';
                        const colType = col.type || 'unknown';
                        return `
                        <div class="form-check">
                            <input class="form-check-input column-checkbox" type="checkbox"
                                   value="${colName}" id="col-${tableName}-${colName}"
                                   data-table="${tableName}">
                            <label class="form-check-label" for="col-${tableName}-${colName}">
                                ${colName} <small class="text-muted">(${colType})</small>
                                ${col.nullable ? '<span class="badge bg-secondary ms-1">nullable</span>' : ''}
                            </label>
                        </div>`;
                    }).join('');

                    columnsDiv.querySelectorAll('.column-checkbox').forEach(checkbox => {
                        checkbox.addEventListener('change', (e) => {
                            this.onColumnSelect(tableName, e.target.value, e.target.checked);
                        });
                    });
                } else {
                    columnsDiv.innerHTML = '<div class="text-muted"><small>No columns found</small></div>';
                }
            } else {
                columnsDiv.innerHTML = `<div class="alert alert-warning py-2"><small>Error loading columns: ${result.message || 'Unknown error'}</small></div>`;
            }
        } catch (error) {
            tableCheckbox.disabled = false;
            columnsDiv.innerHTML = `<div class="alert alert-warning py-2"><small>Error loading columns: ${error.message}</small></div>`;
        }
    }

    onColumnSelect(tableName, columnName, isSelected) {
        if (!this.selectedColumns[tableName]) {
            this.selectedColumns[tableName] = new Set();
        }

        if (isSelected) {
            this.selectedColumns[tableName].add(columnName);
        } else {
            this.selectedColumns[tableName].delete(columnName);
            if (this.selectedColumns[tableName].size === 0) {
                delete this.selectedColumns[tableName];
            }
        }

        this.updateGenerateSchemaButton();
        this.updatePreview();
        this.updateCreateAgentButton();
    }

    updateGenerateSchemaButton() {
        const button = document.getElementById('generateSchemaBtn');
        const hasTables = this.selectedTables.size > 0;
        const hasColumns = Object.keys(this.selectedColumns).length > 0;
        button.disabled = !(hasTables && hasColumns);
    }

    updateCreateAgentButton() {
        const button = document.getElementById('createAgentBtn');
        if (!button) return;
        const hasName = document.getElementById('agentName').value.trim().length > 0;
        const hasDescription = document.getElementById('agentDescription').value.trim().length > 0;
        const hasConnection = this.currentConnection !== null;
        const hasTables = this.selectedTables.size > 0;
        button.disabled = !(hasName && hasDescription && hasConnection && hasTables);
    }

    updatePreview() {
        const previewBody = document.getElementById('previewBody');
        previewBody.innerHTML = '';

        if (this.selectedTables.size === 0) {
            previewBody.innerHTML = `
                <tr>
                    <td colspan="3" class="text-center text-muted">
                        <small>Select tables and columns to see preview</small>
                    </td>
                </tr>`;
            return;
        }

        Array.from(this.selectedTables).forEach(tableName => {
            const columns = this.selectedColumns[tableName] ? Array.from(this.selectedColumns[tableName]) : [];
            const row = document.createElement('tr');
            row.innerHTML = `
                <td><strong>${tableName}</strong></td>
                <td>${columns.length > 0 ? columns.join(', ') : 'No columns selected'}</td>
                <td>${columns.length} columns selected</td>`;
            previewBody.appendChild(row);
        });
    }

    // ========== AGENT MANAGEMENT ==========

    async loadBIAgents() {
        try {
            const response = await fetch('/api/bi-agents' + window.location.search);
            const agents = await response.json();
            this._cachedAgents = agents;
            this.renderAgents(agents);
            this.renderDictionaryCards(agents);
        } catch (error) {
            console.error('Error loading agents:', error);
        }
    }

    renderAgents(agents) {
        const tbody = document.getElementById('agents-tbody');
        if (!tbody) return;

        // Apply org context filter (multi-select: dept_ids + project_ids)
        const ctx = (typeof getOrgContext === 'function') ? getOrgContext() : { dept_ids: [], project_ids: [] };
        const dIds = ctx.dept_ids || [];
        const pIds = ctx.project_ids || [];
        const hasFilter = dIds.length || pIds.length;
        let filtered = agents || [];
        if (hasFilter) {
            filtered = filtered.filter(a =>
                dIds.some(d => (a.dept_ids || []).includes(d)) ||
                pIds.some(p => (a.project_ids || []).includes(p)));
        }

        // Update filter bar
        const bar   = document.getElementById('biAgentsOrgBar');
        const label = document.getElementById('biAgentsOrgLabel');
        if (bar && label) {
            if (hasFilter) {
                bar.style.display = '';
                const parts = [];
                if (dIds.length) parts.push(`${dIds.length} Dept${dIds.length > 1 ? 's' : ''}`);
                if (pIds.length) parts.push(`${pIds.length} Project${pIds.length > 1 ? 's' : ''}`);
                label.textContent = parts.join(', ');
            } else {
                bar.style.display = 'none';
            }
        }

        if (!filtered.length) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center py-4 text-muted">
                        ${hasFilter ? 'No agents match the selected filter.' : 'No BI agents found. Create your first agent to get started.'}
                    </td>
                </tr>`;
            return;
        }

        tbody.innerHTML = filtered.map(agent => {
            const deptBadges = (agent.dept_ids||[]).map((did, i) => {
                const name  = (agent.dept_names||[])[i]  || '';
                const color = (agent.dept_colors||[])[i] || '#6366f1';
                return name ? `<span class="badge me-1" style="background:${color}">${name}</span>` : '';
            }).join('');
            const projBadges = (agent.project_ids||[]).map((pid, i) => {
                const name = (agent.project_names||[])[i] || '';
                return name ? `<span class="text-muted me-1" style="font-size:.8em">${name}</span>` : '';
            }).join('');
            const orgCell = deptBadges || projBadges
                ? `<small>${deptBadges}${projBadges}</small>`
                : '<span class="text-muted">—</span>';
            const deptDataIds = (agent.dept_ids||[]).join(',');
            const projDataIds = (agent.project_ids||[]).join(',');
            return `
            <tr data-dept-ids="${deptDataIds}" data-project-ids="${projDataIds}">
                <td><strong>${agent.name}</strong></td>
                <td>${agent.description}</td>
                <td><span class="badge bg-secondary">${agent.database_connection}</span></td>
                <td>${orgCell}</td>
                <td><small>${new Date(agent.created_at).toLocaleDateString()}</small></td>
                <td class="table-actions">
                    <button class="btn btn-sm btn-primary me-1" onclick="agentManager.openChatWithAgent('${agent.name}', 'bi')" title="Chat with Agent">
                        <i class="fas fa-comments"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger" onclick="agentManager.deleteAgent('${agent.name}')" title="Delete Agent">
                        <i class="fas fa-trash"></i>
                    </button>
                </td>
            </tr>`;
        }).join('');
    }

    renderDictionaryCards(agents) {
        const container = document.getElementById('dictionary-agent-cards');
        if (!container) return;

        if (!agents || agents.length === 0) {
            container.innerHTML = `<div class="alert alert-info">No BI agents found. Create your first agent using the "Create BI Agent" button.</div>`;
            return;
        }

        container.innerHTML = agents.map(agent => {
            const tableCount = agent.selected_tables?.length || 0;
            const totalCols = Object.values(agent.selected_columns || {}).reduce((sum, cols) => sum + cols.length, 0);
            const tableRows = (agent.selected_tables || []).map(tbl => {
                const cols = (agent.selected_columns || {})[tbl] || [];
                return `<tr>
                    <td><code>${tbl}</code></td>
                    <td><small class="text-muted">${cols.join(', ') || '<em>all</em>'}</small></td>
                    <td><span class="badge bg-light text-dark">${cols.length}</span></td>
                </tr>`;
            }).join('');

            return `
            <div class="card mb-4 shadow-sm">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <div>
                        <h5 class="mb-0"><i class="fas fa-robot me-2 text-primary"></i>${agent.name}</h5>
                        <small class="text-muted">${agent.description}</small>
                    </div>
                    <div class="d-flex gap-2 align-items-center">
                        <span class="badge bg-secondary">${agent.database_connection}</span>
                        <span class="badge bg-primary">${tableCount} tables</span>
                        <span class="badge bg-info text-dark">${totalCols} columns</span>
                        <button class="btn btn-sm btn-outline-primary" onclick="agentManager.showEditAgentModal('${agent.name}')">
                            <i class="fas fa-edit me-1"></i>Edit
                        </button>
                    </div>
                </div>
                <div class="card-body p-0">
                    <div class="table-responsive">
                        <table class="table table-sm table-hover mb-0">
                            <thead class="table-light">
                                <tr><th>Table</th><th>Columns</th><th>#</th></tr>
                            </thead>
                            <tbody>${tableRows || '<tr><td colspan="3" class="text-center text-muted py-2"><small>No tables selected</small></td></tr>'}</tbody>
                        </table>
                    </div>
                </div>
                <div class="card-footer text-muted d-flex justify-content-between">
                    <small>Created: ${new Date(agent.created_at).toLocaleDateString()}</small>
                    <small>Connection: ${agent.database_connection}</small>
                </div>
            </div>`;
        }).join('');
    }

    // ========== EDIT AGENT ==========

    async showEditAgentModal(agentName) {
        const agent = (this._cachedAgents || []).find(a => a.name === agentName);
        if (!agent) {
            this.showNotification('Agent not found', 'error');
            return;
        }

        // Reset edit state
        this.editSelectedTables = new Set(agent.selected_tables || []);
        this.editSelectedColumns = {};
        for (const [tbl, cols] of Object.entries(agent.selected_columns || {})) {
            this.editSelectedColumns[tbl] = new Set(cols);
        }
        this.editConnection = agent.database_connection;
        this._editSchemaViewMode = 'form'; // 'form' | 'raw'
        this._editOrigSchemaContext = agent.schema_context || {};
        this._lastCollectedSemanticRules = agent.semantic_rules || {};

        // Populate static fields
        document.getElementById('editAgentOriginalName').value = agentName;
        document.getElementById('editAgentName').value = agentName;
        document.getElementById('editAgentDescription').value = agent.description || '';
        document.getElementById('editAgentConnectionDisplay').value = agent.database_connection;
        document.getElementById('editAgentConnection').value = agent.database_connection;

        // Seed raw JSON textarea (hidden by default)
        document.getElementById('editSchemaContext').value = agent.schema_context
            ? JSON.stringify(agent.schema_context, null, 2) : '{}';

        // Render schema context form
        this._renderSchemaContextForm(agent.schema_context || {}, agent.semantic_rules || {});

        // Reset view toggle to Form
        document.getElementById('schemaFormViewBtn').classList.add('active');
        document.getElementById('schemaRawJsonBtn').classList.remove('active');
        document.getElementById('schemaContextFormView').style.display = '';
        document.getElementById('schemaContextRawView').style.display = 'none';

        // Show modal
        const modal = new bootstrap.Modal(document.getElementById('editAgentModal'));
        modal.show();

        // Load schema tables (runs in background after modal opens)
        this._loadEditSchemaTables(agent);

        // Wire up buttons
        document.getElementById('saveAgentBtn').onclick = () => this.updateAgent();
        document.getElementById('editGenerateSchemaBtn').onclick = () => this._editGenerateSchema();

        // View toggle
        document.getElementById('schemaFormViewBtn').onclick = () => this._switchSchemaView('form');
        document.getElementById('schemaRawJsonBtn').onclick = () => this._switchSchemaView('raw');
    }

    async _loadEditSchemaTables(agent) {
        const container = document.getElementById('editSchemaTables');
        container.innerHTML = `<div class="text-center py-3"><div class="spinner-border spinner-border-sm me-2" role="status"></div>Loading tables from database...</div>`;

        try {
            const response = await fetch('/api/bi-agents/schema', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ connection_name: agent.database_connection, tables: [] })
            });
            const result = await response.json();

            if (result.status === 'success') {
                this._renderEditTables(result.schema, agent);
            } else {
                // Fallback: render only the tables already in the agent
                this._renderEditTablesFromAgent(agent);
            }
        } catch (e) {
            this._renderEditTablesFromAgent(agent);
        }
    }

    _renderEditTables(allTables, agent) {
        const container = document.getElementById('editSchemaTables');
        container.innerHTML = '';

        allTables.forEach(table => {
            const tableName = table.name || table.table_name;
            const isSelected = this.editSelectedTables.has(tableName);
            const card = document.createElement('div');
            card.className = 'card mb-2';

            const existingCols = Array.from(this.editSelectedColumns[tableName] || []);
            const allCols = table.columns || [];

            card.innerHTML = `
            <div class="card-header py-2">
                <div class="form-check">
                    <input class="form-check-input edit-table-checkbox" type="checkbox"
                           value="${tableName}" id="edit-table-${tableName}" ${isSelected ? 'checked' : ''}>
                    <label class="form-check-label fw-bold" for="edit-table-${tableName}">${tableName}</label>
                </div>
            </div>
            <div class="card-body py-2" id="edit-columns-${tableName}" style="${isSelected ? '' : 'display:none'}">
                ${allCols.length > 0
                    ? allCols.map(col => {
                        const colName = col.name || col;
                        const colType = col.type || '';
                        const checked = existingCols.includes(colName) ? 'checked' : '';
                        return `<div class="form-check form-check-inline">
                            <input class="form-check-input edit-col-checkbox" type="checkbox"
                                   value="${colName}" id="edit-col-${tableName}-${colName}"
                                   data-table="${tableName}" ${checked}>
                            <label class="form-check-label" for="edit-col-${tableName}-${colName}">
                                ${colName}${colType ? ` <small class="text-muted">(${colType})</small>` : ''}
                            </label>
                        </div>`;
                    }).join('')
                    : '<small class="text-muted">Click table to load columns</small>'
                }
            </div>`;

            const tableCheck = card.querySelector('.edit-table-checkbox');
            tableCheck.addEventListener('change', (e) => this._onEditTableSelect(tableName, e.target.checked, card));
            card.querySelectorAll('.edit-col-checkbox').forEach(cb => {
                cb.addEventListener('change', (e) => this._onEditColumnSelect(tableName, e.target.value, e.target.checked));
            });

            container.appendChild(card);
        });

        this._updateEditCounts();
        this._updateEditPreview();
    }

    _renderEditTablesFromAgent(agent) {
        // Fallback: only show tables already in the agent
        const container = document.getElementById('editSchemaTables');
        container.innerHTML = '';

        (agent.selected_tables || []).forEach(tableName => {
            const existingCols = Array.from(this.editSelectedColumns[tableName] || []);
            const card = document.createElement('div');
            card.className = 'card mb-2';

            card.innerHTML = `
            <div class="card-header py-2">
                <div class="form-check">
                    <input class="form-check-input edit-table-checkbox" type="checkbox"
                           value="${tableName}" id="edit-table-${tableName}" checked>
                    <label class="form-check-label fw-bold" for="edit-table-${tableName}">${tableName}</label>
                </div>
            </div>
            <div class="card-body py-2" id="edit-columns-${tableName}">
                ${existingCols.map(col => `
                    <div class="form-check form-check-inline">
                        <input class="form-check-input edit-col-checkbox" type="checkbox"
                               value="${col}" id="edit-col-${tableName}-${col}"
                               data-table="${tableName}" checked>
                        <label class="form-check-label" for="edit-col-${tableName}-${col}">${col}</label>
                    </div>`).join('')}
            </div>`;

            const tableCheck = card.querySelector('.edit-table-checkbox');
            tableCheck.addEventListener('change', (e) => this._onEditTableSelect(tableName, e.target.checked, card));
            card.querySelectorAll('.edit-col-checkbox').forEach(cb => {
                cb.addEventListener('change', (e) => this._onEditColumnSelect(tableName, e.target.value, e.target.checked));
            });

            container.appendChild(card);
        });

        this._updateEditCounts();
        this._updateEditPreview();
    }

    async _onEditTableSelect(tableName, isSelected, card) {
        const colDiv = document.getElementById(`edit-columns-${tableName}`);

        if (isSelected) {
            this.editSelectedTables.add(tableName);
            colDiv.style.display = '';

            // Load columns if not yet loaded
            const hasCheckboxes = colDiv.querySelector('.edit-col-checkbox');
            if (!hasCheckboxes) {
                colDiv.innerHTML = `<div class="spinner-border spinner-border-sm" role="status"></div> Loading columns...`;
                try {
                    const res = await fetch('/api/bi-agents/schema', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ connection_name: this.editConnection, tables: [tableName] })
                    });
                    const result = await res.json();
                    const cols = result.schema?.[0]?.columns || [];
                    const existingCols = Array.from(this.editSelectedColumns[tableName] || []);
                    colDiv.innerHTML = cols.map(col => {
                        const colName = col.name || col;
                        const checked = existingCols.includes(colName) ? 'checked' : '';
                        return `<div class="form-check form-check-inline">
                            <input class="form-check-input edit-col-checkbox" type="checkbox"
                                   value="${colName}" id="edit-col-${tableName}-${colName}"
                                   data-table="${tableName}" ${checked}>
                            <label class="form-check-label" for="edit-col-${tableName}-${colName}">
                                ${colName} <small class="text-muted">(${col.type || ''})</small>
                            </label>
                        </div>`;
                    }).join('');
                    colDiv.querySelectorAll('.edit-col-checkbox').forEach(cb => {
                        cb.addEventListener('change', (e) => this._onEditColumnSelect(tableName, e.target.value, e.target.checked));
                    });
                } catch (e) {
                    colDiv.innerHTML = `<small class="text-danger">Failed to load columns</small>`;
                }
            }
        } else {
            this.editSelectedTables.delete(tableName);
            delete this.editSelectedColumns[tableName];
            colDiv.style.display = 'none';
            colDiv.querySelectorAll('.edit-col-checkbox').forEach(cb => cb.checked = false);
        }

        this._updateEditCounts();
        this._updateEditPreview();
        this._refreshSemanticRulesSection();
    }

    _refreshSemanticRulesSection() {
        // Collect current rules text before re-rendering so we don't lose edits
        const current = {};
        document.querySelectorAll('#semanticRulesContainer [data-rules-table]').forEach(card => {
            const tbl = card.dataset.rulesTable;
            current[tbl] = [];
            card.querySelectorAll('.semantic-rule-input').forEach(inp => {
                const v = inp.value.trim();
                if (v) current[tbl].push(v);
            });
        });
        this._renderSemanticRules(current);
    }

    _onEditColumnSelect(tableName, colName, isSelected) {
        if (!this.editSelectedColumns[tableName]) {
            this.editSelectedColumns[tableName] = new Set();
        }
        if (isSelected) {
            this.editSelectedColumns[tableName].add(colName);
        } else {
            this.editSelectedColumns[tableName].delete(colName);
            if (this.editSelectedColumns[tableName].size === 0) {
                delete this.editSelectedColumns[tableName];
            }
        }
        this._updateEditCounts();
        this._updateEditPreview();
    }

    _updateEditCounts() {
        const tabCount = this.editSelectedTables?.size || 0;
        const colCount = Object.values(this.editSelectedColumns || {}).reduce((s, c) => s + c.size, 0);
        const tabBadge = document.getElementById('editSelectedTablesCount');
        const colBadge = document.getElementById('editSelectedColsCount');
        if (tabBadge) tabBadge.textContent = `${tabCount} table${tabCount !== 1 ? 's' : ''}`;
        if (colBadge) colBadge.textContent = `${colCount} column${colCount !== 1 ? 's' : ''}`;
    }

    _updateEditPreview() {
        const body = document.getElementById('editPreviewBody');
        if (!body) return;
        if (!this.editSelectedTables || this.editSelectedTables.size === 0) {
            body.innerHTML = `<tr><td colspan="3" class="text-center text-muted"><small>No tables selected</small></td></tr>`;
            return;
        }
        body.innerHTML = Array.from(this.editSelectedTables).map(tbl => {
            const cols = this.editSelectedColumns[tbl] ? Array.from(this.editSelectedColumns[tbl]) : [];
            return `<tr>
                <td><strong>${tbl}</strong></td>
                <td><small>${cols.length > 0 ? cols.join(', ') : '<em class="text-muted">none selected</em>'}</small></td>
                <td>${cols.length}</td>
            </tr>`;
        }).join('');
    }

    // ========== SCHEMA CONTEXT FORM ==========

    _switchSchemaView(mode) {
        this._editSchemaViewMode = mode;
        const formView = document.getElementById('schemaContextFormView');
        const rawView  = document.getElementById('schemaContextRawView');
        const formBtn  = document.getElementById('schemaFormViewBtn');
        const rawBtn   = document.getElementById('schemaRawJsonBtn');

        if (mode === 'raw') {
            // Sync form → raw textarea
            const { schema_context, semantic_rules } = this._collectSchemaContextFromForm();
            document.getElementById('editSchemaContext').value = JSON.stringify(schema_context, null, 2);
            formView.style.display = 'none';
            rawView.style.display  = '';
            formBtn.classList.remove('active');
            rawBtn.classList.add('active');
        } else {
            // Sync raw textarea → form
            let sc = {};
            try { sc = JSON.parse(document.getElementById('editSchemaContext').value || '{}'); } catch (e) {}
            // preserve semantic rules already in form
            const { semantic_rules } = this._collectSchemaContextFromForm();
            this._renderSchemaContextForm(sc, semantic_rules);
            formView.style.display = '';
            rawView.style.display  = 'none';
            rawBtn.classList.remove('active');
            formBtn.classList.add('active');
        }
    }

    _renderSchemaContextForm(schemaContext, semanticRules) {
        // AI Analysis
        const aiEl = document.getElementById('scAiAnalysis');
        const bcEl = document.getElementById('scBusinessContext');
        if (aiEl) aiEl.value = schemaContext.ai_analysis || '';
        if (bcEl) bcEl.value = schemaContext.business_context || '';

        // Tables accordion
        this._renderSchemaTablesAccordion(schemaContext.tables || []);

        // Semantic rules (per selected table)
        this._renderSemanticRules(semanticRules || {});
    }

    _renderSchemaTablesAccordion(tables) {
        const accordion = document.getElementById('schemaTablesAccordion');
        const placeholder = document.getElementById('schemaTablesAccordionPlaceholder');
        if (!accordion) return;

        // Remove old accordion items (keep placeholder)
        accordion.querySelectorAll('.accordion-item').forEach(el => el.remove());

        if (!tables || tables.length === 0) {
            if (placeholder) placeholder.style.display = '';
            return;
        }
        if (placeholder) placeholder.style.display = 'none';

        tables.forEach((tbl, idx) => {
            const tblName  = tbl.table_name || tbl.name || `table_${idx}`;
            const safeId   = tblName.replace(/[^a-zA-Z0-9_]/g, '_');
            const item     = document.createElement('div');
            item.className = 'accordion-item';

            const colRows = (tbl.columns || []).map((col, ci) => {
                const colName = col.name || '';
                const colType = col.type || '';
                const purpose = col.inferred_purpose || '';
                const samples = (col.sample_values || []).join(', ');
                return `<tr>
                    <td class="text-nowrap"><code>${colName}</code></td>
                    <td><small class="text-muted">${colType}</small></td>
                    <td>
                        <input type="text" class="form-control form-control-sm sc-col-purpose"
                               data-table="${tblName}" data-col="${colName}"
                               value="${purpose.replace(/"/g, '&quot;')}"
                               placeholder="Column purpose...">
                    </td>
                    <td><small class="text-muted" style="max-width:140px;display:inline-block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis" title="${samples}">${samples}</small></td>
                </tr>`;
            }).join('');

            item.innerHTML = `
            <h2 class="accordion-header" id="sch-head-${safeId}">
                <button class="accordion-button collapsed py-2" type="button"
                        data-bs-toggle="collapse" data-bs-target="#sch-body-${safeId}">
                    <span class="fw-semibold">${tblName}</span>
                    <span class="badge bg-secondary ms-2">${(tbl.columns || []).length} cols</span>
                </button>
            </h2>
            <div id="sch-body-${safeId}" class="accordion-collapse collapse"
                 data-bs-parent="">
                <div class="accordion-body pt-2 pb-3">
                    <div class="row g-2 mb-2">
                        <div class="col-md-8">
                            <label class="form-label small text-muted mb-1">Description</label>
                            <textarea class="form-control form-control-sm sc-tbl-desc"
                                      data-table="${tblName}" rows="2"
                                      placeholder="What this table represents...">${(tbl.description || '').replace(/</g,'&lt;')}</textarea>
                        </div>
                        <div class="col-md-4">
                            <label class="form-label small text-muted mb-1">Estimated Purpose</label>
                            <input type="text" class="form-control form-control-sm sc-tbl-purpose"
                                   data-table="${tblName}" value="${(tbl.estimated_purpose || '').replace(/"/g,'&quot;')}"
                                   placeholder="e.g. Financial data, Temporal tracking">
                        </div>
                    </div>
                    ${colRows ? `
                    <label class="form-label small text-muted mb-1">Column Purposes</label>
                    <div class="table-responsive">
                        <table class="table table-sm table-bordered mb-0">
                            <thead class="table-light">
                                <tr><th>Column</th><th>Type</th><th>Inferred Purpose</th><th>Sample Values</th></tr>
                            </thead>
                            <tbody class="sc-cols-body" data-table="${tblName}">${colRows}</tbody>
                        </table>
                    </div>` : '<small class="text-muted">No column data. Re-analyze to populate.</small>'}
                </div>
            </div>`;

            accordion.appendChild(item);
        });
    }

    _renderSemanticRules(semanticRules) {
        const container = document.getElementById('semanticRulesContainer');
        if (!container) return;
        container.innerHTML = '';

        const tables = this.editSelectedTables ? Array.from(this.editSelectedTables) : [];
        if (tables.length === 0) {
            container.innerHTML = '<small class="text-muted">Select tables above to add semantic rules.</small>';
            return;
        }

        tables.forEach(tblName => {
            const rules = semanticRules[tblName] || [];
            const safeId = tblName.replace(/[^a-zA-Z0-9_]/g, '_');
            const card = document.createElement('div');
            card.className = 'card mb-2';
            card.dataset.rulesTable = tblName;
            card.innerHTML = `
            <div class="card-header py-2 d-flex justify-content-between align-items-center">
                <span class="fw-semibold small"><i class="fas fa-table me-1 text-primary"></i>${tblName}</span>
                <button type="button" class="btn btn-sm btn-outline-success add-rule-btn" data-table="${tblName}">
                    <i class="fas fa-plus me-1"></i>Add Rule
                </button>
            </div>
            <div class="card-body py-2 rules-list-${safeId}">
                ${rules.length === 0
                    ? `<small class="text-muted d-block py-1">No rules yet. Click "Add Rule" to add business logic for this table.</small>`
                    : rules.map((rule, i) => this._ruleInputHtml(tblName, i, rule)).join('')}
            </div>`;

            card.querySelector('.add-rule-btn').addEventListener('click', () => this._addSemanticRule(tblName));
            card.querySelectorAll('.remove-rule-btn').forEach(btn => {
                btn.addEventListener('click', () => btn.closest('.rule-row').remove());
            });

            container.appendChild(card);
        });
    }

    _ruleInputHtml(tblName, idx, value = '') {
        const safe = value.replace(/"/g, '&quot;');
        return `<div class="rule-row input-group input-group-sm mb-1">
            <span class="input-group-text text-muted">${idx + 1}</span>
            <input type="text" class="form-control semantic-rule-input"
                   data-table="${tblName}" value="${safe}"
                   placeholder="e.g. For revenue questions, always use total_revenue column">
            <button type="button" class="btn btn-outline-danger remove-rule-btn"><i class="fas fa-times"></i></button>
        </div>`;
    }

    _addSemanticRule(tblName) {
        const safeId = tblName.replace(/[^a-zA-Z0-9_]/g, '_');
        const list = document.querySelector(`.rules-list-${safeId}`);
        if (!list) return;

        // Remove "no rules yet" placeholder if present
        const placeholder = list.querySelector('small.text-muted');
        if (placeholder) placeholder.remove();

        const existingCount = list.querySelectorAll('.rule-row').length;
        const div = document.createElement('div');
        div.innerHTML = this._ruleInputHtml(tblName, existingCount);
        const row = div.firstElementChild;
        row.querySelector('.remove-rule-btn').addEventListener('click', () => row.remove());
        list.appendChild(row);
        row.querySelector('input').focus();
    }

    _collectSchemaContextFromForm() {
        // Collect schema_context from the form inputs
        const schemaCtx = {};
        const aiEl = document.getElementById('scAiAnalysis');
        const bcEl = document.getElementById('scBusinessContext');
        if (aiEl) schemaCtx.ai_analysis = aiEl.value;
        if (bcEl) schemaCtx.business_context = bcEl.value;

        // Tables
        const tables = [];
        document.querySelectorAll('#schemaTablesAccordion .accordion-item').forEach(item => {
            const descEl    = item.querySelector('.sc-tbl-desc');
            const purposeEl = item.querySelector('.sc-tbl-purpose');
            if (!descEl) return;

            const tblName = descEl.dataset.table;
            const columns = [];
            item.querySelectorAll('.sc-col-purpose').forEach(inp => {
                const colName = inp.dataset.col;
                // Grab sample_values and type from the original cached schema_context if available
                const origTbl  = (this._editOrigSchemaContext?.tables || []).find(t => (t.table_name || t.name) === tblName);
                const origCol  = (origTbl?.columns || []).find(c => c.name === colName) || {};
                columns.push({
                    name: colName,
                    type: origCol.type || '',
                    inferred_purpose: inp.value,
                    sample_values: origCol.sample_values || []
                });
            });

            tables.push({
                table_name: tblName,
                description: descEl.value,
                estimated_purpose: purposeEl ? purposeEl.value : '',
                columns,
                sample_row_count: ((this._editOrigSchemaContext?.tables || []).find(t => (t.table_name || t.name) === tblName) || {}).sample_row_count || 0
            });
        });
        if (tables.length) schemaCtx.tables = tables;

        // Preserve metadata fields from original
        const orig = this._editOrigSchemaContext || {};
        if (orig.database_type) schemaCtx.database_type = orig.database_type;
        if (orig.generated_at)  schemaCtx.generated_at  = orig.generated_at;
        if (orig.relationships) schemaCtx.relationships  = orig.relationships;

        // Collect semantic_rules
        const semantic_rules = {};
        document.querySelectorAll('#semanticRulesContainer [data-rules-table]').forEach(card => {
            const tblName = card.dataset.rulesTable;
            const rules = [];
            card.querySelectorAll('.semantic-rule-input').forEach(inp => {
                const v = inp.value.trim();
                if (v) rules.push(v);
            });
            if (rules.length) semantic_rules[tblName] = rules;
        });

        return { schema_context: schemaCtx, semantic_rules };
    }

    async _editGenerateSchema() {
        if (!this.editSelectedTables || this.editSelectedTables.size === 0) {
            this.showNotification('Select at least one table first', 'error');
            return;
        }
        const btn = document.getElementById('editGenerateSchemaBtn');
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Analyzing...';
        btn.disabled = true;

        try {
            const response = await fetch('/api/bi-agents/generate-schema-context', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    connection_name: this.editConnection,
                    tables: Array.from(this.editSelectedTables),
                    columns: Object.fromEntries(
                        Object.entries(this.editSelectedColumns).map(([t, c]) => [t, Array.from(c)])
                    )
                })
            });
            const result = await response.json();
            if (result.status === 'success') {
                const sc = result.schema_context;
                // Preserve existing semantic rules
                const { semantic_rules } = this._collectSchemaContextFromForm();
                this._editOrigSchemaContext = sc;
                // Update raw textarea
                document.getElementById('editSchemaContext').value = JSON.stringify(sc, null, 2);
                // Update form view
                this._renderSchemaContextForm(sc, semantic_rules);
                // Switch to form view if not already there
                if (this._editSchemaViewMode === 'raw') this._switchSchemaView('form');
                this.showNotification('Schema re-analyzed and form updated!', 'success');
            } else {
                this.showNotification('Error: ' + result.message, 'error');
            }
        } catch (e) {
            this.showNotification('Error generating schema context', 'error');
        } finally {
            btn.innerHTML = orig;
            btn.disabled = false;
        }
    }

    async updateAgent() {
        const agentName = document.getElementById('editAgentOriginalName').value;
        const description = document.getElementById('editAgentDescription').value.trim();

        if (!description) {
            this.showNotification('Description is required', 'error');
            return;
        }
        if (!this.editSelectedTables || this.editSelectedTables.size === 0) {
            this.showNotification('Select at least one table', 'error');
            return;
        }

        // Collect schema context + semantic rules
        let schemaContext = {};
        let semanticRules = {};

        if (this._editSchemaViewMode === 'raw') {
            // Parse from raw textarea
            try {
                schemaContext = JSON.parse(document.getElementById('editSchemaContext').value || '{}');
            } catch (e) {
                this.showNotification('Invalid Raw JSON in Schema Context', 'error');
                return;
            }
            // semantic rules come from whatever was last in form (already in raw mode)
            semanticRules = this._lastCollectedSemanticRules || {};
        } else {
            const collected = this._collectSchemaContextFromForm();
            schemaContext  = collected.schema_context;
            semanticRules  = collected.semantic_rules;
            this._lastCollectedSemanticRules = semanticRules;
        }

        const updatedData = {
            name: agentName,
            description,
            database_connection: document.getElementById('editAgentConnection').value,
            selected_tables: Array.from(this.editSelectedTables),
            selected_columns: Object.fromEntries(
                Object.entries(this.editSelectedColumns).map(([t, c]) => [t, Array.from(c)])
            ),
            schema_context: schemaContext,
            semantic_rules: semanticRules
        };

        try {
            const btn = document.getElementById('saveAgentBtn');
            btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Saving...';
            btn.disabled = true;

            const response = await fetch('/api/bi-agents', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updatedData)
            });
            const result = await response.json();

            btn.innerHTML = '<i class="fas fa-save me-1"></i>Save Changes';
            btn.disabled = false;

            if (result.status === 'success') {
                this.showNotification('Agent updated successfully!', 'success');
                bootstrap.Modal.getInstance(document.getElementById('editAgentModal')).hide();
                await this.loadBIAgents();
            } else {
                this.showNotification('Error: ' + result.message, 'error');
            }
        } catch (e) {
            this.showNotification('Error updating agent', 'error');
        }
    }

    async deleteAgent(agentName) {
        if (!confirm(`Are you sure you want to delete agent "${agentName}"?`)) return;

        try {
            const response = await fetch('/api/bi-agents', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: agentName })
            });

            const result = await response.json();

            if (result.status === 'success') {
                this.showNotification('Agent deleted successfully!', 'success');
                this.loadBIAgents();
            } else {
                this.showNotification('Error deleting agent: ' + result.message, 'error');
            }
        } catch (error) {
            this.showNotification('Error deleting agent', 'error');
        }
    }

    // ========== HELPER METHODS ==========

    showAllRows(button) {
        const container = button.closest('.results-section');
        const fullData = window.agentManager.lastQueryResult.data;
        const columns = window.agentManager.lastQueryResult.columns;
        const tableContainer = container.querySelector('.table-container');
        const isExpanded = button.dataset.expanded === "true";

        const buildTable = (rows) => `
            <table class="results-table">
                <thead><tr>${columns.map(c => `<th>${c}</th>`).join('')}</tr></thead>
                <tbody>
                    ${rows.map(row => `<tr>${columns.map(col => `<td>${this.formatCellValue(row[col])}</td>`).join('')}</tr>`).join('')}
                </tbody>
            </table>`;

        if (!isExpanded) {
            tableContainer.innerHTML = buildTable(fullData);
            button.innerText = "Show less";
            button.dataset.expanded = "true";
        } else {
            tableContainer.innerHTML = buildTable(fullData.slice(0, 5));
            button.innerText = `Show all ${fullData.length} records`;
            button.dataset.expanded = "false";
        }
    }

    getAgentConfig(agentId) {
        return { selected_tables: [], schema_context: {} };
    }

    getConnectionName(agentId) {
        return 'your-connection-name';
    }

    getSessionId(agentId) {
        return null;
    }

    showNotification(message, type = 'info') {
        const alertClass = { 'success': 'alert-success', 'error': 'alert-danger', 'warning': 'alert-warning', 'info': 'alert-info' }[type] || 'alert-info';
        const notification = document.createElement('div');
        notification.className = `alert ${alertClass} alert-dismissible fade show position-fixed`;
        notification.style.cssText = 'top: 20px; right: 20px; z-index: 1060; min-width: 300px;';
        notification.innerHTML = `${message}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
        document.body.appendChild(notification);
        setTimeout(() => notification.remove(), 5000);
    }

    async generateSchemaContext() {
        if (!this.currentConnection || this.selectedTables.size === 0) {
            this.showNotification('Please select a connection and tables first', 'error');
            return;
        }

        try {
            this.showNotification('AI is analyzing your database schema...', 'info');
            const generateBtn = document.getElementById('generateSchemaBtn');
            const originalText = generateBtn.innerHTML;
            generateBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Analyzing...';
            generateBtn.disabled = true;

            const response = await fetch('/api/bi-agents/generate-schema-context', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    connection_name: this.currentConnection,
                    tables: Array.from(this.selectedTables),
                    columns: Object.fromEntries(
                        Object.entries(this.selectedColumns).map(([table, cols]) => [table, Array.from(cols)])
                    )
                })
            });

            const result = await response.json();
            generateBtn.innerHTML = originalText;
            generateBtn.disabled = false;

            if (result.status === 'success') {
                document.getElementById('schemaContext').value = JSON.stringify(result.schema_context, null, 2);
                this.showNotification('AI schema analysis completed!', 'success');
            } else {
                this.showNotification('Error: ' + result.message, 'error');
            }
        } catch (error) {
            this.showNotification('Error generating schema context', 'error');
        }
    }

    async createAgent() {
        const agentName = document.getElementById('agentName').value.trim();
        const agentDescription = document.getElementById('agentDescription').value.trim();

        if (!agentName || !agentDescription || !this.currentConnection) {
            this.showNotification('Please fill in all required fields', 'error');
            return;
        }

        if (this.selectedTables.size === 0) {
            this.showNotification('Please select at least one table', 'error');
            return;
        }

        const schemaContextValue = document.getElementById('schemaContext').value.trim();
        let schemaContext = {};
        if (schemaContextValue) {
            try {
                schemaContext = JSON.parse(schemaContextValue);
            } catch (error) {
                this.showNotification('Invalid schema context format', 'error');
                return;
            }
        }

        const agentData = {
            name: agentName,
            description: agentDescription,
            database_connection: this.currentConnection,
            selected_tables: Array.from(this.selectedTables),
            selected_columns: Object.fromEntries(
                Object.entries(this.selectedColumns).map(([table, cols]) => [table, Array.from(cols)])
            ),
            schema_context: schemaContext,
            created_at: new Date().toISOString()
        };

        try {
            const response = await fetch('/api/bi-agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(agentData)
            });

            const result = await response.json();

            if (result.status === 'success') {
                this.showNotification('Agent created successfully!', 'success');
                bootstrap.Modal.getInstance(document.getElementById('createAgentModal')).hide();
                setTimeout(() => location.reload(), 1000);
            } else {
                this.showNotification('Error creating agent: ' + result.message, 'error');
            }
        } catch (error) {
            this.showNotification('Error creating agent', 'error');
        }
    }
}

// Global functions
function showCreateAgentModal() { if (window.agentManager) window.agentManager.showCreateAgentModal(); }
function openChatWithAgent(agentName) { if (window.agentManager) window.agentManager.openChatWithAgent(agentName); }
function openAgentChat(type, agentId, agentName) { if (window.agentManager) window.agentManager.openAgentChat(type, agentId, agentName); }
function closeAgentChat() { if (window.agentManager) window.agentManager.closeAgentChat(); }
function sendMessage(type) { if (window.agentManager) window.agentManager.sendSidebarMessage(type); }

// BI conversation history globals (called from modals.html buttons)
window._biToggleHistory = function() {
    if (!window.agentManager) return;
    window.agentManager._biSetHistory(!window.agentManager._biHistoryVisible);
};
window._biNewChat = function() {
    if (!window.agentManager) return;
    window.agentManager.clearChat();
    window.agentManager._biSetHistory(false);
};
window._biLoadConversation = function(cid) {
    if (!window.agentManager) return;
    window.agentManager._biLoadConversationById(cid);
};
window._biDeleteConvo = function(cid) {
    if (!window.agentManager) return;
    window.agentManager._biDeleteConvo(cid);
};

function clearBiOrgFilter() {
    if (typeof selectOrg === 'function') selectOrg('all', null, 'All Departments');
}

// Initialize
if (!window.agentManager) {
    console.log("🏗️ Creating agentManager immediately...");
    window.agentManager = new AgentManager();
}

document.addEventListener("readystatechange", () => {
    if (!window.agentManager && (document.readyState === "interactive" || document.readyState === "complete")) {
        console.log("🏗️ Creating agentManager on readystatechange...");
        window.agentManager = new AgentManager();
    }
});