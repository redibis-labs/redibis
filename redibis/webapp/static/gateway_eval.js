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
const { applyCuration, emptyCuration, entityCounts, isRejected, persistable, spanKey, upsertEntry } = await import(`./gateway_curation.mjs?v=${window.GW_CURATION_V || window.GW_RENDER_V || ""}`);

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
let dirty = false;
const caseCuration = {};
const rulesEditor = $("evRules")
  ? createRulesEditor($("evRules"), {
    getText: () => (currentCase() && currentCase().text) || (textEl && textEl.value) || "",
    getSpans: () => {
      const reportCase = lastReport && currentCase() && (lastReport.cases || []).find((row) => row.id === currentCase().id);
      return (reportCase && reportCase.predicted_spans) || [];
    },
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
  return { id: "case-" + n, name: "", text: "", language: "en", tags: [], expected_spans: [], forbidden_spans: [] };
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
    label.appendChild(document.createTextNode((c.name || c.id) + " · " + (c.expected_spans || []).length + " spans"));
    btn.appendChild(label);
    btn.addEventListener("click", () => selectCase(i));
    if (i === selectedIndex) {
      const rename = document.createElement("button");
      rename.type = "button";
      rename.className = "btn btn-ghost btn-sm";
      rename.appendChild(document.createTextNode("✎"));
      rename.addEventListener("click", (ev) => {
        ev.stopPropagation();
        askInline($("evRenameHost") || $("evInlineForm"), {
          label: "Case name",
          value: c.name || c.id,
          onOk: (name) => {
            c.name = (name || "").trim();
            dirty = true;
            renderCases();
          },
        });
      });
      btn.appendChild(rename);
    }
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
        text: sliceCodepoints(toChars(c.text || ""), span.start, span.end),
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
  const c = currentCase();
  const reportCase = lastReport && c && (lastReport.cases || []).find((row) => row.id === c.id);
  if (reportCase && caseCuration[c.id]) {
    const predicted = applyCuration(reportCase.predicted_spans || [], caseCuration[c.id], c.text || "");
    const gold = reportCase.expected_spans || [];
    let tp = 0;
    predicted.forEach((p) => {
      if (gold.some((g) => g.start === p.start && g.end === p.end && g.entity_type === p.entity_type)) tp += 1;
    });
    const prec = predicted.length ? tp / predicted.length : 1;
    const rec = gold.length ? tp / gold.length : 1;
    const f1 = (prec + rec) ? (2 * prec * rec / (prec + rec)) : 0;
    rows.push(["Curated F1", f1.toFixed(3), null]);
  }
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

async function curatePredicted(span, decision, extra) {
  const c = currentCase();
  if (!c || !span) return;
  extra = extra || {};
  if (decision === "accept" && $("evAutoTrim") && $("evAutoTrim").checked && !extra.trimmed) {
    const res = await fetch("/api/gateway/trim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: c.text || "", spans: [span] }),
    });
    if (res.ok) {
      const body = await res.json();
      const next = (body.spans || [])[0];
      if (next && (next.start !== span.start || next.end !== span.end)) {
        extra = Object.assign({}, extra, {
          new_start: next.start,
          new_end: next.end,
          trimmed: true,
          reason: extra.reason || (next.trim_rules || []).join(", "),
        });
      }
    }
  }
  const cur = caseCuration[c.id] || emptyCuration(lastEvalRunUuid);
  caseCuration[c.id] = upsertEntry(cur, Object.assign({
    key: spanKey(span, span.source || "engine"),
    decision,
  }, extra));
  dirty = true;
  renderAll();
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
  ["Expected", "Predicted", "Class", "Coverage", "Char prec.", "Δ", "Type", "Engine", "Curate"].forEach((label) => {
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
    const td = document.createElement("td");
    if (pred) {
      const c = currentCase();
      if (c && isRejected(pred, caseCuration[c.id])) tr.classList.add("gw-rejected");
      [["✓ keep", "accept"], ["✗ reject", "reject"]].forEach(([label, dec]) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-ghost btn-sm";
        btn.appendChild(document.createTextNode(label));
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          curatePredicted(pred, dec);
        });
        td.appendChild(btn);
      });
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn btn-ghost btn-sm";
      edit.appendChild(document.createTextNode("✎ edit"));
      edit.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const sel = selectionOffsets(resultEl);
        if (sel && sel.end > sel.start) {
          curatePredicted(pred, "modify", { new_start: sel.start, new_end: sel.end });
        }
      });
      td.appendChild(edit);
      const trimBtn = document.createElement("button");
      trimBtn.type = "button";
      trimBtn.className = "btn btn-ghost btn-sm";
      trimBtn.appendChild(document.createTextNode("⟲ trim"));
      trimBtn.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const c2 = currentCase();
        if (!c2) return;
        const res = await fetch("/api/gateway/trim", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: c2.text || "", spans: [pred] }),
        });
        if (!res.ok) return;
        const body = await res.json();
        const next = (body.spans || [])[0];
        if (!next) return;
        curatePredicted(pred, "modify", {
          new_start: next.start,
          new_end: next.end,
          trimmed: true,
          reason: (next.trim_rules || []).join(", "),
        });
      });
      td.appendChild(trimBtn);
    }
    tr.appendChild(td);
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
      text: span.text || sliceCodepoints(toChars(reportCase.text || currentCase().text || ""), span.start, span.end),
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
  renderEvalVerdict();
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
  askInline($("evRenameHost") || $("evInlineForm"), {
    label: "New case name",
    value: "",
    onOk: (name) => {
      const c = emptyCase(dataset.cases.length + 1);
      c.name = (name || "").trim();
      dataset.cases.push(c);
      dirty = true;
      selectCase(dataset.cases.length - 1);
    },
  });
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
      llm_verdict: ($("evLlmVerdict") && $("evLlmVerdict").checked)
        ? (($("evUseLlm") && $("evUseLlm").checked) ? "both" : "independent")
        : "off",
      recommend: !!($("evRecommend") && $("evRecommend").checked),
      trim: !!($("evAutoTrim") && $("evAutoTrim").checked),
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
      name: String(c.name || ""),
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
      name: String(c.name || ""),
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
  if (dirty || Object.keys(caseCuration).length) {
    if (!window.confirm("Clear unsaved cases and curation?")) return;
  }
  dataset = emptyDataset();
  lastReport = null;
  downloadReportBtn.disabled = true;
  selectedIndex = 0;
  dirty = false;
  Object.keys(caseCuration).forEach((k) => { delete caseCuration[k]; });
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
  downloadJson("gateway-eval-all-cases.eval.json", stampedDataset());
});
if ($("evDownloadCase")) $("evDownloadCase").addEventListener("click", () => {
  persistCurrentText();
  const c = currentCase();
  if (!c) return;
  const payload = {
    kind: DATASET_KIND,
    schema_version: dataset.schema_version || "1.2",
    offset_unit: "unicode_codepoint",
    cases: [c],
  };
  const stem = (c.name || c.id || "case").replace(/[^\w.-]+/g, "_");
  downloadJson(stem + ".eval.json", payload);
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
  const go = async () => {
    if (field === "context_cues") {
      rulesEditor.addTerm("context_cues", value, extra && extra.entity_type);
    } else if (rulesEditor.addTermChecked) {
      const result = await rulesEditor.addTermChecked(value, { preferred: field });
      if (result && result.refused) {
        paintBanners(bannerEl, [{ kind: "warn", text: result.reason }]);
        return;
      }
    } else {
      rulesEditor.addTerm(field, value);
    }
    rulesEditor.preview();
    if (alsoQueue() && selectedMatch) {
      queueAction(extra && extra.queueAction || field, { term: value, trigger: value, pattern: value, ...extra });
    }
  };
  go();
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
  if (selectedMatch) {
    const row = selectedMatch.row;
    const reportCase = selectedMatch.reportCase;
    const pred = (reportCase.predicted_spans || [])[row.predicted_index] || {};
    const c = currentCase();
    if (c && pred.start != null) {
      c.expected_spans = c.expected_spans || [];
      c.expected_spans.push({
        start: pred.start,
        end: pred.end,
        entity_type: pred.entity_type,
      });
      dirty = true;
      renderAll();
    }
    queueAction("accept_as_expected");
  } else paintBanners(bannerEl, [{ kind: "info", text: "Accept as expected needs a scored span-table row." }]);
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

function evalVerdictSpans() {
  const c = currentCase();
  if (lastReport && lastReport.llm_verdict && Array.isArray(lastReport.llm_verdict.spans)) {
    return lastReport.llm_verdict.spans;
  }
  const reportCase = lastReport && c && (lastReport.cases || []).find((row) => row.id === c.id);
  return (reportCase && reportCase.llm_verdict && reportCase.llm_verdict.spans) || [];
}

function renderEvalVerdict() {
  const card = $("evVerdictCard");
  const pane = $("evVerdict");
  const meta = $("evVerdictMeta");
  const list = $("evVerdictList");
  if (!card) return;
  const spans = evalVerdictSpans();
  if (!spans.length) {
    card.hidden = true;
    if (pane) pane.hidden = true;
    if (list) list.textContent = "";
    return;
  }
  const c = currentCase();
  card.hidden = false;
  if (pane) {
    pane.hidden = false;
    pane.dir = ((c && c.language) || "").startsWith("ar") ? "rtl" : "ltr";
    renderHighlights(pane, (c && c.text) || "", spans);
  }
  if (meta) meta.textContent = spans.length + " spans";
  if (list) {
    list.textContent = "";
    spans.forEach((span) => {
      const row = document.createElement("div");
      row.className = "gw-sum-row";
      const label = document.createElement("span");
      label.dir = "auto";
      label.style.unicodeBidi = "isolate";
      label.appendChild(document.createTextNode((span.entity_type || "") + " · " + (span.text || "")));
      row.appendChild(label);
      const acc = document.createElement("button");
      acc.type = "button";
      acc.className = "btn btn-ghost btn-sm";
      acc.appendChild(document.createTextNode("✓ accept"));
      acc.addEventListener("click", () => {
        curatePredicted(Object.assign({}, span, { source: "llm_verdict" }), "accept");
      });
      const ed = document.createElement("button");
      ed.type = "button";
      ed.className = "btn btn-ghost btn-sm";
      ed.appendChild(document.createTextNode("✎ accept with edit"));
      ed.addEventListener("click", () => {
        const extra = {};
        const sel = pane && selectionOffsets(pane);
        if (sel && sel.end > sel.start) {
          extra.new_start = sel.start;
          extra.new_end = sel.end;
        }
        curatePredicted(Object.assign({}, span, { source: "llm_verdict" }), "accept", extra);
      });
      const ign = document.createElement("button");
      ign.type = "button";
      ign.className = "btn btn-ghost btn-sm";
      ign.appendChild(document.createTextNode("✗ ignore"));
      ign.addEventListener("click", () => {
        curatePredicted(Object.assign({}, span, { source: "llm_verdict" }), "reject");
      });
      row.appendChild(acc);
      row.appendChild(ed);
      row.appendChild(ign);
      list.appendChild(row);
    });
  }
  renderEvalRecommendations();
}

function renderEvalRecommendations() {
  const card = $("evRecommendCard");
  const list = $("evRecommendList");
  if (!card || !list) return;
  const c = currentCase();
  const reportCase = lastReport && c && (lastReport.cases || []).find((row) => row.id === c.id);
  const recs = (reportCase && reportCase.recommendations)
    || (lastReport && lastReport.recommendations);
  if (!recs || !(recs.items || []).length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  list.textContent = "";
  (recs.items || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "gw-sum-row";
    const badge = (item.validation && item.validation.ok === false) ? " refused" : " ok";
    row.appendChild(document.createTextNode(
      item.target + " · " + JSON.stringify(item.value) + " · " + (item.reason || "") + badge
    ));
    const add = document.createElement("button");
    add.type = "button";
    add.className = "btn btn-ghost btn-sm";
    add.appendChild(document.createTextNode("+ add to draft"));
    add.addEventListener("click", async () => {
      if (!rulesEditor) return;
      const term = typeof item.value === "string" ? item.value : (item.value && item.value.value) || "";
      await rulesEditor.addTermChecked(term || JSON.stringify(item.value), { preferred: item.target });
    });
    row.appendChild(add);
    list.appendChild(row);
  });
}

function caseToUsecase(c) {
  return {
    name: (c && (c.name || c.id)) || "case",
    text: (c && c.text) || "",
    language: (c && c.language) || "en",
    tags: (c && c.tags) || [],
    expected_spans: ((c && c.expected_spans) || []).map((s) => ({
      start: s.start, end: s.end, entity_type: s.entity_type,
      value: sliceCodepoints(toChars((c && c.text) || ""), s.start, s.end),
    })),
    forbidden_spans: (c && c.forbidden_spans) || [],
  };
}

async function ensureSession(name) {
  const created = await fetch("/api/gateway/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      defaults: { auto_trim: !!($("evAutoTrim") && $("evAutoTrim").checked) },
    }),
  });
  if (created.status === 201) {
    const meta = await created.json();
    return meta.slug || name;
  }
  if (created.status === 409) return name.replace(/\s+/g, "-");
  throw new Error("Could not create session (" + created.status + ")");
}

async function saveCasesToSession(cases) {
  persistCurrentText();
  const name = (($("evSessionName") && $("evSessionName").value) || "").trim();
  if (!name) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Enter a session name first." }]);
    return;
  }
  const slug = await ensureSession(name);
  let ok = 0;
  for (const c of cases) {
    const res = await fetch("/api/gateway/sessions/" + encodeURIComponent(slug) + "/usecases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        usecase: caseToUsecase(c),
        curation: persistable(caseCuration[c.id] || emptyCuration(lastEvalRunUuid)),
      }),
    });
    if (res.ok) ok += 1;
  }
  paintBanners(bannerEl, [{
    kind: ok ? "ok" : "warn",
    text: ok ? ("Saved " + ok + " case(s) to session " + slug) : "Save to session failed.",
  }]);
}

async function openSession() {
  const host = $("evSessionList");
  const res = await fetch("/api/gateway/sessions");
  if (!res.ok) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Could not list sessions." }]);
    return;
  }
  const body = await res.json();
  const rows = body.sessions || [];
  if (!host) return;
  host.textContent = "";
  if (!rows.length) {
    host.appendChild(document.createTextNode("No sessions yet."));
    return;
  }
  rows.forEach((meta) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm";
    btn.appendChild(document.createTextNode(meta.name || meta.slug));
    btn.addEventListener("click", async () => {
      const detail = await fetch("/api/gateway/sessions/" + encodeURIComponent(meta.slug));
      if (!detail.ok) return;
      const info = await detail.json();
      const dates = info.dates || [];
      const day = dates.length ? dates[dates.length - 1].date : "";
      if (!day) {
        paintBanners(bannerEl, [{ kind: "warn", text: "Session has no saved dates." }]);
        return;
      }
      const ucs = await fetch(
        "/api/gateway/sessions/" + encodeURIComponent(meta.slug) + "/" + encodeURIComponent(day) + "/usecases?full=1"
      );
      if (!ucs.ok) return;
      const pack = await ucs.json();
      const loaded = (pack.usecases || []).map((uc, i) => ({
        id: String(uc.id || ("case-" + (i + 1))),
        name: String(uc.name || ""),
        text: String(uc.text || ""),
        language: String(uc.language || "en"),
        tags: Array.isArray(uc.tags) ? uc.tags : [],
        expected_spans: Array.isArray(uc.expected_spans) ? uc.expected_spans : [],
        forbidden_spans: Array.isArray(uc.forbidden_spans) ? uc.forbidden_spans : [],
      }));
      if (!loaded.length) {
        paintBanners(bannerEl, [{ kind: "warn", text: "No use cases in that session date." }]);
        return;
      }
      dataset = { ...emptyDataset(), cases: loaded };
      Object.keys(caseCuration).forEach((k) => { delete caseCuration[k]; });
      (pack.usecases || []).forEach((uc) => {
        const cur = uc.context && uc.context.curation;
        if (cur && uc.id) caseCuration[uc.id] = cur;
      });
      dirty = false;
      if ($("evSessionName")) $("evSessionName").value = meta.name || meta.slug;
      selectCase(0);
      paintBanners(bannerEl, [{ kind: "ok", text: "Opened session " + (meta.name || meta.slug) + " (" + loaded.length + ")." }]);
    });
    host.appendChild(btn);
  });
}

if ($("evSessionSave")) $("evSessionSave").addEventListener("click", () => {
  const c = currentCase();
  if (c) saveCasesToSession([c]).catch((err) => paintBanners(bannerEl, [{ kind: "warn", text: err.message }]));
});
if ($("evSessionSaveAll")) $("evSessionSaveAll").addEventListener("click", () => {
  persistCurrentText();
  saveCasesToSession(dataset.cases || []).catch((err) => paintBanners(bannerEl, [{ kind: "warn", text: err.message }]));
});
if ($("evSessionOpen")) $("evSessionOpen").addEventListener("click", () => {
  openSession().catch((err) => paintBanners(bannerEl, [{ kind: "warn", text: err.message }]));
});
async function acceptEvalVerdictFilter(pred) {
  const c = currentCase();
  if (!c) return;
  let cur = caseCuration[c.id] || emptyCuration(lastEvalRunUuid);
  let spans = evalVerdictSpans().filter((span) => !pred || pred(span));
  if ($("evAutoTrim") && $("evAutoTrim").checked && spans.length) {
    const res = await fetch("/api/gateway/trim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: c.text || "", spans }),
    });
    if (res.ok) {
      const body = await res.json();
      spans = body.spans || spans;
    }
  }
  spans.forEach((span) => {
    cur = upsertEntry(cur, {
      key: spanKey(span, "llm_verdict"),
      decision: "accept",
      new_start: span.start,
      new_end: span.end,
      trimmed: !!span.trimmed,
    });
  });
  caseCuration[c.id] = cur;
  dirty = true;
  renderAll();
}

function evalVerdictAgrees(span) {
  const reportCase = reportCaseForCurrent();
  const preds = (reportCase && reportCase.predicted_spans) || [];
  return preds.some((p) => p.start === span.start && p.end === span.end && p.entity_type === span.entity_type);
}

if ($("evVerdictAcceptAll")) $("evVerdictAcceptAll").addEventListener("click", () => acceptEvalVerdictFilter(null));
if ($("evVerdictAcceptAgree")) $("evVerdictAcceptAgree").addEventListener("click", () => {
  acceptEvalVerdictFilter((span) => evalVerdictAgrees(span));
});
if ($("evVerdictAcceptLlmOnly")) $("evVerdictAcceptLlmOnly").addEventListener("click", () => {
  acceptEvalVerdictFilter((span) => !evalVerdictAgrees(span));
});
if ($("evRecommendDownload")) $("evRecommendDownload").addEventListener("click", () => {
  const c = currentCase();
  const reportCase = lastReport && c && (lastReport.cases || []).find((row) => row.id === c.id);
  const recs = (reportCase && reportCase.recommendations) || (lastReport && lastReport.recommendations);
  if (!recs) return;
  downloadJson("recommendations.json", recs);
});

if ($("evLlmShow")) $("evLlmShow").addEventListener("click", toggleEvalLlm);
if ($("evLlmDownload")) $("evLlmDownload").addEventListener("click", async () => {
  if (!lastEvalRunUuid) return;
  const log = lastLlmLog || await fetch("/api/gateway/llm-log/" + encodeURIComponent(lastEvalRunUuid)).then((r) => r.json());
  downloadJson("llm-log-" + lastEvalRunUuid.slice(0, 8) + ".json", log);
});

setActionButtonsEnabled(false);
selectCase(0);
loadHealth();
