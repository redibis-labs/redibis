/**
 * Contracts v2 — workspace picker, paged/searchable list, batch actions.
 * Nothing here triggers a scan.
 */
const PAGE_SIZE = 50;
const ARTIFACTS = ["contract", "verdicts", "evidence", "llm_context", "corpus", "graph"];

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

function wrapIdent(s) {
  return esc(s).replace(/([._/-])/g, "$1&#8203;");
}

function qs(params) {
  const u = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "" && v !== false) u.set(k, String(v));
  });
  const s = u.toString();
  return s ? `?${s}` : "";
}

function readUrl() {
  const p = new URLSearchParams(location.search);
  return {
    ws: p.get("ws") || "default",
    q: p.get("q") || "",
    page: Math.max(1, parseInt(p.get("page") || "1", 10) || 1),
    table: p.get("table") || "",
    tab: p.get("tab") || "",
  };
}

function writeUrl(state) {
  const p = new URLSearchParams(location.search);
  if (state.ws && state.ws !== "default") p.set("ws", state.ws); else p.delete("ws");
  if (state.q) p.set("q", state.q); else p.delete("q");
  if (state.page && state.page > 1) p.set("page", String(state.page)); else p.delete("page");
  if (state.table) p.set("table", state.table); else p.delete("table");
  const url = `${location.pathname}?${p.toString()}`.replace(/\?$/, "");
  history.replaceState(null, "", url);
}

function reviewChip(review) {
  if (review === "guaranteed") return `<span class="chip chip-green">guaranteed</span>`;
  if (review === "in_progress") return `<span class="chip chip-amber">in progress</span>`;
  return `<span class="chip chip-muted">none</span>`;
}

function artifactDots(arts) {
  const set = new Set(arts || []);
  return ARTIFACTS.map((name, i) =>
    `<span class="art-dot ${set.has(name) ? "on" : ""}" title="${esc(name)}">${i}</span>`
  ).join("");
}

export async function mountContractWorkspace(opts) {
  const { bar, list, batch, state, api, onSelect, toast } = opts;
  if (!bar || !list || !api) return;

  const url = readUrl();
  state.ws = state.ws || url.ws || "default";
  const local = {
    workspaces: [],
    rows: [],
    total: 0,
    page: url.page || 1,
    q: url.q || "",
    status: "",
    review: "",
    selected: new Set(),
    selectMatching: false,
    matchingTotal: 0,
    batchId: "",
    pollTimer: null,
    admin: true,
  };

  async function loadWorkspaces() {
    const data = await api("GET", "/api/workspaces");
    local.workspaces = data.workspaces || [];
    if (!local.workspaces.some((w) => w.slug === state.ws)) state.ws = "default";
  }

  async function loadPage() {
    const offset = (local.page - 1) * PAGE_SIZE;
    const data = await api("GET",
      `/api/workspaces/${encodeURIComponent(state.ws)}/contracts${qs({
        q: local.q, status: local.status, review: local.review,
        sort: "updated", desc: "true", limit: PAGE_SIZE, offset,
      })}`);
    local.rows = data.rows || [];
    local.total = data.total || 0;
    local.matchingTotal = local.total;
    if (!local.rows.length && local.page > 1) {
      local.page = 1;
      return loadPage();
    }
    writeUrl({ ws: state.ws, q: local.q, page: local.page, table: state.table });
  }

  function pages() {
    return Math.max(1, Math.ceil(local.total / PAGE_SIZE));
  }

  function renderBar() {
    const optsWs = local.workspaces.map((w) =>
      `<option value="${esc(w.slug)}" ${w.slug === state.ws ? "selected" : ""}>${esc(w.name || w.slug)}${w.kind === "s3" ? " · minio" : ""}</option>`
    ).join("");
    bar.innerHTML = `
      <label class="ws-label">Workspace
        <select id="wsPicker">${optsWs}<option value="__add_local">+ add folder…</option><option value="__add_s3">+ add MinIO…</option></select>
      </label>
      <input id="wsSearch" type="search" placeholder="search table / uuid / path" value="${esc(local.q)}"/>
      <select id="wsStatus"><option value="">status</option>
        <option ${local.status === "active" ? "selected" : ""}>active</option>
        <option ${local.status === "draft" ? "selected" : ""}>draft</option></select>
      <select id="wsReview"><option value="">review</option>
        <option value="none" ${local.review === "none" ? "selected" : ""}>none</option>
        <option value="in_progress" ${local.review === "in_progress" ? "selected" : ""}>in progress</option>
        <option value="guaranteed" ${local.review === "guaranteed" ? "selected" : ""}>guaranteed</option></select>
      <button type="button" class="btn-sm" id="wsReindex" title="Rebuild index">⟳</button>
      <div class="ws-pager">
        <button type="button" class="btn-sm" id="wsPrev" ${local.page <= 1 ? "disabled" : ""}>‹</button>
        <span>${local.page} / ${pages()}</span>
        <button type="button" class="btn-sm" id="wsNext" ${local.page >= pages() ? "disabled" : ""}>›</button>
      </div>`;
    bar.querySelector("#wsPicker").onchange = onPicker;
    const search = bar.querySelector("#wsSearch");
    search.onkeydown = (ev) => { if (ev.key === "Enter") { local.q = search.value; local.page = 1; refresh(); } };
    search.oninput = () => { local.q = search.value; };
    bar.querySelector("#wsStatus").onchange = (ev) => { local.status = ev.target.value; local.page = 1; refresh(); };
    bar.querySelector("#wsReview").onchange = (ev) => { local.review = ev.target.value; local.page = 1; refresh(); };
    bar.querySelector("#wsReindex").onclick = async () => {
      await api("POST", `/api/workspaces/${encodeURIComponent(state.ws)}/reindex`, {});
      refresh();
    };
    bar.querySelector("#wsPrev").onclick = () => { if (local.page > 1) { local.page -= 1; refresh(); } };
    bar.querySelector("#wsNext").onclick = () => { if (local.page < pages()) { local.page += 1; refresh(); } };
  }

  async function onPicker(ev) {
    const val = ev.target.value;
    if (val === "__add_local") { await addFolder(); return; }
    if (val === "__add_s3") { await addMinio(); return; }
    state.ws = val;
    local.page = 1;
    local.selected.clear();
    await refresh();
  }

  async function addFolder() {
    const root = prompt("Absolute folder of contracts (must sit inside workspaces.allowed_roots):");
    if (!root) { renderBar(); return; }
    const name = prompt("Workspace name:", root.split(/[\\/]/).pop() || "workspace") || "";
    try {
      const ref = await api("POST", "/api/workspaces", { name, kind: "local", root });
      await loadWorkspaces();
      state.ws = ref.slug;
      local.page = 1;
      await refresh();
    } catch (e) {
      if (toast) toast(e.message || String(e), false);
      renderBar();
    }
  }

  async function addMinio() {
    const bucket = prompt("MinIO / S3 bucket:");
    if (!bucket) { renderBar(); return; }
    const prefix = prompt("Key prefix (optional):", "") || "";
    const endpoint = prompt("Endpoint (blank = server default):", "") || "";
    try {
      const ref = await api("POST", "/api/workspaces", {
        name: prefix ? `${bucket}/${prefix}` : bucket,
        kind: "s3", bucket, prefix, endpoint,
      });
      await loadWorkspaces();
      state.ws = ref.slug;
      local.page = 1;
      await refresh();
    } catch (e) {
      if (toast) toast(e.message || String(e), false);
      renderBar();
    }
  }

  function renderList() {
    if (!local.rows.length) {
      list.innerHTML = `<div class="empty" style="padding:8px">No contracts in this workspace.</div>`;
      return;
    }
    const pageIds = local.rows.map((r) => r.table);
    const allPage = pageIds.every((t) => local.selected.has(t));
    list.innerHTML = `
      <div class="ws-select-row">
        <label><input type="checkbox" id="wsSelPage" ${allPage ? "checked" : ""}/> page</label>
        <button type="button" class="btn-sm" id="wsSelMatch">select all ${local.total} matching</button>
      </div>
      <div class="ws-table-head"><span></span><span>table</span><span>uuid</span><span>cols</span><span>review</span></div>
      ${local.rows.map((r) => `
        <div class="tbl-item ${r.table === state.table ? "active" : ""}" data-table="${esc(r.table)}">
          <input type="checkbox" class="ws-row-cb" data-table="${esc(r.table)}" ${local.selected.has(r.table) ? "checked" : ""}/>
          <span class="ws-row-main">
            <span class="ws-row-name">${wrapIdent(r.table)}</span>
            <span class="ws-row-meta">${esc(r.name || "")} · v${esc(r.version || "∅")} · ${esc((r.updated || "").slice(0, 16))}</span>
            <span class="ws-row-chips">
              <span class="chip chip-muted ws-uuid" title="${esc(r.contract_uuid)}" data-uuid="${esc(r.contract_uuid)}">${esc((r.contract_uuid || "").slice(0, 8))}</span>
              <span class="chip chip-muted">${r.columns || 0}/${r.pii_columns || 0}</span>
              ${reviewChip(r.review)}
              <span class="art-dots">${artifactDots(r.artifacts)}</span>
            </span>
          </span>
        </div>`).join("")}
      <div class="ws-batch-bar">
        <details><summary>Batch ▾</summary>
          <button type="button" data-kind="enrich">Enrich…</button>
          <button type="button" data-kind="synthesize">Deep Enrich…</button>
          <button type="button" data-kind="steward_finalize">Finalize reviews</button>
          <button type="button" data-kind="export">Export artifacts…</button>
        </details>
      </div>`;
    list.querySelector("#wsSelPage").onchange = (ev) => {
      pageIds.forEach((t) => ev.target.checked ? local.selected.add(t) : local.selected.delete(t));
      local.selectMatching = false;
    };
    const matchBtn = list.querySelector("#wsSelMatch");
    if (matchBtn) matchBtn.onclick = () => {
      local.selectMatching = true;
      local.rows.forEach((r) => local.selected.add(r.table));
      if (toast) toast(`Will batch ${local.total} matching contracts`);
    };
    list.querySelectorAll(".tbl-item").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target.closest("input")) return;
        const t = el.getAttribute("data-table");
        if (t && onSelect) onSelect(t);
      });
    });
    list.querySelectorAll(".ws-row-cb").forEach((cb) => {
      cb.addEventListener("change", () => {
        const t = cb.getAttribute("data-table");
        if (cb.checked) local.selected.add(t); else local.selected.delete(t);
        local.selectMatching = false;
      });
    });
    list.querySelectorAll(".ws-uuid").forEach((el) => {
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const u = el.getAttribute("data-uuid") || "";
        if (u && navigator.clipboard) navigator.clipboard.writeText(u);
      });
    });
    list.querySelectorAll("[data-kind]").forEach((btn) => {
      btn.addEventListener("click", () => startBatch(btn.getAttribute("data-kind")));
    });
  }

  function selectedTables() {
    return [...local.selected];
  }

  async function startBatch(kind) {
    let tables = selectedTables();
    const selection = local.selectMatching ? { q: local.q, status: local.status, review: local.review } : null;
    if (!tables.length && !selection) {
      if (toast) toast("Select at least one contract", false);
      return;
    }
    const options = {};
    if (kind === "enrich") {
      const provider = prompt("Enrich provider:", "demo") || "demo";
      options.provider = provider;
    }
    if (kind === "synthesize") {
      const mode = prompt("Deep Enrich analysis mode:", "deterministic") || "deterministic";
      options.analysis_mode = mode;
      if (mode === "assisted") {
        options.provider = prompt("LLM provider (same as Enrich):", "demo") || "demo";
      }
    }
    if (kind === "export") {
      const arts = prompt("Artifacts (comma):", ARTIFACTS.join(","));
      const qtables = tables.length ? tables.join(",") : "";
      window.location = `/api/workspaces/${encodeURIComponent(state.ws)}/export?tables=${encodeURIComponent(qtables)}&artifacts=${encodeURIComponent(arts || ARTIFACTS.join(","))}`;
      return;
    }
    try {
      const body = selection && !tables.length
        ? { kind, selection, options }
        : { kind, tables, options };
      const man = await api("POST", `/api/workspaces/${encodeURIComponent(state.ws)}/batches`, body);
      local.batchId = man.id;
      pollBatch();
    } catch (e) {
      if (toast) toast(e.message || String(e), false);
    }
  }

  async function pollBatch() {
    if (!local.batchId || !batch) return;
    const man = await api("GET", `/api/workspaces/${encodeURIComponent(state.ws)}/batches/${encodeURIComponent(local.batchId)}`);
    const counts = man.counts || {};
    const done = (counts.done || 0) + (counts.error || 0) + (counts.skipped || 0);
    const total = counts.total || (man.items || []).length;
    const items = (man.items || []).map((it) =>
      `<div class="ws-batch-item ${it.status}">${esc(it.table)} · ${esc(it.status)}${it.error ? " — " + esc(it.error) : ""}</div>`
    ).join("");
    batch.innerHTML = `
      <div class="ws-batch-panel">
        <strong>Batch ${esc(man.kind)}</strong>
        <span>${done}/${total} · ${counts.error || 0} errors</span>
        <button type="button" class="btn-sm" id="wsBatchCancel">Cancel</button>
        ${items}
      </div>`;
    const cancel = batch.querySelector("#wsBatchCancel");
    if (cancel) cancel.onclick = () => api("POST",
      `/api/workspaces/${encodeURIComponent(state.ws)}/batches/${encodeURIComponent(local.batchId)}/cancel`, {});
    if (man.status === "running" || man.status === "pending" || man.status === "cancelling") {
      local.pollTimer = setTimeout(pollBatch, 2000);
    } else {
      refresh();
    }
  }

  function onKey(ev) {
    const tag = (ev.target && ev.target.tagName) || "";
    if (ev.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") {
      ev.preventDefault();
      const el = bar.querySelector("#wsSearch");
      if (el) el.focus();
    }
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (ev.key === "[") { ev.preventDefault(); if (local.page > 1) { local.page -= 1; refresh(); } }
    if (ev.key === "]") { ev.preventDefault(); if (local.page < pages()) { local.page += 1; refresh(); } }
  }

  async function refresh() {
    await loadPage();
    renderBar();
    renderList();
  }

  document.addEventListener("keydown", onKey);
  await loadWorkspaces();
  await refresh();
  return refresh;
}

if (typeof window !== "undefined") {
  window.mountContractWorkspace = mountContractWorkspace;
}
