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

export function normalizeEntityType(name) {
  return String(name || "").trim().toUpperCase().replace(/\s+/g, "_");
}

export function splitTriggers(raw) {
  if (Array.isArray(raw)) return raw.map((s) => String(s).trim()).filter(Boolean);
  return String(raw || "").split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
}

export function escapeRegexLiteral(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function triggerPattern(trigger) {
  const escaped = escapeRegexLiteral(trigger);
  if (/[./:@?#]/.test(trigger)) return escaped;
  return "\\b" + escaped + "\\b";
}

export function patternName(entityType, trigger) {
  const et = normalizeEntityType(entityType).toLowerCase() || "custom";
  const slug = String(trigger || "custom")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_|_$/g, "")
    .slice(0, 40);
  return (et + "_" + (slug || "custom")).slice(0, 60);
}

export function buildCategoryPatch(entityType, triggers, extraPattern) {
  const et = normalizeEntityType(entityType);
  const trigs = splitTriggers(triggers);
  const add = {};
  if (!et) return { entity_type: "", triggers: [], patternsAdd: add };
  trigs.forEach((t) => {
    add[patternName(et, t)] = {
      pattern: triggerPattern(t),
      entity_type: et,
      recognizer_group: "free_text",
      presidio_score: 0.86,
      context_hints: trigs.slice(0, 8),
      unvalidated_reason: "operator-authored trigger for " + et,
    };
  });
  const extra = String(extraPattern || "").trim();
  if (extra) {
    add[patternName(et, "custom")] = {
      pattern: extra,
      entity_type: et,
      recognizer_group: "free_text",
      presidio_score: 0.86,
      unvalidated_reason: "operator-authored pattern for " + et,
    };
  }
  return { entity_type: et, triggers: trigs, patternsAdd: add };
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj || emptyDraft()));
}

function ensureDraftShape(raw) {
  const draft = { ...emptyDraft(), ...(raw || {}) };
  if (!draft.ner_stoplist) draft.ner_stoplist = { "*": [] };
  if (!draft.context_cues) draft.context_cues = {};
  if (!draft.patterns || typeof draft.patterns !== "object") {
    draft.patterns = { add: {}, remove: [], replace_all: false };
  }
  if (!draft.patterns.add) draft.patterns.add = {};
  if (!draft.patterns.remove) draft.patterns.remove = [];
  return draft;
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
    collapsed: true,
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
    const head = el("button", { type: "button", className: "btn gw-rules-toggle" });
    const ineffectiveN = (state.ineffective || []).length;
    let title = state.dirty ? "Detection rules · unsaved draft" : "Detection rules";
    if (state.applyToRun) title += " · staged";
    if (ineffectiveN) title += " · " + ineffectiveN + " ineffective";
    head.appendChild(document.createTextNode((state.collapsed ? "▸ " : "▾ ") + title));
    head.setAttribute("aria-expanded", state.collapsed ? "false" : "true");
    if (ineffectiveN) head.style.color = "#991b1b";
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
        addTermChecked(v, { preferred: key, spans: opts.getSpans ? opts.getSpans() : [] });
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
    body.appendChild(addRow("New category for a cue (e.g. SOCIAL_URL)", (v) => {
      const et = normalizeEntityType(v);
      if (!et) return;
      if (!state.draft.context_cues[et]) {
        state.draft.context_cues[et] = { triggers: [], extend: "sentence" };
        markDirty();
      }
    }));

    body.appendChild(el("h3", { className: "gw-rules-h", text: "Add PII category" }));
    body.appendChild(el("p", {
      className: "gw-hint",
      text: "Name a type such as SOCIAL_URL and the wording that starts it (instagram, linkedin.com, profile:).",
    }));
    const catForm = el("div", { className: "gw-cat-form" });
    const catName = isolate(el("input", { type: "text", placeholder: "Category name (e.g. SOCIAL_URL)" }));
    const catTrig = isolate(el("input", { type: "text", placeholder: "Trigger wording, comma-separated" }));
    const catPat = isolate(el("input", { type: "text", placeholder: "Optional regex" }));
    const catBtn = el("button", { type: "button", className: "btn btn-sm", text: "Add category" });
    catBtn.addEventListener("click", () => {
      if (!addCategory(catName.value, catTrig.value, catPat.value)) return;
      catName.value = "";
      catTrig.value = "";
      catPat.value = "";
    });
    catForm.appendChild(catName);
    catForm.appendChild(catTrig);
    catForm.appendChild(catPat);
    catForm.appendChild(catBtn);
    body.appendChild(catForm);

    const customAdd = (state.draft.patterns && state.draft.patterns.add) || {};
    const customNames = Object.keys(customAdd);
    if (customNames.length) {
      body.appendChild(el("h3", { className: "gw-rules-h", text: "Custom patterns" }));
      customNames.forEach((name) => {
        const entry = customAdd[name] || {};
        const row = el("div", { className: "gw-sum-row" });
        const label = isolate(el("span"));
        label.appendChild(document.createTextNode(
          name + " · " + (entry.entity_type || "") + " · " + (entry.pattern || "")
        ));
        const rm = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "×" });
        rm.addEventListener("click", () => {
          delete state.draft.patterns.add[name];
          markDirty();
        });
        row.appendChild(label);
        row.appendChild(rm);
        body.appendChild(row);
      });
    }

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
        state.draft = ensureDraftShape(parsed.rules || parsed);
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
    state.draft = ensureDraftShape(clone(body.rules || emptyDraft()));
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
    state.ineffective = body.ineffective_terms || [];
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

  function addTerm(field, value, extra) {
    extra = extra || {};
    if (field === "ner_stoplist") {
      const et = extra.entity_type || "*";
      state.draft.ner_stoplist = state.draft.ner_stoplist || {};
      state.draft.ner_stoplist[et] = uniquePush(state.draft.ner_stoplist[et] || [], value);
    } else if (field === "context_cues") {
      const et = normalizeEntityType(extra.entity_type || "LOCATION");
      const cue = state.draft.context_cues[et] || { triggers: [], extend: "sentence" };
      state.draft.context_cues[et] = { ...cue, triggers: uniquePush(cue.triggers || [], value) };
    } else if (field === "forbidden_span") {
      return;
    } else {
      state.draft[field] = uniquePush(state.draft[field] || [], value);
    }
    state.applyToRun = true;
    markDirty();
  }

  function addCategory(entityType, triggers, extraPattern) {
    const patch = buildCategoryPatch(entityType, triggers, extraPattern);
    if (!patch.entity_type || (!patch.triggers.length && !Object.keys(patch.patternsAdd).length)) {
      return null;
    }
    state.draft = ensureDraftShape(state.draft);
    const cue = state.draft.context_cues[patch.entity_type] || { triggers: [], extend: "sentence" };
    let next = cue.triggers || [];
    patch.triggers.forEach((t) => { next = uniquePush(next, t); });
    state.draft.context_cues[patch.entity_type] = {
      ...cue,
      triggers: next,
      extend: cue.extend || "sentence",
    };
    state.draft.patterns.add = Object.assign({}, state.draft.patterns.add || {}, patch.patternsAdd);
    state.applyToRun = true;
    state.collapsed = false;
    markDirty();
    return patch;
  }

  function expand() {
    if (state.collapsed) {
      state.collapsed = false;
      render();
    }
  }

  function chooserHost() {
    return opts.chooserHost
      || (typeof document !== "undefined" && document.getElementById("gwInlineForm"))
      || (typeof document !== "undefined" && document.getElementById("evInlineForm"));
  }

  function revealHost(node) {
    if (!node) return;
    node.hidden = false;
    if (typeof node.scrollIntoView === "function") {
      node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function fillCategoryForm(host, { entityType, trigger, advice, onDone }) {
    host.textContent = "";
    host.appendChild(el("div", {
      className: "gw-hint",
      text: "Add a PII category. Trigger wording is what starts the span (e.g. instagram, linkedin.com).",
    }));
    const nameInp = isolate(el("input", { type: "text", placeholder: "Category name (e.g. SOCIAL_URL)" }));
    nameInp.value = entityType || "";
    const trigInp = isolate(el("input", { type: "text", placeholder: "Trigger wording" }));
    trigInp.value = trigger || "";
    const row = el("div", { className: "gw-pol-actions" });
    const ok = el("button", { type: "button", className: "btn btn-sm", text: "Add category" });
    const cancel = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Cancel" });
    ok.addEventListener("click", () => {
      const patch = addCategory(nameInp.value, trigInp.value);
      host.hidden = true;
      host.textContent = "";
      onDone(patch ? { action: "add_category", patch, advice } : null);
    });
    cancel.addEventListener("click", () => {
      host.hidden = true;
      host.textContent = "";
      onDone(null);
    });
    host.appendChild(nameInp);
    host.appendChild(trigInp);
    row.appendChild(ok);
    row.appendChild(cancel);
    host.appendChild(row);
    nameInp.focus();
  }

  async function addTermChecked(value, options) {
    const optsLocal = options || {};
    const term = String(value || "").trim();
    if (!term) return null;
    const text = opts.getText ? opts.getText() : "";
    const res = await fetch("/api/gateway/rules/advise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        term,
        text,
        spans: optsLocal.spans || [],
        draft_rules: state.draft,
        requested: optsLocal.preferred || "",
      }),
    });
    if (!res.ok) {
      if (opts.onError) opts.onError(new Error("Advise failed (" + res.status + ")"));
      return null;
    }
    const body = await res.json();
    const advice = body.advice || [];
    const viable = advice.filter((a) => (a.would_remove || []).length);
    const preferred = optsLocal.preferred;
    const pick = viable.find((a) => a.target === preferred && a.would_remove && a.would_remove.length)
      || viable.find((a) => a.default)
      || viable[0];
    if (optsLocal.skipChooser) {
      if (!pick) return { refused: true, reason: (advice.find((a) => a.reason) || {}).reason || "", advice };
      addTerm(pick.target, term);
      return pick;
    }
    return new Promise((resolve) => {
      const host = chooserHost();
      if (!host || typeof window === "undefined") {
        if (pick) {
          addTerm(pick.target, term);
          resolve(pick);
        } else {
          resolve({ refused: true, advice });
        }
        return;
      }
      revealHost(host);
      host.textContent = "";
      const reason = (advice.find((a) => a.reason) || {}).reason || "";
      host.appendChild(el("div", {
        className: "gw-hint",
        text: viable.length
          ? ("Which rule should receive “" + term + "”?")
          : (reason || ("No suppression field removes “" + term + "”. Add it as a PII category instead.")),
      }));
      const cat = el("button", {
        type: "button",
        className: "btn btn-sm",
        text: "Add as PII category…",
      });
      cat.addEventListener("click", () => {
        fillCategoryForm(host, {
          entityType: optsLocal.entityType || "",
          trigger: term,
          advice,
          onDone: resolve,
        });
      });
      host.appendChild(cat);
      advice.forEach((row) => {
        const btn = el("button", {
          type: "button",
          className: "btn btn-ghost btn-sm",
          text: row.target + (row.would_remove && row.would_remove.length
            ? (" · removes " + row.would_remove.length)
            : " · no effect"),
        });
        btn.disabled = !(row.would_remove && row.would_remove.length) && row.target !== "forbidden_span";
        btn.title = row.reason || "";
        btn.addEventListener("click", () => {
          host.hidden = true;
          host.textContent = "";
          if (row.target === "forbidden_span") {
            resolve({ ...row, action: "forbidden_span" });
            return;
          }
          addTerm(row.target, term);
          resolve(row);
        });
        host.appendChild(btn);
      });
      if (optsLocal.onReject) {
        const rej = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Reject this span" });
        rej.addEventListener("click", () => {
          host.hidden = true;
          host.textContent = "";
          optsLocal.onReject();
          resolve({ refused: true, reason, advice, action: "reject_span" });
        });
        host.appendChild(rej);
      }
      const cancel = el("button", { type: "button", className: "btn btn-ghost btn-sm", text: "Cancel" });
      cancel.addEventListener("click", () => {
        host.hidden = true;
        host.textContent = "";
        resolve(null);
      });
      host.appendChild(cancel);
    });
  }

  if (host) {
    render();
    load();
  }

  return {
    getDraft: () => (state.applyToRun || state.dirty ? clone(state.draft) : null),
    getDraftAlways: () => clone(state.draft),
    addTerm,
    addTermChecked,
    addCategory,
    expand,
    preview,
    save,
    load,
    onChange: (fn) => listeners.push(fn),
    applyToRun: () => { state.applyToRun = true; notify(); },
    isDirty: () => state.dirty,
    isCollapsed: () => !!state.collapsed,
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
