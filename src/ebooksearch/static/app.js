// ebooksearch SPA — vanilla JS, no build step.

const $ = (sel) => document.querySelector(sel);

const els = {
    q: $("#q"),
    results: $("#results"),
    resultsTitle: $("#results-title"),
    empty: $("#empty"),
    stats: $("#stats"),
    reindex: $("#reindex"),
    phase: $("#phase"),
    summary: $("#summary"),
    barFill: $("#bar-fill"),
    errorsWrap: $("#errors-wrap"),
    errCount: $("#err-count"),
    errors: $("#errors"),
};

const fmtBytes = (n) => {
    if (n == null) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
};

const fmtAgo = (iso) => {
    if (!iso) return "—";
    const t = new Date(iso);
    if (isNaN(t)) return "—";
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60) return `${Math.floor(diff)}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
};

const triggerLabel = (t) => ({ startup: "Startup", manual: "Manual", watch: "Folder change" }[t] || t || "—");

const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

// ---------------------------------------------------------------------------
// Stats / header
// ---------------------------------------------------------------------------
async function refreshStats() {
    try {
        const r = await fetch("/api/stats");
        const s = await r.json();
        const slots = els.stats.children;
        slots[0].textContent = `${s.total_books.toLocaleString()} books`;
        slots[1].textContent = `${fmtBytes(s.db_size_bytes)} DB`;
        if (s.last_run && s.last_run.ended_at) {
            slots[2].textContent = `last reindex ${fmtAgo(s.last_run.ended_at)}`;
            slots[2].title = `${triggerLabel(s.last_run.trigger)} · ${s.last_run.started_at} → ${s.last_run.ended_at} (${s.last_run.duration_seconds}s)`;
        } else if (s.last_run) {
            slots[2].textContent = `running…`;
            slots[2].title = s.last_run.started_at;
        } else {
            slots[2].textContent = "no reindex yet";
            slots[2].title = "";
        }
    } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Results list — shared accordion for search + recent
// ---------------------------------------------------------------------------
function renderResults(rows, { showAgo = false } = {}) {
    els.results.innerHTML = "";
    els.empty.classList.toggle("hidden", rows.length > 0);
    for (const b of rows) {
        const li = document.createElement("li");
        li.className = "row collapsed";
        li.dataset.id = b.id;
        const ago = showAgo && b.indexed_at ? `<span class="ago" title="${esc(b.indexed_at)}">${fmtAgo(b.indexed_at)}</span>` : "";
        li.innerHTML = `
            <div class="row-head" tabindex="0" role="button" aria-expanded="false">
                <div class="title">${esc(b.title || b.filename)}</div>
                <div class="meta">
                    ${b.author ? `<span class="author">${esc(b.author)}</span>` : ""}
                    <span class="badge">${esc((b.ext || "").replace(".", ""))}</span>
                    <span class="size">${fmtBytes(b.size_bytes)}</span>
                    ${ago}
                </div>
            </div>
            <div class="row-body"><div class="loading">Loading…</div></div>
        `;
        const head = li.querySelector(".row-head");
        const open = () => toggleRow(li);
        head.addEventListener("click", open);
        head.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
        });
        els.results.appendChild(li);
    }
}

async function toggleRow(li) {
    const collapsed = li.classList.contains("collapsed");
    li.classList.toggle("collapsed");
    li.querySelector(".row-head").setAttribute("aria-expanded", String(collapsed));
    if (collapsed && !li.dataset.loaded) {
        await loadBody(li);
    }
}

async function loadBody(li) {
    const id = li.dataset.id;
    const body = li.querySelector(".row-body");
    try {
        const r = await fetch(`/api/book/${id}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const b = await r.json();
        body.innerHTML = renderDetail(b);
        li.dataset.loaded = "1";
    } catch (e) {
        body.innerHTML = `<div class="empty">Failed to load: ${esc(e.message)}</div>`;
    }
}

function renderDetail(b) {
    const meta = [
        ["Title", b.title],
        ["Author", b.author],
        ["Language", b.language],
        ["Publisher", b.publisher],
        ["Published", b.pub_date],
        ["Pages", b.page_count],
        ["File", b.filename],
        ["Size", fmtBytes(b.size_bytes)],
        ["Indexed", b.indexed_at],
    ].filter(([, v]) => v !== null && v !== undefined && v !== "");

    const dl = meta.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    const toc = (b.toc && b.toc.length)
        ? `<ul class="toc">${b.toc.slice(0, 50).map(t => `<li>${esc(t.title)}</li>`).join("")}</ul>`
        : "";
    const snippet = b.snippet ? `<div class="snippet">${esc(b.snippet)}</div>` : "";
    return `
        <dl>${dl}</dl>
        ${snippet}
        ${toc}
        <div class="actions">
            <a class="download" href="/api/download/${b.id}" download>Download</a>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Search + recent
// ---------------------------------------------------------------------------
let searchSeq = 0;

async function runSearch() {
    const q = els.q.value.trim();
    const seq = ++searchSeq;
    if (!q) {
        els.resultsTitle.textContent = "Recently indexed";
        const r = await fetch(`/api/recent?limit=30`);
        if (seq !== searchSeq) return;
        const data = await r.json();
        renderResults(data.results, { showAgo: true });
        return;
    }
    els.resultsTitle.textContent = "Search results";
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=100`);
    if (seq !== searchSeq) return;
    const data = await r.json();
    renderResults(data.results);
}

const debounce = (fn, ms) => {
    let t;
    return (...args) => {
        clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
    };
};

els.q.addEventListener("input", debounce(runSearch, 180));

// ---------------------------------------------------------------------------
// Reindex
// ---------------------------------------------------------------------------
els.reindex.addEventListener("click", async () => {
    els.reindex.disabled = true;
    try {
        await fetch("/api/reindex", { method: "POST" });
    } finally {
        // Re-enabled when SSE reports terminal.
    }
});

// ---------------------------------------------------------------------------
// SSE: live progress
// ---------------------------------------------------------------------------
function connectStream() {
    const es = new EventSource("/api/index/stream");
    es.addEventListener("progress", (e) => onProgress(JSON.parse(e.data), false));
    es.addEventListener("terminal", (e) => onProgress(JSON.parse(e.data), true));
    es.addEventListener("error", () => {
        // EventSource auto-reconnects; nothing to do.
    });
}

function onProgress(snap, terminal) {
    const isIdle = snap.status === "idle" || snap.status === "done" || snap.status === "error";

    els.phase.className = `phase ${snap.status}`;
    if (isIdle && snap.last_run) {
        const lr = snap.last_run;
        els.phase.textContent = triggerLabel(lr.trigger);
        const parts = [];
        if (lr.added) parts.push(`+${lr.added}`);
        if (lr.updated) parts.push(`~${lr.updated}`);
        if (lr.removed) parts.push(`−${lr.removed}`);
        if (lr.skipped) parts.push(`${lr.skipped} skipped`);
        if (lr.error_count) parts.push(`${lr.error_count} errors`);
        const dur = lr.duration_seconds != null ? ` · ${lr.duration_seconds}s` : "";
        els.summary.textContent = (parts.join(" ") || "no changes") + dur;
        els.barFill.style.width = "100%";
    } else if (isIdle) {
        els.phase.textContent = "idle";
        els.summary.textContent = "";
        els.barFill.style.width = "0%";
    } else {
        els.phase.textContent = snap.status;
        const total = snap.total_discovered || 0;
        const done = snap.processed || 0;
        const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
        els.barFill.style.width = `${pct}%`;
        const eta = snap.eta_seconds != null && snap.eta_seconds > 0 ? ` · ETA ${Math.round(snap.eta_seconds)}s` : "";
        const counter = total > 0 ? `${done}/${total}` : "";
        const rate = `${(snap.files_per_sec || 0).toFixed(1)}/s`;
        els.summary.textContent = [counter, rate + eta].filter(Boolean).join(" · ");
        els.summary.title = snap.current_file || "";
    }

    const errs = snap.errors || [];
    els.errorsWrap.classList.toggle("hidden", errs.length === 0);
    els.errCount.textContent = errs.length;
    if (errs.length) {
        els.errors.innerHTML = errs.map(e => `<li>${esc(e.path)}: ${esc(e.message)}</li>`).join("");
    }

    if (terminal) {
        els.reindex.disabled = false;
        refreshStats();
        if (!els.q.value.trim()) runSearch();
    }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
refreshStats();
runSearch();
connectStream();
setInterval(refreshStats, 30000);
