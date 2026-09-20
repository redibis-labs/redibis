/**
 * Steward Review — three-step flow (table → overview → column).
 * Nothing here triggers a scan. Keyboard: ←/→ columns, a/r/n/x/e verdicts, Enter confirm.
 */
const DECISIONS = [
  { id: "accept", key: "a", label: "Accept" },
  { id: "reject", key: "r", label: "Reject" },
  { id: "needs_review", key: "n", label: "Needs review" },
  { id: "no_action", key: "x", label: "No action" },
  { id: "edit", key: "e", label: "Edit" },
];

const STATUS_MARK = {
  approved: "✓",
  edited: "✎",
  rejected: "✗",
  needs_review: "!",
  no_action: "·",
  pending: "·",
};

const AGREEMENT_MARK = {
  contested: "!",
  no_evidence: "?",
  majority: "≈",
  unanimous: "●",
};
const RAIL_AGREEMENT_ORDER = { contested: 0, no_evidence: 1, majority: 2, unanimous: 3 };

const MIX_FIELDS = ["pii", "definition", "tags", "classification"];
const EDIT_FIELDS = ["pii", "entity_type", "classification", "definition", "tags"];

function wrapIdent(s) {
  return esc(s).replace(/([._/-])/g, "$1&#8203;");
}

function piiIcon(isPii, extra = "") {
  if (isPii) {
    return `<span class="sr-pii-icon on ${extra}" title="PII detected" aria-label="PII detected">
      <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
        <path fill="currentColor" d="M12 2a5 5 0 00-5 5v3H6a2 2 0 00-2 2v8a2 2 0 002 2h12a2 2 0 002-2v-8a2 2 0 00-2-2h-1V7a5 5 0 00-5-5zm-3 8V7a3 3 0 016 0v3H9zm3 4a2 2 0 110 4 2 2 0 010-4z"/>
      </svg>
      <span>PII</span>
    </span>`;
  }
  return `<span class="sr-pii-icon off ${extra}" title="Not PII" aria-label="Not PII">
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm-1 14.2L6.8 12l1.4-1.4 2.8 2.8 5.6-5.6L18 9.2 11 16.2z"/>
    </svg>
    <span>not PII</span>
  </span>`;
}

function agreementChip(agr) {
  if (agr === "no_evidence") {
    return `<span class="chip no-ev">no engine has scanned this field</span>`;
  }
  return `<small>${esc(agr || "")}</small>`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

function pct(n) {
  if (n == null || n === "") return "";
  const f = Number(n);
  if (!Number.isFinite(f)) return "";
  return `${Math.round(f <= 1 && f >= 0 ? f * 100 : f)}%`;
}

function fmtRate(n) {
  if (n == null || n === "") return "—";
  const f = Number(n);
  if (!Number.isFinite(f)) return esc(n);
  if (f >= 0 && f <= 1) return `${(f * 100).toFixed(1)}%`;
  return String(f);
}

function apiFn(api) {
  return api || (async (method, path, body) => {
    const opt = { method, headers: {} };
    if (body) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const r = await fetch(path, opt);
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) {
      const detail = (data && data.detail) || (typeof data === "string" && data) || r.statusText;
      if (Array.isArray(detail)) {
        const msgs = detail.map((d) => `${(d.loc || []).slice(1).join(".") || "request"}: ${d.msg || d}`).join("; ");
        throw new Error(msgs || `HTTP ${r.status}`);
      }
      if (detail && typeof detail === "object") {
        throw new Error(detail.message || detail.hint || JSON.stringify(detail));
      }
      throw new Error(String(detail || r.statusText));
    }
    return data;
  });
}

function emptyReasons() {
  const out = {};
  EDIT_FIELDS.forEach((f) => { out[f] = { code: "domain_knowledge", text: "" }; });
  return out;
}

export async function mountStewardReview(el, { table, api, ws } = {}) {
  if (!el || !table) return;
  const call = apiFn(api);
  const wsQ = (ws && ws !== "default") ? `?ws=${encodeURIComponent(ws)}` : "";
  const state = {
    table,
    overview: null,
    column: null,
    artifacts: null,
    step: "table",
    colIndex: 0,
    field: "pii",
    chosenSource: "",
    chosen: { pii: "", definition: "", tags: "", classification: "" },
    flashStatus: "",
    rationale: "engine_correct",
    rationaleText: "",
    filter: "",
    editOpen: false,
    tableEditItem: "",
    tableEdit: { name: "", description: "", owner: "" },
    error: "",
    reasons: emptyReasons(),
    edit: {
      isPii: false,
      entityType: "",
      classification: "",
      definition: "",
      definitionSource: "human",
      tags: "",
    },
  };

  const root = document.createElement("div");
  root.className = "steward-root";
  root.innerHTML = `<style>
    .steward-root{display:grid;grid-template-columns:minmax(220px,280px) minmax(0,1fr);grid-template-rows:auto 1fr;gap:0;min-height:620px;background:#eef2f7;border:1px solid #d4dee9;border-radius:14px;overflow:hidden}
    .steward-header{grid-column:1/-1;background:#1e293b;color:#f8fafc;padding:12px 16px 14px;display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:10px;transition:background .2s ease}
    .steward-header.ok{background:#14532d}
    .steward-header.ok .sr-kicker{color:#86efac}
    .steward-header.edited{background:#1e3a8a}
    .steward-header.edited .sr-kicker{color:#bfdbfe}
    .steward-header.bad{background:#7f1d1d}
    .steward-header.bad .sr-kicker{color:#fecaca}
    .steward-header.warn{background:#9a3412}
    .steward-header.warn .sr-kicker{color:#fed7aa}
    .steward-header h2{margin:0;font-size:15px;font-weight:700;letter-spacing:.01em;line-height:1.35;overflow-wrap:break-word;word-break:normal;max-width:min(72ch,100%)}
    .steward-header .sr-kicker{display:block;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#93c5fd;margin-bottom:4px}
    .steward-header .sr-sub{color:#94a3b8;font-size:12px;margin-top:4px}
    .steward-header .sr-head-actions{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
    .steward-header .sr-progress{font-size:12px;color:#cbd5e1;background:#0f172a;border:1px solid #334155;border-radius:999px;padding:4px 10px}
    .steward-header.ok .sr-progress{background:#052e16;border-color:#166534;color:#bbf7d0}
    .steward-header button{background:#334155;color:#f8fafc;border-color:#475569}
    .steward-header button:hover{background:#475569}
    .steward-header .btn-primary{background:#3b82f6;border-color:#3b82f6}
    .steward-rail{border:0;border-right:1px solid #d4dee9;background:#e8eef6;padding:12px;overflow:auto;min-width:0}
    .steward-rail h4{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin:12px 0 6px}
    .steward-rail .item{padding:7px 8px;border-radius:8px;cursor:pointer;font-size:13px;line-height:1.4;overflow-wrap:break-word;word-break:normal;white-space:normal}
    .steward-rail .item:hover{background:#dbe7f6}
    .steward-rail .item.active{background:#1e3a5f;color:#eff6ff}
    .steward-main{min-width:0;background:#f7f9fc;padding:14px 16px;overflow:auto}
    .sr-card{background:#fff;border:1px solid #dbe3ee;border-radius:12px;overflow:hidden;margin-bottom:12px;box-shadow:0 1px 0 rgba(15,23,42,.04)}
    .sr-card-head{background:#f1f5f9;border-bottom:1px solid #e2e8f0;padding:10px 14px;display:flex;flex-wrap:wrap;align-items:center;gap:10px}
    .sr-card-head h3{margin:0;font-size:13px;text-transform:none;letter-spacing:0;color:#0f172a;font-weight:700}
    .sr-card-head p{margin:4px 0 0;color:#64748b;font-size:12px;width:100%}
    .sr-card-body{padding:14px}
    .steward-btns{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
    .btn-accept.ready{background:#dcfce7;border-color:#16a34a;color:#14532d}
    .sr-pii-icon{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.04em}
    .sr-pii-icon.on{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
    .sr-pii-icon.off{background:#dcfce7;color:#166534;border:1px solid #86efac}
    .sr-pii-icon svg{display:block;flex:0 0 auto}
    .sr-choice{font-size:11px;color:#475569;margin:4px 0 8px}
    .steward-tile{border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;cursor:pointer;background:#fff}
    .steward-tile strong{display:block;font-size:18px}
    .steward-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin:12px 0}
    .gen-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
    .gen-opt{border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;cursor:pointer;min-width:140px;background:#fbfdff}
    .gen-opt.on{border-color:#2563eb;background:#dbeafe}
    .gen-opt.missing{opacity:.55}
    .iso{unicode-bidi:isolate;direction:auto}
    .steward-samples{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;font-size:12px}
    .guarantee-bar{font-size:12px;color:#64748b;margin-top:10px}
    .chip.no-ev{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:#fef3c7;color:#92400e;font-size:11px}
    .chip.pii-on{background:#fee2e2;color:#991b1b}
    .chip.pii-off{background:#dcfce7;color:#166534}
    .chip.conf{background:#dbeafe;color:#1e40af;margin-left:4px}
    .stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin:8px 0}
    .stat-cell{border:1px solid #e2e8f0;border-radius:8px;padding:8px;background:#f8fafc}
    .stat-cell b{display:block;font-size:16px;overflow-wrap:anywhere;word-break:break-word}
    .stat-cell span{font-size:11px;color:#64748b}
    .pii-toggle{display:flex;gap:8px;margin:8px 0}
    .pii-toggle button.on-pii{background:#fee2e2;border-color:#fca5a5;color:#991b1b}
    .pii-toggle button.on-ok{background:#dcfce7;border-color:#86efac;color:#166534}
    .reason-box{margin:8px 0;padding:8px;background:#f8fafc;border-radius:8px}
    .reason-box textarea{min-height:52px}
    .def-card{border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer}
    .def-card.on{border-color:#2563eb;background:#dbeafe}
    .sr-error{background:#fee2e2;color:#991b1b;padding:8px 10px;border-radius:8px;margin:8px 0}
    .sr-meta-row{display:grid;grid-template-columns:110px minmax(0,1fr) 110px;gap:10px;align-items:start;padding:10px 0;border-bottom:1px solid #eef2f7}
    .sr-meta-row:last-child{border-bottom:0}
    .sr-meta-row .sr-k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#64748b;padding-top:6px}
    .sr-meta-row .iso,.sr-meta-row input,.sr-meta-row textarea{overflow-wrap:anywhere;word-break:break-word}
    .sr-meta-row .sr-status{font-size:12px;color:#475569;padding-top:6px}
    meter{width:100%}
    button:disabled{opacity:.5;cursor:not-allowed}
  </style>
  <header class="steward-header" id="srHead"></header>
  <aside class="steward-rail" id="srRail"></aside>
  <div class="steward-main" id="srMain">Loading…</div>`;
  el.innerHTML = "";
  el.appendChild(root);
  const head = root.querySelector("#srHead");
  const rail = root.querySelector("#srRail");
  const main = root.querySelector("#srMain");

  async function loadOverview() {
    state.overview = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward`);
    try {
      state.artifacts = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward/artifacts`);
    } catch {
      state.artifacts = null;
    }
    const t = (state.overview && state.overview.table_section) || {};
    state.tableEdit = {
      name: t.name || "",
      description: t.description || "",
      owner: t.owner || "",
    };
  }
  async function loadColumn(name, samples) {
    const q = samples ? "?samples=1" : "";
    state.column = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(name)}${q}`);
    seedEditFromColumn();
  }

  function seedEditFromColumn() {
    const c = state.column;
    if (!c) return;
    const cur = c.current || {};
    state.edit.isPii = !!cur.pii;
    state.edit.entityType = cur.entity_type || "";
    state.edit.classification = cur.classification || "";
    state.edit.definition = cur.definition || "";
    state.chosen = { pii: "", definition: "", tags: "", classification: "" };
    const review = (c.review && c.review.verdicts) || {};
    MIX_FIELDS.forEach((f) => {
      const v = review[f];
      if (v && v.chosen_source) state.chosen[f] = v.chosen_source;
    });
    state.edit.definitionSource = state.chosen.definition || "human";
    state.edit.tags = (cur.tags || []).join(", ");
    EDIT_FIELDS.forEach((f) => {
      const v = review[f];
      state.reasons[f] = {
        code: (v && v.rationale_code) || "domain_knowledge",
        text: (v && v.rationale_text) || "",
      };
    });
  }

  function columns() {
    const cols = [...((state.overview && state.overview.columns) || [])];
    cols.sort((a, b) => (RAIL_AGREEMENT_ORDER[a.agreement] ?? 9) - (RAIL_AGREEMENT_ORDER[b.agreement] ?? 9));
    if (!state.filter) return cols;
    const f = state.filter;
    return cols.filter((c) => {
      if (f === "contested") return c.agreement === "contested";
      if (f === "no_evidence") return c.agreement === "no_evidence";
      if (f === "needs_review") return c.status === "needs_review";
      if (f === "pending") return c.status === "pending";
      if (f === "pii") return c.pii;
      return true;
    });
  }

  function headerTone() {
    const flash = state.flashStatus;
    const st = flash || (state.step === "column" && state.column && state.column.review && state.column.review.status) || "";
    if (st === "approved") return "ok";
    if (st === "edited") return "edited";
    if (st === "rejected") return "bad";
    if (st === "needs_review") return "warn";
    const g = (state.overview && state.overview.guarantee) || {};
    if (g.guaranteed) return "ok";
    return "";
  }

  function renderHeader() {
    const ov = state.overview || {};
    const g = ov.guarantee || {};
    const t = ov.table_section || {};
    const blocked = g.guaranteed === false;
    const tone = headerTone();
    const stepLabel = state.step === "column"
      ? `Column ${(state.column && state.column.column) || ""}`
      : (state.step === "overview" ? "Overview" : "Table");
    const b = g.blockers || {};
    const why = [
      b.needs_review && b.needs_review.length ? `${b.needs_review.length} needs review` : "",
      b.rejected && b.rejected.length ? `${b.rejected.length} rejected` : "",
      b.pending && b.pending.length ? `${b.pending.length} pending` : "",
      b.table && b.table.length ? `${b.table.length} table items` : "",
    ].filter(Boolean).join(" · ");
    const statusLabel = (state.step === "column" && state.column && state.column.review && state.column.review.status)
      || state.flashStatus
      || "";
    head.className = `steward-header ${tone}`.trim();
    head.innerHTML = `
      <div>
        <span class="sr-kicker">Steward review${statusLabel ? ` · ${esc(statusLabel)}` : ""}</span>
        <h2 class="iso">${wrapIdent(t.name || table)}</h2>
        <div class="sr-sub">${esc(stepLabel)} · ${wrapIdent(table)}</div>
      </div>
      <div class="sr-head-actions">
        <span class="sr-progress">reviewed ${g.reviewed||0}/${g.total||0}${why ? ` · ${esc(why)}` : ""}</span>
        <button class="btn-sm" id="srExport">Export verdicts</button>
        <button class="btn-sm" id="srExportAll">Export all artifacts</button>
        <button class="btn-primary" id="srFinalize" ${blocked?"disabled":""} title="${esc(blocked ? (why || "Not ready") : "Finalize review")}">Finalize</button>
      </div>`;
    const fin = head.querySelector("#srFinalize");
    fin.disabled = blocked;
    fin.onclick = finalize;
    head.querySelector("#srExport").onclick = exportVerdicts;
    head.querySelector("#srExportAll").onclick = exportAllArtifacts;
  }

  function renderRail() {
    const cols = columns();
    rail.innerHTML = `
      <div class="item ${state.step==="table"?"active":""}" data-step="table">Table metadata</div>
      <div class="item ${state.step==="overview"?"active":""}" data-step="overview">Overview</div>
      <h4>columns</h4>
      ${cols.map((c,i)=>`<div class="item ${state.step==="column"&&state.colIndex===i?"active":""}" data-col="${esc(c.column)}" data-i="${i}">
        ${STATUS_MARK[c.status]||"·"} ${AGREEMENT_MARK[c.agreement]||""} ${piiIcon(c.pii)} ${wrapIdent(c.column)}
      </div>`).join("")}
      <p class="hint" style="margin-top:12px"><a href="/review?table=${encodeURIComponent(table)}" target="_blank">Open run explorer</a></p>
    `;
    rail.querySelectorAll("[data-step]").forEach((n) => {
      n.onclick = () => { state.step = n.getAttribute("data-step"); render(); };
    });
    rail.querySelectorAll("[data-col]").forEach((n) => {
      n.onclick = async () => {
        state.colIndex = Number(n.getAttribute("data-i"));
        state.step = "column";
        state.editOpen = false;
        await loadColumn(n.getAttribute("data-col"));
        render();
      };
    });
  }

  function verdictButtons(scope, extra) {
    const mixed = scope === "column" && MIX_FIELDS.some((f) => state.chosen[f]);
    return `<div class="steward-btns">${DECISIONS.map((d)=>
      `<button class="btn-sm ${d.id==="accept"?"btn-accept":""} ${d.id==="accept"&&mixed?"ready":""}" data-dec="${d.id}" data-scope="${scope}">${d.label}</button>`
    ).join("")}${extra||""}</div>`;
  }

  function renderTable() {
    const t = (state.overview && state.overview.table_section) || {};
    const review = t.review || {};
    const rows = ["name","description","owner"].map((k) => {
      const v = review[k];
      const val = state.tableEdit[k] != null ? state.tableEdit[k] : (t[k] || "");
      const open = state.tableEditItem === k;
      const editor = open
        ? (k === "description"
          ? `<textarea id="srTableVal" class="iso">${esc(val)}</textarea>`
          : `<input id="srTableVal" class="iso" value="${esc(val)}"/>`)
        : `<div class="iso">${esc(val) || "—"}</div>`;
      const saveBtns = open
        ? `<div class="steward-btns">
            <button class="btn-primary" id="srTableSave">Save ${esc(k)}</button>
            <button class="btn-sm" id="srTableCancel">Cancel</button>
          </div>`
        : verdictButtons("table:"+k);
      return `<div class="sr-meta-row">
        <div class="sr-k">${k}</div>
        <div>${editor}</div>
        <div class="sr-status">${v?esc(v.decision):"pending"}</div>
        <div style="grid-column:2 / -1">${saveBtns}</div>
      </div>`;
    }).join("");
    const qrows = (t.quality_rules||[]).map((q,i)=>{
      const id = "quality:"+(q.meta&&q.meta.redibis_rule_id || i);
      const v = review[id];
      return `<div class="sr-meta-row">
        <div class="sr-k">quality</div>
        <div>${esc(q.type||q.rule||"rule")}</div>
        <div class="sr-status">${v?esc(v.decision):"pending"}</div>
        <div style="grid-column:2 / -1">${verdictButtons("table:"+id)}</div>
      </div>`;
    }).join("");
    main.innerHTML = `${errorBanner()}<div class="sr-card">
      <div class="sr-card-head"><h3>Table metadata</h3>
        <p>Accept the current value, or Edit to change name, description, or owner.</p></div>
      <div class="sr-card-body">${rows}${qrows}</div>
    </div>`;
    bindVerdicts();
    const inp = main.querySelector("#srTableVal");
    if (inp) inp.oninput = () => { state.tableEdit[state.tableEditItem] = inp.value; };
    const save = main.querySelector("#srTableSave");
    if (save) save.onclick = () => saveTableItem(state.tableEditItem);
    const cancel = main.querySelector("#srTableCancel");
    if (cancel) cancel.onclick = () => {
      state.tableEditItem = "";
      const cur = (state.overview && state.overview.table_section) || {};
      state.tableEdit = { name: cur.name || "", description: cur.description || "", owner: cur.owner || "" };
      render();
    };
  }

  function artifactLinks() {
    const arts = (state.artifacts && (state.artifacts.artifacts || state.artifacts)) || {};
    const entries = Object.entries(arts).filter(([, p]) => typeof p === "string");
    const zip = `<a class="chip chip-blue" href="/api/contracts/${encodeURIComponent(table)}/steward/export/artifacts${wsQ}">all artifacts (zip)</a>`;
    if (!entries.length) {
      return `<p class="hint">No finalized artifacts yet. Export verdicts any time; the zip always includes current A1. Finalize writes the full A0–A5 pack.</p><div class="doclist">${zip}</div>`;
    }
    return `<div class="doclist">${zip}${entries.map(([k]) =>
      `<a class="chip chip-blue" href="/api/contracts/${encodeURIComponent(table)}/steward/artifacts/${encodeURIComponent(k)}${wsQ}">${esc(k)}</a>`
    ).join("")}</div>`;
  }

  function renderOverview() {
    const s = (state.overview && state.overview.stats) || {};
    const agr = s.engine_agreement || {};
    const tiles = [
      ["columns_total","Columns"],
      ["reviewed","Reviewed"],
      ["needs_review","Needs review"],
      ["pending","Pending"],
      ["rejected","Rejected"],
    ].map(([k,l])=>`<div class="steward-tile" data-filter="${k==="needs_review"||k==="pending"?k:""}">
      <strong>${s[k]??0}</strong>${l}</div>`).join("");
    const prof = s.profile || {};
    main.innerHTML = `${errorBanner()}<div class="sr-card"><div class="sr-card-head"><h3>Overview</h3>
      <p>Filter the column rail from a tile. Profile p50 is across scanned columns.</p></div>
      <div class="sr-card-body">
      <div class="steward-tiles">${tiles}
        <div class="steward-tile" data-filter="contested"><strong>${agr.contested||0}</strong>Contested</div>
        <div class="steward-tile" data-filter="no_evidence"><strong>${agr.no_evidence||0}</strong>No evidence</div>
        <div class="steward-tile" data-filter="pii"><strong>${Object.values(s.pii_columns||{}).reduce((a,b)=>a+b,0)}</strong>PII columns</div>
      </div>
      <p class="hint">Agreement: no evidence ${agr.no_evidence||0} · contested ${agr.contested||0} · majority ${agr.majority||0} · unanimous ${agr.unanimous||0}.</p>
      <p class="hint">Profile p50 null ${fmtRate(prof.null_rate_p50)} · ndv ${fmtRate(prof.ndv_ratio_p50)} · columns with samples ${prof.columns_with_samples||0}</p>
      <h4>Artifacts</h4>
      ${artifactLinks()}
      </div>
    </div>`;
    main.querySelectorAll("[data-filter]").forEach((n)=>{
      n.onclick = () => { state.filter = n.getAttribute("data-filter")||""; renderRail(); };
    });
  }

  function errorBanner() {
    return state.error ? `<div class="sr-error">${esc(state.error)}</div>` : "";
  }

  function engineCards(field) {
    const c = state.column || {};
    const list = ((c.engines || {})[field]) || [];
    if (!list.length) {
      const gens = (c.generations || {})[field] || [];
      if (!gens.length) return `<p class="hint">No generations for ${esc(field)}.</p>`;
      return `<div class="gen-row">${gens.map((g) => engineCardFromGen(field, g)).join("")}</div>`;
    }
    return `<div class="gen-row">${list.map((g) => {
      const on = state.chosen[field] === g.source ? "on" : "";
      const missing = g.present ? "" : "missing";
      const conf = g.confidence_pct != null ? `<span class="chip conf">${g.confidence_pct}%</span>` : "";
      const pii = g.is_pii == null ? "" : (g.is_pii
        ? `<span class="chip pii-on">PII</span>`
        : `<span class="chip pii-off">not PII</span>`);
      const val = g.present
        ? (typeof g.value === "object" ? JSON.stringify(g.value) : String(g.value ?? ""))
        : "no verdict yet";
      return `<label class="gen-opt ${on} ${missing}">
        <input type="radio" name="gen-${esc(field)}" value="${esc(g.source)}" ${on?"checked":""} ${g.present?"":"disabled"}/>
        <strong>${esc(g.label || g.source)}</strong> ${pii} ${conf}<br>
        <small class="iso">${esc(val).slice(0, 120)}</small>
      </label>`;
    }).join("")}</div>`;
  }

  function engineCardFromGen(field, g) {
    const on = state.chosen[field] === g.source ? "on" : "";
    const conf = pct(g.confidence);
    return `<label class="gen-opt ${on}">
      <input type="radio" name="gen-${esc(field)}" value="${esc(g.source)}" ${on?"checked":""}/>
      ${esc(g.source)} ${conf ? `<span class="chip conf">${conf}</span>` : ""}<br>
      <small class="iso">${esc(typeof g.value==="object"?JSON.stringify(g.value):String(g.value??"")).slice(0,80)}</small>
    </label>`;
  }

  function reasonBox(field) {
    const r = state.reasons[field] || { code: "domain_knowledge", text: "" };
    const codes = ((state.column && state.column.rationale_codes) || {})[state.chosen[field] || state.chosenSource || "human"]
      || ["domain_knowledge", "other"];
    return `<div class="reason-box" data-reason="${esc(field)}">
      <label>Reason for ${esc(field)}
        <select data-reason-code="${esc(field)}">${codes.map((c)=>
          `<option value="${esc(c)}" ${c===r.code?"selected":""}>${esc(c)}</option>`
        ).join("")}</select>
      </label>
      <textarea data-reason-text="${esc(field)}" placeholder="free text reason for this ${esc(field)} edit">${esc(r.text)}</textarea>
    </div>`;
  }

  function profilePanel(c) {
    const profile = c.profile || {};
    const stats = profile.stats || {};
    const samples = profile.samples;
    const withheld = profile.samples_withheld;
    const quality = profile.quality || [];
    const cells = [
      ["null rate", fmtRate(stats.null_rate)],
      ["ndv", esc(stats.ndv ?? stats.nunique ?? fmtRate(stats.ndv_ratio || stats.cardinality_ratio))],
      ["type", esc(stats.logical_type || c.logical_type || "—")],
      ["format", esc(profile.format_signature || stats.format_signature || "—")],
      ["avg length", esc(stats.avg_value_length ?? "—")],
      ["quality rules", String(quality.length)],
    ].map(([l,v]) => `<div class="stat-cell"><b>${v}</b><span>${l}</span></div>`).join("");
    const nullBar = stats.null_rate != null
      ? `<label>null rate<meter min="0" max="1" value="${Number(stats.null_rate)||0}"></meter></label>`
      : "";
    const qlist = quality.length
      ? `<ul>${quality.slice(0, 12).map((q)=>`<li>${esc(q.type||q.rule||q.expectation||JSON.stringify(q).slice(0,80))}</li>`).join("")}</ul>`
      : `<p class="hint">No quality rules on this column.</p>`;
    const sampleBlock = samples
      ? `<div class="steward-samples iso">${samples.map(esc).join("<br>")}</div>`
      : `<p class="hint">${withheld ? esc(withheld) : "Samples hidden"}
         <button class="btn-sm" id="srShowSamples">Show samples · consented</button></p>`;
    const empty = cells.includes("—") && !stats.null_rate && stats.ndv == null && stats.nunique == null
      ? `<p class="hint">No profile stats for this column yet. Stats come from the last scan profile, ledger, or contract type.</p>`
      : "";
    return `<h4>Profile</h4>
      <div class="stat-grid">${cells}</div>
      ${empty}
      ${nullBar}
      ${qlist}
      ${sampleBlock}`;
  }

  function fieldPickBar(field) {
    const src = state.chosen[field];
    if (!src) {
      return `<p class="sr-choice">Pick an engine for ${esc(field)}. Accept keeps this field independent of the others.</p>`;
    }
    return `<p class="sr-choice">Using <strong>${esc(src)}</strong> for ${esc(field)}
      <button class="btn-sm btn-accept ready" data-dec="accept" data-scope="column-field" data-field="${esc(field)}">Accept ${esc(field)}</button></p>`;
  }

  function piiEditor(c) {
    const on = state.edit.isPii;
    return `<div>
      <h4>Is this column PII? ${agreementChip((c.agreement||{}).pii)}</h4>
      <p>Current: ${piiIcon(c.current&&c.current.pii)}
         · entity ${esc((c.current&&c.current.entity_type)||"—")} · ${esc((c.current&&c.current.classification)||"—")}</p>
      ${state.editOpen ? `<div class="pii-toggle">
        <button type="button" class="btn-sm ${on?"on-pii":""}" id="srPiiOn">PII on</button>
        <button type="button" class="btn-sm ${on?"":"on-ok"}" id="srPiiOff">PII off</button>
      </div>
      <label>Entity type <input id="srEntity" value="${esc(state.edit.entityType)}"/></label>
      <label>Classification <input id="srClass" value="${esc(state.edit.classification)}"/></label>
      ${reasonBox("pii")}` : ""}
      ${engineCards("pii")}
      ${fieldPickBar("pii")}
    </div>`;
  }

  function definitionEditor(c) {
    const cands = c.definition_candidates || [];
    const cards = cands.map((d) => {
      const on = state.edit.definitionSource === d.source && !d.custom ? "on"
        : (d.custom && state.edit.definitionSource === "human" && !cands.some((x)=>x.source==="human"&&!x.custom&&state.edit.definitionSource==="human") ? "" : "");
      const selected = d.custom
        ? state.edit.definitionSource === "human" && !cands.filter((x)=>x.source==="human"&&!x.custom).length
        : state.edit.definitionSource === d.source;
      const conf = d.confidence_pct != null ? `<span class="chip conf">${d.confidence_pct}%</span>` : "";
      if (d.custom) {
        return `<label class="def-card ${state.edit.definitionSource==="human"||state.edit.definitionSource==="custom"?"on":""}">
          <input type="radio" name="srDefSrc" value="custom" ${state.edit.definitionSource==="human"||state.edit.definitionSource==="custom"?"checked":""}/>
          <strong>Write your own</strong>
        </label>`;
      }
      return `<label class="def-card ${selected?"on":""}">
        <input type="radio" name="srDefSrc" value="${esc(d.source)}" ${selected?"checked":""}/>
        <strong>${esc(d.label)}</strong> ${conf}
        <p class="iso">${esc(d.value).slice(0, 400)}</p>
      </label>`;
    }).join("");
    return `<div>
      <h4>Definition ${agreementChip((c.agreement||{}).definition)}</h4>
      <p class="iso">${esc((c.current&&c.current.definition)||"—")}</p>
      ${cards || engineCards("definition")}
      ${fieldPickBar("definition")}
      ${state.editOpen ? `<label>Edit or write a definition<textarea id="srDefCustom" class="iso" placeholder="pick a candidate above, edit it, or write your own">${esc(state.edit.definition)}</textarea></label>${reasonBox("definition")}` : ""}
    </div>`;
  }

  function renderColumn() {
    const c = state.column;
    if (!c) { main.innerHTML = "Loading column…"; return; }
    const current = c.current || {};
    main.innerHTML = `${errorBanner()}<div class="sr-card">
      <div class="sr-card-head">
        ${piiIcon(current.pii)}
        <div>
          <h3>Column ${wrapIdent(c.column)}</h3>
          <p>${c.position} / ${c.of} · ${esc(c.review&&c.review.status||"pending")}</p>
        </div>
      </div>
      <div class="sr-card-body">
      <div class="grid2">
        <div>${profilePanel(c)}</div>
        <div>
          <h4>Current</h4>
          <p>${piiIcon(current.pii)} · ${esc(current.entity_type)} · ${esc(current.classification)}</p>
          <p class="iso">${esc(current.definition)}</p>
          <p>tags: ${esc((current.tags||[]).join(", "))}</p>
        </div>
      </div>
      ${piiEditor(c)}
      ${definitionEditor(c)}
      <div style="margin-top:12px"><strong>tags</strong> ${agreementChip((c.agreement||{}).tags)}
        ${engineCards("tags")}
        ${fieldPickBar("tags")}
        ${state.editOpen ? `<label>Tags (comma)</label><input id="srTags" value="${esc(state.edit.tags)}"/>${reasonBox("tags")}` : ""}
      </div>
      <div style="margin-top:12px"><strong>classification</strong> ${agreementChip((c.agreement||{}).classification)}
        ${engineCards("classification")}
        ${fieldPickBar("classification")}
        ${state.editOpen ? reasonBox("classification") : ""}
      </div>
      <p class="hint">Accept uses the engine you picked on each field separately — tags can come from a deterministic engine while definition comes from an LLM.</p>
      ${state.editOpen
        ? `<button class="btn-primary" id="srEditSave">Save edits</button>
           <button class="btn-sm" id="srEditCancel">Cancel</button>`
        : ""}
      ${verdictButtons("column")}
      </div>
    </div>`;
    bindColumnEvents(c);
  }

  function bindColumnEvents(c) {
    main.querySelectorAll('input[type=radio]').forEach((inp) => {
      inp.onchange = () => {
        const name = inp.name || "";
        if (name === "srDefSrc") {
          const src = inp.value === "custom" ? "human" : inp.value;
          state.edit.definitionSource = src;
          state.chosen.definition = src;
          const cand = ((c.definition_candidates) || []).find((d) => d.source === inp.value && !d.custom);
          if (cand && cand.value) state.edit.definition = cand.value;
          state.chosenSource = src;
          state.field = "definition";
          render();
          return;
        }
        const field = name.replace("gen-", "");
        state.field = field;
        state.chosenSource = inp.value;
        if (MIX_FIELDS.includes(field)) state.chosen[field] = inp.value;
        const cards = ((c.engines || {})[field]) || [];
        const card = cards.find((x) => x.source === inp.value);
        if (field === "pii" && card && card.is_pii != null) {
          state.edit.isPii = !!card.is_pii;
          if (card.entity_type) state.edit.entityType = card.entity_type;
        }
        if (field === "definition" && card && card.value != null) {
          state.edit.definition = typeof card.value === "string" ? card.value : JSON.stringify(card.value);
          state.edit.definitionSource = inp.value;
        }
        if (field === "tags" && card && card.value != null) {
          state.edit.tags = Array.isArray(card.value) ? card.value.join(", ") : String(card.value);
        }
        if (field === "classification" && card && card.value != null) {
          state.edit.classification = String(card.value);
        }
        render();
      };
    });
    const show = main.querySelector("#srShowSamples");
    if (show) show.onclick = async () => {
      await loadColumn(c.column, true);
      render();
    };
    const on = main.querySelector("#srPiiOn");
    const off = main.querySelector("#srPiiOff");
    if (on) on.onclick = () => { state.edit.isPii = true; state.chosen.pii = "human"; state.chosenSource = "human"; state.field = "pii"; render(); };
    if (off) off.onclick = () => { state.edit.isPii = false; state.chosen.pii = "human"; state.chosenSource = "human"; state.field = "pii"; render(); };
    const ent = main.querySelector("#srEntity");
    if (ent) ent.oninput = () => { state.edit.entityType = ent.value; };
    const cls = main.querySelector("#srClass");
    if (cls) cls.oninput = () => { state.edit.classification = cls.value; };
    const tags = main.querySelector("#srTags");
    if (tags) tags.oninput = () => { state.edit.tags = tags.value; };
    const custom = main.querySelector("#srDefCustom");
    if (custom) custom.oninput = () => {
      state.edit.definition = custom.value;
      state.edit.definitionSource = "human";
      state.chosen.definition = "human";
    };
    main.querySelectorAll("[data-reason-code]").forEach((sel) => {
      sel.onchange = () => {
        const f = sel.getAttribute("data-reason-code");
        state.reasons[f] = state.reasons[f] || { code: "", text: "" };
        state.reasons[f].code = sel.value;
      };
    });
    main.querySelectorAll("[data-reason-text]").forEach((ta) => {
      ta.oninput = () => {
        const f = ta.getAttribute("data-reason-text");
        state.reasons[f] = state.reasons[f] || { code: "domain_knowledge", text: "" };
        state.reasons[f].text = ta.value;
      };
    });
    bindVerdicts();
    const save = main.querySelector("#srEditSave");
    if (save) save.onclick = () => saveEdits(c);
    const cancel = main.querySelector("#srEditCancel");
    if (cancel) cancel.onclick = () => { state.editOpen = false; seedEditFromColumn(); render(); };
  }

  function bindVerdicts() {
    main.querySelectorAll("[data-dec]").forEach((btn) => {
      btn.onclick = () => onDecision(
        btn.getAttribute("data-dec"),
        btn.getAttribute("data-scope"),
        btn.getAttribute("data-field"),
      );
    });
  }

  function fieldReason(field) {
    const r = state.reasons[field] || { code: state.rationale, text: state.rationaleText };
    let code = r.code || "domain_knowledge";
    let text = r.text || "";
    if (code !== "other") {
      /* free text is always sent so the steward can explain each part */
    } else if (!text) {
      text = "other";
    }
    return { rationale_code: code, rationale_text: text };
  }

  function chosenFor(field) {
    if (field === "definition") {
      const src = state.chosen.definition || state.edit.definitionSource || "";
      if (!src || src === "custom" || src === "current") return "human";
      return src;
    }
    return state.chosen[field] || "human";
  }

  function engineCardFor(field, source) {
    const cards = ((state.column && state.column.engines) || {})[field] || [];
    return cards.find((x) => x.source === source && (x.present !== false));
  }

  function valueFromChoice(field, source) {
    if (!source || source === "human") return undefined;
    if (field === "definition") {
      const cand = ((state.column && state.column.definition_candidates) || []).find((d) => d.source === source && !d.custom);
      if (cand && cand.value != null) return cand.value;
    }
    const card = engineCardFor(field, source);
    if (!card) return undefined;
    if (field === "pii") {
      if (card.value && typeof card.value === "object") return card.value;
      return { is_pii: card.is_pii, entity_type: card.entity_type || null };
    }
    return card.value;
  }

  function verdictForField(field, decision) {
    const source = chosenFor(field);
    const reason = fieldReason(field);
    const body = {
      field,
      decision,
      chosen_source: source,
      rationale_code: reason.rationale_code || (source === "human" ? "domain_knowledge" : "engine_correct"),
      rationale_text: reason.rationale_text || "",
    };
    const value = valueFromChoice(field, source);
    if (value !== undefined) body.value = value;
    if (body.rationale_code !== "other") body.rationale_text = body.rationale_text || "";
    if (body.rationale_code === "other" && !body.rationale_text) body.rationale_text = "other";
    return body;
  }

  async function onDecision(decision, scope, field) {
    state.error = "";
    try {
      if (decision === "edit" && scope === "column") {
        state.editOpen = true;
        seedEditFromColumn();
        render();
        return;
      }
      if ((scope || "").startsWith("table:")) {
        const item = scope.slice(6);
        if (decision === "edit" && (item === "name" || item === "description" || item === "owner")) {
          state.tableEditItem = item;
          render();
          return;
        }
        const inp = main.querySelector("#srTableVal");
        if (inp && state.tableEditItem) state.tableEdit[state.tableEditItem] = inp.value;
        const current = (state.overview && state.overview.table_section) || {};
        const value = (item in state.tableEdit)
          ? state.tableEdit[item]
          : (current[item] || null);
        const body = {
          item, decision,
          rationale_code: state.rationale,
          rationale_text: (main.querySelector("#srRationaleText") || {}).value || "n/a",
          value,
        };
        if (body.rationale_code !== "other") body.rationale_text = "";
        else if (!body.rationale_text) body.rationale_text = "other";
        await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/table/verdict`, body);
        state.tableEditItem = "";
        await loadOverview();
        render();
        return;
      }
      if (decision === "accept") {
        state.flashStatus = "approved";
        renderHeader();
        const col = state.column && state.column.column;
        if (!col) return;
        const fields = (scope === "column-field" && field) ? [field] : MIX_FIELDS;
        const verdicts = fields.map((f) => verdictForField(f, "accept"));
        await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(col)}/verdicts`, { verdicts });
        await loadOverview();
        await loadColumn(col);
        state.flashStatus = "";
        render();
        return;
      }
      if (decision === "reject") state.flashStatus = "rejected";
      else if (decision === "needs_review") state.flashStatus = "needs_review";
      else if (decision === "no_action") state.flashStatus = "approved";
      renderHeader();
      await sendVerdict(decision);
      state.flashStatus = "";
    } catch (e) {
      state.flashStatus = "";
      state.error = e && e.message ? e.message : String(e);
      render();
    }
  }

  async function saveTableItem(item) {
    state.error = "";
    try {
      const inp = main.querySelector("#srTableVal");
      if (inp) state.tableEdit[item] = inp.value;
      const body = {
        item,
        decision: "edit",
        value: state.tableEdit[item] || "",
        rationale_code: "domain_knowledge",
        rationale_text: "steward edit",
      };
      await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/table/verdict`, body);
      state.tableEditItem = "";
      await loadOverview();
      render();
    } catch (e) {
      state.error = e && e.message ? e.message : String(e);
      render();
    }
  }

  async function sendVerdict(decision, extra) {
    const col = state.column && state.column.column;
    if (!col) return;
    const field = (extra && extra.field) || state.field || "pii";
    const reason = fieldReason(field);
    const body = {
      field,
      decision,
      chosen_source: (extra && extra.chosen_source) || state.chosenSource || "human",
      rationale_code: reason.rationale_code || "engine_correct",
      rationale_text: reason.rationale_text,
      value: extra && extra.value,
    };
    if (body.rationale_code !== "other") body.rationale_text = body.rationale_text || "";
    if (body.rationale_code === "other" && !body.rationale_text) body.rationale_text = "other";
    await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(col)}/verdict`, body);
    await loadOverview();
    await loadColumn(col);
    render();
  }

  async function saveEdits(c) {
    state.error = "";
    try {
      const tagsEl = main.querySelector("#srTags");
      const entEl = main.querySelector("#srEntity");
      const clsEl = main.querySelector("#srClass");
      const defEl = main.querySelector("#srDefCustom");
      if (tagsEl) state.edit.tags = tagsEl.value;
      if (entEl) state.edit.entityType = entEl.value;
      if (clsEl) state.edit.classification = clsEl.value;
      if (defEl) state.edit.definition = defEl.value;
      const tags = state.edit.tags.split(",").map((s) => s.trim()).filter(Boolean);
      const piiReason = fieldReason("pii");
      const defReason = fieldReason("definition");
      const tagReason = fieldReason("tags");
      const classReason = fieldReason("classification");
      const defSourceRaw = state.chosen.definition || state.edit.definitionSource || "human";
      const defSource = (defSourceRaw === "current" || defSourceRaw === "custom") ? "human" : defSourceRaw;
      const piiSource = state.chosen.pii || "human";
      const tagSource = state.chosen.tags || "human";
      const classSource = state.chosen.classification || "human";
      const verdicts = [
        {
          field: "pii",
          decision: "edit",
          chosen_source: piiSource,
          value: { is_pii: !!state.edit.isPii, entity_type: state.edit.entityType || null },
          rationale_code: piiReason.rationale_code,
          rationale_text: piiReason.rationale_text || (piiReason.rationale_code === "other" ? "other" : "steward edit"),
        },
        {
          field: "definition",
          decision: "edit",
          chosen_source: defSource,
          value: state.edit.definition,
          rationale_code: defReason.rationale_code,
          rationale_text: defReason.rationale_text || (defReason.rationale_code === "other" ? "other" : "steward edit"),
        },
        {
          field: "tags",
          decision: "edit",
          chosen_source: tagSource,
          value: tags,
          rationale_code: tagReason.rationale_code,
          rationale_text: tagReason.rationale_text || (tagReason.rationale_code === "other" ? "other" : "steward edit"),
        },
        {
          field: "classification",
          decision: "edit",
          chosen_source: classSource,
          value: state.edit.classification,
          rationale_code: classReason.rationale_code,
          rationale_text: classReason.rationale_text || (classReason.rationale_code === "other" ? "other" : "steward edit"),
        },
      ];
      if (!state.edit.isPii) {
        verdicts[0].value.entity_type = null;
      }
      await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(c.column)}/verdicts`, { verdicts });
      state.editOpen = false;
      state.flashStatus = "edited";
      await loadOverview();
      await loadColumn(c.column);
      state.flashStatus = "";
      render();
    } catch (e) {
      state.error = e && e.message ? e.message : String(e);
      render();
    }
  }

  async function finalize() {
    state.error = "";
    try {
      const res = await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/finalize`);
      if (!res.ok) {
        main.innerHTML = `${errorBanner()}<div class="banner invalid">${esc(res.message || "Not ready")}</div>`;
        return;
      }
      const arts = res.artifacts || {};
      state.artifacts = arts;
      const digest = arts.review_digest || "";
      const cards = Object.entries(arts.artifacts || {}).map(([k, p]) =>
        `<div class="card"><h3>${esc(k)}</h3><p class="mono">${esc(p)}</p>
         <a class="btn" href="/api/contracts/${encodeURIComponent(table)}/steward/artifacts/${encodeURIComponent(k)}${wsQ}">Download</a></div>`
      ).join("");
      const zip = `<p><a class="btn" href="/api/contracts/${encodeURIComponent(table)}/steward/export/artifacts${wsQ}">Download all artifacts (zip)</a></p>`;
      main.innerHTML = `<div class="banner valid">Guaranteed · digest ${esc(digest)}</div>${zip}${cards}`;
    } catch (e) {
      state.error = e && e.message ? e.message : String(e);
      render();
    }
  }

  async function exportVerdicts() {
    state.error = "";
    try {
      const data = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward/export/verdicts`);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `steward_verdicts_${table.replace(/[^\w.-]+/g, "_")}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      state.error = e && e.message ? e.message : String(e);
      render();
    }
  }

  function exportAllArtifacts() {
    const a = document.createElement("a");
    a.href = `/api/contracts/${encodeURIComponent(table)}/steward/export/artifacts${wsQ}`;
    a.download = `steward_artifacts_${table.replace(/[^\w.-]+/g, "_")}.zip`;
    a.click();
  }

  function render() {
    renderHeader();
    renderRail();
    if (state.step === "table") renderTable();
    else if (state.step === "overview") renderOverview();
    else renderColumn();
  }

  function onKey(ev) {
    if (ev.target && (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA" || ev.target.tagName === "SELECT")) return;
    const cols = columns();
    if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
      ev.preventDefault();
      if (!cols.length) return;
      if (state.step !== "column") state.colIndex = 0;
      else state.colIndex = Math.max(0, Math.min(cols.length - 1, state.colIndex + (ev.key === "ArrowRight" ? 1 : -1)));
      state.step = "column";
      loadColumn(cols[state.colIndex].column).then(render);
      return;
    }
    const map = { a: "accept", r: "reject", n: "needs_review", x: "no_action", e: "edit" };
    if (map[ev.key] && state.step === "column") {
      ev.preventDefault();
      onDecision(map[ev.key], "column");
    }
    if (ev.key === "Enter" && state.step === "column") {
      ev.preventDefault();
      onDecision("accept", "column");
    }
  }

  root.addEventListener("keydown", onKey);
  root.tabIndex = 0;
  await loadOverview();
  render();
  root.focus();
}

if (typeof window !== "undefined") {
  window.mountStewardReview = mountStewardReview;
}
