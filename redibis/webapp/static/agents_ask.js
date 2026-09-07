/* Phase 3 — Ask main page (prompt + sources + debug + artifacts) */
(function () {
  'use strict';

  const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
  const HITL = 'awaiting_hitl';
  const MAX_LOG_LINES = 300;
  const POLL_MS = 4000;

  const state = {
    prompt: '',
    files: [],
    sourceSessionId: '',
    runId: null,
    lastAskPayload: null,
    clarification: null,
    polling: null,
    eventSource: null,
    logLines: [],
    fullLogArtifactId: '',
    error: '',
    runtime: null,
  };

  function trimLogLines() {
    if (state.logLines.length > MAX_LOG_LINES) {
      state.logLines = state.logLines.slice(-MAX_LOG_LINES);
    }
  }

  function fullLogDownloadUrl(runId, artifactId) {
    if (!runId || !artifactId) return '';
    return `/api/agents/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`;
  }

  function openResultsForRun(runId) {
    if (typeof window.REDIBIS_AGENTS !== 'undefined' && typeof window.REDIBIS_AGENTS.bindRun === 'function') {
      window.REDIBIS_AGENTS.bindRun(runId, { view: 'results', silent: false });
      return;
    }
    if (typeof window.redibisAgentsBindRun === 'function') {
      window.redibisAgentsBindRun(runId, { view: 'results', silent: false });
    }
  }

  function renderLogMeta(run) {
    const meta = document.getElementById('askLogMeta');
    if (!meta) return;
    if (!state.runId && !(run && run.run_id)) {
      meta.innerHTML = '';
      return;
    }
    const rid = (run && run.run_id) || state.runId;
    const parts = [`Showing last ${MAX_LOG_LINES} lines.`];
    if (state.fullLogArtifactId) {
      parts.push(
        `<a href="${esc(fullLogDownloadUrl(rid, state.fullLogArtifactId))}" download>Download full log</a>`,
      );
    }
    if (TERMINAL.has((run && run.status) || '')) {
      parts.push(`<button type="button" class="ask-link-btn" id="askOpenResults">Open in Results</button>`);
    } else {
      parts.push('Full log available on the Results page when the run finishes.');
    }
    meta.innerHTML = parts.join(' ');
    const btn = document.getElementById('askOpenResults');
    if (btn) btn.onclick = () => openResultsForRun(rid);
  }

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  function ts() {
    return new Date().toISOString().slice(11, 19);
  }

  function appendLog(msg) {
    state.logLines.push(`[${ts()}] ${msg}`);
    trimLogLines();
    const log = document.getElementById('askLog');
    if (log) {
      log.textContent = state.logLines.join('\n');
      log.scrollTop = log.scrollHeight;
    }
    const debug = document.getElementById('askDebug');
    if (debug) debug.classList.remove('ask-hidden');
    renderLogMeta(null);
  }

  function parseApiError(text, status) {
    if (!text) return `HTTP ${status}`;
    try {
      const j = JSON.parse(text);
      if (j.detail) {
        return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
      }
    } catch (_) { /* plain text */ }
    return text;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      const t = await r.text();
      const msg = parseApiError(t, r.status);
      const err = new Error(msg);
      err.status = r.status;
      err.path = path;
      throw err;
    }
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) return r.json();
    return r.text();
  }

  function showError(err) {
    const msg = err && err.message ? err.message : String(err);
    state.error = msg;
    const hint = err && err.status === 404
      ? ' (endpoint missing — restart the webapp after pulling latest code)'
      : '';
    appendLog(`ERROR: ${msg}${hint}`);
    renderDebug(state.runId ? { run_id: state.runId, status: 'failed', steps: [] } : null);
  }

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function enrichmentStatus(run) {
    for (const step of run.steps || []) {
      const out = step.output || {};
      if (out.enrichment_status) return out.enrichment_status;
      if (out.degraded) return 'degraded';
      for (const sub of out.sub_steps || []) {
        if (sub.enrichment_status) return sub.enrichment_status;
      }
    }
    const intent = (run.batch_meta || {}).intent || {};
    return intent.enrichment_status || '';
  }

  function collectModelCalls(run) {
    const calls = [];
    const planner = ((run || {}).batch_meta || {}).planner || {};
    if (planner.method && planner.method !== 'heuristic') {
      calls.push({
        purpose: planner.purpose || 'intent planning',
        method: planner.method,
        provider: planner.provider,
        model: planner.model,
        rai: planner.rai,
      });
    }
    for (const step of (run && run.steps) || []) {
      const outputs = [step.output || {}, ...((step.output || {}).sub_steps || [])];
      for (const out of outputs) {
        if (!out.provider) continue;
        calls.push({
          purpose: out.model_purpose || step.node_kind || 'model call',
          method: 'llm',
          provider: out.provider,
          model: out.model || '',
          rai: out.rai || null,
        });
      }
    }
    return calls;
  }

  function runtimeCard(label, data) {
    const method = data.method || 'deterministic';
    const target = [data.provider, data.model].filter(Boolean).join(' · ');
    const rai = data.rai ? ` · RAI ${data.rai.blocked ? 'blocked' : 'checked'}` : '';
    const statusClass = method === 'disabled' || method.includes('fallback') ? 'warn' : 'clean';
    return `<div class="ask-runtime-card"><strong>${esc(label)}</strong>`
      + `<span class="ask-status-chip ${statusClass}">${esc(method)}</span>`
      + `<div>${esc(target || (method === 'heuristic' ? 'No model call' : 'Not configured'))}</div>`
      + `<small>${esc(data.when || data.source || '')}${esc(rai)}</small></div>`;
  }

  function renderAiRuntime(run) {
    const root = document.getElementById('askRuntimeBody');
    if (!root) return;
    const runtime = state.runtime || {};
    const calls = collectModelCalls(run);
    if (calls.length) {
      root.innerHTML = calls.map((call) => runtimeCard(call.purpose, call)).join('');
      return;
    }
    root.innerHTML = runtimeCard('Planner', runtime.planner || { method: 'heuristic' })
      + runtimeCard('Enrichment', runtime.enrichment || { method: 'disabled' })
      + runtimeCard('Code generation', runtime.codegen || { method: 'disabled' })
      + `<div class="ask-runtime-card"><strong>Deterministic capabilities</strong>`
      + `<div>${esc((runtime.deterministic || []).join(', ') || 'Loading…')}</div>`
      + `<small>No LLM call unless an optional model capability is shown above.</small></div>`;
  }

  function stepRows(run) {
    const rows = [];
    const seen = new Set();
    for (const step of run.steps || []) {
      let st = step.status || 'pending';
      const out = step.output || {};
      if (out.degraded) st = 'degraded';
      const key = `${step.node_kind}:${step.table}`;
      seen.add(key);
      rows.push({
        label: `${step.node_kind || 'step'} · ${step.table || '-'}`,
        status: st,
        attempts: step.attempts || 1,
      });
    }
    if (run.status === 'running' && run.ledger && Array.isArray(run.ledger.tasks)) {
      for (const task of run.ledger.tasks) {
        if (task.status !== 'running') continue;
        const key = `${task.node_kind}:${task.table}`;
        if (seen.has(key)) continue;
        rows.push({
          label: `${task.node_kind || 'step'} · ${task.table || '-'}`,
          status: 'running',
          attempts: 1,
        });
      }
    }
    if (!rows.length && run.status) {
      rows.push({ label: 'run', status: run.status, attempts: 1 });
    }
    return rows;
  }

  function renderChips() {
    const el = document.getElementById('askChips');
    if (!el) return;
    if (!state.files.length) {
      el.innerHTML = '';
      return;
    }
    el.innerHTML = state.files.map((f, i) =>
      `<span class="ask-chip">${esc(f.name)} · ${formatBytes(f.size)}`
      + `<button type="button" data-rm="${i}" title="Remove">✕</button></span>`
    ).join('');
    el.querySelectorAll('button[data-rm]').forEach((btn) => {
      btn.onclick = () => {
        state.files.splice(Number(btn.dataset.rm), 1);
        renderChips();
      };
    });
  }

  function renderDebug(run) {
    const debug = document.getElementById('askDebug');
    const clarify = document.getElementById('askClarify');
    const log = document.getElementById('askLog');
    const steps = document.getElementById('askSteps');
    if (!debug) return;

    const active = state.runId || (run && run.run_id) || state.logLines.length;
    debug.classList.toggle('ask-hidden', !active);

    if (clarify) {
      if (run && run.status === HITL) {
        const pending = (run.hitl_pending || {}).interrupts || [];
        const val = pending[0] && pending[0].value ? pending[0].value : {};
        const q = val.question || val.reason || 'Additional confirmation required before continuing.';
        clarify.classList.remove('ask-hidden');
        clarify.innerHTML = `<div class="ask-clarify"><strong>Clarification needed</strong><p>${esc(q)}</p>`
          + `<input class="ask-clarify-input" id="askClarifyInput" placeholder="Your answer…"/>`
          + `<div style="margin-top:8px"><button type="button" class="ask-send" id="askClarifySend">Continue</button></div></div>`;
        document.getElementById('askClarifySend').onclick = () => submitClarification(run);
      } else if (state.clarification) {
        clarify.classList.remove('ask-hidden');
        clarify.innerHTML = `<div class="ask-clarify"><strong>Clarification needed</strong><p>${esc(state.clarification.question)}</p>`
          + `<input class="ask-clarify-input" id="askClarifyInput" placeholder="Your answer…"/>`
          + `<div style="margin-top:8px"><button type="button" class="ask-send" id="askClarifySend">Continue</button></div></div>`;
        document.getElementById('askClarifySend').onclick = () => {
          const input = document.getElementById('askClarifyInput');
          const answer = (input && input.value.trim()) || '';
          if (!answer) return;
          sendAsk({ ...state.clarification.pendingPayload, clarification: answer });
        };
      } else {
        clarify.classList.add('ask-hidden');
        clarify.innerHTML = '';
      }
    }

    if (steps && run) {
      steps.innerHTML = stepRows(run).map((r) => {
        const cls = r.status === 'degraded' ? 'degraded'
          : (r.status === 'failed' ? 'failed'
            : (r.status === 'completed' || r.status === 'done' ? 'done' : ''));
        const retry = r.attempts > 1 ? ` · retry ${r.attempts - 1}` : '';
        return `<li class="ask-step ${cls}"><span>${esc(r.label)}</span><span>${esc(r.status)}${retry}</span></li>`;
      }).join('');
    }

    if (log) {
      log.textContent = state.logLines.join('\n') || '(waiting for logs…)';
      log.scrollTop = log.scrollHeight;
    }
    renderAiRuntime(run);
    renderLogMeta(run);
  }

  function artifactSortKey(item) {
    const name = String(item.name || '');
    const kind = String(item.kind || '');
    if (kind === 'contract' || name.includes('contract')) return `0:${name}`;
    if (kind === 'report' || name.includes('agent_report')) return `1:${name}`;
    if (kind === 'diff') return `2:${name}`;
    if (kind === 'log' || name.endsWith('.log')) return `9:${name}`;
    return `5:${name}`;
  }

  function renderArtifactRows(runId, artifacts) {
    const logNames = new Set(['run.log', 'run_debug.log']);
    const sorted = [...(artifacts || [])].sort((a, b) => artifactSortKey(a).localeCompare(artifactSortKey(b)));
    const logArt = sorted.find((a) => logNames.has(a.name));
    const rest = sorted.filter((a) => !logNames.has(a.name));
    const rows = rest.map((a) =>
      `<div class="ask-artifact-row ask-artifact-${esc(a.kind || 'file')}">`
      + `<span>${esc(a.name || a.id)}</span>`
      + `<a href="/api/agents/runs/${esc(runId)}/artifacts/${esc(a.id)}" download>${esc(a.kind || 'file')}</a></div>`
    ).join('');
    const logLink = logArt
      ? `<div class="ask-artifact-row ask-artifact-log"><span>${esc(logArt.name)}</span>`
        + `<a href="${esc(fullLogDownloadUrl(runId, logArt.id))}" download>log</a></div>`
      : '';
    return rows + logLink;
  }

  function rememberFullLogArtifact(artifacts) {
    const items = artifacts || [];
    const preferred = items.find((a) => a.name === 'run.log')
      || items.find((a) => a.name === 'run_debug.log');
    state.fullLogArtifactId = preferred ? preferred.id : '';
  }

  async function renderArtifacts(run) {
    const panel = document.getElementById('askArtifacts');
    if (!panel || !run || !TERMINAL.has(run.status) && run.status !== HITL) {
      if (panel) panel.classList.add('ask-hidden');
      return;
    }
    if (run.status === HITL) {
      panel.classList.add('ask-hidden');
      return;
    }
    panel.classList.remove('ask-hidden');
    const status = enrichmentStatus(run) || 'unknown';
    const chipCls = ['clean', 'warn', 'degraded'].includes(status) ? status : 'warn';
    let arts = { artifacts: [] };
    try {
      arts = await api(`/api/agents/runs/${run.run_id}/artifacts`);
      rememberFullLogArtifact(arts.artifacts);
      renderLogMeta(run);
    } catch (e) {
      appendLog(`Artifacts list failed: ${e.message}`);
    }

    const artifactBody = renderArtifactRows(run.run_id, arts.artifacts)
      || '<p style="color:var(--muted)">No artifacts yet.</p>';

    panel.innerHTML = `<h3>Downloads</h3>`
      + `<p style="margin-bottom:10px">Enrichment status: <span class="ask-status-chip ${chipCls}">${esc(status)}</span></p>`
      + `<div class="ask-artifacts">${artifactBody}</div>`
      + `<div style="margin-top:14px"><button type="button" class="ask-send" id="askRunAgain">Run again</button></div>`;
    document.getElementById('askRunAgain').onclick = () => runAgain();
  }

  function stopDashboardTracking() {
    if (typeof window.REDIBIS_AGENTS !== 'undefined'
      && typeof window.REDIBIS_AGENTS.stopRunTracking === 'function') {
      window.REDIBIS_AGENTS.stopRunTracking();
    }
  }

  function startAskPolling() {
    stopDashboardTracking();
    if (state.polling) clearInterval(state.polling);
    state.polling = setInterval(pollRun, POLL_MS);
    pollRun();
  }

  function connectStream(runId) {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    if (!runId) return;
    appendLog(`Connecting live log stream for run ${runId}…`);
    state.eventSource = new EventSource(`/api/agents/runs/${runId}/stream`);
    state.eventSource.onmessage = (ev) => {
      if (!ev.data) return;
      try {
        const d = JSON.parse(ev.data);
        if (d.type === 'done') {
          appendLog(`Run finished: ${d.status || 'done'}`);
          return;
        }
        if (d.type === 'error') {
          appendLog(`Stream error: ${d.message || 'unknown'}`);
          return;
        }
        if (d.message) appendLog(d.message);
      } catch (_) {
        appendLog(ev.data);
      }
    };
    state.eventSource.onerror = () => {
      appendLog('Log stream disconnected (run may have finished or server restarted).');
    };
  }

  async function pollRun() {
    if (!state.runId) return;
    try {
      const run = await api(`/api/agents/runs/${state.runId}`);
      renderDebug(run);
      await renderArtifacts(run);
      if (TERMINAL.has(run.status)) {
        appendLog(`Run finished: ${run.status}`);
        clearInterval(state.polling);
        state.polling = null;
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
      }
    } catch (e) {
      appendLog(`Status poll failed: ${e.message}`);
    }
  }

  async function ensureSourceSession() {
    if (!state.files.length) {
      state.sourceSessionId = '';
      return {};
    }
    appendLog(`Uploading ${state.files.length} source file(s)…`);
    if (!state.sourceSessionId) {
      const sess = await api('/api/agents/source/samples/session', { method: 'POST' });
      state.sourceSessionId = sess.session_id;
      appendLog(`Created sample workspace ${state.sourceSessionId}`);
    }
    const fd = new FormData();
    state.files.forEach((f) => fd.append('files', f, f.name));
    const upload = await api(
      `/api/agents/source/upload?session_id=${encodeURIComponent(state.sourceSessionId)}`,
      { method: 'POST', body: fd },
    );
    const saved = upload.saved || [];
    appendLog(`Uploaded: ${saved.length ? saved.join(', ') : '(no supported .csv/.parquet files)'}`);
    const listing = await api(`/api/agents/source/samples?session_id=${encodeURIComponent(state.sourceSessionId)}`);
    const tables = listing.tables || [];
    if (tables.length) {
      appendLog(`Inferred table(s): ${tables.join(', ')}`);
    }
    return { source_session_id: state.sourceSessionId, tables };
  }

  async function sendAsk(override) {
    if (window.REDIBIS_AGENTS_ENABLED === false) {
      appendLog('Agent execution is disabled. Enable agents in deployment configuration.');
      return;
    }
    const promptEl = document.getElementById('askPrompt');
    const prompt = (override && override.prompt) || (promptEl && promptEl.value.trim()) || '';
    if (!prompt) return;

    const sendBtn = document.getElementById('askSend');
    if (sendBtn) sendBtn.disabled = true;
    state.clarification = null;
    state.error = '';
    state.fullLogArtifactId = '';
    if (!override || !override._keepLogs) {
      state.logLines = [];
      state.runId = null;
    }

    appendLog(`Prompt: ${prompt.slice(0, 120)}${prompt.length > 120 ? '…' : ''}`);

    try {
      const src = await ensureSourceSession();
      const payload = {
        prompt,
        source_session_id: src.source_session_id || '',
        tables: src.tables || [],
        clarification: (override && override.clarification) || '',
        dry_run: false,
      };
      state.lastAskPayload = { ...payload, files: [...state.files] };

      appendLog('Starting orchestrator…');
      const res = await api('/api/agents/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (res.profile) {
        appendLog(`Intent: ${(res.profile.intents || []).join(', ') || 'not resolved'}`
          + `${res.profile.ambiguous ? ' · ambiguous' : ''}`);
      }
      if (res.status === 'clarification_required') {
        appendLog(`Clarification needed: ${res.question}`);
        state.clarification = {
          question: res.question,
          pendingPayload: { ...payload, clarification: '' },
        };
        renderDebug(null);
        return;
      }

      state.runId = res.run_id;
      appendLog(`Run started: ${state.runId}`);
      if (res.tables && res.tables.length) {
        appendLog(`Target table(s): ${res.tables.join(', ')}`);
      }
      if (res.planner) {
        appendLog(`Planner: ${res.planner.method}${res.planner.provider ? ` · ${res.planner.provider}/${res.planner.model || 'default'}` : ''}`);
        if (res.planner.fallback_reason) appendLog(`Planner fallback: ${res.planner.fallback_reason}`);
      }
      if (res.pipeline && res.pipeline.nodes) {
        appendLog(`Pipeline: ${res.pipeline.nodes.map((node) => node.kind || node.type || node.id).join(' → ')}`);
      }
      if (typeof window.redibisAgentsBindRun === 'function') {
        window.redibisAgentsBindRun(state.runId, {
          table: (res.tables && res.tables[0]) || '',
          silent: true,
          askOwned: true,
        });
      }
      connectStream(state.runId);
      renderDebug({ run_id: state.runId, status: 'running', steps: [] });
      startAskPolling();
    } catch (e) {
      showError(e);
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  async function submitClarification(run) {
    const input = document.getElementById('askClarifyInput');
    const answer = (input && input.value.trim()) || 'approved';
    try {
      appendLog(`Resuming with: ${answer}`);
      await api(`/api/agents/runs/${run.run_id}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          table: (run.tables && run.tables[0]) || '',
          approved: true,
          note: answer,
        }),
      });
      state.clarification = null;
      startAskPolling();
    } catch (e) {
      showError(e);
    }
  }

  function runAgain() {
    if (!state.lastAskPayload) return;
    const promptEl = document.getElementById('askPrompt');
    if (promptEl) promptEl.value = state.lastAskPayload.prompt || '';
    if (state.lastAskPayload.files) {
      state.files = [...state.lastAskPayload.files];
      renderChips();
    }
    sendAsk({ ...state.lastAskPayload, _keepLogs: true });
  }

  function bindUi() {
    const prompt = document.getElementById('askPrompt');
    const send = document.getElementById('askSend');
    const addBtn = document.getElementById('askAddFiles');
    const fileInput = document.getElementById('askFileInput');

    if (prompt) {
      prompt.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && !ev.shiftKey) {
          ev.preventDefault();
          sendAsk();
        }
      });
    }
    if (send) send.onclick = () => sendAsk();
    if (addBtn && fileInput) {
      addBtn.onclick = () => fileInput.click();
      fileInput.onchange = () => {
        Array.from(fileInput.files || []).forEach((f) => state.files.push(f));
        fileInput.value = '';
        renderChips();
      };
    }
  }

  function rehydrateFromUrl() {
    const view = (location.hash || '#ask').replace('#', '');
    if (view !== 'ask') return;
    const runId = new URLSearchParams(location.search).get('run');
    if (!runId || state.runId) return;
    state.runId = runId;
    stopDashboardTracking();
    connectStream(runId);
    startAskPolling();
  }

  function init() {
    bindUi();
    renderChips();
    const send = document.getElementById('askSend');
    if (send && window.REDIBIS_AGENTS_ENABLED === false) {
      send.disabled = true;
      send.title = 'Agent execution is disabled; enable it in deployment configuration.';
    }
    api('/api/agents/runtime').then((runtime) => {
      state.runtime = runtime;
      renderAiRuntime(null);
      if (send && runtime.agents_enabled === false) {
        send.disabled = true;
        send.title = 'Agent execution is disabled; enable it in deployment configuration.';
      }
    }).catch((error) => {
      const root = document.getElementById('askRuntimeBody');
      if (root) root.textContent = `Runtime details unavailable: ${error.message}`;
    });
    rehydrateFromUrl();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.REDIBIS_ASK = { sendAsk, runAgain, state, appendLog };
})();
