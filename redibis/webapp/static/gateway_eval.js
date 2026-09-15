const {
  overlayEvalSpans,
  overlayFromMatchClasses,
  paintBanners,
  renderHighlights,
  selectionOffsets,
  sliceCodepoints,
  toChars,
} = await import(`./gateway_render.mjs?v=${window.GW_RENDER_V || ""}`);
const { askInline, createRulesEditor } = await import(`./gateway_rules.mjs?v=${window.GW_RULES_V || window.GW_RENDER_V || ""}`);

const MAX = Number(window.GW_MAX_CHARS || 200000);
const MAX_CASES = Number(window.GW_EVAL_MAX_CASES || 100);
const DATASET_KIND = "redibis.text_span_eval_dataset";
const REPORT_KIND = "redibis.text_span_eval_report";
const BATCH_REPORT_KIND = "redibis.text_span_eval_batch_report";
let metricMode = "exact";
let viewMode = "edit";
const $ = (id) => document.getElementById(id);

const textEl = $("evText");
const resultEl = $("evResult");
const bannerEl = $("gwBanner");
const healthBannerEl = $("gwHealthBanner");
const casesEl = $("evCases");
const spansEl = $("evSpans");
const entityEl = $("evEntity");
const langEl = $("evLang");
const countEl = $("evCount");
const metricsCard = $("evMetricsCard");
const metricsEl = $("evMetrics");
const progressEl = $("gwProgress");
const progressFillEl = $("gwProgressFill");
const progressStageEl = $("gwProgressStage");
const runBtn = $("evRun");
const downloadReportBtn = $("evDownloadReport");
const tip = $("gwTip");

let dataset = emptyDataset();
let selectedIndex = 0;
let lastReport = null;
let catalogue = [];
let evalAbort = null;
let draftActions = [];
let selectedMatch = null;
let selectedSpan = null;
let lastEvalRunUuid = "";
let lastLlmLog = null;
const rulesEditor = $("evRules")
  ? createRulesEditor($("evRules"), {
    getText: () => (currentCase() && currentCase().text) || (textEl && textEl.value) || "",
    getLanguage: () => langEl.value,
    getEngines: () => ($("evEngines") && $("evEngines").value) || "regex",
    getMinScore: () => Number(($("evMinScore") && $("evMinScore").value) || 0.35),
    onError: (err) => paintBanners(bannerEl, [{ kind: "warn", text: err.message }]),
    onSaved: () => paintBanners(bannerEl, [{ kind: "ok", text: "Rules saved as default." }]),
    onPreview: () => paintBanners(bannerEl, [{ kind: "ok", text: "Preview updated." }]),
  })
  : null;
const CLASS_TINT = {
  exact: "exact", equivalent: "exact", superset: "near", subset: "near",
  overlap_partial: "near", split: "near", merged: "near",
  type_mismatch: "bad", spurious: "bad", guard_violation: "bad", missed: "miss",
};

function emptyDataset() {
  return {
    kind: DATASET_KIND,
    schema_version: "1.2",
    redibis_version: window.REDIBIS_VERSION || "",
    offset_unit: "unicode_codepoint",
    id: "",
    cases: [emptyCase(1)],
  };
}

function emptyCase(n) {
  return { id: "case-" + n, text: "", language: "en", tags: [], expected_spans: [], forbidden_spans: [] };
}

function currentCase() {
  return dataset.cases[selectedIndex] || dataset.cases[0];
}

function updateCount() {
  const n = toChars(textEl.value || "").length;
  countEl.textContent = n.toLocaleString() + " / " + MAX.toLocaleString();
}

function persistCurrentText() {
  const c = currentCase();
  if (!c) return;
  c.text = textEl.value || "";
  c.language = langEl.value || "en";
}

function invalidateReport() {
  lastReport = null;
  if (downloadReportBtn) downloadReportBtn.disabled = true;
}

function stampedDataset() {
  persistCurrentText();
  return {
    ...dataset,
    kind: DATASET_KIND,
    schema_version: dataset.schema_version || "1.2",
    redibis_version: window.REDIBIS_VERSION || dataset.redibis_version || "",
    offset_unit: "unicode_codepoint",
  };
}

function setProgress(pct, label) {
  progressFillEl.style.width = Math.max(0, Math.min(100, pct)) + "%";
  progressFillEl.setAttribute("aria-valuenow", String(Math.round(pct)));
  if (label) progressStageEl.textContent = label;
}

function showProgress(label) {
  progressEl.hidden = false;
  setProgress(8, label || "Starting…");
}

function hideProgress() {
  progressEl.hidden = true;
  setProgress(0, "Starting…");
}

function downloadJson(name, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function fillEntitySelect(entities) {
  entityEl.textContent = "";
  const list = entities && entities.length ? entities : [{ entity_type: "EMAIL_ADDRESS" }];
  for (const row of list) {
    const type = String(row.entity_type || "").toUpperCase();
    if (!type) continue;
    const opt = document.createElement("option");
    opt.value = type;
    opt.appendChild(document.createTextNode(type));
    entityEl.appendChild(opt);
  }
}

async function loadHealth() {
  try {
    const res = await fetch("/api/gateway/health");
    if (!res.ok) return;
    const health = await res.json();
    catalogue = health.entity_catalogue || [];
    fillEntitySelect(catalogue);
    const llm = health.default_llm || {};
    const banners = [];
    if (llm.role_bound) {
      const label = [llm.provider, llm.model].filter(Boolean).join(" / ");
      banners.push({
        kind: llm.ready ? "ok" : "warn",
        text: (llm.ready ? "LLM refiner ready" : "LLM refiner not ready") +
          (label ? " (" + label + ")" : "") +
          (llm.reason ? ". " + llm.reason : ""),
      });
    } else {
      banners.push({
        kind: "info",
        text: "Evaluation can run without an LLM. Bind pii.text_refiner in Settings → Text Gateway to score the refiner.",
      });
    }
    paintBanners(healthBannerEl, banners);
  } catch {
    fillEntitySelect([]);
  }
}

function renderCases() {
  casesEl.textContent = "";
  dataset.cases.forEach((c, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "gw-eval-case" + (i === selectedIndex ? " active" : "");
    const label = document.createElement("span");
    label.appendChild(document.createTextNode(c.id + " · " + (c.expected_spans || []).length + " spans"));
    btn.appendChild(label);
    btn.addEventListener("click", () => selectCase(i));
    casesEl.appendChild(btn);
  });
}

function renderSpans() {
  const c = currentCase();
  spansEl.textContent = "";
  const spans = (c && c.expected_spans) || [];
  if (!spans.length) {
    spansEl.className = "gw-eval-span-list gw-empty";
    spansEl.appendChild(document.createTextNode("No spans yet."));
    return;
  }
  spansEl.className = "gw-eval-span-list";
  spans.forEach((span, i) => {
    const row = document.createElement("div");
    row.className = "gw-eval-span-row";
    const label = document.createElement("span");
    label.appendChild(document.createTextNode(
      span.entity_type + " [" + span.start + "," + span.end + ")"
    ));
    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-ghost btn-sm";
    del.appendChild(document.createTextNode("Remove"));
    del.addEventListener("click", () => {
      c.expected_spans.splice(i, 1);
      renderAll();
    });
    row.appendChild(label);
    row.appendChild(del);
    row.addEventListener("click", (ev) => {
      if (ev.target === del) return;
      selectedSpan = {
        text: sliceCodepoints(c.text || "", span.start, span.end),
        entity_type: span.entity_type,
        span,
      };
      selectedMatch = null;
      setActionButtonsEnabled(true);
    });
    spansEl.appendChild(row);
  });
}

function caseOverlaySpans() {
  const c = currentCase();
  if (!c) return [];
  const reportCase = lastReport && (lastReport.cases || []).find((row) => row.id === c.id);
  if (reportCase && (reportCase.match_classes || []).length) {
    return overlayFromMatchClasses(
      reportCase.expected_spans,
      reportCase.predicted_spans,
      filteredMatchRows(reportCase),
      reportCase.proposal_spans
    );
  }
  if (reportCase) {
    return overlayEvalSpans(
      reportCase.expected_spans,
      reportCase.predicted_spans,
      reportCase[metricMode] || reportCase.exact,
      reportCase.proposal_spans
    );
  }
  return (c.expected_spans || []).map((span) => ({ ...span, eval_kind: "gold" }));
}

function filteredMatchRows(reportCase) {
  const cls = ($("evFilterClass") && $("evFilterClass").value) || "";
  const ent = ($("evFilterEntity") && $("evFilterEntity").value) || "";
  const tag = ($("evFilterTag") && $("evFilterTag").value) || "";
  const mismatches = $("evMismatches") && $("evMismatches").checked;
  const near = $("evNearMiss") && $("evNearMiss").checked;
  const contested = $("evContested") && $("evContested").checked;
  const tags = reportCase.tags || [];
  if (tag && tags.indexOf(tag) === -1) return [];
  const contestedKinds = ["type_conflict", "boundary_conflict", "vetoed"];
  return (reportCase.match_classes || []).filter((row) => {
    if (cls && row.class !== cls) return false;
    if (ent && row.expected_type !== ent && row.got_type !== ent) return false;
    if (mismatches && (row.class === "exact" || row.class === "equivalent")) return false;
    if (near && !(Number(row.coverage || 0) >= 0.9 && row.class !== "exact")) return false;
    if (contested) {
      const pred = (reportCase.predicted_spans || [])[row.predicted_index] || {};
      const a = pred.agreement || row.agreement || "";
      if (!contestedKinds.includes(a)) return false;
    }
    return true;
  });
}

function paintCase() {
  const c = currentCase();
  const text = (c && c.text) || "";
  resultEl.hidden = false;
  renderHighlights(resultEl, text, caseOverlaySpans());
  resultEl.dir = (c && c.language || "").startsWith("ar") ? "rtl" : "ltr";
}

function renderMetrics() {
  metricsEl.textContent = "";
  const provEl = $("evProv");
  const classCard = $("evClassCard");
  const tableCard = $("evSpanTableCard");
  if (!lastReport) {
    metricsCard.hidden = true;
    if (provEl) provEl.hidden = true;
    if (classCard) classCard.hidden = true;
    if (tableCard) tableCard.hidden = true;
    if ($("evEntityCard")) $("evEntityCard").hidden = true;
    if ($("evTagCard")) $("evTagCard").hidden = true;
    if ($("evFilters")) $("evFilters").hidden = true;
    return;
  }
  metricsCard.hidden = false;
  if ($("evFilters")) $("evFilters").hidden = false;
  const exact = lastReport.exact && lastReport.exact.micro || lastReport.strict && lastReport.strict.micro || {};
  const value = lastReport.value && lastReport.value.micro || {};
  const overlap = lastReport.overlap && lastReport.overlap.micro || {};
  const typeBlock = lastReport.type && lastReport.type.micro || {};
  const deltas = (lastReport.gates && lastReport.gates.deltas) || {};
  const rows = [
    ["Strict F1", exact.f1, deltas.strict_f1],
    ["Value F1", value.f1, deltas.value_f1],
    ["Overlap F1", overlap.f1, deltas.overlap_f1],
    ["Type acc.", typeBlock.accuracy != null ? typeBlock.accuracy : typeBlock.f1, deltas.type_f1],
    ["Proposals excluded", lastReport.proposal_count, null],
  ];
  for (const [k, v, delta] of rows) {
    const dt = document.createElement("dt");
    dt.appendChild(document.createTextNode(k));
    const dd = document.createElement("dd");
    let label = String(v == null ? "—" : v);
    if (delta != null && delta !== "") label += " (Δ " + delta + ")";
    dd.appendChild(document.createTextNode(label));
    metricsEl.appendChild(dt);
    metricsEl.appendChild(dd);
  }
  renderProvenance();
  renderClassDist();
  renderSpanTable();
  renderEntityTable();
  renderTagTable();
  fillFilterOptions();
}

function renderProvenance() {
  const el = $("evProv");
  if (!el) return;
  const p = lastReport.provenance || {};
  const pack = p.pack_stack || {};
  el.hidden = false;
  el.textContent = [
    "run " + (p.run_uuid || "—"),
    "redibis " + (lastReport.redibis_version || p.redibis_version || "—"),
    "pack " + (pack.stack_uuid || "—"),
    "rules " + (p.rules_checksum || "—"),
    "norm " + (lastReport.normalization_profile || p.normalization_profile || "v1"),
    "engines " + ((p.engines_ran || []).join(",") || "—"),
    p.ner_backend ? ("ner " + p.ner_backend) : "",
    p.rules_unpinned ? "UNPINNED rules" : "",
    p.provenance_degraded ? ("degraded: " + (p.provenance_degraded_reason || "yes")) : "",
  ].filter(Boolean).join(" · ");
}

function renderClassDist() {
  const card = $("evClassCard");
  const el = $("evClassDist");
  if (!card || !el) return;
  const dist = lastReport.class_distribution || {};
  el.textContent = "";
  const names = Object.keys(dist);
  if (!names.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  names.forEach((name) => {
    const row = document.createElement("div");
    row.className = "gw-eval-span-row";
    const pill = document.createElement("span");
    pill.className = "gw-pill " + (CLASS_TINT[name] || "near");
    pill.appendChild(document.createTextNode(name));
    const n = document.createElement("span");
    n.appendChild(document.createTextNode(String(dist[name])));
    row.appendChild(pill);
    row.appendChild(n);
    el.appendChild(row);
  });
}

function reportCaseForCurrent() {
  const c = currentCase();
  if (!lastReport || !c) return null;
  return (lastReport.cases || []).find((row) => row.id === c.id) || null;
}

function renderSpanTable() {
  const card = $("evSpanTableCard");
  const el = $("evSpanTable");
  if (!card || !el) return;
  const reportCase = reportCaseForCurrent();
  el.textContent = "";
  if (!reportCase) {
    card.hidden = true;
    return;
  }
  const rows = filteredMatchRows(reportCase);
  card.hidden = false;
  if (!rows.length) {
    el.appendChild(document.createTextNode("No spans for this filter."));
    return;
  }
  const table = document.createElement("table");
  table.className = "gw-eval-table";
  const head = document.createElement("tr");
  ["Expected", "Predicted", "Class", "Coverage", "Char prec.", "Δ", "Type", "Engine"].forEach((label) => {
    const th = document.createElement("th");
    th.appendChild(document.createTextNode(label));
    head.appendChild(th);
  });
  table.appendChild(head);
  const chars = toChars(reportCase.text || "");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    const gold = (reportCase.expected_spans || [])[row.expected_index];
    const pred = (reportCase.predicted_spans || [])[row.predicted_index];
    const expTxt = gold ? sliceCodepoints(chars, gold.start, gold.end) : "";
    const predTxt = pred ? sliceCodepoints(chars, pred.start, pred.end) : "";
    const classLabel = (row.class || "") + (row.value_equal ? " =value" : "");
    const classTint = (row.value_equal && (row.class === "exact" || row.class === "equivalent"))
      ? "exact"
      : (CLASS_TINT[row.class] || "near");
    const cells = [
      expTxt || "—",
      predTxt || "—",
      classLabel,
      row.coverage == null ? "—" : Math.round(100 * row.coverage) + "%",
      row.char_precision == null ? "—" : Math.round(100 * row.char_precision) + "%",
      row.delta_label || "—",
      row.class === "type_mismatch"
        ? (row.expected_type || "") + " → " + (row.got_type || "")
        : (row.expected_type || row.got_type || "—"),
      pred ? [pred.engine, pred.recognizer].filter(Boolean).join(" · ") : "",
    ];
    tr.title = [row.delta_label, row.extra_left, row.extra_right, row.missing_left, row.missing_right, row.canonical_match]
      .filter(Boolean).join(" · ");
    cells.forEach((val, i) => {
      const td = document.createElement("td");
      if (i === 2) {
        const pill = document.createElement("span");
        pill.className = "gw-pill " + classTint;
        pill.appendChild(document.createTextNode(String(val)));
        td.appendChild(pill);
      } else {
        td.appendChild(document.createTextNode(String(val)));
      }
      tr.appendChild(td);
    });
    tr.addEventListener("click", () => selectMatchRow(row, reportCase));
    table.appendChild(tr);
  });
  el.appendChild(table);
}

function renderMetricMap(cardId, elId, mapping, columns, onClick) {
  const card = $(cardId);
  const el = $(elId);
  if (!card || !el) return;
  el.textContent = "";
  const keys = Object.keys(mapping || {});
  if (!keys.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const table = document.createElement("table");
  table.className = "gw-eval-table";
  const head = document.createElement("tr");
  columns.forEach((label) => {
    const th = document.createElement("th");
    th.appendChild(document.createTextNode(label));
    head.appendChild(th);
  });
  table.appendChild(head);
  keys.forEach((key) => {
    const row = mapping[key] || {};
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    columns.forEach((col, i) => {
      const td = document.createElement("td");
      let val = key;
      if (i === 1) val = row.f1 != null ? row.f1 : (row.recall != null ? row.recall : "—");
      if (i === 2) val = row.recall != null ? row.recall : "—";
      if (i === 3) val = row.precision != null ? row.precision : "—";
      td.appendChild(document.createTextNode(String(val)));
      tr.appendChild(td);
    });
    if (onClick) tr.addEventListener("click", () => onClick(key));
    table.appendChild(tr);
  });
  el.appendChild(table);
}

function renderEntityTable() {
  const valueEnt = (lastReport.value && lastReport.value.by_entity) || {};
  renderMetricMap("evEntityCard", "evEntityTable", valueEnt, ["Entity", "Value F1", "Recall", "Prec."], (entity) => {
    if ($("evFilterEntity")) $("evFilterEntity").value = entity;
    renderAll();
  });
}

function renderTagTable() {
  renderMetricMap("evTagCard", "evTagTable", lastReport.by_tag || {}, ["Tag", "F1", "Recall", "Prec."], (tag) => {
    if ($("evFilterTag")) $("evFilterTag").value = tag;
    renderAll();
  });
}

function setActionButtonsEnabled(on, hint) {
  const ids = ["evActExclude", "evActNoise", "evActCue", "evActNumber", "evActAccept", "evActAdvisory"];
  ids.forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.disabled = !on;
    if (!on) btn.title = hint || "Select a span first";
    else btn.title = "";
  });
  const hintEl = $("evActHint");
  if (hintEl) {
    hintEl.textContent = on
      ? "Actions apply to the selected span and the draft rules panel."
      : (hint || "Select a span in the table to enable actions.");
  }
}

function selectedSurface() {
  if (selectedMatch) {
    const row = selectedMatch.row;
    const reportCase = selectedMatch.reportCase;
    const pred = (reportCase.predicted_spans || [])[row.predicted_index] || {};
    const gold = (reportCase.expected_spans || [])[row.expected_index] || {};
    const span = pred.start != null ? pred : gold;
    return {
      text: span.text || sliceCodepoints((reportCase.text || currentCase().text || ""), span.start, span.end),
      entity_type: row.got_type || row.expected_type || entityEl.value,
      span,
      row,
      reportCase,
    };
  }
  if (selectedSpan) return selectedSpan;
  return null;
}

function selectMatchRow(row, reportCase) {
  selectedMatch = { row, reportCase };
  const why = $("evWhy");
  if (why) {
    why.hidden = false;
    const pred = (reportCase.predicted_spans || [])[row.predicted_index] || {};
    why.textContent = [
      row.class,
      "coverage " + (row.coverage == null ? "—" : row.coverage),
      "char_precision " + (row.char_precision == null ? "—" : row.char_precision),
      row.delta_label || "",
      pred.engine || "",
      pred.recognizer || "",
      pred.validator || "",
      "score " + (pred.score == null ? "—" : pred.score),
    ].filter(Boolean).join(" · ");
  }
  ["evActExclude", "evActNoise", "evActCue", "evActNumber", "evActAccept", "evActAdvisory"].forEach((id) => {
    if ($(id)) $(id).disabled = false;
  });
  if ($("evActPatch") && draftActions.length) $("evActPatch").disabled = false;
  setActionButtonsEnabled(true);
}

function fillFilterOptions() {
  const clsSel = $("evFilterClass");
  const entSel = $("evFilterEntity");
  const tagSel = $("evFilterTag");
  if (!clsSel) return;
  const keepCls = clsSel.value;
  const keepEnt = entSel ? entSel.value : "";
  const keepTag = tagSel ? tagSel.value : "";
  const classes = new Set();
  const entities = new Set();
  const tags = new Set();
  (lastReport.cases || []).forEach((c) => {
    (c.tags || []).forEach((t) => tags.add(t));
    (c.match_classes || []).forEach((row) => {
      if (row.class) classes.add(row.class);
      if (row.expected_type) entities.add(row.expected_type);
      if (row.got_type) entities.add(row.got_type);
    });
  });
  function refill(sel, values, current) {
    if (!sel) return;
    sel.textContent = "";
    const all = document.createElement("option");
    all.value = "";
    all.appendChild(document.createTextNode("all"));
    sel.appendChild(all);
    Array.from(values).sort().forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.appendChild(document.createTextNode(v));
      sel.appendChild(opt);
    });
    sel.value = current;
  }
  refill(clsSel, classes, keepCls);
  refill(entSel, entities, keepEnt);
  refill(tagSel, tags, keepTag);
}

function renderAll() {
  renderCases();
  renderSpans();
  paintCase();
  renderMetrics();
  updateCount();
}

function selectCase(index) {
  persistCurrentText();
  selectedIndex = Math.max(0, Math.min(index, dataset.cases.length - 1));
  const c = currentCase();
  textEl.value = (c && c.text) || "";
  langEl.value = (c && c.language) || "en";
  renderAll();
}

function addCase() {
  persistCurrentText();
  if (dataset.cases.length >= MAX_CASES) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Case limit reached (" + MAX_CASES + ")." }]);
    return;
  }
  dataset.cases.push(emptyCase(dataset.cases.length + 1));
  selectCase(dataset.cases.length - 1);
}

function classifySelection() {
  persistCurrentText();
  const c = currentCase();
  const fromPane = selectionOffsets(resultEl);
  let start;
  let end;
  if (fromPane) {
    start = fromPane.start;
    end = fromPane.end;
  } else {
    const rawStart = textEl.selectionStart;
    const rawEnd = textEl.selectionEnd;
    if (rawStart == null || rawEnd == null || rawEnd <= rawStart) {
      paintBanners(bannerEl, [{ kind: "warn", text: "Select some text first." }]);
      return;
    }
    start = toChars((textEl.value || "").slice(0, rawStart)).length;
    end = toChars((textEl.value || "").slice(0, rawEnd)).length;
  }
  if (!(end > start)) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Select some text first." }]);
    return;
  }
  c.expected_spans.push({
    start,
    end,
    entity_type: entityEl.value || "EMAIL_ADDRESS",
  });
  lastReport = null;
  downloadReportBtn.disabled = true;
  viewMode = "edit";
  renderAll();
}

async function streamEvaluation(payload, { signal } = {}) {
  const res = await fetch("/api/gateway/evaluations/run/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (res.status === 401) {
    window.location.href = "/login?next=/gateway/evaluations";
    return null;
  }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(typeof d.detail === "string" ? d.detail : "Evaluation failed (" + res.status + ")");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let report = null;
  let serverError = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, idx);
      buf = buf.slice(idx + 1);
      if (!line.trim()) continue;
      let evt;
      try { evt = JSON.parse(line); } catch { continue; }
      if (evt.event === "stage") {
        const total = Number(evt.total || 0);
        const index = Number(evt.index || 0);
        const pct = total ? Math.round(100 * index / total) : Number(evt.percent || 0);
        setProgress(pct, evt.stage === "case" ? ("Scoring " + (evt.id || (index + 1))) : (evt.stage || "Scoring…"));
      } else if (evt.event === "result") {
        report = evt.report;
        setProgress(100, "Done");
      } else if (evt.event === "error") {
        serverError = evt.detail || "evaluation failed";
      }
    }
  }
  if (serverError) throw new Error(serverError);
  return report;
}

async function runEvaluation() {
  persistCurrentText();
  const nonempty = dataset.cases.filter((c) => (c.text || "").trim());
  if (!nonempty.length) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Add at least one case with text." }]);
    return;
  }
  runBtn.disabled = true;
  showProgress("Scoring " + nonempty.length + " case(s)…");
  evalAbort = new AbortController();
  try {
    lastReport = await streamEvaluation({
      dataset: { ...stampedDataset(), cases: nonempty },
      language: langEl.value,
      engines: $("evEngines").value,
      min_score: Number($("evMinScore").value || 0.35),
      use_llm: !!$("evUseLlm").checked,
      llm_api_key: ($("evUseLlm").checked && $("evLlmKey") && $("evLlmKey").value.trim()) || "",
      overlap_iou: 0.5,
      normalization: "v1",
      tier: ["strict", "value", "overlap", "type"],
      draft_rules: rulesEditor && rulesEditor.getDraft() || undefined,
    }, { signal: evalAbort.signal });
    if (!lastReport) return;
    lastEvalRunUuid = (lastReport.provenance && lastReport.provenance.run_uuid) || lastReport.run_uuid || "";
    renderEvalLlm(lastReport);
    downloadReportBtn.disabled = false;
    viewMode = "view";
    const exact = lastReport.exact && lastReport.exact.micro || {};
    const value = lastReport.value && lastReport.value.micro || {};
    paintBanners(bannerEl, [{
      kind: "ok",
      text: "Strict F1 " + exact.f1 + " · value F1 " + (value.f1 || 0) +
        " · overlap F1 " +
        ((lastReport.overlap && lastReport.overlap.micro && lastReport.overlap.micro.f1) || 0) +
        ". LLM proposals are excluded from the primary score.",
    }]);
    renderAll();
  } catch (err) {
    if (err && err.name === "AbortError") {
      paintBanners(bannerEl, [{ kind: "info", text: "Evaluation stopped." }]);
    } else {
      paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
    }
  } finally {
    runBtn.disabled = false;
    evalAbort = null;
    hideProgress();
  }
}

function importDataset(raw) {
  if (raw && (raw.kind === REPORT_KIND || raw.kind === BATCH_REPORT_KIND)) {
    importReport(raw);
    return;
  }
  if (!raw || raw.kind !== DATASET_KIND) {
    throw new Error("Not a redibis.text_span_eval_dataset file.");
  }
  const cases = Array.isArray(raw.cases) ? raw.cases : [];
  if (!cases.length) throw new Error("Dataset has no cases.");
  dataset = {
    kind: DATASET_KIND,
    schema_version: String(raw.schema_version || "1.0"),
    redibis_version: String(raw.redibis_version || window.REDIBIS_VERSION || ""),
    offset_unit: "unicode_codepoint",
    id: String(raw.id || ""),
    cases: cases.map((c, i) => ({
      id: String(c.id || ("case-" + (i + 1))),
      text: String(c.text || ""),
      language: String(c.language || "en"),
      tags: Array.isArray(c.tags) ? c.tags : [],
      expected_spans: Array.isArray(c.expected_spans)
        ? c.expected_spans
        : Array.isArray(c.gold_spans) ? c.gold_spans : [],
      forbidden_spans: Array.isArray(c.forbidden_spans) ? c.forbidden_spans : [],
    })),
  };
  invalidateReport();
  viewMode = "edit";
  selectCase(0);
}

function importReport(raw) {
  const cases = raw.kind === BATCH_REPORT_KIND
    ? (raw.files || []).flatMap((f) => f.cases || [])
    : (raw.cases || []);
  if (!cases.length) throw new Error("Report has no cases.");
  dataset = {
    kind: DATASET_KIND,
    schema_version: "1.0",
    redibis_version: String(raw.redibis_version || window.REDIBIS_VERSION || ""),
    offset_unit: "unicode_codepoint",
    id: String(raw.dataset_id || ""),
    cases: cases.map((c, i) => ({
      id: String(c.id || ("case-" + (i + 1))),
      text: String(c.text || ""),
      language: String(c.language || "en"),
      expected_spans: Array.isArray(c.expected_spans) ? c.expected_spans : [],
    })),
  };
  lastReport = raw.kind === BATCH_REPORT_KIND ? {
    kind: REPORT_KIND,
    exact: raw.exact,
    overlap: raw.overlap,
    cases,
    proposal_count: cases.reduce((n, c) => n + ((c.proposal_spans || []).length), 0),
  } : raw;
  downloadReportBtn.disabled = false;
  viewMode = "view";
  selectCase(0);
}

function resetAll() {
  dataset = emptyDataset();
  lastReport = null;
  downloadReportBtn.disabled = true;
  selectedIndex = 0;
  textEl.value = "";
  resultEl.textContent = "";
  paintBanners(bannerEl, []);
  renderAll();
}

function hideTip() {
  tip.hidden = true;
  tip.textContent = "";
}

function showTip(mark) {
  let spans;
  try {
    spans = JSON.parse(mark.dataset.spans || "[]");
  } catch {
    spans = [];
  }
  tip.textContent = "";
  for (const s of spans) {
    const line = document.createElement("div");
    line.appendChild(document.createTextNode(
      [s.entity_type, s.eval_kind, s.start + "–" + s.end].filter(Boolean).join(" · ")
    ));
    tip.appendChild(line);
  }
  tip.hidden = false;
}

textEl.addEventListener("input", () => {
  const c = currentCase();
  const next = textEl.value || "";
  if (c && next !== (c.text || "") && (c.expected_spans || []).length) {
    if (!window.confirm("Changing the text will clear expected spans. Continue?")) {
      textEl.value = c.text || "";
      return;
    }
    c.expected_spans = [];
  }
  persistCurrentText();
  invalidateReport();
  viewMode = "edit";
  renderAll();
});
langEl.addEventListener("change", persistCurrentText);
$("evAddSpan").addEventListener("click", classifySelection);
$("evAddCase").addEventListener("click", addCase);
runBtn.addEventListener("click", runEvaluation);
$("evUseLlm").addEventListener("change", () => {
  const wrap = $("evLlmKeyWrap");
  if (wrap) wrap.hidden = !$("evUseLlm").checked;
});
$("evCancel").addEventListener("click", () => {
  if (evalAbort) evalAbort.abort();
});
$("evImport").addEventListener("click", () => $("evFile").click());
$("evFile").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try {
    importDataset(JSON.parse(await file.text()));
    paintBanners(bannerEl, [{ kind: "ok", text: "Imported " + dataset.cases.length + " case(s)." }]);
  } catch (err) {
    paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
  }
});
$("evDownload").addEventListener("click", () => {
  downloadJson("gateway-eval-dataset.json", stampedDataset());
});
downloadReportBtn.addEventListener("click", () => {
  if (lastReport) downloadJson("gateway-eval-report.json", lastReport);
});
$("evClear").addEventListener("click", resetAll);
["evFilterClass", "evFilterEntity", "evFilterTag", "evMismatches", "evNearMiss", "evContested"].forEach((id) => {
  const el = $(id);
  if (el) el.addEventListener("change", () => renderAll());
});
function queueAction(action, extra) {
  if (!selectedMatch) return;
  const row = selectedMatch.row;
  const reportCase = selectedMatch.reportCase;
  const pred = (reportCase.predicted_spans || [])[row.predicted_index] || {};
  const gold = (reportCase.expected_spans || [])[row.expected_index] || {};
  const payload = {
    action,
    case_id: reportCase.id,
    expected_id: row.expected_id,
    span: pred.start != null ? pred : gold,
    term: extra && extra.term,
    ...extra,
  };
  draftActions.push(payload);
  if ($("evActPatch")) $("evActPatch").disabled = false;
  paintBanners(bannerEl, [{ kind: "ok", text: "Queued " + action + " (" + draftActions.length + ")." }]);
}
function alsoQueue() {
  return !!( $("evAlsoQueue") && $("evAlsoQueue").checked );
}

function applyDraftAndPreview(field, value, extra) {
  if (!rulesEditor) return;
  if (field === "context_cues") {
    rulesEditor.addTerm("context_cues", value, extra && extra.entity_type);
  } else {
    rulesEditor.addTerm(field, value);
  }
  rulesEditor.preview();
  if (alsoQueue() && selectedMatch) {
    queueAction(extra && extra.queueAction || field, { term: value, trigger: value, pattern: value, ...extra });
  }
}

if ($("evActExclude")) $("evActExclude").addEventListener("click", () => {
  const sel = selectedSurface();
  askInline($("evInlineForm"), {
    label: "Exclude term",
    value: sel && sel.text || "",
    onOk: (term) => {
      if (!term) return;
      applyDraftAndPreview("exclude_terms", term, { queueAction: "exclude_term" });
    },
  });
});
if ($("evActNoise")) $("evActNoise").addEventListener("click", () => {
  const sel = selectedSurface();
  askInline($("evInlineForm"), {
    label: "Noise term (ignored entirely)",
    value: sel && sel.text || "",
    onOk: (term) => {
      if (!term) return;
      applyDraftAndPreview("noise_terms", term, { queueAction: "noise_term" });
    },
  });
});
if ($("evActCue")) $("evActCue").addEventListener("click", () => {
  const sel = selectedSurface();
  askInline($("evInlineForm"), {
    label: "Context cue trigger",
    value: sel && sel.text || "",
    onOk: (trigger) => {
      if (!trigger) return;
      applyDraftAndPreview("context_cues", trigger, {
        entity_type: sel && sel.entity_type,
        queueAction: "context_cue",
      });
    },
  });
});
if ($("evActNumber")) $("evActNumber").addEventListener("click", () => {
  const sel = selectedSurface();
  askInline($("evInlineForm"), {
    label: "Number rule pattern (regex)",
    value: sel && sel.text ? sel.text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : "",
    onOk: (pattern) => {
      if (!pattern) return;
      if (alsoQueue() && selectedMatch) {
        queueAction("number_rule", {
          pattern,
          entity_type: sel && sel.entity_type || "PHONE_NUMBER",
        });
      }
      paintBanners(bannerEl, [{ kind: "ok", text: "Number rule queued in the draft. Use Preview to inspect." }]);
    },
  });
});
if ($("evActAccept")) $("evActAccept").addEventListener("click", () => {
  if (selectedMatch) queueAction("accept_as_expected");
  else paintBanners(bannerEl, [{ kind: "info", text: "Accept as expected needs a scored span-table row." }]);
});
if ($("evActAdvisory")) $("evActAdvisory").addEventListener("click", () => {
  if (selectedMatch) queueAction("mark_advisory");
  else paintBanners(bannerEl, [{ kind: "info", text: "Mark advisory needs a scored span-table row." }]);
});
if ($("evActPatch")) $("evActPatch").addEventListener("click", async () => {
  if (!draftActions.length) return;
  const res = await fetch("/api/gateway/evaluations/corpus-patch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset: stampedDataset(), actions: draftActions }),
  });
  if (!res.ok) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Corpus patch failed." }]);
    return;
  }
  const body = await res.json();
  downloadJson("corpus-patch.json", body);
  if (body.draft_rules) downloadJson("draft-text-rules.json", body.draft_rules);
});
if ($("evCompare")) $("evCompare").addEventListener("click", async () => {
  const a = ($("evRunA") && $("evRunA").value || "").trim();
  const b = ($("evRunB") && $("evRunB").value || "").trim();
  const out = $("evCompareOut");
  if (!a || !b) {
    if (out) out.textContent = "Enter two run UUIDs.";
    return;
  }
  const res = await fetch("/api/gateway/evaluations/compare?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b));
  if (!res.ok) {
    if (out) out.textContent = "Compare failed (" + res.status + ").";
    return;
  }
  const body = await res.json();
  if (out) {
    out.textContent = (body.change_count || 0) + " class change(s).";
  }
});
window.addEventListener("beforeunload", () => {
  if (evalAbort) evalAbort.abort();
  textEl.value = "";
  resultEl.textContent = "";
  dataset = emptyDataset();
  lastReport = null;
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadHealth();
});
window.addEventListener("focus", loadHealth);
resultEl.addEventListener("mouseover", (ev) => {
  const mark = ev.target.closest && ev.target.closest("mark");
  if (mark) showTip(mark);
});
resultEl.addEventListener("mouseout", hideTip);

function renderEvalLlm(report) {
  const card = $("evLlmCard");
  if (!card) return;
  const llm = (report && report.llm) || (report && report.scan_config && report.scan_config.use_llm ? { used: true, requested: true } : null);
  if (!llm && !lastEvalRunUuid) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const summary = $("evLlmSummary");
  if (summary) {
    const bits = [
      llm && llm.used ? "used" : (llm && llm.requested ? "requested" : "idle"),
      lastEvalRunUuid ? ("run " + lastEvalRunUuid.slice(0, 8)) : "",
    ].filter(Boolean);
    summary.textContent = bits.join(" · ");
  }
  const body = $("evLlm");
  if (body) body.hidden = true;
}

async function toggleEvalLlm() {
  const body = $("evLlm");
  const btn = $("evLlmShow");
  if (!body) return;
  if (!body.hidden) {
    body.hidden = true;
    if (btn) btn.textContent = "Show LLM log";
    return;
  }
  if (!lastEvalRunUuid) {
    body.hidden = false;
    body.textContent = "No evaluation run id yet.";
    return;
  }
  const res = await fetch("/api/gateway/llm-log/" + encodeURIComponent(lastEvalRunUuid));
  const log = await res.json().catch(() => ({ available: false, reason: "fetch failed" }));
  lastLlmLog = log;
  body.textContent = "";
  if (log.available === false) {
    body.appendChild(document.createTextNode(log.reason || "log not retained for this run"));
  } else {
    if (log.transcripts_withheld) {
      const n = document.createElement("p");
      n.className = "gw-hint";
      n.appendChild(document.createTextNode("Transcripts withheld for this role."));
      body.appendChild(n);
    }
    (log.calls || []).forEach((call) => {
      const pre = document.createElement("pre");
      pre.className = "gw-llm-pre";
      pre.appendChild(document.createTextNode(
        [call.provider, call.model_id, call.status].filter(Boolean).join(" · ") + "\n" +
        (call.user_prompt || "") + "\n---\n" + (call.response || "")
      ));
      body.appendChild(pre);
    });
  }
  body.hidden = false;
  if (btn) btn.textContent = "Hide";
}

if ($("evLlmShow")) $("evLlmShow").addEventListener("click", toggleEvalLlm);
if ($("evLlmDownload")) $("evLlmDownload").addEventListener("click", async () => {
  if (!lastEvalRunUuid) return;
  const log = lastLlmLog || await fetch("/api/gateway/llm-log/" + encodeURIComponent(lastEvalRunUuid)).then((r) => r.json());
  downloadJson("llm-log-" + lastEvalRunUuid.slice(0, 8) + ".json", log);
});

setActionButtonsEnabled(false);
selectCase(0);
loadHealth();
