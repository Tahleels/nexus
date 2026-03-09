// static/reports.js

class ReportManager {
    constructor() {
        this.currentConfig = { title: "", subtitle: "", columns: [] };
        this.currentData = { columns: [], rows: [] };
        this.filteredData = [];
        this.currentPage = 1;
        this.recordsPerPage = 25;
        this.currentSort = { column: "", direction: "asc" };
    }

    // Called when parent sends data into iframe
    loadFromMessage(config, rawData) {
        console.log("📄 Loading report data inside reports.js…");

        this.currentConfig = config || {};
        this.currentData = {
            columns: config?.columns || [],
            rows: rawData || []
        };

        // Full report data for Excel export
        window.fullReportData = {
            columns: this.currentData.columns,
            rows: [...this.currentData.rows]
        };

        this.filteredData = [...this.currentData.rows];

        this.renderReport();
        this.setupEventListeners();
    }

    // Render the whole report
    renderReport() {
        this.updateHeader();
        this.renderTableHeaders();
        this.calculateSummary();
        this.displayData();
    }

    updateHeader() {
        const titleEl = document.getElementById("reportTitle");
        const subtitleEl = document.getElementById("reportSubtitle");
        const dateEl = document.getElementById("reportDate");

        if (titleEl) titleEl.textContent = this.currentConfig.title || "Generated Report";
        if (subtitleEl) subtitleEl.textContent = this.currentConfig.subtitle || "";
        if (dateEl) dateEl.textContent = new Date().toLocaleString();
    }

    renderTableHeaders() {
        const thead = document.getElementById("tableHeaders");
        if (!thead) return;

        let html = "<tr>";
        this.currentData.columns.forEach(col => {
            html += `<th class="sortable" data-column="${col}">${col}</th>`;
        });
        html += "</tr>";

        thead.innerHTML = html;
    }

    calculateSummary() {
        const rows = this.currentData.rows;
        const total = rows.length;

        const numericCols = this.currentData.columns.filter(c => {
            const v = rows?.[0]?.[c];
            return !isNaN(parseFloat(v));
        });

        let numericSummary = "N/A";
        if (numericCols.length > 0) {
            const col = numericCols[0];
            const numbers = rows.map(r => parseFloat(r[col])).filter(x => !isNaN(x));
            const sum = numbers.reduce((a, b) => a + b, 0);
            numericSummary = sum.toLocaleString();
        }

        const unique = new Set();
        rows.forEach(r => {
            this.currentData.columns.forEach(c => {
                if (r[c] !== undefined) unique.add(`${c}_${r[c]}`);
            });
        });

        // Update UI
        const totalRecordsEl = document.getElementById("totalRecords");
        const numericEl = document.getElementById("numericColumnsSummary");
        const uniqueEl = document.getElementById("uniqueValues");

        if (totalRecordsEl) totalRecordsEl.textContent = total.toLocaleString();
        if (numericEl) numericEl.textContent = numericSummary;
        if (uniqueEl) uniqueEl.textContent = unique.size.toLocaleString();
    }

    displayData() {
        const tbody = document.getElementById("reportTableBody");
        if (!tbody) return;

        if (this.filteredData.length === 0) {
            tbody.innerHTML = `
                <tr><td colspan="100" class="no-data">No Data Available</td></tr>
            `;
            return;
        }

        const start = (this.currentPage - 1) * this.recordsPerPage;
        const end = start + this.recordsPerPage;
        const pageRows = this.filteredData.slice(start, end);

        let html = "";
        pageRows.forEach(row => {
            html += "<tr>";
            this.currentData.columns.forEach(col => {
                let val = row[col];
                const numeric = !isNaN(parseFloat(val));
                html += `<td class="${numeric ? "numeric" : ""}">${val ?? ""}</td>`;
            });
            html += "</tr>";
        });

        tbody.innerHTML = html;
        this.updatePagination();
    }

    updatePagination() {
        const total = this.filteredData.length;
        const pages = Math.ceil(total / this.recordsPerPage);

        const currentPageEl = document.getElementById("currentPage");
        const totalPagesEl = document.getElementById("totalPages");
        const totalRecordsFooter = document.getElementById("totalRecordsFooter");

        if (currentPageEl) currentPageEl.textContent = this.currentPage;
        if (totalPagesEl) totalPagesEl.textContent = pages;
        if (totalRecordsFooter) totalRecordsFooter.textContent = total;

        const pageNumbersEl = document.getElementById("pageNumbers");
        if (!pageNumbersEl) return;

        let html = "";
        for (let i = Math.max(1, this.currentPage - 2); i <= Math.min(pages, this.currentPage + 2); i++) {
            html += `<button class="page-btn ${i === this.currentPage ? "active" : ""}" 
                     onclick="window.reportManager.goToPage(${i})">${i}</button>`;
        }
        pageNumbersEl.innerHTML = html;
    }

    goToPage(page) {
        this.currentPage = page;
        this.displayData();
    }

    // Sorting logic
    handleSort(column) {
        if (this.currentSort.column === column) {
            this.currentSort.direction =
                this.currentSort.direction === "asc" ? "desc" : "asc";
        } else {
            this.currentSort = { column, direction: "asc" };
        }

        this.filteredData.sort((a, b) => {
            let x = a[column] ?? "";
            let y = b[column] ?? "";

            if (!isNaN(x) && !isNaN(y)) {
                x = parseFloat(x);
                y = parseFloat(y);
            } else {
                x = String(x).toLowerCase();
                y = String(y).toLowerCase();
            }

            return this.currentSort.direction === "asc" ? x - y : y - x;
        });

        this.displayData();
    }

    setupEventListeners() {
        document.addEventListener("click", e => {
            if (e.target.classList.contains("sortable")) {
                this.handleSort(e.target.dataset.column);
            }
        });

        const prev = document.getElementById("prevPageBtn");
        const next = document.getElementById("nextPageBtn");

        if (prev) prev.onclick = () => this.goToPage(this.currentPage - 1);
        if (next) next.onclick = () => this.goToPage(this.currentPage + 1);
    }
}

// ---------------------------
// Global Excel Export Function
// ---------------------------
function exportToExcel() {
    if (!window.fullReportData) return alert("No data found!");

    let html = "<table><tr>";

    window.fullReportData.columns.forEach(col => {
        html += `<th>${col}</th>`;
    });

    html += "</tr>";

    window.fullReportData.rows.forEach(row => {
        html += "<tr>";
        window.fullReportData.columns.forEach(col => {
            html += `<td>${row[col] ?? ""}</td>`;
        });
        html += "</tr>";
    });

    html += "</table>";

    const blob = new Blob([html], { type: "application/vnd.ms-excel" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "report.xls";
    link.click();
}

async function exportToPDF() {
    const reportElement = document.querySelector(".report-container");

    if (!reportElement) {
        alert("Cannot find report container.");
        return;
    }

    // Scroll to top for accurate rendering
    window.scrollTo(0, 0);

    // Capture FULL report area
    const canvas = await html2canvas(reportElement, {
        scale: 2,
        useCORS: true
    });

    const imgData = canvas.toDataURL("image/png");

    // Canvas dimensions (pixels)
    const canvasWidth = canvas.width;
    const canvasHeight = canvas.height;

    // Convert px → mm (1px = 0.264583 mm)
    const mmWidth = canvasWidth * 0.264583;
    const mmHeight = canvasHeight * 0.264583;

    // Create ONE-PAGE infinite height PDF
    const { jsPDF } = window.jspdf;
    const pdf = new jsPDF({
        orientation: "p",
        unit: "mm",
        format: [mmWidth, mmHeight]
    });

    // Place full report as one-page image
    pdf.addImage(imgData, "PNG", 0, 0, mmWidth, mmHeight);

    pdf.save("report.pdf");
}



// Initialize manager globally
window.reportManager = new ReportManager();

// Parent page will call this via postMessage
window.addEventListener("message", async (event) => {
    if (event.data.type !== "reportData") return;

    console.log("📄 Loading report data inside reports.js…");

    const config = event.data.config;
    const rawData = event.data.rawData;

    // 🛑 1. IGNORE EMPTY RAWDATA (THIS FIXES YOUR ISSUE)
    if (!rawData || !Array.isArray(rawData) || rawData.length === 0) {
        console.warn("⚠ Empty rawData received — ignoring message");
        return;
    }

    // ⭐ FIX: Convert the LLM "tables" format → standard "columns" + "rows"

    let finalColumns = [];
    let finalRows = [];

    if (config.tables && config.tables.length > 0) {
        const detailTable = config.tables.find(t => t.type === "detail");

        if (detailTable && detailTable.columns) {
            finalColumns = detailTable.columns.map(c => c.field);

            finalRows = rawData.map(row => {
                const cleaned = {};
                detailTable.columns.forEach(col => {
                    cleaned[col.field] = row[col.field];
                });
                return cleaned;
            });
        }
    }

    // ⛳ 2. FALLBACK (but ONLY if rows exist)
    if (finalColumns.length === 0) {
        finalColumns = Object.keys(rawData[0]);
        finalRows = rawData;
    }

    window.reportManager.currentConfig = config;
    window.reportManager.currentData = {
        columns: finalColumns,
        rows: finalRows
    };

    window.fullReportData = window.reportManager.currentData;

    window.reportManager.filteredData = [...finalRows];

    window.reportManager.renderReport();
});


