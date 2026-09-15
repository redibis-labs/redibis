import {
  offsetsFromPrefixAndSelected,
  paintBanners,
  renderHighlights,
  selectionOffsets,
  sliceCodepoints,
  toChars,
} from "./gateway_render.mjs";

const MAX = Number(window.GW_MAX_CHARS || 20000);
const ENTITY_TYPES = [
  "LOCATION", "PHONE_NUMBER", "PERSON", "EMAIL_ADDRESS", "EG_NATIONAL_ID",
  "VOUCHER", "SIM_PUK", "SUPPORT_TICKET", "CREDIT_CARD", "IMEI", "IMSI",
  "OTP", "AGE", "IP_ADDRESS", "URL", "MAC_ADDRESS",
];

const $ = (id) => document.getElementById(id);
const bannerEl = $("gwBanner");
const textEl = $("ucText");
const overlayEl = $("ucOverlay");
const entityEl = $("ucEntity");
const spansEl = $("ucSpans");

let draft = emptyDraft();
let selectedSpan = null;

function emptyDraft() {
  return {
    id: "",
    version: 0,
    name: "",
    text: "",
    language: "ar",
    tags: [],
    author: "",
    notes: "",
    context: { source: "", speakers: [], expect_entities: [] },
    expected_spans: [],
    forbidden_spans: [],
    rule_edits: { exclude_terms: [], quantity_units: [], context_cues: {} },
  };
}

function csv(value) {
  return String(value || "")
    .split(/[,،]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function showBanner(kind, text) {
  paintBanners(bannerEl, [{ kind, text }]);
}

function syncFormFromDraft() {
  $("ucName").value = draft.name || "";
  $("ucText").value = draft.text || "";
  $("ucLang").value = draft.language || "ar";
  $("ucTags").value = (draft.tags || []).join(", ");
  const ctx = draft.context || {};
  $("ucSource").value = ctx.source || "";
  $("ucSpeakers").value = (ctx.speakers || []).join(", ");
  $("ucExpect").value = (ctx.expect_entities || []).join(", ");
  const edits = draft.rule_edits || {};
  $("ucExclude").value = (edits.exclude_terms || []).join(", ");
  $("ucUnits").value = (edits.quantity_units || []).join(", ");
  const loc = ((edits.context_cues || {}).LOCATION || {}).triggers || [];
  $("ucLocTriggers").value = loc.join(", ");
  $("ucVersion").textContent = draft.version ? ("v" + draft.version) : "unsaved";
  $("ucTitle").textContent = draft.name || "Use case";
  $("ucCount").textContent = toChars(draft.text || "").length.toLocaleString()
    + " / " + MAX.toLocaleString();
}

function readFormIntoDraft() {
  draft.name = $("ucName").value || "";
  draft.text = textEl.value || "";
  draft.language = $("ucLang").value || "ar";
  draft.tags = csv($("ucTags").value);
  draft.context = {
    source: $("ucSource").value || "",
    speakers: csv($("ucSpeakers").value),
    expect_entities: csv($("ucExpect").value).map((s) => s.toUpperCase()),
  };
  const locTriggers = csv($("ucLocTriggers").value);
  draft.rule_edits = {
    exclude_terms: csv($("ucExclude").value),
    quantity_units: csv($("ucUnits").value),
    context_cues: locTriggers.length
      ? { LOCATION: { triggers: locTriggers, extend: "sentence" } }
      : {},
  };
}

function overlaySpans() {
  const out = [];
  (draft.expected_spans || []).forEach((span, i) => {
    out.push({ ...span, eval_kind: "gold", _kind: "expected", _index: i });
  });
  (draft.forbidden_spans || []).forEach((span, i) => {
    out.push({ ...span, eval_kind: "bad", _kind: "forbidden", _index: i });
  });
  return out;
}

function renderOverlay() {
  renderHighlights(overlayEl, draft.text || "", overlaySpans());
  spansEl.textContent = "";
  function addRow(span, kind, index) {
    const row = document.createElement("div");
    row.className = "gw-eval-span-row";
    const chip = document.createElement("span");
    chip.className = "gw-pill " + (kind === "forbidden" ? "bad" : "exact");
    chip.dir = "auto";
    chip.style.unicodeBidi = "isolate";
    chip.appendChild(document.createTextNode(
      (span.entity_type || "") + " · " + (span.value || "").slice(0, 48)
    ));
    chip.tabIndex = 0;
    chip.addEventListener("click", () => {
      selectedSpan = { kind, index };
      const next = prompt("Entity type (empty deletes)", span.entity_type || "");
      if (next == null) return;
      const list = kind === "forbidden" ? draft.forbidden_spans : draft.expected_spans;
      if (!String(next).trim()) {
        list.splice(index, 1);
      } else {
        list[index] = { ...span, entity_type: String(next).trim().toUpperCase() };
      }
      readFormIntoDraft();
      renderOverlay();
    });
    row.appendChild(chip);
    spansEl.appendChild(row);
  }
  (draft.expected_spans || []).forEach((s, i) => addRow(s, "expected", i));
  (draft.forbidden_spans || []).forEach((s, i) => addRow(s, "forbidden", i));
}

function currentSelection() {
  const fromPane = selectionOffsets(overlayEl);
  if (fromPane) return fromPane;
  const rawStart = textEl.selectionStart;
  const rawEnd = textEl.selectionEnd;
  if (rawStart == null || rawEnd == null || rawEnd <= rawStart) return null;
  return offsetsFromPrefixAndSelected(
    (textEl.value || "").slice(0, rawStart),
    (textEl.value || "").slice(rawStart, rawEnd),
  );
}

function markSpan(kind) {
  readFormIntoDraft();
  const off = currentSelection();
  if (!off) {
    showBanner("warn", "Select some text first.");
    return;
  }
  const value = sliceCodepoints(toChars(draft.text || ""), off.start, off.end);
  const entity = (entityEl.value || "LOCATION").toUpperCase();
  const span = {
    id: (kind === "forbidden" ? "f" : "s") + String(Date.now()).slice(-6),
    start: off.start,
    end: off.end,
    entity_type: entity,
    value,
    grade: "strict",
  };
  if (kind === "forbidden") {
    span.reason = "";
    draft.forbidden_spans = [...(draft.forbidden_spans || []), span];
  } else {
    draft.expected_spans = [...(draft.expected_spans || []), span];
  }
  renderOverlay();
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail;
    const msg = typeof detail === "string"
      ? detail
      : (detail && (detail.message || JSON.stringify(detail))) || res.statusText;
    throw new Error(msg);
  }
  return body;
}

async function saveDraft() {
  readFormIntoDraft();
  if (!draft.id) {
    const created = await api("/api/gateway/usecases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: draft.name || "untitled",
        text: draft.text,
        language: draft.language,
        tags: draft.tags,
        context: draft.context,
        expected_spans: draft.expected_spans,
        forbidden_spans: draft.forbidden_spans,
        rule_edits: draft.rule_edits,
        notes: draft.notes,
      }),
    });
    draft = { ...draft, ...created };
    history.replaceState({}, "", "/gateway/usecases/" + created.id);
    syncFormFromDraft();
    renderOverlay();
    showBanner("ok", "Saved v" + created.version);
    return created;
  }
  const saved = await api("/api/gateway/usecases/" + encodeURIComponent(draft.id), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: draft.name,
      text: draft.text,
      language: draft.language,
      tags: draft.tags,
      context: draft.context,
      expected_spans: draft.expected_spans,
      forbidden_spans: draft.forbidden_spans,
      rule_edits: draft.rule_edits,
      notes: draft.notes,
    }),
  });
  draft = { ...draft, ...saved };
  syncFormFromDraft();
  renderOverlay();
  showBanner("ok", "Saved v" + saved.version);
  return saved;
}

async function runDraft() {
  await saveDraft();
  const body = await api("/api/gateway/usecases/" + encodeURIComponent(draft.id) + "/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      draft_rules: draft.rule_edits,
      context: draft.context,
      language: draft.language,
      engines: "both",
      min_score: 0.35,
    }),
  });
  if (body.report_url) window.open(body.report_url, "_blank");
}

async function importAsset(file) {
  const text = await file.text();
  const payload = JSON.parse(text);
  const saved = await api("/api/gateway/usecases/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  location.href = "/gateway/usecases/" + saved.id;
}

async function loadList() {
  const body = await api("/api/gateway/usecases");
  const host = $("ucList");
  host.textContent = "";
  (body.usecases || []).forEach((row) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "gw-eval-case";
    btn.appendChild(document.createTextNode(
      (row.name || row.id) + " · v" + row.latest_version
    ));
    btn.addEventListener("click", () => {
      location.href = "/gateway/usecases/" + row.id;
    });
    host.appendChild(btn);
  });
}

async function loadEditor(ucId) {
  if (!ucId || ucId === "new") {
    $("ucEditor").hidden = false;
    $("ucListPane").hidden = true;
    draft = emptyDraft();
    if (ucId === "new") {
      const created = await api("/api/gateway/usecases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "untitled", text: "", language: "ar" }),
      });
      draft = { ...draft, ...created };
      history.replaceState({}, "", "/gateway/usecases/" + created.id);
    }
    syncFormFromDraft();
    renderOverlay();
    return;
  }
  const saved = await api("/api/gateway/usecases/" + encodeURIComponent(ucId));
  draft = { ...emptyDraft(), ...saved };
  $("ucEditor").hidden = false;
  $("ucListPane").hidden = true;
  syncFormFromDraft();
  renderOverlay();
}

function fillEntities() {
  entityEl.textContent = "";
  ENTITY_TYPES.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.appendChild(document.createTextNode(name));
    entityEl.appendChild(opt);
  });
}

function wire() {
  fillEntities();
  $("ucMarkExpected").addEventListener("click", () => markSpan("expected"));
  $("ucMarkForbidden").addEventListener("click", () => markSpan("forbidden"));
  $("ucSave").addEventListener("click", () => saveDraft().catch((err) => showBanner("warn", err.message)));
  $("ucRun").addEventListener("click", () => runDraft().catch((err) => showBanner("warn", err.message)));
  $("ucDownload").addEventListener("click", () => {
    if (!draft.id) {
      showBanner("warn", "Save the use case first.");
      return;
    }
    window.location = "/api/gateway/usecases/" + encodeURIComponent(draft.id) + "/download";
  });
  $("ucImport").addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (file) importAsset(file).catch((err) => showBanner("warn", err.message));
  });
  $("ucImportList").addEventListener("change", (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (file) importAsset(file).catch((err) => showBanner("warn", err.message));
  });
  $("ucCreate").addEventListener("click", async () => {
    try {
      const created = await api("/api/gateway/usecases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: $("ucNewName").value || "untitled",
          text: "",
          language: "ar",
        }),
      });
      location.href = "/gateway/usecases/" + created.id;
    } catch (err) {
      showBanner("warn", err.message);
    }
  });
  textEl.addEventListener("input", () => {
    draft.text = textEl.value || "";
    $("ucCount").textContent = toChars(draft.text).length.toLocaleString()
      + " / " + MAX.toLocaleString();
    renderOverlay();
  });
}

async function boot() {
  wire();
  const ucId = String(window.GW_USECASE_ID || "");
  try {
    if (!ucId) {
      $("ucListPane").hidden = false;
      $("ucEditor").hidden = true;
      await loadList();
    } else {
      await loadEditor(ucId);
    }
  } catch (err) {
    showBanner("warn", err.message);
  }
}

boot();
