// static/js/dashboard.js
console.log("📊 dashboard.js loaded");

// ======================= 🚀 INSTANT CACHE RENDER =======================
// run immediately (no DOMContentLoaded delay)
(function () {
    const cached = localStorage.getItem("dashboardStats");
    const cachedTime = localStorage.getItem("dashboardStatsTime");

    if (!cached || !cachedTime) return;

    const age = Date.now() - parseInt(cachedTime);
    const FIVE_MIN = 5 * 60 * 1000;

    if (age > FIVE_MIN) return;

    const data = JSON.parse(cached);

    // store temporarily for later use
    window.__DASHBOARD_CACHE__ = data;
})();

// ======================= DOM READY =======================
document.addEventListener("DOMContentLoaded", () => {

    if (window.__DASHBOARD_CACHE__) {
        renderFromCache(window.__DASHBOARD_CACHE__);
    } else {
        setLoadingState(); // only first visit
    }

    loadDashboardStats();

    setInterval(() => {
        loadDashboardStats(true);
    }, 30000);
});

// ======================= MAIN LOAD =======================
async function loadDashboardStats(isAutoRefresh = false) {
    try {
        const res = await fetch('/api/dashboard/stats', {
            credentials: 'include'
        });

        if (!res.ok) throw new Error("API failed");

        const data = await res.json();
        console.log("DASHBOARD DATA:", data);

        // save cache
        localStorage.setItem("dashboardStats", JSON.stringify(data));
        localStorage.setItem("dashboardStatsTime", Date.now());

        updateStat("biAgentCount", data.bi_agents?.count || 0, isAutoRefresh);
        updateStat("connectionCount", data.connections?.count || 0, isAutoRefresh);
        updateStat("reasoningAgentCount", data.reasoning_agents?.count || 0, isAutoRefresh);
        updateStat("documentCount", data.knowledge_docs?.count || 0, isAutoRefresh);

        renderRecentAgents(data.bi_agents?.items || []);
        renderRecentReasoningAgents(data.reasoning_agents?.items || []);

    } catch (err) {
        console.error("Dashboard load failed:", err);
        setErrorState();
    }
}

// ======================= CACHE RENDER =======================
function renderFromCache(data) {
    // remove skeleton instantly
    ["biAgentCount", "connectionCount", "reasoningAgentCount", "documentCount"]
        .forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.classList.remove("skeleton");
                el.innerText = data[idMap(id)] || 0;
            }
        });

    renderRecentAgents(data.bi_agents?.items || []);
    renderRecentReasoningAgents(data.reasoning_agents?.items || []);
}

// helper mapping
function idMap(id) {
    return {
        biAgentCount: "bi_agents",
        connectionCount: "connections",
        reasoningAgentCount: "reasoning_agents",
        documentCount: "knowledge_docs"
    }[id]?.count;
}

// ======================= UPDATE =======================
function updateStat(id, newValue, isAutoRefresh) {
    const el = document.getElementById(id);
    if (!el) return;

    el.classList.remove("skeleton");

    if (isAutoRefresh) {
        highlightChange(id, newValue);
    } else {
        animateNumber(id, newValue);
    }
}

// ======================= LOADING =======================
function setLoadingState() {
    ["biAgentCount", "connectionCount", "reasoningAgentCount", "documentCount"]
        .forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.innerText = "";
                el.classList.add("skeleton");
            }
        });
}

// ======================= ERROR =======================
function setErrorState() {
    ["biAgentCount", "connectionCount", "reasoningAgentCount", "documentCount"]
        .forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.innerText = "—";
                el.classList.remove("skeleton");
            }
        });

    const container = document.getElementById("recentAgents");
    if (container) {
        container.innerHTML = `<span class="text-danger">Failed to load data</span>`;
    }
}

// ======================= ANIMATION =======================
function animateNumber(elementId, newValue) {
    const el = document.getElementById(elementId);
    if (!el) return;

    const start = parseInt(el.innerText) || 0;
    const end = newValue;
    const duration = 300;

    let startTime = null;

    function update(currentTime) {
        if (!startTime) startTime = currentTime;
        const progress = Math.min((currentTime - startTime) / duration, 1);

        el.innerText = Math.floor(progress * (end - start) + start);

        if (progress < 1) requestAnimationFrame(update);
    }

    requestAnimationFrame(update);
}

// ======================= CHANGE =======================
function highlightChange(id, newValue) {
    const el = document.getElementById(id);
    if (!el) return;

    const oldValue = parseInt(el.innerText) || 0;

    if (newValue > oldValue) {
        el.style.color = "green";
        el.innerText = `+${newValue}`;
    } else if (newValue < oldValue) {
        el.style.color = "red";
        el.innerText = newValue;
    } else {
        el.innerText = newValue;
    }

    setTimeout(() => {
        el.style.color = "";
        el.innerText = newValue;
    }, 1200);
}

// ======================= LISTS =======================
function renderRecentAgents(agents) {
    const container = document.getElementById("recentAgents");
    if (!container) return;

    container.innerHTML = agents.length
        ? agents.slice(0, 3).map(agent => `
            <div class="d-flex justify-content-between align-items-center mb-2">
                <span>${agent.name}</span>
                <span class="status-badge status-active">Active</span>
            </div>
        `).join("")
        : `<span class="text-muted">No agents available</span>`;
}

function renderRecentReasoningAgents(agents) {
    const container = document.getElementById("recentReasoningAgents");
    if (!container) return;

    container.innerHTML = agents.length
        ? agents.slice(0, 3).map(agent => `
            <div class="d-flex justify-content-between align-items-center mb-2">
                <span>${agent.name}</span>
                <span class="status-badge status-active">Active</span>
            </div>
        `).join("")
        : `<span class="text-muted">No reasoning agents yet</span>`;
}