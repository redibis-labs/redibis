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

const EDIT_FIELDS = ["pii", "entity_type", "classification", "definition", "tags"];

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
    if (!r.ok) throw new Error((data && data.detail) || r.statusText);
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
    rationale: "engine_correct",
    rationaleText: "",
    filter: "",
    editOpen: false,
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
    .steward-root{display:grid;grid-template-columns:220px 1fr;gap:16px;min-height:520px}
    .steward-rail{border:1px solid var(--border,#e2e8f0);border-radius:12px;background:#fff;padding:12px;overflow:auto}
    .steward-rail h4{font-size:11px;text-transform:uppercase;color:#64748b;margin:10px 0 6px}
    .steward-rail .item{padding:6px 8px;border-radius:8px;cursor:pointer;font-size:13px}
    .steward-rail .item:hover,.steward-rail .item.active{background:#dbeafe;color:#1e40af}
    .steward-main{min-width:0}
    .steward-btns{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
    .steward-tile{border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;cursor:pointer;background:#fff}
    .steward-tile strong{display:block;font-size:18px}
    .steward-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;margin:12px 0}
    .gen-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
    .gen-opt{border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;cursor:pointer;min-width:140px}
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
    .stat-cell{border:1px solid #e2e8f0;border-radius:8px;padding:8px}
    .stat-cell b{display:block;font-size:16px}
    .stat-cell span{font-size:11px;color:#64748b}
    .pii-toggle{display:flex;gap:8px;margin:8px 0}
    .pii-toggle button.on-pii{background:#fee2e2;border-color:#fca5a5;color:#991b1b}
    .pii-toggle button.on-ok{background:#dcfce7;border-color:#86efac;color:#166534}
    .reason-box{margin:8px 0;padding:8px;background:#f8fafc;border-radius:8px}
    .reason-box textarea{min-height:52px}
    .def-card{border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;margin:6px 0;cursor:pointer}
    .def-card.on{border-color:#2563eb;background:#dbeafe}
    .sr-error{background:#fee2e2;color:#991b1b;padding:8px 10px;border-radius:8px;margin:8px 0}
    meter{width:100%}
    button:disabled{opacity:.5;cursor:not-allowed}
  </style>
  <aside class="steward-rail" id="srRail"></aside>
  <div class="steward-main" id="srMain">Loading…</div>`;
  el.innerHTML = "";
  el.appendChild(root);
  const rail = root.querySelector("#srRail");
  const main = root.querySelector("#srMain");

  async function loadOverview() {
    state.overview = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward`);
    try {
      state.artifacts = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward/artifacts`);
    } catch {
      state.artifacts = null;
    }
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
    state.edit.definitionSource = "human";
    state.edit.tags = (cur.tags || []).join(", ");
    const review = (c.review && c.review.verdicts) || {};
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

  function renderRail() {
    const ov = state.overview || {};
    const g = ov.guarantee || {};
    const cols = columns();
    rail.innerHTML = `
      <div class="item ${state.step==="table"?"active":""}" data-step="table">● Table</div>
      <div class="item ${state.step==="overview"?"active":""}" data-step="overview">● Overview</div>
      <h4>columns</h4>
      ${cols.map((c,i)=>`<div class="item ${state.step==="column"&&state.colIndex===i?"active":""}" data-col="${esc(c.column)}" data-i="${i}">
        ${STATUS_MARK[c.status]||"·"} ${AGREEMENT_MARK[c.agreement]||""} ${esc(c.column)}
      </div>`).join("")}
      <div class="guarantee-bar">guarantee: ${g.reviewed||0}/${g.total||0}</div>
      <button class="btn-primary" id="srFinalize" style="width:100%;margin-top:8px">Finalize</button>
      <button class="btn-sm" id="srExport" style="width:100%;margin-top:6px">Export verdicts (memory)</button>
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
    const fin = rail.querySelector("#srFinalize");
    const blocked = g.guaranteed === false;
    fin.disabled = blocked;
    const b = g.blockers || {};
    const why = [
      b.needs_review && b.needs_review.length ? `${b.needs_review.length} needs review` : "",
      b.rejected && b.rejected.length ? `${b.rejected.length} rejected` : "",
      b.pending && b.pending.length ? `${b.pending.length} pending` : "",
      b.table && b.table.length ? `${b.table.length} table items` : "",
    ].filter(Boolean).join(" · ");
    if (blocked) fin.title = why || "Not ready";
    fin.onclick = finalize;
    rail.querySelector("#srExport").onclick = exportVerdicts;
  }

  function verdictButtons(scope, extra) {
    return `<div class="steward-btns">${DECISIONS.map((d)=>
      `<button class="btn-sm" data-dec="${d.id}" data-scope="${scope}">${d.label}</button>`
    ).join("")}${extra||""}</div>`;
  }

  function renderTable() {
    const t = (state.overview && state.overview.table_section) || {};
    const review = t.review || {};
    const rows = ["name","description","owner"].map((k) => {
      const v = review[k];
      const val = t[k] || "";
      return `<tr><td>${k}</td><td class="iso">${esc(val)}</td><td>${v?esc(v.decision):"pending"}</td>
        <td>${verdictButtons("table:"+k)}</td></tr>`;
    }).join("");
    const qrows = (t.quality_rules||[]).map((q,i)=>{
      const id = "quality:"+(q.meta&&q.meta.redibis_rule_id || i);
      const v = review[id];
      return `<tr><td>quality</td><td>${esc(q.type||q.rule||"rule")}</td><td>${v?esc(v.decision):"pending"}</td>
        <td>${verdictButtons("table:"+id)}</td></tr>`;
    }).join("");
    main.innerHTML = `${errorBanner()}<div class="card"><h3>Table</h3>
      <table><thead><tr><th>item</th><th>current</th><th>review</th><th></th></tr></thead>
      <tbody>${rows}${qrows}</tbody></table></div>`;
    bindVerdicts();
  }

  function artifactLinks() {
    const arts = (state.artifacts && (state.artifacts.artifacts || state.artifacts)) || {};
    const entries = Object.entries(arts).filter(([, p]) => typeof p === "string");
    if (!entries.length) return `<p class="hint">No finalized artifacts yet. Export verdicts any time; finalize writes the full A0–A5 pack for the next scan to memorize.</p>`;
    return `<div class="doclist">${entries.map(([k]) =>
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
    main.innerHTML = `${errorBanner()}<div class="card"><h3>Overview</h3>
      <div class="steward-tiles">${tiles}
        <div class="steward-tile" data-filter="contested"><strong>${agr.contested||0}</strong>Contested</div>
        <div class="steward-tile" data-filter="no_evidence"><strong>${agr.no_evidence||0}</strong>No evidence</div>
        <div class="steward-tile" data-filter="pii"><strong>${Object.values(s.pii_columns||{}).reduce((a,b)=>a+b,0)}</strong>PII columns</div>
      </div>
      <p class="hint">Agreement: no evidence ${agr.no_evidence||0} · contested ${agr.contested||0} · majority ${agr.majority||0} · unanimous ${agr.unanimous||0}. Click a tile to filter the rail.</p>
      <p class="hint">Profile p50 null ${fmtRate(prof.null_rate_p50)} · ndv ${fmtRate(prof.ndv_ratio_p50)} · columns with samples ${prof.columns_with_samples||0}</p>
      <h4>Artifacts</h4>
      ${artifactLinks()}
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
      const on = state.field === field && state.chosenSource === g.source ? "on" : "";
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
    const on = state.field === field && state.chosenSource === g.source ? "on" : "";
    const conf = pct(g.confidence);
    return `<label class="gen-opt ${on}">
      <input type="radio" name="gen-${esc(field)}" value="${esc(g.source)}" ${on?"checked":""}/>
      ${esc(g.source)} ${conf ? `<span class="chip conf">${conf}</span>` : ""}<br>
      <small class="iso">${esc(typeof g.value==="object"?JSON.stringify(g.value):String(g.value??"")).slice(0,80)}</small>
    </label>`;
  }

  function reasonBox(field) {
    const r = state.reasons[field] || { code: "domain_knowledge", text: "" };
    const codes = ((state.column && state.column.rationale_codes) || {})[state.chosenSource || "human"]
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
    return `<h4>Profile</h4>
      <div class="stat-grid">${cells}</div>
      ${nullBar}
      ${qlist}
      ${sampleBlock}`;
  }

  function piiEditor(c) {
    const on = state.edit.isPii;
    return `<div>
      <h4>Is this column PII? ${agreementChip((c.agreement||{}).pii)}</h4>
      <p>Current: <span class="chip ${c.current&&c.current.pii?"pii-on":"pii-off"}">${c.current&&c.current.pii?"PII":"not PII"}</span>
         · entity ${esc((c.current&&c.current.entity_type)||"—")} · ${esc((c.current&&c.current.classification)||"—")}</p>
      ${state.editOpen ? `<div class="pii-toggle">
        <button type="button" class="btn-sm ${on?"on-pii":""}" id="srPiiOn">PII on</button>
        <button type="button" class="btn-sm ${on?"":"on-ok"}" id="srPiiOff">PII off</button>
      </div>
      <label>Entity type <input id="srEntity" value="${esc(state.edit.entityType)}"/></label>
      <label>Classification <input id="srClass" value="${esc(state.edit.classification)}"/></label>
      ${reasonBox("pii")}` : ""}
      ${engineCards("pii")}
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
      ${state.editOpen ? `<label>Edit or write a definition<textarea id="srDefCustom" class="iso" placeholder="pick a candidate above, edit it, or write your own">${esc(state.edit.definition)}</textarea></label>${reasonBox("definition")}` : ""}
    </div>`;
  }

  function renderColumn() {
    const c = state.column;
    if (!c) { main.innerHTML = "Loading column…"; return; }
    const current = c.current || {};
    main.innerHTML = `${errorBanner()}<div class="card">
      <h3>Column ${esc(c.column)} ‹ ${c.position} / ${c.of} › · ${esc(c.review&&c.review.status||"pending")}</h3>
      <div class="grid2">
        <div>${profilePanel(c)}</div>
        <div>
          <h4>Current</h4>
          <p>pii=${esc(current.pii)} · ${esc(current.entity_type)} · ${esc(current.classification)}</p>
          <p class="iso">${esc(current.definition)}</p>
          <p>tags: ${esc((current.tags||[]).join(", "))}</p>
        </div>
      </div>
      ${piiEditor(c)}
      ${definitionEditor(c)}
      <div style="margin-top:12px"><strong>tags</strong> ${agreementChip((c.agreement||{}).tags)}
        ${engineCards("tags")}
        ${state.editOpen ? `<label>Tags (comma)</label><input id="srTags" value="${esc(state.edit.tags)}"/>${reasonBox("tags")}` : ""}
      </div>
      <div style="margin-top:12px"><strong>classification</strong> ${agreementChip((c.agreement||{}).classification)}
        ${engineCards("classification")}
        ${state.editOpen ? reasonBox("classification") : ""}
      </div>
      ${state.editOpen
        ? `<button class="btn-primary" id="srEditSave">Save edits</button>
           <button class="btn-sm" id="srEditCancel">Cancel</button>`
        : ""}
      ${verdictButtons("column")}
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
          const cand = ((c.definition_candidates) || []).find((d) => d.source === inp.value && !d.custom);
          if (cand && cand.value) state.edit.definition = cand.value;
          state.chosenSource = src;
          state.field = "definition";
          render();
          return;
        }
        state.field = name.replace("gen-", "");
        state.chosenSource = inp.value;
        const cards = ((c.engines || {})[state.field]) || [];
        const card = cards.find((x) => x.source === inp.value);
        if (state.field === "pii" && card && card.is_pii != null) {
          state.edit.isPii = !!card.is_pii;
          if (card.entity_type) state.edit.entityType = card.entity_type;
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
    if (on) on.onclick = () => { state.edit.isPii = true; state.chosenSource = "human"; state.field = "pii"; render(); };
    if (off) off.onclick = () => { state.edit.isPii = false; state.chosenSource = "human"; state.field = "pii"; render(); };
    const ent = main.querySelector("#srEntity");
    if (ent) ent.oninput = () => { state.edit.entityType = ent.value; };
    const cls = main.querySelector("#srClass");
    if (cls) cls.oninput = () => { state.edit.classification = cls.value; };
    const tags = main.querySelector("#srTags");
    if (tags) tags.oninput = () => { state.edit.tags = tags.value; };
    const custom = main.querySelector("#srDefCustom");
    if (custom) custom.oninput = () => { state.edit.definition = custom.value; state.edit.definitionSource = "human"; };
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
      btn.onclick = () => onDecision(btn.getAttribute("data-dec"), btn.getAttribute("data-scope"));
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

  async function onDecision(decision, scope) {
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
        const body = {
          item, decision,
          rationale_code: state.rationale,
          rationale_text: (main.querySelector("#srRationaleText") || {}).value || "n/a",
          value: null,
        };
        if (body.rationale_code !== "other") body.rationale_text = "";
        else if (!body.rationale_text) body.rationale_text = "other";
        await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/table/verdict`, body);
        await loadOverview();
        render();
        return;
      }
      await sendVerdict(decision);
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
      const defSourceRaw = state.edit.definitionSource || "human";
      const defSource = (defSourceRaw === "current" || defSourceRaw === "custom") ? "human" : defSourceRaw;
      const piiSource = (state.field === "pii" && state.chosenSource && state.chosenSource !== "human")
        ? state.chosenSource : "human";
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
          chosen_source: "human",
          value: tags,
          rationale_code: tagReason.rationale_code,
          rationale_text: tagReason.rationale_text || (tagReason.rationale_code === "other" ? "other" : "steward edit"),
        },
        {
          field: "classification",
          decision: "edit",
          chosen_source: "human",
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
      await loadOverview();
      await loadColumn(c.column);
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
      main.innerHTML = `<div class="banner valid">Guaranteed · digest ${esc(digest)}</div>${cards}`;
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

  function render() {
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
