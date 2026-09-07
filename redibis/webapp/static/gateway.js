import {
  classOf,
  classifyBanners,
  paintBanners,
  remapSpansAfterDeid,
  renderHighlights,
} from "./gateway_render.mjs";

const MAX = Number(window.GW_MAX_CHARS || 20000);
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
const checkToxicityEl = $("gwCheckToxicity");
const checkInjectionEl = $("gwCheckInjection");
const guardHintEl = $("gwGuardHint");
const healthBannerEl = $("gwHealthBanner");
const safetyCard = $("gwSafetyCard");
const safetyEl = $("gwSafety");
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

function scanPayload() {
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
    check_toxicity: !!(checkToxicityEl && checkToxicityEl.checked),
    check_prompt_injection: !!(checkInjectionEl && checkInjectionEl.checked),
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
  if (!llm.available && !llm.enabled) {
    banners.push({
      kind: "info",
      text: "LLM refiner not configured — bind pii.text_refiner under Settings → Text Gateway Models, or enable pii.llm.",
    });
  } else if (llm.available || llm.enabled) {
    const defaultLlm = (health && health.default_llm) || {};
    const label = [defaultLlm.provider || llm.role_provider, defaultLlm.model].filter(Boolean).join(" / ");
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

function renderSummary(env) {
  const pii = env.analysers.pii;
  const counts = pii.entity_counts || {};
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
  }
  const spans = pii.spans || [];
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
  ];
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
  const spans = maskPreviewOn ? maskedSpans : (pii.spans || []);
  resultEl.dir = (lastEnvelope.text_meta.language || "").startsWith("ar") ? "rtl" : "ltr";
  renderHighlights(resultEl, text, spans);
  bindMarks(resultEl);
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
  cancelScan();
  sourceText = "";
  lastEnvelope = null;
  reviewedPolicy = null;
  resetMaskPreview();
  input.value = "";
  resultEl.textContent = "";
  summaryEl.textContent = "";
  summaryEl.appendChild(document.createTextNode("Nothing scanned yet."));
  metaEl.textContent = "";
  policyEl.textContent = "";
  safetyEl.textContent = "";
  safetyCard.hidden = true;
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
  scanAbort = new AbortController();
  try {
    const env = await streamScan(scanPayload(), { signal: scanAbort.signal });
    if (!env) return;
    lastEnvelope = env;
    const pii = env.analysers.pii;
    const banners = classifyBanners({
      truncated: env.text_meta.truncated,
      maxChars: MAX,
      wantedEngines: enginesEl.value,
      enginesRan: pii.engines_ran || [],
      spanCount: (pii.spans || []).length,
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
    const body = scanPayload();
    body.policy = policy;
    const env = await api("/api/gateway/deidentify", body);
    if (!env) return;
    const text = (env.deidentified && env.deidentified.text) || "";
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
    paintBanners(bannerEl, [{ kind: "ok", text: "De-identified text copied." }]);
  } catch (err) {
    paintBanners(bannerEl, [{ kind: "warn", text: err.message }]);
  } finally {
    deidBtn.disabled = false;
  }
}

function downloadJson() {
  if (!lastEnvelope) return;
  const blob = new Blob([JSON.stringify(lastEnvelope, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "gateway-scan.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function syncLlmSelectors() {
  const on = !!(useLlmEl && useLlmEl.checked);
  llmProviderWrap.hidden = !on;
  llmModelWrap.hidden = !on;
}

input.addEventListener("input", updateCount);
scanBtn.addEventListener("click", runScan);
$("gwCancelScan").addEventListener("click", cancelScan);
useLlmEl.addEventListener("change", syncLlmSelectors);
editBtn.addEventListener("click", () => {
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
window.addEventListener("beforeunload", () => {
  cancelScan();
  input.value = "";
  resultEl.textContent = "";
});

updateCount();
syncLlmSelectors();
resetMaskPreview();
loadHealth();
