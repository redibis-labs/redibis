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

export async function mountStewardReview(el, { table, api, ws } = {}) {
  if (!el || !table) return;
  const call = apiFn(api);
  const wsQ = (ws && ws !== "default") ? `?ws=${encodeURIComponent(ws)}` : "";
  const state = {
    table,
    overview: null,
    column: null,
    step: "table",
    colIndex: 0,
    field: "pii",
    chosenSource: "",
    rationale: "engine_correct",
    rationaleText: "",
    filter: "",
    editOpen: false,
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
    .gen-opt{border:1px solid #e2e8f0;border-radius:8px;padding:6px 10px;cursor:pointer}
    .gen-opt.on{border-color:#2563eb;background:#dbeafe}
    .iso{unicode-bidi:isolate;direction:auto}
    .steward-samples{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;font-size:12px}
    .guarantee-bar{font-size:12px;color:#64748b;margin-top:10px}
    .chip.no-ev{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:#fef3c7;color:#92400e;font-size:11px}
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
  }
  async function loadColumn(name, samples) {
    const q = samples ? "?samples=1" : "";
    state.column = await call("GET", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(name)}${q}`);
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
      <p class="hint" style="margin-top:12px"><a href="/review?table=${encodeURIComponent(table)}" target="_blank">Open run explorer</a></p>
    `;
    rail.querySelectorAll("[data-step]").forEach((n) => {
      n.onclick = () => { state.step = n.getAttribute("data-step"); render(); };
    });
    rail.querySelectorAll("[data-col]").forEach((n) => {
      n.onclick = async () => {
        state.colIndex = Number(n.getAttribute("data-i"));
        state.step = "column";
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
    main.innerHTML = `<div class="card"><h3>Table</h3>
      <table><thead><tr><th>item</th><th>current</th><th>review</th><th></th></tr></thead>
      <tbody>${rows}${qrows}</tbody></table></div>`;
    bindVerdicts();
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
    main.innerHTML = `<div class="card"><h3>Overview</h3>
      <div class="steward-tiles">${tiles}
        <div class="steward-tile" data-filter="contested"><strong>${agr.contested||0}</strong>Contested</div>
        <div class="steward-tile" data-filter="no_evidence"><strong>${agr.no_evidence||0}</strong>No evidence</div>
        <div class="steward-tile" data-filter="pii"><strong>${Object.values(s.pii_columns||{}).reduce((a,b)=>a+b,0)}</strong>PII columns</div>
      </div>
      <p class="hint">Agreement: no evidence ${agr.no_evidence||0} · contested ${agr.contested||0} · majority ${agr.majority||0} · unanimous ${agr.unanimous||0}. Click a tile to filter the rail.</p>
    </div>`;
    main.querySelectorAll("[data-filter]").forEach((n)=>{
      n.onclick = () => { state.filter = n.getAttribute("data-filter")||""; renderRail(); };
    });
  }

  function genRadios(field, gens) {
    const list = gens || [];
    if (!list.length) return `<p class="hint">No generations for ${esc(field)}.</p>`;
    return `<div class="gen-row">${list.map((g)=>{
      const on = state.field===field && state.chosenSource===g.source ? "on":"";
      const conf = g.confidence==null?"":Number(g.confidence).toFixed(2);
      const val = typeof g.value==="object"?JSON.stringify(g.value):String(g.value??"");
      return `<label class="gen-opt ${on}"><input type="radio" name="gen-${esc(field)}" value="${esc(g.source)}" ${on?"checked":""}/>
        ${esc(g.source)} ${conf?("●"+conf):""}<br><small class="iso">${esc(val).slice(0,80)}</small></label>`;
    }).join("")}</div>`;
  }

  function renderColumn() {
    const c = state.column;
    if (!c) { main.innerHTML = "Loading column…"; return; }
    const gens = c.generations || {};
    const profile = c.profile || {};
    const samples = profile.samples;
    const withheld = profile.samples_withheld;
    const stats = profile.stats || {};
    const current = c.current || {};
    const fields = ["pii","entity_type","classification","definition","tags"];
    main.innerHTML = `<div class="card">
      <h3>Column ${esc(c.column)} ‹ ${c.position} / ${c.of} › · ${esc(c.review&&c.review.status||"pending")}</h3>
      <div class="grid2">
        <div>
          <h4>Profile</h4>
          <p>null ${esc(stats.null_rate)} · ndv ${esc(stats.ndv||stats.ndv_ratio)} · type ${esc(stats.logical_type||c.logical_type)}</p>
          <p>quality: ${(profile.quality||[]).length} rules · format ${esc(profile.format_signature)}</p>
          ${samples ? `<div class="steward-samples iso">${samples.map(esc).join("<br>")}</div>` :
            `<p class="hint">${withheld?esc(withheld):"Samples hidden"}
             ${withheld===""||withheld==="no consent"?"":""}
             <button class="btn-sm" id="srShowSamples">Show samples · consented</button></p>`}
        </div>
        <div>
          <h4>Current</h4>
          <p>pii=${esc(current.pii)} · ${esc(current.entity_type)} · ${esc(current.classification)}</p>
          <p class="iso">${esc(current.definition)}</p>
        </div>
      </div>
      ${fields.map((f)=>`<div style="margin-top:12px"><strong>${f}</strong> ${agreementChip((c.agreement||{})[f])}
        ${genRadios(f, gens[f])}
      </div>`).join("")}
      <label>Rationale <select id="srRationale"></select></label>
      <textarea id="srRationaleText" placeholder="required when rationale is other" style="display:${state.rationale==="other"?"block":"none"}">${esc(state.rationaleText)}</textarea>
      ${verdictButtons("column")}
      ${state.editOpen ? `<div id="srEdit"><label>Definition</label><textarea id="srDef">${esc(current.definition)}</textarea>
        <label>Tags (comma)</label><input id="srTags" value="${esc((current.tags||[]).join(", "))}"/>
        <button class="btn-sm" id="srEditSave">Save edit</button></div>` : ""}
    </div>`;
    fillRationale();
    main.querySelectorAll('input[type=radio]').forEach((inp)=>{
      inp.onchange = () => {
        state.field = inp.name.replace("gen-","");
        state.chosenSource = inp.value;
        fillRationale();
      };
    });
    const show = main.querySelector("#srShowSamples");
    if (show) show.onclick = async () => {
      await loadColumn(c.column, true);
      render();
    };
    bindVerdicts();
    const save = main.querySelector("#srEditSave");
    if (save) save.onclick = async () => {
      const definition = main.querySelector("#srDef").value;
      const tags = main.querySelector("#srTags").value.split(",").map((s)=>s.trim()).filter(Boolean);
      await call("PUT", `/api/contracts/${encodeURIComponent(table)}/review/columns/${encodeURIComponent(c.column)}`, {
        definition, tags,
      });
      await sendVerdict("edit", { value: definition, field: "definition" });
    };
  }

  function fillRationale() {
    const sel = main.querySelector("#srRationale");
    if (!sel) return;
    let codes = ((state.column && state.column.rationale_codes) || {})[state.chosenSource || "human"]
      || ["domain_knowledge","other"];
    const agr = ((state.column && state.column.agreement) || {})[state.field || "pii"];
    if (agr === "no_evidence") {
      codes = codes.filter((c) => c !== "engine_correct");
    }
    if (agr === "no_evidence" && state.rationale === "engine_correct") {
      state.rationale = codes[0] || "domain_knowledge";
    }
    sel.innerHTML = codes.map((c)=>`<option value="${esc(c)}" ${c===state.rationale?"selected":""}>${esc(c)}</option>`).join("");
    sel.onchange = () => {
      state.rationale = sel.value;
      const ta = main.querySelector("#srRationaleText");
      if (ta) ta.style.display = state.rationale === "other" ? "block" : "none";
    };
  }

  function bindVerdicts() {
    main.querySelectorAll("[data-dec]").forEach((btn) => {
      btn.onclick = () => onDecision(btn.getAttribute("data-dec"), btn.getAttribute("data-scope"));
    });
  }

  async function onDecision(decision, scope) {
    if (decision === "edit" && scope === "column") {
      state.editOpen = true;
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
  }

  async function sendVerdict(decision, extra) {
    const col = state.column && state.column.column;
    if (!col) return;
    const ta = main.querySelector("#srRationaleText");
    const body = {
      field: (extra && extra.field) || state.field || "pii",
      decision,
      chosen_source: state.chosenSource || "human",
      rationale_code: state.rationale || "engine_correct",
      rationale_text: ta ? ta.value : "",
      value: extra && extra.value,
    };
    if (body.rationale_code !== "other") body.rationale_text = body.rationale_text || "";
    if (body.rationale_code === "other" && !body.rationale_text) body.rationale_text = "other";
    await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/columns/${encodeURIComponent(col)}/verdict`, body);
    await loadOverview();
    await loadColumn(col);
    render();
  }

  async function finalize() {
    const res = await call("POST", `/api/contracts/${encodeURIComponent(table)}/steward/finalize`);
    if (!res.ok) {
      main.innerHTML = `<div class="banner invalid">${esc(res.message || "Not ready")}</div>`;
      return;
    }
    const arts = res.artifacts || {};
    const digest = arts.review_digest || "";
    const cards = Object.entries(arts.artifacts || {}).map(([k, p]) =>
      `<div class="card"><h3>${esc(k)}</h3><p class="mono">${esc(p)}</p>
       <a class="btn" href="/api/contracts/${encodeURIComponent(table)}/steward/artifacts/${encodeURIComponent(k)}${wsQ}">Download</a></div>`
    ).join("");
    main.innerHTML = `<div class="banner valid">Guaranteed · digest ${esc(digest)}</div>${cards}`;
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
