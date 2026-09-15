/** Shared Text Gateway rules editor. Mutates an in-memory draft overlay. */

const ROLE = (typeof window !== "undefined" && window.REDIBIS_USER && window.REDIBIS_USER.role) || "";
const IS_ADMIN = ROLE === "admin";

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach((k) => {
      if (k === "className") node.className = attrs[k];
      else if (k === "text") node.appendChild(document.createTextNode(attrs[k]));
      else if (k.startsWith("on") && typeof attrs[k] === "function") node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else if (attrs[k] === false || attrs[k] == null) return;
      else node.setAttribute(k, String(attrs[k]));
    });
  }
  (children || []).forEach((c) => { if (c) node.appendChild(c); });
  return node;
}

function isolate(node) {
  node.dir = "auto";
  node.style.unicodeBidi = "isolate";
  return node;
}

function emptyDraft() {
  return {
    noise_terms: [],
    exclude_terms: [],
    quantity_units: [],
    ner_stoplist: { "*": [] },
    context_cues: {},
    exclude_patterns: [],
    patterns: { add: {}, remove: [], replace_all: false },
  };
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj || emptyDraft()));
}

export function createRulesEditor(host, options) {
  const opts = options || {};
  const state = {
    defaults: emptyDraft(),
    stored: {},
    sources: [],
    draft: emptyDraft(),
    dirty: false,
    applyToRun: false,
    lastDiff: null,
  };
  const listeners = [];

  function notify() {
    listeners.forEach((fn) => fn(state));
  }

  function markDirty() {
    state.dirty = true;
    render();
    notify();
  }

  function uniquePush(list, value) {
    const v = String(value || "").trim();
    if (!v) return list;
    const folded = v.toLocaleLowerCase();
    if (list.some((x) => String(x).toLocaleLowerCase() === folded)) return list;
    return list.concat([v]);
  }

  function chips(list, onRemove) {
    const wrap = el("div", { className: "gw-chips" });
    (list || []).forEach((term, i) => {
      const chip = isolate(el("span", { className: "gw-chip" }));
      chip.appendChild(document.createTextNode(term));
      const rm = el("button", { type: "button", className: "gw-chip-x", text: "×" });
      rm.addEventListener("click", () => onRemove(i));
      chip.appendChild(rm);
      wrap.appendChild(chip);
    });
    return wrap;
  }

  function addRow(placeholder, onAdd) {
    const row = el("div", { className: "gw-chip-add" });
    const input = isolate(el("input", { type: "text", placeholder }));
    const btn = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Add" });
    const go = () => {
      const v = input.value;
      input.value = "";
      onAdd(v);
    };
    btn.addEventListener("click", go);
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); go(); } });
    row.appendChild(input);
    row.appendChild(btn);
    return row;
  }

  function renderDiff(diff) {
    const box = el("div", { className: "gw-rule-diff" });
    if (!diff) return box;
    function block(title, items, kind) {
      if (!items || !items.length) return;
      const h = el("div", { className: "gw-hint", text: title });
      box.appendChild(h);
      items.forEach((item) => {
        const row = isolate(el("div", { className: "gw-diff-row gw-diff-" + kind }));
        const span = item.after || item.before || item;
        const text = (span && (span.text || "")) + " [" + (span.entity_type || "") + " " + (span.start ?? "") + "–" + (span.end ?? "") + "]";
        row.appendChild(document.createTextNode(text));
        box.appendChild(row);
      });
    }
    block("Added", diff.added, "add");
    block("Removed", diff.removed, "remove");
    block("Retyped", diff.retyped, "change");
    block("Resized", diff.resized, "change");
    if (!((diff.added || []).length || (diff.removed || []).length || (diff.retyped || []).length || (diff.resized || []).length)) {
      box.appendChild(el("div", { className: "gw-hint", text: "No span changes." }));
    }
    return box;
  }

  function render() {
    host.textContent = "";
    const head = el("button", { type: "button", className: "gw-rules-toggle" });
    head.appendChild(document.createTextNode(state.dirty ? "Detection rules · unsaved draft" : "Detection rules"));
    const body = el("div", { className: "gw-rules-body" });
    body.hidden = !!state.collapsed;
    head.addEventListener("click", () => {
      state.collapsed = !state.collapsed;
      render();
    });

    function section(title, key) {
      body.appendChild(el("h3", { className: "gw-rules-h", text: title }));
      body.appendChild(chips(state.draft[key] || [], (i) => {
        const next = (state.draft[key] || []).slice();
        next.splice(i, 1);
        state.draft[key] = next;
        markDirty();
      }));
      body.appendChild(addRow("Add " + title.toLowerCase(), (v) => {
        state.draft[key] = uniquePush(state.draft[key] || [], v);
        markDirty();
      }));
    }
    section("Noise terms", "noise_terms");
    section("Exclude terms", "exclude_terms");
    section("Quantity units", "quantity_units");

    body.appendChild(el("h3", { className: "gw-rules-h", text: "NER stoplist" }));
    const stop = state.draft.ner_stoplist || {};
    Object.keys(stop).forEach((et) => {
      body.appendChild(el("div", { className: "gw-hint", text: et }));
      body.appendChild(chips(stop[et] || [], (i) => {
        const next = (stop[et] || []).slice();
        next.splice(i, 1);
        state.draft.ner_stoplist[et] = next;
        markDirty();
      }));
      body.appendChild(addRow("Add surface for " + et, (v) => {
        state.draft.ner_stoplist[et] = uniquePush(stop[et] || [], v);
        markDirty();
      }));
    });
    body.appendChild(addRow("New entity type (or *)", (v) => {
      const et = v.trim() === "*" ? "*" : v.trim().toUpperCase().replace(/\s+/g, "_");
      if (!et) return;
      if (!state.draft.ner_stoplist[et]) state.draft.ner_stoplist[et] = [];
      markDirty();
    }));

    body.appendChild(el("h3", { className: "gw-rules-h", text: "Context cues" }));
    const cues = state.draft.context_cues || {};
    Object.keys(cues).forEach((et) => {
      const cue = cues[et] || {};
      const row = el("div", { className: "gw-cue-row" });
      row.appendChild(el("div", { className: "gw-hint", text: et + (cue.extend ? " · extend=" + cue.extend : "") }));
      row.appendChild(chips(cue.triggers || [], (i) => {
        const next = (cue.triggers || []).slice();
        next.splice(i, 1);
        state.draft.context_cues[et] = { ...cue, triggers: next };
        markDirty();
      }));
      row.appendChild(addRow("Add trigger", (v) => {
        state.draft.context_cues[et] = { ...cue, triggers: uniquePush(cue.triggers || [], v) };
        markDirty();
      }));
      body.appendChild(row);
    });

    const actions = el("div", { className: "gw-pol-actions" });
    const previewBtn = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Preview" });
    previewBtn.addEventListener("click", () => preview());
    actions.appendChild(previewBtn);
    const applyBtn = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: state.applyToRun ? "Applied to this run" : "Apply to this run" });
    applyBtn.addEventListener("click", () => {
      state.applyToRun = true;
      markDirty();
      if (opts.onApply) opts.onApply(state.draft);
    });
    actions.appendChild(applyBtn);
    if (IS_ADMIN) {
      const saveBtn = el("button", { type: "button", className: "btn btn-sm", text: "Save as default" });
      saveBtn.addEventListener("click", () => save());
      actions.appendChild(saveBtn);
    }
    const dl = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Download" });
    dl.addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(state.draft, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "text-rules-draft.json";
      a.click();
    });
    actions.appendChild(dl);
    const file = el("input", { type: "file", accept: "application/json,.json", hidden: true });
    const imp = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Import" });
    imp.addEventListener("click", () => file.click());
    file.addEventListener("change", async (ev) => {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = "";
      if (!f) return;
      try {
        const parsed = JSON.parse(await f.text());
        state.draft = { ...emptyDraft(), ...(parsed.rules || parsed) };
        markDirty();
      } catch (err) {
        if (opts.onError) opts.onError(err);
      }
    });
    actions.appendChild(imp);
    actions.appendChild(file);
    body.appendChild(actions);
    if (state.lastDiff) body.appendChild(renderDiff(state.lastDiff));
    if (state.sources && state.sources.length) {
      body.appendChild(el("p", { className: "gw-hint", text: "Sources: " + state.sources.join(" → ") }));
    }
    host.appendChild(head);
    host.appendChild(body);
  }

  async function load() {
    const res = await fetch("/api/gateway/rules");
    if (!res.ok) return;
    const body = await res.json();
    state.defaults = body.defaults || emptyDraft();
    state.stored = body.stored || {};
    state.sources = body.sources || [];
    state.draft = clone(body.rules || emptyDraft());
    if (!state.draft.ner_stoplist) state.draft.ner_stoplist = { "*": [] };
    state.dirty = false;
    render();
    notify();
  }

  async function preview() {
    const text = opts.getText ? opts.getText() : "";
    const res = await fetch("/api/gateway/rules/dry-run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        language: opts.getLanguage ? opts.getLanguage() : "en",
        engines: opts.getEngines ? opts.getEngines() : "regex",
        min_score: opts.getMinScore ? opts.getMinScore() : 0.35,
        draft_rules: state.draft,
      }),
    });
    if (!res.ok) {
      if (opts.onError) opts.onError(new Error("Preview failed (" + res.status + ")"));
      return null;
    }
    const body = await res.json();
    state.lastDiff = body.diff;
    render();
    if (opts.onPreview) opts.onPreview(body);
    return body;
  }

  async function save() {
    const res = await fetch("/api/gateway/rules", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rules: state.draft }),
    });
    if (!res.ok) {
      if (opts.onError) opts.onError(new Error("Save failed (" + res.status + ")"));
      return;
    }
    state.dirty = false;
    state.applyToRun = false;
    await load();
    if (opts.onSaved) opts.onSaved();
  }

  function addTerm(field, value) {
    if (field === "ner_stoplist") {
      const et = "*";
      state.draft.ner_stoplist = state.draft.ner_stoplist || {};
      state.draft.ner_stoplist[et] = uniquePush(state.draft.ner_stoplist[et] || [], value);
    } else if (field === "context_cues") {
      const et = (arguments[2] || "LOCATION").toUpperCase();
      const cue = state.draft.context_cues[et] || { triggers: [], extend: "sentence" };
      state.draft.context_cues[et] = { ...cue, triggers: uniquePush(cue.triggers || [], value) };
    } else {
      state.draft[field] = uniquePush(state.draft[field] || [], value);
    }
    state.applyToRun = true;
    markDirty();
  }

  host && load();

  return {
    getDraft: () => (state.applyToRun || state.dirty ? clone(state.draft) : null),
    getDraftAlways: () => clone(state.draft),
    addTerm,
    preview,
    save,
    load,
    onChange: (fn) => listeners.push(fn),
    applyToRun: () => { state.applyToRun = true; notify(); },
    isDirty: () => state.dirty,
  };
}

export function askInline(host, { label, value, onOk, onCancel }) {
  if (!host) return;
  host.hidden = false;
  host.textContent = "";
  const lab = el("label", { className: "gw-field" });
  lab.appendChild(document.createTextNode(label || "Value"));
  const input = isolate(el("input", { type: "text" }));
  input.value = value || "";
  lab.appendChild(input);
  const row = el("div", { className: "gw-pol-actions" });
  const ok = el("button", { type: "button", className: "btn btn-sm", text: "Apply" });
  const cancel = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Cancel" });
  ok.addEventListener("click", () => {
    host.hidden = true;
    host.textContent = "";
    if (onOk) onOk(input.value);
  });
  cancel.addEventListener("click", () => {
    host.hidden = true;
    host.textContent = "";
    if (onCancel) onCancel();
  });
  row.appendChild(ok);
  row.appendChild(cancel);
  host.appendChild(lab);
  host.appendChild(row);
  input.focus();
}
