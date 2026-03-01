/**
 * training.js — SLM Training Data review UI
 * Namespace: TD (Training Data)
 */

const TD = (() => {
  // ── state ──────────────────────────────────────────────────────────────────
  let _state = {
    page:      1,
    per_page:  50,
    total:     0,
    filters:   {},
    selected:  new Set(),
    activePairId: null,
    selectedFmt:  "alpaca_jsonl",
  };

  // ── init ───────────────────────────────────────────────────────────────────
  function init() {
    loadPairs();
    _wireExtractRadios();
  }

  // ── filter helpers ─────────────────────────────────────────────────────────
  function applyFilters() {
    _state.page = 1;
    _state.selected.clear();
    _updateBulkButtons();
    loadPairs();
  }

  function _buildParams(extra = {}) {
    const p = new URLSearchParams({
      page:      _state.page,
      per_page:  _state.per_page,
      status:    document.getElementById("filter-status")?.value  || "",
      domain:    document.getElementById("filter-domain")?.value  || "",
      min_score: document.getElementById("filter-min-score")?.value || 0,
      max_score: 100,
      ...extra,
    });
    return p.toString();
  }

  // ── load pairs ─────────────────────────────────────────────────────────────
  async function loadPairs() {
    const tbody = document.getElementById("pairs-tbody");
    tbody.innerHTML = `<tr><td colspan="9" class="text-center py-4 text-muted">
      <div class="spinner-border spinner-border-sm me-2"></div>Loading…</td></tr>`;

    try {
      const res  = await fetch(`/api/workspace/training/pairs?${_buildParams()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);

      _state.total = data.total;
      _renderTable(data.pairs);
      _renderPagination(data.total, data.page, data.per_page);
      document.getElementById("td-count-label").textContent =
        `${data.total.toLocaleString()} pairs total`;
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center py-3 text-danger">
        <i class="fas fa-exclamation-circle me-1"></i>${e.message}</td></tr>`;
    }
  }

  // ── render table rows ──────────────────────────────────────────────────────
  function _renderTable(pairs) {
    const tbody = document.getElementById("pairs-tbody");
    if (!pairs.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center py-5 text-muted">
        <i class="fas fa-inbox fa-2x d-block mb-2 opacity-40"></i>
        No training pairs match your filters.<br>
        <button class="btn btn-sm btn-outline-primary mt-2" onclick="TD.openExtractModal()">
          <i class="fas fa-magic me-1"></i>Extract from conversations
        </button></td></tr>`;
      return;
    }

    tbody.innerHTML = pairs.map(p => _pairRow(p)).join("");
  }

  function _pairRow(p) {
    const scoreClass = p.quality_score >= 75 ? "td-score-high"
                     : p.quality_score >= 40 ? "td-score-mid"
                     : "td-score-low";
    const statusClass = `status-${p.status}`;
    const statusLabel = p.status.replace("_", " ");
    const created = p.created_at ? p.created_at.slice(0, 10) : "—";
    const modelShort = p.model_used
      ? p.model_used.replace("gpt-", "").replace("-preview", "").substring(0, 8)
      : "—";
    const checked = _state.selected.has(p.id) ? "checked" : "";

    const iconFlags = [
      p.has_citations ? `<i class="fas fa-link text-info" title="Has citations"></i>` : "",
      p.has_artifact  ? `<i class="fas fa-paperclip text-purple ms-1" title="Has artifact"></i>` : "",
      p.is_edited     ? `<i class="fas fa-pen text-warning ms-1" title="Was edited"></i>` : "",
      p.feedback_rating === "thumbs_up"   ? `<i class="fas fa-thumbs-up text-success ms-1"></i>` : "",
      p.feedback_rating === "thumbs_down" ? `<i class="fas fa-thumbs-down text-danger ms-1"></i>` : "",
    ].filter(Boolean).join("");

    const instr = _esc(p.instruction || "").substring(0, 120);
    const out   = _esc(p.output || "").substring(0, 120);

    return `
    <tr class="td-pair-row" id="row-${p.id}">
      <td class="ps-3">
        <input type="checkbox" class="pair-checkbox" data-id="${p.id}"
               ${checked} onchange="TD.onCheckboxChange(${p.id}, this.checked)">
      </td>
      <td class="td-text-cell">${instr}…</td>
      <td class="td-text-cell">${out}…</td>
      <td>
        <span class="td-score-badge ${scoreClass}">${Math.round(p.quality_score)}</span>
        <div class="td-quality-bar mt-1">
          <div class="td-quality-bar-fill ${scoreClass}" style="width:${p.quality_score}%"></div>
        </div>
      </td>
      <td><span class="td-status-badge ${statusClass}">${statusLabel}</span></td>
      <td class="small text-muted">${_esc(p.domain || "—")}</td>
      <td class="small text-muted">${modelShort} ${iconFlags}</td>
      <td class="small text-muted">${created}</td>
      <td class="td-actions-col">
        <button class="btn btn-xs btn-outline-primary py-0 px-2"
                title="Review" onclick="TD.openReview(${p.id})">
          <i class="fas fa-search-plus"></i>
        </button>
        ${p.status === 'pending' || p.status === 'needs_review' ? `
        <button class="btn btn-xs btn-outline-success py-0 px-2 ms-1"
                title="Quick approve" onclick="TD.quickApprove(${p.id})">
          <i class="fas fa-check"></i>
        </button>
        <button class="btn btn-xs btn-outline-danger py-0 px-2 ms-1"
                title="Quick reject" onclick="TD.quickReject(${p.id})">
          <i class="fas fa-times"></i>
        </button>` : ""}
      </td>
    </tr>`;
  }

  // ── pagination ─────────────────────────────────────────────────────────────
  function _renderPagination(total, page, per_page) {
    const pages = Math.ceil(total / per_page) || 1;
    const el    = document.getElementById("td-pagination");
    if (pages <= 1) { el.innerHTML = ""; return; }

    let html = "";
    const addBtn = (label, p, disabled = false) => {
      html += `<button class="btn btn-sm ${p === _state.page ? 'btn-primary' : 'btn-outline-secondary'}"
               ${disabled ? "disabled" : ""} onclick="TD.goPage(${p})">${label}</button>`;
    };

    addBtn("«", 1, page === 1);
    addBtn("‹", page - 1, page === 1);
    const start = Math.max(1, page - 2);
    const end   = Math.min(pages, page + 2);
    for (let i = start; i <= end; i++) addBtn(i, i);
    addBtn("›", page + 1, page === pages);
    addBtn("»", pages, page === pages);
    el.innerHTML = html;
  }

  function goPage(p) {
    _state.page = p;
    loadPairs();
  }

  // ── checkbox / bulk ────────────────────────────────────────────────────────
  function toggleSelectAll(checked) {
    document.querySelectorAll(".pair-checkbox").forEach(cb => {
      cb.checked = checked;
      const id = parseInt(cb.dataset.id);
      checked ? _state.selected.add(id) : _state.selected.delete(id);
    });
    _updateBulkButtons();
  }

  function onCheckboxChange(id, checked) {
    checked ? _state.selected.add(id) : _state.selected.delete(id);
    _updateBulkButtons();
  }

  function _updateBulkButtons() {
    const n = _state.selected.size;
    const approveBtn = document.getElementById("bulk-approve-btn");
    const rejectBtn  = document.getElementById("bulk-reject-btn");
    if (approveBtn) approveBtn.disabled = n === 0;
    if (rejectBtn)  rejectBtn.disabled  = n === 0;
    if (approveBtn) approveBtn.textContent = n > 0
      ? `✓ Approve (${n})` : "Approve Selected";
    if (rejectBtn)  rejectBtn.textContent  = n > 0
      ? `✗ Reject (${n})` : "Reject Selected";
  }

  async function bulkAction(action) {
    if (_state.selected.size === 0) return;
    const ids = Array.from(_state.selected);
    try {
      const res = await fetch("/api/workspace/training/pairs/bulk-review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids, action }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      _state.selected.clear();
      document.getElementById("select-all").checked = false;
      _updateBulkButtons();
      _showToast(`${action === "approve" ? "Approved" : "Rejected"} ${data.updated} pairs`, "success");
      loadPairs();
      refreshStats();
    } catch (e) {
      _showToast(e.message, "danger");
    }
  }

  // ── quick actions ──────────────────────────────────────────────────────────
  async function quickApprove(id) {
    await _reviewRequest(id, "approve", "");
    loadPairs(); refreshStats();
  }

  async function quickReject(id) {
    await _reviewRequest(id, "reject", "");
    loadPairs(); refreshStats();
  }

  async function _reviewRequest(id, action, notes) {
    try {
      const res = await fetch(`/api/workspace/training/pairs/${id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, notes }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      return data;
    } catch (e) {
      _showToast(e.message, "danger");
      return null;
    }
  }

  // ── review modal ───────────────────────────────────────────────────────────
  async function openReview(pairId) {
    _state.activePairId = pairId;

    document.getElementById("review-pair-id").textContent = `#${pairId}`;
    document.getElementById("review-instruction").textContent = "Loading…";
    document.getElementById("review-output").textContent = "";
    document.getElementById("review-score").textContent = "—";
    document.getElementById("review-status-badge").innerHTML = "—";
    document.getElementById("review-domain").value = "";
    document.getElementById("review-tags").value = "";
    document.getElementById("review-notes").value = "";
    document.getElementById("review-breakdown").innerHTML = "";

    const modal = new bootstrap.Modal(document.getElementById("reviewModal"));
    modal.show();

    try {
      const res  = await fetch(`/api/workspace/training/pairs?page=1&per_page=200`);
      // Re-use the list endpoint for now; find the pair in results
      const data = await res.json();
      const pair = (data.pairs || []).find(p => p.id === pairId);
      if (!pair) {
        document.getElementById("review-instruction").textContent = "Pair not found.";
        return;
      }
      _fillReviewModal(pair);
    } catch (e) {
      document.getElementById("review-instruction").textContent = `Error: ${e.message}`;
    }
  }

  function _fillReviewModal(p) {
    document.getElementById("review-instruction").textContent = p.instruction || "";
    document.getElementById("review-output").textContent      = p.output || "";

    const sc = Math.round(p.quality_score);
    const scoreClass = sc >= 75 ? "td-score-high" : sc >= 40 ? "td-score-mid" : "td-score-low";
    document.getElementById("review-score").innerHTML =
      `<span class="td-score-badge ${scoreClass} fs-5">${sc}</span>`;

    const statusClass = `status-${p.status}`;
    document.getElementById("review-status-badge").innerHTML =
      `<span class="td-status-badge ${statusClass}">${p.status.replace("_"," ")}</span>`;

    document.getElementById("review-domain").value = p.domain || "";
    document.getElementById("review-tags").value   = p.tags   || "";
    document.getElementById("review-notes").value  = p.review_notes || "";

    // Quality breakdown
    let bd = null;
    try { bd = p.quality_breakdown ? JSON.parse(p.quality_breakdown) : null; } catch {}
    if (bd) {
      const rows = Object.entries(bd)
        .map(([k, v]) => `<tr><td class="text-capitalize text-muted small">${k.replace(/_/g," ")}</td>
          <td class="text-end fw-semibold small ${v >= 0 ? 'text-success' : 'text-danger'}">
            ${v >= 0 ? "+" : ""}${v}
          </td></tr>`)
        .join("");
      document.getElementById("review-breakdown").innerHTML = `
        <h6 class="small fw-semibold mt-2 mb-1">Score Breakdown</h6>
        <table class="table table-sm table-borderless mb-0" style="max-width:260px;">${rows}</table>`;
    }
  }

  async function reviewAction(action) {
    const id    = _state.activePairId;
    const notes = document.getElementById("review-notes").value;
    const domain= document.getElementById("review-domain").value;
    const tags  = document.getElementById("review-tags").value;

    let payload = { action, notes, domain, tags };
    if (action === "tag") payload = { action: "tag", domain, tags };

    try {
      const res = await fetch(`/api/workspace/training/pairs/${id}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      bootstrap.Modal.getInstance(document.getElementById("reviewModal"))?.hide();
      _showToast(
        action === "approve" ? "Pair approved ✓"
        : action === "reject" ? "Pair rejected"
        : "Tags saved",
        action === "reject" ? "warning" : "success"
      );
      loadPairs();
      refreshStats();
    } catch (e) {
      _showToast(e.message, "danger");
    }
  }

  // ── extract modal ──────────────────────────────────────────────────────────
  function openExtractModal() {
    document.getElementById("extract-result").classList.add("d-none");
    new bootstrap.Modal(document.getElementById("extractModal")).show();
  }

  function _wireExtractRadios() {
    document.querySelectorAll("input[name='extract-scope']").forEach(r => {
      r.addEventListener("change", () => {
        const row = document.getElementById("extract-conv-id-row");
        row.classList.toggle("d-none", r.value !== "one");
      });
    });
  }

  async function runExtract() {
    const scope   = document.querySelector("input[name='extract-scope']:checked")?.value || "all";
    const convId  = document.getElementById("extract-conv-id")?.value;
    const spinner = document.getElementById("extract-spinner");
    const result  = document.getElementById("extract-result");

    spinner.classList.remove("d-none");
    result.classList.add("d-none");

    const body = scope === "all"
      ? { extract_all: true }
      : { conversation_id: parseInt(convId) };

    try {
      const res  = await fetch("/api/workspace/training/extract", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      result.className = "mt-3 alert alert-success small";
      result.innerHTML = scope === "all"
        ? `<i class="fas fa-check-circle me-1"></i>Inserted <strong>${data.inserted}</strong> new pairs
           from <strong>${data.conversations_processed}</strong> conversations.`
        : `<i class="fas fa-check-circle me-1"></i>Inserted <strong>${data.inserted}</strong> new pairs.`;
      result.classList.remove("d-none");
      loadPairs();
      refreshStats();
    } catch (e) {
      result.className = "mt-3 alert alert-danger small";
      result.textContent = e.message;
      result.classList.remove("d-none");
    } finally {
      spinner.classList.add("d-none");
    }
  }

  // ── export modal ───────────────────────────────────────────────────────────
  function openExportModal() {
    new bootstrap.Modal(document.getElementById("exportModal")).show();
  }

  function selectFmt(card) {
    document.querySelectorAll(".td-fmt-card").forEach(c => c.classList.remove("selected"));
    card.classList.add("selected");
    _state.selectedFmt = card.dataset.fmt;

    // Hide regular filters for DPO (they're not applicable)
    const filterRow = document.getElementById("export-filters-row");
    if (_state.selectedFmt === "dpo_jsonl") {
      filterRow.style.opacity = "0.4";
      filterRow.style.pointerEvents = "none";
    } else {
      filterRow.style.opacity = "";
      filterRow.style.pointerEvents = "";
    }
  }

  async function runExport() {
    const spinner   = document.getElementById("export-spinner");
    const fmt       = _state.selectedFmt;
    const domain    = document.getElementById("export-domain")?.value || "";
    const minScore  = document.getElementById("export-min-score")?.value || 0;
    const inclAuto  = document.getElementById("export-include-auto")?.checked ? 1 : 0;

    spinner.classList.remove("d-none");

    try {
      const params = new URLSearchParams({
        format:       fmt,
        domain,
        min_score:    minScore,
        include_auto: inclAuto,
      });
      const res = await fetch(`/api/workspace/training/export?${params}`);
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || res.statusText);
      }

      const blob        = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const fnMatch     = disposition.match(/filename="([^"]+)"/);
      const fileName    = fnMatch ? fnMatch[1] : `training_${fmt}.jsonl`;

      const url  = URL.createObjectURL(blob);
      const a    = document.createElement("a");
      a.href     = url;
      a.download = fileName;
      a.click();
      URL.revokeObjectURL(url);

      bootstrap.Modal.getInstance(document.getElementById("exportModal"))?.hide();
      _showToast(`Exported ${res.headers.get("X-Pair-Count") || "?"} pairs as ${fmt}`, "success");
    } catch (e) {
      _showToast(e.message, "danger");
    } finally {
      spinner.classList.add("d-none");
    }
  }

  // ── stats refresh ──────────────────────────────────────────────────────────
  async function refreshStats() {
    try {
      const res  = await fetch("/api/workspace/training/stats");
      const data = await res.json();
      if (!res.ok) return;

      _setKpi("kpi-total",       data.total_pairs);
      _setKpi("kpi-ready",       data.export_ready);
      _setKpi("kpi-pending",     data.pending);
      _setKpi("kpi-needs-review",data.needs_review);
      _setKpi("kpi-avg-q",       data.avg_quality);
      _setKpi("kpi-pref",        data.preference_pairs);
    } catch {}
  }

  function _setKpi(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value ?? "—";
  }

  // ── helpers ────────────────────────────────────────────────────────────────
  function _esc(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  let _toastEl = null;
  function _showToast(msg, type = "success") {
    if (!_toastEl) {
      _toastEl = document.createElement("div");
      _toastEl.style.cssText = `
        position:fixed;bottom:24px;right:24px;z-index:9999;
        padding:10px 18px;border-radius:8px;font-size:.85rem;
        box-shadow:0 4px 12px rgba(0,0,0,.15);transition:opacity .3s;`;
      document.body.appendChild(_toastEl);
    }
    const colors = { success: "#065f46:#d1fae5", danger: "#991b1b:#fee2e2", warning: "#92400e:#fef3c7" };
    const [color, bg] = (colors[type] || "#1f2937:#f3f4f6").split(":");
    _toastEl.style.color      = color;
    _toastEl.style.background = bg;
    _toastEl.style.opacity    = "1";
    _toastEl.textContent      = msg;
    clearTimeout(_toastEl._timer);
    _toastEl._timer = setTimeout(() => { _toastEl.style.opacity = "0"; }, 3500);
  }

  // ── public API ─────────────────────────────────────────────────────────────
  return {
    init,
    applyFilters,
    loadPairs,
    goPage,
    toggleSelectAll,
    onCheckboxChange,
    bulkAction,
    quickApprove,
    quickReject,
    openReview,
    reviewAction,
    openExtractModal,
    runExtract,
    openExportModal,
    selectFmt,
    runExport,
    refreshStats,
  };
})();

document.addEventListener("DOMContentLoaded", () => TD.init());
