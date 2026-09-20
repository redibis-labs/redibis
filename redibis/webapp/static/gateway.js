const {
  classOf,
  classifyBanners,
  paintBanners,
  remapSpansAfterDeid,
  renderHighlights,
  selectionOffsets,
} = await import(`./gateway_render.mjs?v=${window.GW_RENDER_V || ""}`);
const { createRulesEditor, askInline } = await import(`./gateway_rules.mjs?v=${window.GW_RULES_V || window.GW_RENDER_V || ""}`);
const {
  applyCuration,
  emptyCuration,
  entityCounts,
  isAccepted,
  isRejected,
  persistable,
  spanKey,
  upsertEntry,
} = await import(`./gateway_curation.mjs?v=${window.GW_CURATION_V || window.GW_RENDER_V || ""}`);

const MAX = Number(window.GW_MAX_CHARS || 200000);
const STRATEGIES = ["redact", "mask", "hash", "fpe", "fake", "passthrough"];
const $ = (id) => document.getElementById(id);

const input = $("gwInput");
const resultEl = $("gwResult");
const bannerEl = $("gwBanner");
const summaryEl = $("gwSummary");
const metaEl = $("gwMeta");
const metaCard = $("gwMetaCard");
const policyEl = $("gwPolicy");
const policyCard = $("gwPolicyCard");
const policyAllRedactBtn = $("gwPolicyAllRedact");
const actionsEl = $("gwActions");
const scanBtn = $("gwScan");
const editBtn = $("gwEdit");
const maskToggleBtn = $("gwMaskToggle");
const deidBtn = $("gwDeid");
const downloadBtn = $("gwDownload");
const clearBtn = $("gwClear");
const countEl = $("gwCount");
const tip = $("gwTip");
const langEl = $("gwLang");
const enginesEl = $("gwEngines");
const minScoreEl = $("gwMinScore");
const useLlmEl = $("gwUseLlm");
const llmProviderWrap = $("gwLlmProviderWrap");
const llmProviderEl = $("gwLlmProvider");
const llmModelWrap = $("gwLlmModelWrap");
const llmModelEl = $("gwLlmModel");
const llmKeyWrap = $("gwLlmKeyWrap");
const llmKeyEl = $("gwLlmKey");
const checkToxicityEl = $("gwCheckToxicity");
const checkInjectionEl = $("gwCheckInjection");
const guardHintEl = $("gwGuardHint");
const healthBannerEl = $("gwHealthBanner");
const safetyCard = $("gwSafetyCard");
const safetyEl = $("gwSafety");
const llmCard = $("gwLlmCard");
const llmEl = $("gwLlm");
const llmSummaryEl = $("gwLlmSummary");
const llmShowBtn = $("gwLlmShow");
const llmDownloadBtn = $("gwLlmDownload");
const llmDownloadBarBtn = $("gwLlmDownloadBar");
const progressEl = $("gwProgress");
const progressFillEl = $("gwProgressFill");
const progressStageEl = $("gwProgressStage");

// Backend stage order → cumulative percent once that stage's callback fires.
// Only completed backend stages advance the bar (no fake/timed progress).
const STAGE_PERCENT = {
  validate: 5,
  preprocess: 18,
  regex: 30,
  phone: 42,
  ner: 65,
  llm: 82,
  resolve: 95,
  done: 100,
};
const STAGE_LABEL = {
  validate: "Validating input…",
  preprocess: "Expanding spoken / obfuscated forms…",
  regex: "Running regex + Presidio…",
  phone: "Checking phone numbers…",
  ner: "Running NER model…",
  llm: "Refining with LLM…",
  resolve: "Resolving overlapping spans…",
  done: "Finishing…",
};

let sourceText = "";
let lastEnvelope = null;
let lastLlmLog = null;
let llmLogOpen = false;
let curation = emptyCuration();
let hideRejected = false;
const rulesEditor = $("gwRules")
  ? createRulesEditor($("gwRules"), {
    getText: () => sourceText || (input && input.value) || "",
    getSpans: () => (lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.pii && lastEnvelope.analysers.pii.spans) || [],
    getLanguage: () => langEl.value,
    getEngines: () => enginesEl.value,
    getMinScore: () => Number(minScoreEl.value || 0.35),
    onError: (err) => paintBanners(bannerEl, [{ kind: "warn", text: err.message }]),
    onSaved: () => paintBanners(bannerEl, [{ kind: "ok", text: "Rules saved as default." }]),
  })
  : null;
let reviewedPolicy = null;
let health = null;
let scanAbort = null;
let maskPreviewOn = false;
let maskedText = "";
let maskedSpans = [];

function updateCount() {
  const n = Array.from(input.value || "").length;
  countEl.textContent = n.toLocaleString() + " / " + MAX.toLocaleString();
}

function looksLikeHfModel(id) {
  const m = (id || "").trim();
  if (!m || m.indexOf(" ") >= 0 || m.indexOf("/") < 0) return false;
  const head = m.split("/")[0].toLowerCase();
  return ["gemini", "openai", "anthropic", "ollama", "hosted_vllm", "openrouter", "azure"].indexOf(head) < 0;
}

function preferLocalProviderForHfModel() {
  if (!llmProviderEl) return;
  const model = (llmModelEl && llmModelEl.value.trim()) || "";
  if (!looksLikeHfModel(model)) return;
  const cur = llmProviderEl.value || "";
  if (cur && cur !== "gemini" && cur !== "google_genai") return;
  for (const name of ["sglang", "sglang-qwen", "vllm"]) {
    const hit = Array.from(llmProviderEl.options).some((o) => o.value === name);
    if (hit) {
      llmProviderEl.value = name;
      return;
    }
  }
}

function scanPayload() {
  preferLocalProviderForHfModel();
  return {
    text: sourceText,
    language: langEl.value,
    engines: enginesEl.value,
    min_score: Number(minScoreEl.value || 0.35),
    resolve: "priority",
    use_llm: !!(useLlmEl && useLlmEl.checked),
    entities: [],
    llm_provider: (useLlmEl && useLlmEl.checked && llmProviderEl.value) || "",
    llm_model: (useLlmEl && useLlmEl.checked && llmModelEl.value.trim()) || "",
    llm_api_key: (useLlmEl && useLlmEl.checked && llmKeyEl && llmKeyEl.value.trim()) || "",
    check_toxicity: !!(checkToxicityEl && checkToxicityEl.checked),
    check_prompt_injection: !!(checkInjectionEl && checkInjectionEl.checked),
    draft_rules: rulesEditor && rulesEditor.getDraft() || undefined,
    llm_verdict: ($("gwLlmVerdict") && $("gwLlmVerdict").checked)
      ? ((useLlmEl && useLlmEl.checked) ? "both" : "independent")
      : "off",
    recommend: !!( $("gwRecommend") && $("gwRecommend").checked ),
  };
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location.href = "/login?next=/gateway";
    return null;
  }
  if (res.status === 429) throw new Error("Too many scans — wait a moment.");
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    const detail = d.detail;
    throw new Error(typeof detail === "string" ? detail : "Request failed (" + res.status + ")");
  }
  return res.json();
}

function setProgress(pct, label) {
  progressFillEl.style.width = Math.max(0, Math.min(100, pct)) + "%";
  progressFillEl.setAttribute("aria-valuenow", String(Math.round(pct)));
  if (label) progressStageEl.textContent = label;
}

function showProgress() {
  progressEl.hidden = false;
  setProgress(0, "Starting…");
}

function hideProgress() {
  progressEl.hidden = true;
  setProgress(0, "Starting…");
}

async function loadHealth() {
  try {
    const res = await fetch("/api/gateway/health");
    if (!res.ok) return;
    health = await res.json();
  } catch {
    health = null;
    return;
  }
  const providers = (health && health.llm_providers) || [];
  llmProviderEl.textContent = "";
  const def = document.createElement("option");
  def.value = "";
  def.appendChild(document.createTextNode("(default)"));
  llmProviderEl.appendChild(def);
  for (const p of providers) {
    const opt = document.createElement("option");
    opt.value = p.name || "";
    const bits = [p.name, p.cloud ? "cloud" : "local"];
    if (p.needs_key && !p.api_key_env_set) bits.push("no key set");
    opt.appendChild(document.createTextNode(bits.filter(Boolean).join(" · ")));
    llmProviderEl.appendChild(opt);
  }
  const defaultLlm = (health && health.default_llm) || {};
  if (llmModelEl && defaultLlm.model && !llmModelEl.value.trim()) {
    llmModelEl.value = defaultLlm.model;
  }
  if (defaultLlm.provider) {
    const has = Array.from(llmProviderEl.options).some((o) => o.value === defaultLlm.provider);
    if (has) llmProviderEl.value = defaultLlm.provider;
  }
  preferLocalProviderForHfModel();

  const banners = [];
  const ner = (health && health.engines && health.engines.ner) || {};
  if (ner.configured && !ner.loadable) {
    banners.push({
      kind: "warn",
      text: "NER model is configured but failed to load — names/addresses will not be detected. " +
        (ner.error ? String(ner.error).slice(0, 200) : "Check the webapp startup log."),
    });
  } else if (!ner.configured) {
    banners.push({
      kind: "info",
      text: "No NER model configured — only regex/phone patterns will run. Names and free-text addresses will not be detected.",
    });
  }
  const llm = (health && health.engines && health.engines.llm) || {};
  const label = [defaultLlm.provider || llm.role_provider, defaultLlm.model].filter(Boolean).join(" / ");
  if (defaultLlm.status === "missing_credentials") {
    banners.push({
      kind: "warn",
      text: "LLM refiner is bound" + (label ? " (" + label + ")" : "") +
        " but credentials are missing. " + (defaultLlm.reason || "Set the provider API key and restart."),
    });
  } else if (defaultLlm.status === "raw_text_external_blocked") {
    banners.push({
      kind: "warn",
      text: "LLM refiner is bound to a cloud provider" + (label ? " (" + label + ")" : "") +
        ". " + (defaultLlm.reason || "Cloud free-text inference is disabled."),
    });
  } else if (defaultLlm.status === "configuration_error") {
    banners.push({
      kind: "warn",
      text: "LLM refiner configuration error: " + (defaultLlm.reason || "check Settings → Text Gateway."),
    });
  } else if (!defaultLlm.role_bound && !llm.available && !llm.enabled) {
    banners.push({
      kind: "info",
      text: "LLM refiner not configured — bind pii.text_refiner under Settings → Text Gateway, then return here (health refreshes on focus).",
    });
  } else if (defaultLlm.ready || llm.available || llm.enabled) {
    banners.push({
      kind: "info",
      text: "LLM refiner ready" + (label ? " (" + label + ")" : "") +
        ". Check “Use LLM refiner” to run it; local OpenAI-compatible servers do not need a cloud API key.",
    });
  }
  paintBanners(healthBannerEl, banners);

  const guardBits = [];
  const tox = (health && health.guards && health.guards.toxicity) || {};
  const inj = (health && health.guards && health.guards.prompt_injection) || {};
  guardBits.push(
    "toxicity LLM: " + (tox.configured ? "configured (" + (tox.model || tox.provider) + ")" : "heuristic only")
  );
  guardBits.push(
    "prompt-injection LLM: " + (inj.configured ? "configured (" + (inj.model || inj.provider) + ")" : "heuristic only")
  );
  guardHintEl.textContent = guardBits.join(" · ");
}

function hideTip() {
  tip.hidden = true;
  tip.textContent = "";
}

function showTip(mark, ev) {
  let spans;
  try {
    spans = JSON.parse(mark.dataset.spans || "[]");
  } catch {
    spans = [];
  }
  tip.textContent = "";
  for (const s of spans) {
    const line = document.createElement("div");
    const bits = [
      s.entity_type,
      s.score != null ? Number(s.score).toFixed(2) : "",
      s.engine || "",
      s.is_proposal ? "needs review" : "",
      s.agreement ? "agree " + s.agreement : "",
      s.arbitration_rule || "",
      s.llm_verdict ? "llm " + s.llm_verdict : "",
    ].filter(Boolean);
    line.appendChild(document.createTextNode(bits.join(" · ")));
    tip.appendChild(line);
  }
  tip.hidden = false;
  const rect = mark.getBoundingClientRect();
  const x = (ev && ev.clientX) || rect.left;
  const y = (ev && ev.clientY) || rect.bottom;
  tip.style.left = x + 12 + window.scrollX + "px";
  tip.style.top = y + 12 + window.scrollY + "px";
}

function bindMarks(container) {
  container.querySelectorAll("mark.gw-hl").forEach((mark) => {
    mark.addEventListener("mouseenter", (ev) => showTip(mark, ev));
    mark.addEventListener("mouseleave", hideTip);
    mark.addEventListener("focus", (ev) => showTip(mark, ev));
    mark.addEventListener("blur", hideTip);
  });
}

function rawSpans() {
  if (!lastEnvelope) return [];
  const pii = lastEnvelope.analysers && lastEnvelope.analysers.pii;
  return (pii && pii.spans) || [];
}

function curatedSpans() {
  return applyCuration(rawSpans(), curation, sourceText);
}

function visibleSpans() {
  return applyCuration(rawSpans(), curation, sourceText, { keepRejected: !hideRejected });
}

function persistCuration() {
  if (!lastEnvelope || !lastEnvelope.run_uuid) return;
  curation.run_uuid = lastEnvelope.run_uuid;
  fetch("/api/gateway/curation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_uuid: lastEnvelope.run_uuid, curation: persistable(curation) }),
  }).catch(() => {});
}

function autoTrimEnabled() {
  return !!($("gwAutoTrim") && $("gwAutoTrim").checked);
}

async function trimSpanBounds(span, text) {
  const res = await fetch("/api/gateway/trim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: text || "", spans: [span] }),
  });
  if (!res.ok) return null;
  const body = await res.json();
  return (body.spans || [])[0] || null;
}

async function decide(span, decision, extra) {
  extra = extra || {};
  if (decision === "accept" && autoTrimEnabled() && !extra.trimmed) {
    const next = await trimSpanBounds(span, sourceText);
    if (next && (next.start !== span.start || next.end !== span.end)) {
      extra = Object.assign({}, extra, {
        new_start: next.start,
        new_end: next.end,
        trimmed: true,
        reason: extra.reason || (next.trim_rules || []).join(", "),
      });
    }
  }
  const key = spanKey(span, span.source || "engine");
  const entry = Object.assign({
    key,
    decision,
    by: (window.REDIBIS_USER && window.REDIBIS_USER.username) || "",
    at: new Date().toISOString(),
  }, extra);
  curation = upsertEntry(curation, entry);
  persistCuration();
  paintResultPane();
  if (lastEnvelope) renderSummary(lastEnvelope);
}

async function promoteToRule(span) {
  if (!rulesEditor) return;
  const term = span.text || sourceText.slice(span.start, span.end);
  if (rulesEditor.expand) rulesEditor.expand();
  const card = $("gwRulesCard");
  if (card && typeof card.scrollIntoView === "function") {
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  const result = await rulesEditor.addTermChecked(term, {
    preferred: "exclude_terms",
    entityType: span.entity_type || "",
    spans: curatedSpans(),
    onReject: () => decide(span, "reject", { reason: "rejected instead of a rule" }),
  });
  if (result && (result.action === "forbidden_span" || result.target === "forbidden_span")) {
    decide(span, "reject", { reason: "forbidden_span" });
  }
  if (result && result.action === "add_category") {
    paintBanners(bannerEl, [{
      kind: "ok",
      text: "Added PII category " + ((result.patch && result.patch.entity_type) || "") +
        ". Apply to this run is already staged — scan again to detect it.",
    }]);
  }
}

function curationButtons(span) {
  const wrap = document.createElement("span");
  wrap.className = "gw-curate";
  const keep = document.createElement("button");
  keep.type = "button";
  keep.className = "btn btn-ghost btn-sm";
  keep.appendChild(document.createTextNode("✓ keep"));
  keep.addEventListener("click", () => decide(span, "accept"));
  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "btn btn-ghost btn-sm";
  reject.appendChild(document.createTextNode("✗ reject"));
  reject.addEventListener("click", () => decide(span, "reject"));
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "btn btn-ghost btn-sm";
  edit.appendChild(document.createTextNode("✎ edit"));
  edit.addEventListener("click", () => {
    const sel = selectionOffsets(resultEl);
    if (sel && sel.end > sel.start) {
      decide(span, "modify", { new_start: sel.start, new_end: sel.end });
      return;
    }
    askInline($("gwInlineForm"), {
      label: "New entity type (or leave blank) — select text in the pane then click edit to set bounds",
      value: span.entity_type || "",
      onOk: (et) => decide(span, "modify", { new_entity_type: (et || "").toUpperCase() }),
    });
  });
  const trimBtn = document.createElement("button");
  trimBtn.type = "button";
  trimBtn.className = "btn btn-ghost btn-sm";
  trimBtn.appendChild(document.createTextNode("⟲ trim"));
  trimBtn.addEventListener("click", async () => {
    const next = await trimSpanBounds(span, sourceText);
    if (!next) return;
    const before = sourceText.slice(span.start, span.end);
    const after = sourceText.slice(next.start, next.end);
    paintBanners(bannerEl, [{
      kind: "info",
      text: "Trim “" + before + "” → “" + after + "”",
    }]);
    decide(span, "modify", {
      new_start: next.start, new_end: next.end, trimmed: true,
      reason: (next.trim_rules || []).join(", "),
    });
  });
  const rule = document.createElement("button");
  rule.type = "button";
  rule.className = "btn btn-ghost btn-sm";
  rule.appendChild(document.createTextNode("→ make a rule…"));
  rule.addEventListener("click", () => promoteToRule(span));
  wrap.appendChild(keep);
  wrap.appendChild(reject);
  wrap.appendChild(edit);
  wrap.appendChild(trimBtn);
  wrap.appendChild(rule);
  return wrap;
}

function renderSummary(env) {
  const pii = env.analysers.pii;
    const view = curatedSpans();
    const listed = visibleSpans();
    const counts = entityCounts(view);
    const rows = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    summaryEl.textContent = "";
    if (!rows.length) {
      summaryEl.appendChild(document.createTextNode("No entity counts."));
    } else {
      for (const [type, n] of rows) {
        const row = document.createElement("div");
        row.className = "gw-sum-row";
        const sw = document.createElement("span");
        sw.className = "gw-swatch gw-" + classOf(type);
        row.appendChild(sw);
        row.appendChild(document.createTextNode(type + " · " + n + " · " + classOf(type)));
        summaryEl.appendChild(row);
      }
      listed.slice(0, 24).forEach((span) => {
        const line = document.createElement("div");
        line.className = "gw-sum-finding" + (isRejected(span, curation) ? " gw-rejected" : "")
          + (isAccepted(span, curation) ? " gw-accepted-row" : "");
        const typeEl = document.createElement("div");
        typeEl.className = "gw-sum-finding-type";
        typeEl.appendChild(document.createTextNode((span.entity_type || "") + " ·"));
        line.appendChild(typeEl);
        const surface = document.createElement("div");
        surface.className = "gw-sum-finding-value";
        surface.dir = "auto";
        surface.style.unicodeBidi = "isolate";
        surface.appendChild(document.createTextNode(span.text || sourceText.slice(span.start, span.end) || ""));
        line.appendChild(surface);
        if (isRejected(span, curation)) {
          const status = document.createElement("div");
          status.className = "gw-sum-finding-status";
          status.appendChild(document.createTextNode("· rejected"));
          line.appendChild(status);
        } else if (isAccepted(span, curation)) {
          const status = document.createElement("div");
          status.className = "gw-sum-finding-status";
          status.appendChild(document.createTextNode("· accepted"));
          line.appendChild(status);
        }
        line.appendChild(curationButtons(span));
        summaryEl.appendChild(line);
      });
    }
    const spans = view;
  const proposals = spans.filter((s) => s.is_proposal).length;
  const foot = document.createElement("div");
  foot.className = "gw-hint";
  const bits = [
    spans.length + " finding" + (spans.length === 1 ? "" : "s"),
    env.text_meta.char_count != null
      ? Number(env.text_meta.char_count).toLocaleString() + " chars"
      : "",
  ];
  if (proposals) bits.splice(1, 0, proposals + " need review");
  foot.appendChild(document.createTextNode(bits.filter(Boolean).join(" · ")));
  summaryEl.appendChild(foot);

  metaEl.textContent = "";
  const unavailable = pii.engines_unavailable || {};
  const unavailableKeys = Object.keys(unavailable);
  const kv = [
    ["ruleset", pii.ruleset_id || ""],
    ["version", pii.ruleset_version || ""],
    ["engines", (pii.engines_ran || []).join(", ")],
    ["unavailable", unavailableKeys.join(", ")],
    ["language", env.text_meta.language || ""],
    ["provenance", env.provenance_uuid || ""],
    ["run", env.run_uuid || ""],
  ];
  if (env.provenance_degraded) {
    kv.push(["degraded", "true"]);
  }
  const full = env.provenance || {};
  if (full.stack_uuid) kv.push(["stack", full.stack_uuid]);
  if (full.ner_backend) kv.push(["ner", full.ner_backend]);
  const llm = env.llm || {};
  kv.push(["llm used", llm.used ? "yes" : "no"]);
  if (llm.skip_reason) kv.push(["llm skip", llm.skip_reason]);
  for (const [k, v] of kv) {
    const dt = document.createElement("dt");
    dt.appendChild(document.createTextNode(k));
    const dd = document.createElement("dd");
    dd.appendChild(document.createTextNode(v || "—"));
    metaEl.appendChild(dt);
    metaEl.appendChild(dd);
  }
  if (unavailable.llm) {
    const dt = document.createElement("dt");
    dt.appendChild(document.createTextNode("llm error"));
    const dd = document.createElement("dd");
    dd.appendChild(document.createTextNode(String(unavailable.llm).slice(0, 280)));
    metaEl.appendChild(dt);
    metaEl.appendChild(dd);
  }
  metaCard.hidden = false;
  renderLlmLog(env);
}

function renderSafety(env) {
  const analysers = env.analysers || {};
  safetyEl.textContent = "";
  const checked = [
    ["toxicity", analysers.toxicity],
    ["prompt_injection", analysers.prompt_injection],
  ].filter(([, g]) => g && g.status && g.status !== "not_configured");
  if (!checked.length) {
    safetyCard.hidden = true;
    return;
  }
  for (const [name, g] of checked) {
    const row = document.createElement("div");
    row.className = "gw-safety-row " + (g.flagged ? "flagged" : "ok");
    const head = document.createElement("div");
    head.className = "gw-safety-head";
    head.appendChild(document.createTextNode(name.replace("_", " ")));
    const badge = document.createElement("span");
    badge.appendChild(document.createTextNode(g.flagged ? "flagged" : "clear"));
    head.appendChild(badge);
    row.appendChild(head);
    if (g.reason) {
      const reason = document.createElement("div");
      reason.appendChild(document.createTextNode(g.reason));
      row.appendChild(reason);
    }
    const meta = document.createElement("div");
    meta.className = "gw-safety-meta";
    const bits = [
      g.score != null ? "score=" + Number(g.score).toFixed(2) : "",
      g.engine || "",
      g.model || "",
    ].filter(Boolean);
    meta.appendChild(document.createTextNode(bits.join(" · ")));
    row.appendChild(meta);
    safetyEl.appendChild(row);
  }
  safetyCard.hidden = false;
}

function appendKv(parent, key, value) {
  const dt = document.createElement("dt");
  dt.appendChild(document.createTextNode(key));
  const dd = document.createElement("dd");
  dd.appendChild(document.createTextNode(value || "—"));
  parent.appendChild(dt);
  parent.appendChild(dd);
}

function llmSummaryLine(llm) {
  if (!llm) return "No LLM activity.";
  const bits = [
    llm.used ? "used" : "skipped",
    (llm.call_count || 0) + " call" + ((llm.call_count || 0) === 1 ? "" : "s"),
    (llm.providers || []).join("/") || "",
    (llm.models || []).join("/") || "",
    llm.windows_total ? ("windows " + (llm.windows_scanned || 0) + "/" + llm.windows_total) : "",
    llm.skip_reason || "",
  ].filter(Boolean);
  return bits.join(" · ");
}

async function fetchLlmLog(runUuid) {
  if (!runUuid) return { available: false, reason: "no run id", calls: [] };
  const res = await fetch("/api/gateway/llm-log/" + encodeURIComponent(runUuid));
  if (!res.ok) return { available: false, reason: "fetch failed (" + res.status + ")", calls: [] };
  return res.json();
}

function paintLlmTranscripts(container, log) {
  container.textContent = "";
  if (!log || log.available === false) {
    container.appendChild(document.createTextNode((log && log.reason) || "log not retained for this run"));
    const note = document.createElement("p");
    note.className = "gw-hint";
    note.appendChild(document.createTextNode(
      (log && log.retention && log.retention.note) ||
      "The log is a process-local ring of the last 200 calls on this worker."
    ));
    container.appendChild(note);
    return;
  }
  if (log.transcripts_withheld) {
    const note = document.createElement("p");
    note.className = "gw-hint";
    note.appendChild(document.createTextNode("Transcripts withheld for this role."));
    container.appendChild(note);
  }
  (log.calls || []).forEach((call) => {
    const wrap = document.createElement("div");
    wrap.className = "gw-llm-call";
    const head = document.createElement("div");
    head.className = "gw-llm-call-head";
    const bits = [
      call.provider || "llm",
      call.model_id || "",
      call.status || "",
      call.latency_ms != null ? Math.round(call.latency_ms) + "ms" : "",
    ].filter(Boolean);
    head.appendChild(document.createTextNode(bits.join(" · ")));
    wrap.appendChild(head);
    [
      ["system", call.system_prompt],
      ["user", call.user_prompt],
      ["response", call.response],
    ].forEach(([label, text]) => {
      if (text == null || text === "") return;
      const lab = document.createElement("div");
      lab.className = "gw-llm-label";
      lab.appendChild(document.createTextNode(label));
      const pre = document.createElement("pre");
      pre.className = "gw-llm-pre";
      pre.appendChild(document.createTextNode(String(text)));
      wrap.appendChild(lab);
      wrap.appendChild(pre);
    });
    if (call.error) {
      const err = document.createElement("div");
      err.className = "gw-hint";
      err.appendChild(document.createTextNode(String(call.error)));
      wrap.appendChild(err);
    }
    container.appendChild(wrap);
  });
}

function renderLlmLog(env) {
  if (!llmCard) return;
  const llm = env && env.llm;
  lastLlmLog = null;
  llmLogOpen = false;
  if (llmEl) {
    llmEl.textContent = "";
    llmEl.hidden = true;
  }
  if (!llm) {
    llmCard.hidden = true;
    if (llmDownloadBarBtn) llmDownloadBarBtn.hidden = true;
    return;
  }
  if (llmSummaryEl) llmSummaryEl.textContent = llmSummaryLine(llm);
  if (llmShowBtn) llmShowBtn.textContent = "Show LLM log";
  llmCard.hidden = false;
  if (llmDownloadBarBtn) llmDownloadBarBtn.hidden = false;
}

async function toggleLlmLog() {
  if (!lastEnvelope) return;
  if (llmLogOpen) {
    llmLogOpen = false;
    if (llmEl) llmEl.hidden = true;
    if (llmShowBtn) llmShowBtn.textContent = "Show LLM log";
    return;
  }
  const log = lastLlmLog || await fetchLlmLog(lastEnvelope.run_uuid || (lastEnvelope.llm && lastEnvelope.llm.run_id));
  lastLlmLog = log;
  llmLogOpen = true;
  if (llmEl) {
    paintLlmTranscripts(llmEl, log);
    llmEl.hidden = false;
  }
  if (llmShowBtn) llmShowBtn.textContent = "Hide";
}

function selectFor(value) {
  const sel = document.createElement("select");
  for (const s of STRATEGIES) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.appendChild(document.createTextNode(s));
    if (s === value) opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}

function renderPolicy(policy) {
  policyEl.textContent = "";
  if (!policy) {
    policyCard.hidden = true;
    deidBtn.disabled = true;
    setMaskToggleEnabled(false);
    return;
  }
  const def = policy.default || {};
  const defRow = document.createElement("div");
  defRow.className = "gw-pol-row";
  const defLab = document.createElement("label");
  defLab.appendChild(document.createTextNode("default (*)"));
  const defSel = selectFor(def.strategy || "redact");
  defSel.dataset.kind = "default";
  defRow.appendChild(defLab);
  defRow.appendChild(defSel);
  policyEl.appendChild(defRow);
  (policy.overrides || []).forEach((ov, i) => {
    const row = document.createElement("div");
    row.className = "gw-pol-row";
    const lab = document.createElement("label");
    lab.appendChild(document.createTextNode(ov.entity_type || "*"));
    const sel = selectFor(ov.strategy || "redact");
    sel.dataset.kind = "override";
    sel.dataset.index = String(i);
    row.appendChild(lab);
    row.appendChild(sel);
    policyEl.appendChild(row);
  });
  policyCard.hidden = false;
  deidBtn.disabled = false;
  setMaskToggleEnabled(true);
}

function collectPolicy() {
  if (!reviewedPolicy) return null;
  const copy = JSON.parse(JSON.stringify(reviewedPolicy));
  policyEl.querySelectorAll("select").forEach((sel) => {
    if (sel.dataset.kind === "default") {
      copy.default = copy.default || {};
      copy.default.strategy = sel.value;
    } else if (sel.dataset.kind === "override") {
      const i = Number(sel.dataset.index);
      if (copy.overrides && copy.overrides[i]) copy.overrides[i].strategy = sel.value;
    }
  });
  return copy;
}

function setAllStrategies(strategy) {
  const value = strategy || "redact";
  if (!policyEl) return;
  policyEl.querySelectorAll("select").forEach((sel) => {
    sel.value = value;
  });
  if (reviewedPolicy) {
    reviewedPolicy.default = reviewedPolicy.default || {};
    reviewedPolicy.default.strategy = value;
    (reviewedPolicy.overrides || []).forEach((ov) => {
      ov.strategy = value;
    });
  }
  // Masked preview was built with the previous strategies — drop it.
  if (maskPreviewOn) {
    maskPreviewOn = false;
    maskedText = "";
    maskedSpans = [];
    if (maskToggleBtn) {
      maskToggleBtn.setAttribute("aria-pressed", "false");
      maskToggleBtn.textContent = "Show masked";
    }
    paintResultPane();
  }
  paintBanners(bannerEl, [{
    kind: "ok",
    text: "All de-identification strategies set to " + value + ".",
  }]);
}

function setMaskToggleEnabled(on) {
  if (!maskToggleBtn) return;
  maskToggleBtn.disabled = !on;
  if (!on) {
    maskPreviewOn = false;
    maskedText = "";
    maskedSpans = [];
    maskToggleBtn.setAttribute("aria-pressed", "false");
    maskToggleBtn.textContent = "Show masked";
  }
}

function paintResultPane() {
  if (!lastEnvelope) return;
  const pii = lastEnvelope.analysers.pii;
  const text = maskPreviewOn ? maskedText : sourceText;
  // Rejected spans drop out so the word paints as ordinary text.
  let spans = maskPreviewOn ? maskedSpans : curatedSpans();
  resultEl.dir = (lastEnvelope.text_meta.language || "").startsWith("ar") ? "rtl" : "ltr";
  renderHighlights(resultEl, text, spans);
  resultEl.querySelectorAll("mark.gw-hl").forEach((mark) => {
    try {
      const items = JSON.parse(mark.dataset.spans || "[]");
      if (items.some((s) => s.accepted || isAccepted(s, curation))) mark.classList.add("gw-accepted");
    } catch { /* ignore */ }
  });
  bindMarks(resultEl);
  renderVerdictPane();
}

function showResultMode(on) {
  input.hidden = on;
  resultEl.hidden = !on;
  editBtn.hidden = !on;
  if (maskToggleBtn) maskToggleBtn.hidden = !on;
  actionsEl.hidden = !on;
  scanBtn.hidden = on;
}

function resetMaskPreview() {
  maskPreviewOn = false;
  maskedText = "";
  maskedSpans = [];
  if (maskToggleBtn) {
    maskToggleBtn.setAttribute("aria-pressed", "false");
    maskToggleBtn.textContent = "Show masked";
    maskToggleBtn.disabled = true;
  }
}

function resetAll() {
  if ((curation.entries || []).length) {
    if (!window.confirm("Clear " + curation.entries.length + " curation decision(s) and the current result?")) return;
  }
  cancelScan();
  sourceText = "";
  lastEnvelope = null;
  reviewedPolicy = null;
  curation = emptyCuration();
  resetMaskPreview();
  input.value = "";
  resultEl.textContent = "";
  summaryEl.textContent = "";
  summaryEl.appendChild(document.createTextNode("Nothing scanned yet."));
  metaEl.textContent = "";
  policyEl.textContent = "";
  safetyEl.textContent = "";
  safetyCard.hidden = true;
  if (llmEl) llmEl.textContent = "";
  if (llmCard) llmCard.hidden = true;
  if (llmDownloadBarBtn) llmDownloadBarBtn.hidden = true;
  hideTip();
  paintBanners(bannerEl, []);
  metaCard.hidden = true;
  policyCard.hidden = true;
  deidBtn.disabled = true;
  showResultMode(false);
  hideProgress();
  updateCount();
  input.focus();
}

/** Stream `/api/gateway/scan/stream` (NDJSON): one line per stage, then a
 * final `{"event":"result","envelope":{...}}` or `{"event":"error",...}`.
 * Only completed backend stages move the progress bar. */
async function streamScan(payload, { signal } = {}) {
  const res = await fetch("/api/gateway/scan/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (res.status === 401) {
    window.location.href = "/login?next=/gateway";
    return null;
  }
  if (res.status === 429) throw new Error("Too many scans — wait a moment.");
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    const detail = d.detail;
    throw new Error(typeof detail === "string" ? detail : "Request failed (" + res.status + ")");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let resultEnvelope = null;
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
      try {
        evt = JSON.parse(line);
      } catch {
        continue;
      }
      if (evt.event === "stage") {
        const pct = STAGE_PERCENT[evt.stage] || 0;
        setProgress(pct, STAGE_LABEL[evt.stage] || evt.stage);
      } else if (evt.event === "result") {
        resultEnvelope = evt.envelope;
        setProgress(100, "Done");
      } else if (evt.event === "error") {
        serverError = evt.detail || "scan failed";
      }
    }
  }
  if (serverError) throw new Error(serverError);
  return resultEnvelope;
}

function cancelScan() {
  if (scanAbort) {
    scanAbort.abort();
    scanAbort = null;
  }
}

async function runScan() {
  sourceText = input.value || "";
  if (!sourceText.trim()) {
    paintBanners(bannerEl, [{ kind: "warn", text: "Paste some text first." }]);
    return;
  }
  scanBtn.disabled = true;
  resetMaskPreview();
  showProgress();
  await loadHealth();
  scanAbort = new AbortController();
  try {
    const env = await streamScan(scanPayload(), { signal: scanAbort.signal });
    if (!env) return;
    lastEnvelope = env;
    curation = emptyCuration(env.run_uuid);
    if (env.run_uuid) {
      try {
        const saved = await fetch("/api/gateway/curation/" + encodeURIComponent(env.run_uuid));
        if (saved.ok) curation = Object.assign(emptyCuration(env.run_uuid), await saved.json());
      } catch { /* none yet */ }
    }
    const pii = env.analysers.pii;
    const banners = classifyBanners({
      truncated: env.text_meta.truncated,
      maxChars: MAX,
      wantedEngines: enginesEl.value,
      enginesRan: pii.engines_ran || [],
      spanCount: (pii.spans || []).length,
      coverage: (env.text_meta && env.text_meta.coverage) || pii.coverage,
    });
    if (env.decision && env.decision.action === "block") {
      banners.push({
        kind: "block",
        text: "Safety check blocked this text: " + (env.decision.reasons || []).join("; "),
      });
    }
    const unavailable = pii.engines_unavailable || {};
    if (unavailable.llm) {
      banners.push({
        kind: "warn",
        text: "LLM refiner unavailable: " + String(unavailable.llm).slice(0, 220),
      });
    }
    paintBanners(bannerEl, banners);
    paintResultPane();
    renderSummary(env);
    renderSafety(env);
    renderRecommendations(env);
    showResultMode(true);
    resultEl.focus();
    reviewedPolicy = null;
    deidBtn.disabled = true;
    setMaskToggleEnabled(false);
    try {
      const pol = await api("/api/gateway/suggest-policy", scanPayload());
      if (pol) {
        reviewedPolicy = pol;
        renderPolicy(pol);
      }
    } catch (err) {
      paintBanners(bannerEl, banners.concat([
        { kind: "warn", text: "Policy suggestion failed: " + err.message },
      ]));
    }
  } catch (err) {
    if (err && err.name === "AbortError") {
      paintBanners(bannerEl, [{ kind: "info", text: "Scan cancelled." }]);
    } else {
      paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
    }
  } finally {
    scanBtn.disabled = false;
    scanAbort = null;
    hideProgress();
  }
}

async function toggleMaskPreview() {
  if (!lastEnvelope) return;
  if (maskPreviewOn) {
    maskPreviewOn = false;
    maskToggleBtn.setAttribute("aria-pressed", "false");
    maskToggleBtn.textContent = "Show masked";
    paintResultPane();
    return;
  }
  const policy = collectPolicy();
  if (!policy) {
    paintBanners(bannerEl, [{
      kind: "warn",
      text: "Review the de-identification policy first, then toggle masking.",
    }]);
    return;
  }
  maskToggleBtn.disabled = true;
  try {
    const body = scanPayload();
    body.policy = policy;
    const env = await api("/api/gateway/deidentify", body);
    if (!env) return;
    const deid = env.deidentified || {};
    maskedText = deid.text || "";
    const origSpans = (lastEnvelope.analysers.pii && lastEnvelope.analysers.pii.spans) || [];
    maskedSpans = remapSpansAfterDeid(deid.applied || [], origSpans);
    maskPreviewOn = true;
    maskToggleBtn.setAttribute("aria-pressed", "true");
    maskToggleBtn.textContent = "Show original";
    paintResultPane();
    paintBanners(bannerEl, [{
      kind: "ok",
      text: "Masked preview on — PII spans stay highlighted on the transformed text. Toggle off to restore the original.",
    }]);
  } catch (err) {
    paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
  } finally {
    maskToggleBtn.disabled = !reviewedPolicy;
  }
}

async function copyDeidentified() {
  const policy = collectPolicy();
  if (!policy) return;
  deidBtn.disabled = true;
  try {
    const spans = curatedSpans().slice().sort((a, b) => (b.start || 0) - (a.start || 0));
    let text = sourceText;
    spans.forEach((span) => {
      const start = span.start || 0;
      const end = span.end || 0;
      if (end <= start) return;
      text = text.slice(0, start) + "[" + (span.entity_type || "PII") + "]" + text.slice(end);
    });
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    paintBanners(bannerEl, [{ kind: "ok", text: "De-identified text copied (curated spans)." }]);
  } catch (err) {
    paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
  } finally {
    deidBtn.disabled = false;
  }
}

function downloadJson() {
  if (!lastEnvelope) return;
  const payload = JSON.parse(JSON.stringify(lastEnvelope));
  payload.curation = persistable(curation);
  payload.analysers = payload.analysers || {};
  payload.analysers.pii = payload.analysers.pii || {};
  payload.analysers.pii.spans = curatedSpans();
  payload.analysers.pii.entity_counts = entityCounts(payload.analysers.pii.spans);
  payload.analysers.pii.rejected_spans = applyCuration(rawSpans(), curation, sourceText, { keepRejected: true })
    .filter((s) => s.rejected);
  if (lastEnvelope.analysers && lastEnvelope.analysers.llm_verdict) {
    payload.llm_verdict = lastEnvelope.analysers.llm_verdict;
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const stem = String((lastEnvelope.run_uuid || lastEnvelope.llm && lastEnvelope.llm.run_id) || "result").slice(0, 12);
  a.download = "gateway-scan-" + stem + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
}

async function downloadLlmLog() {
  if (!lastEnvelope) return;
  const log = lastLlmLog || await fetchLlmLog(lastEnvelope.run_uuid || (lastEnvelope.llm && lastEnvelope.llm.run_id));
  lastLlmLog = log;
  const blob = new Blob([JSON.stringify(log, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const stem = String((log && log.run_uuid) || lastEnvelope.run_uuid || "run").slice(0, 12);
  a.download = "llm-log-" + stem + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function renderVerdictPane() {
  const pane = $("gwVerdict");
  const card = $("gwVerdictCard");
  const list = $("gwVerdictList");
  const meta = $("gwVerdictMeta");
  if (!pane || !card) return;
  const verdict = lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.llm_verdict;
  if (!verdict) {
    pane.hidden = true;
    card.hidden = true;
    return;
  }
  const spans = verdict.spans || [];
  const diff = (lastEnvelope.analysers && lastEnvelope.analysers.verdict_diff) || {};
  pane.hidden = false;
  card.hidden = false;
  pane.dir = (lastEnvelope.text_meta.language || "").startsWith("ar") ? "rtl" : "ltr";
  const showDiff = $("gwVerdictDiff") && $("gwVerdictDiff").checked;
  const paint = showDiff
    ? (verdict.spans || []).map((s) => Object.assign({}, s, { eval_kind: "llm_only" }))
    : spans;
  renderHighlights(pane, sourceText, paint);
  if (meta) {
    meta.textContent = [
      verdict.model || "llm",
      "coverage " + ((verdict.coverage_fraction || 0) * 100).toFixed(0) + "%",
      spans.length + " spans",
      (diff.agree || 0) + " agree",
      (diff.llm_only || 0) + " llm-only",
      verdict.error ? ("error: " + verdict.error) : "",
    ].filter(Boolean).join(" · ");
  }
  if (list) {
    list.textContent = "";
    spans.forEach((span) => {
      const row = document.createElement("div");
      row.className = "gw-sum-row"
        + (isRejected(Object.assign({}, span, { source: "llm_verdict" }), curation) ? " gw-rejected" : "")
        + (isAccepted(Object.assign({}, span, { source: "llm_verdict" }), curation) ? " gw-accepted-row" : "");
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
        decide(Object.assign({}, span, { source: "llm_verdict" }), "accept");
        paintBanners(bannerEl, [{ kind: "ok", text: "Accepted — the span is now in the result and in Download JSON." }]);
      });
      const ed = document.createElement("button");
      ed.type = "button";
      ed.className = "btn btn-ghost btn-sm";
      ed.appendChild(document.createTextNode("✎ accept with edit"));
      ed.addEventListener("click", () => {
        const sel = selectionOffsets(pane);
        const extra = { };
        if (sel && sel.end > sel.start) {
          extra.new_start = sel.start;
          extra.new_end = sel.end;
        }
        decide(Object.assign({}, span, { source: "llm_verdict" }), "accept", extra);
      });
      const ign = document.createElement("button");
      ign.type = "button";
      ign.className = "btn btn-ghost btn-sm";
      ign.appendChild(document.createTextNode("✗ ignore"));
      ign.addEventListener("click", () => {
        decide(Object.assign({}, span, { source: "llm_verdict" }), "reject");
      });
      row.appendChild(acc);
      row.appendChild(ed);
      row.appendChild(ign);
      list.appendChild(row);
    });
  }
}

function acceptVerdictFilter(pred) {
  const verdict = lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.llm_verdict;
  const diff = (lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.verdict_diff) || {};
  (verdict && verdict.spans || []).forEach((span) => {
    if (pred && !pred(span, diff)) return;
    decide(Object.assign({}, span, { source: "llm_verdict" }), "accept");
  });
}

function renderRecommendations(env) {
  const card = $("gwRecommendCard");
  const list = $("gwRecommendList");
  if (!card || !list) return;
  const recs = env && env.analysers && env.analysers.recommendations;
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

function syncLlmSelectors() {
  const on = !!(useLlmEl && useLlmEl.checked);
  llmProviderWrap.hidden = !on;
  llmModelWrap.hidden = !on;
  if (llmKeyWrap) llmKeyWrap.hidden = !on;
}

input.addEventListener("input", updateCount);
scanBtn.addEventListener("click", runScan);
$("gwCancelScan").addEventListener("click", cancelScan);
useLlmEl.addEventListener("change", syncLlmSelectors);
if (llmModelEl) llmModelEl.addEventListener("change", preferLocalProviderForHfModel);
if (llmModelEl) llmModelEl.addEventListener("blur", preferLocalProviderForHfModel);
editBtn.addEventListener("click", () => {
  if ((curation.entries || []).length) {
    if (!window.confirm("Editing the text invalidates " + curation.entries.length + " curation decision(s). Continue?")) return;
    curation = emptyCuration(lastEnvelope && lastEnvelope.run_uuid);
  }
  resetMaskPreview();
  input.value = sourceText;
  showResultMode(false);
  updateCount();
  input.focus();
});
if (maskToggleBtn) maskToggleBtn.addEventListener("click", toggleMaskPreview);
if (policyAllRedactBtn) {
  policyAllRedactBtn.addEventListener("click", () => setAllStrategies("redact"));
}
clearBtn.addEventListener("click", resetAll);
deidBtn.addEventListener("click", copyDeidentified);
downloadBtn.addEventListener("click", downloadJson);
if ($("gwHideRejected")) $("gwHideRejected").addEventListener("change", (ev) => {
  hideRejected = !!ev.target.checked;
  if (lastEnvelope) { paintResultPane(); renderSummary(lastEnvelope); }
});
if ($("gwVerdictAcceptAll")) $("gwVerdictAcceptAll").addEventListener("click", () => acceptVerdictFilter(null));
if ($("gwVerdictAcceptAgree")) $("gwVerdictAcceptAgree").addEventListener("click", () => {
  const diff = (lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.verdict_diff) || {};
  const agree = {};
  ((diff.rows && diff.rows.agree) || []).forEach((row) => {
    const llm = row.llm || [];
    agree[llm.join("\t")] = true;
  });
  acceptVerdictFilter((span) => agree[[span.start, span.end, span.entity_type].join("\t")]);
});
if ($("gwVerdictAcceptLlmOnly")) $("gwVerdictAcceptLlmOnly").addEventListener("click", () => {
  const diff = (lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.verdict_diff) || {};
  const only = {};
  ((diff.rows && diff.rows.llm_only) || []).forEach((row) => {
    const llm = row.llm || [];
    only[llm.join("\t")] = true;
  });
  acceptVerdictFilter((span) => only[[span.start, span.end, span.entity_type].join("\t")]);
});
if ($("gwVerdictDiff")) $("gwVerdictDiff").addEventListener("change", () => paintResultPane());
if ($("gwRecommendDownload")) $("gwRecommendDownload").addEventListener("click", () => {
  const recs = lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.recommendations;
  if (!recs) return;
  const blob = new Blob([JSON.stringify(recs, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "recommendations.json";
  a.click();
});
if ($("gwSaveUsecase")) $("gwSaveUsecase").addEventListener("click", async () => {
  if (!sourceText) return;
  askInline($("gwInlineForm"), {
    label: "Session name",
    value: "",
    onOk: async (sessionName) => {
      if (!sessionName) return;
      let slug = sessionName;
      const created = await fetch("/api/gateway/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: sessionName }),
      });
      if (created.status === 201) {
        const meta = await created.json();
        slug = meta.slug;
      } else if (created.status === 409) {
        slug = sessionName.replace(/\s+/g, "-");
      } else if (!created.ok) {
        paintBanners(bannerEl, [{ kind: "warn", text: "Could not create session (" + created.status + ")" }]);
        return;
      }
      const spans = curatedSpans().map((s) => ({
        start: s.start, end: s.end, entity_type: s.entity_type, value: s.text,
      }));
      const res = await fetch("/api/gateway/sessions/" + encodeURIComponent(slug) + "/usecases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          usecase: { name: sessionName, text: sourceText, language: langEl.value, expected_spans: spans },
          curation: persistable(curation),
          llm_verdict: lastEnvelope && lastEnvelope.analysers && lastEnvelope.analysers.llm_verdict,
        }),
      });
      paintBanners(bannerEl, [{
        kind: res.ok ? "ok" : "warn",
        text: res.ok ? "Saved to session " + slug : "Save failed (" + res.status + ")",
      }]);
    },
  });
});
if (llmDownloadBtn) llmDownloadBtn.addEventListener("click", downloadLlmLog);
if (llmShowBtn) llmShowBtn.addEventListener("click", toggleLlmLog);
if (llmDownloadBarBtn) llmDownloadBarBtn.addEventListener("click", downloadLlmLog);
window.addEventListener("beforeunload", () => {
  cancelScan();
  input.value = "";
  resultEl.textContent = "";
});

updateCount();
syncLlmSelectors();
resetMaskPreview();
loadHealth();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadHealth();
});
window.addEventListener("focus", loadHealth);
