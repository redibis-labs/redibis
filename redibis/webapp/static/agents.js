/* redibis agentic UI — thin shell over REST APIs (see AGENTIC_UI_SPEC) */
(function () {
  'use strict';

  const AGENTS_ENABLED = window.REDIBIS_AGENTS_ENABLED === true;
  const STORAGE_KEY = 'redibis_agents_v1';

  const STATUS_CLASS = {
    done: 'success',
    completed: 'success',
    needs_review: 'warning',
    awaiting_hitl: 'warning',
    running: 'info',
    failed: 'danger',
    queued: 'neutral',
    partial: 'warning',
    cancelled: 'neutral',
  };

  const JUDGMENT_KINDS = new Set(['classify', 'mask', 'enrich', 'approval_gate', 'gate']);
  const TERMINAL_RUN_STATUS = new Set(['completed', 'failed', 'cancelled', 'awaiting_hitl']);
  const ARTIFACT_KIND_LABEL = {
    report: 'Reports',
    contract: 'Contract',
    diff: 'Diff',
    evidence: 'Evidence',
    masked_csv: 'Masked',
    log: 'Logs',
  };
  const ARTIFACT_KIND_ORDER = { report: 0, contract: 1, diff: 2, evidence: 3, masked_csv: 4, log: 5 };

  let state = {
    view: 'ask',
    runId: null,
    sessionId: null,
    selectedTable: null,
    planMode: 'describe',
    pipeline: null,
    preview: null,
    codegen: null,
    codegenStatus: null,
    codegenForm: { table: 'telecom.customers', intent: 'Generate Apache Ranger policy from maskingPolicy', target: 'ranger', residency: 'local' },
    polling: null,
    planProvider: '',
  };

  let graphNodes = [];
  let graphEdges = [];
  let registry = [];
  let selectedNodeId = null;
  let nodeSeq = 1;
  let setNodesRef = null;
  let previewAction = null;
  let runEventSource = null;
  let streamRunId = null;
  let liveLogLines = [];
  const LIVE_LOG_MAX_LINES = 300;

  const RF = window.ReactFlow;
  const { useState, useCallback, useEffect } = React;

  function runPrefsKey(runId) {
    return `redibis_agents_run_prefs_${runId}`;
  }

  function loadRunPrefs(runId) {
    if (!runId) return {};
    try {
      const raw = localStorage.getItem(runPrefsKey(runId));
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }

  function saveRunPrefs(runId, patch) {
    if (!runId) return;
    const merged = { ...loadRunPrefs(runId), ...patch };
    try {
      localStorage.setItem(runPrefsKey(runId), JSON.stringify(merged));
    } catch (_) { /* ignore */ }
  }

  function readUrlRunPointer() {
    const p = new URLSearchParams(location.search);
    return {
      runId: p.get('run') || null,
      table: p.get('table') || null,
    };
  }

  function syncUrlRunPointer() {
    const url = new URL(location.href);
    if (state.runId) {
      url.searchParams.set('run', state.runId);
      if (state.selectedTable) url.searchParams.set('table', state.selectedTable);
      else url.searchParams.delete('table');
    } else {
      url.searchParams.delete('run');
      url.searchParams.delete('table');
    }
    const next = url.pathname + url.search + location.hash;
    if (location.pathname + location.search + location.hash !== next) {
      history.replaceState(null, '', next);
    }
  }

  function loadPersisted() {
    try {
      const urlPtr = readUrlRunPointer();
      const raw = localStorage.getItem(STORAGE_KEY);
      const saved = raw ? JSON.parse(raw) : {};
      if (urlPtr.runId) state.runId = urlPtr.runId;
      else if (saved.runId) state.runId = saved.runId;
      if (urlPtr.table) state.selectedTable = urlPtr.table;
      else if (saved.selectedTable) state.selectedTable = saved.selectedTable;
      if (saved.view) state.view = saved.view;
      if (saved.sessionId) state.sessionId = saved.sessionId;
      if (saved.pipeline) {
        state.pipeline = saved.pipeline;
        loadGraphFromPipeline(saved.pipeline);
      }
    } catch (_) { /* ignore */ }
  }

  function persist() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      runId: state.runId,
      sessionId: state.sessionId,
      view: state.view,
      selectedTable: state.selectedTable,
      pipeline: buildPipelinePayload(),
    }));
    syncUrlRunPointer();
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || r.statusText);
    }
    return r.json();
  }

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  function runDone(status) {
    return TERMINAL_RUN_STATUS.has(status || '');
  }

  function formatBytes(value) {
    const size = Number(value || 0);
    if (!size) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let idx = 0;
    let n = size;
    while (n >= 1024 && idx < units.length - 1) {
      n /= 1024;
      idx += 1;
    }
    return `${n >= 10 || idx === 0 ? Math.round(n) : n.toFixed(1)} ${units[idx]}`;
  }

  function artifactDownloadUrl(runId, artifactId) {
    return `/api/agents/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`;
  }

  function closeRunStream() {
    if (runEventSource) {
      try { runEventSource.close(); } catch (_) { /* noop */ }
      runEventSource = null;
    }
    streamRunId = null;
  }

  function appendLiveLog(line) {
    if (line == null || line === '') return;
    liveLogLines.push(String(line));
    if (liveLogLines.length > LIVE_LOG_MAX_LINES) {
      liveLogLines = liveLogLines.slice(-LIVE_LOG_MAX_LINES);
    }
    const body = document.getElementById('ag-live-log-body');
    if (body) {
      body.textContent = liveLogLines.join('\n');
      body.scrollTop = body.scrollHeight;
    }
  }

  function connectRunStream(runId) {
    if (!runId || !window.EventSource) return;
    if (state.view === 'ask') return;
    if (streamRunId === runId && runEventSource) return;
    closeRunStream();
    streamRunId = runId;
    try {
      const es = new EventSource(`/api/agents/runs/${encodeURIComponent(runId)}/stream`);
      runEventSource = es;
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          if (d.type === 'done' || d.type === 'failed') {
            appendLiveLog(d.message || `run ${d.status || 'finished'}`);
            closeRunStream();
            return;
          }
          if (d.type === 'error') {
            appendLiveLog(d.message || 'stream error');
            return;
          }
          appendLiveLog(d.message != null ? d.message : e.data);
        } catch (_) {
          appendLiveLog(e.data);
        }
      };
      es.onerror = () => { /* SSE auto-reconnects while run is active */ };
    } catch (_) { /* ignore */ }
  }

  async function fetchRunArtifacts(runId) {
    if (!runId) return { artifacts: [], error: '' };
    try {
      const art = await api(`/api/agents/runs/${runId}/artifacts`);
      return { artifacts: art.artifacts || [], error: '' };
    } catch (e) {
      return { artifacts: [], error: String(e.message || e) };
    }
  }

  function renderDebugTraceButtons(runId, artifacts) {
    const trace = (artifacts || []).find((a) => a.name === 'run_trace.json');
    const fullLog = (artifacts || []).find((a) => a.name === 'run.log')
      || (artifacts || []).find((a) => a.name === 'run_debug.log');
    if (!trace && !fullLog) return '';
    const btns = [];
    if (fullLog) {
      btns.push(`<a class="ag-btn" href="${artifactDownloadUrl(runId, fullLog.id)}" download>Download full log</a>`);
    }
    if (trace) {
      btns.push(`<a class="ag-btn ghost" href="${artifactDownloadUrl(runId, trace.id)}" target="_blank" rel="noopener">Download trace</a>`);
    }
    return `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">${btns.join('')}</div>`;
  }

  function renderLiveLogPane(runId) {
    const prefs = loadRunPrefs(runId);
    const open = prefs.logPaneOpen !== false;
    const body = liveLogLines.length ? esc(liveLogLines.join('\n')) : 'Waiting for log output…';
    return `<div style="margin-top:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <div style="font-weight:600">Live log <span style="font-weight:400;color:var(--muted);font-size:12px">(last ${LIVE_LOG_MAX_LINES} lines)</span></div>
        <button type="button" class="ag-btn ghost" id="btnToggleLogPane">${open ? 'Hide' : 'Show'}</button>
      </div>
      ${open ? `<div style="font-size:11px;color:var(--muted);margin-top:6px">Download the complete log from the artifacts section below.</div>` : ''}
      ${open ? `<pre id="ag-live-log-body" class="cmp-debug-body" style="margin-top:8px;max-height:220px;overflow:auto">${body}</pre>` : ''}
    </div>`;
  }

  function renderArtifactsSection(runId, artifacts, error, highlightTable) {
    if (error) {
      return `<div class="ag-note" style="margin-top:12px;border-color:var(--danger);color:var(--danger)">Artifacts unavailable: ${esc(error)}</div>`;
    }
    if (!artifacts || !artifacts.length) {
      return `<div class="ag-note" style="margin-top:12px">No artifacts recorded yet.</div>`;
    }
    const byTable = {};
    artifacts.forEach((item) => {
      const tbl = item.table || '(run-level)';
      (byTable[tbl] = byTable[tbl] || []).push(item);
    });
    const tableKeys = Object.keys(byTable).sort((a, b) => {
      if (a === '(run-level)') return -1;
      if (b === '(run-level)') return 1;
      return a.localeCompare(b);
    });
    const body = tableKeys.map((tbl) => {
      const rows = byTable[tbl];
      const groups = {};
      rows.forEach((item) => {
        const kind = item.kind || 'report';
        (groups[kind] = groups[kind] || []).push(item);
      });
      const kindHtml = Object.keys(groups)
        .sort((a, b) => (ARTIFACT_KIND_ORDER[a] ?? 99) - (ARTIFACT_KIND_ORDER[b] ?? 99))
        .map((kind) => {
          const lines = groups[kind].map((item) => {
            const handoffTable = item.table || highlightTable || '';
            const open = item.viewable_in_redibis && handoffTable
              ? `<button class="ag-btn ghost" data-art-handoff="${esc(handoffTable)}">Open in redibis</button>`
              : '';
            return `<div style="display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--border)">
              <div>
                <div style="font-weight:500">${esc(item.name)}</div>
                <div style="font-size:12px;color:var(--muted)">${esc(item.type || '')}${item.size ? ` · ${esc(formatBytes(item.size))}` : ''}</div>
              </div>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                <a class="ag-btn ghost" href="${artifactDownloadUrl(runId, item.id)}" target="_blank" rel="noopener">Download</a>
                ${open}
              </div>
            </div>`;
          }).join('');
          return `<div style="margin-top:8px">
            <div style="font-size:12px;font-weight:600;color:var(--muted)">${esc(ARTIFACT_KIND_LABEL[kind] || kind)}</div>
            ${lines}
          </div>`;
        }).join('');
      const title = tbl === '(run-level)' ? 'Run-level' : tbl;
      const hl = highlightTable && tbl === highlightTable ? ' style="border-left:3px solid var(--accent)"' : '';
      return `<div style="margin-top:12px;padding-left:8px"${hl}>
        <div style="font-weight:600;font-size:13px">${esc(title)}</div>
        ${kindHtml}
      </div>`;
    }).join('');
    return `<div style="margin-top:14px">
      <div style="font-weight:600">Artifacts</div>
      <div class="ag-note" style="margin-top:8px">All tables and kinds — partial artifacts appear while the run is in progress.</div>
      ${renderDebugTraceButtons(runId, artifacts)}
      ${body}
    </div>`;
  }

  function chip(status) {
    const cls = STATUS_CLASS[status] || 'neutral';
    return `<span class="ag-chip ${cls}">${esc(status.replace(/_/g, ' '))}</span>`;
  }

  function legendHtml() {
    return `<div class="ag-legend">
      <span class="ag-legend-item"><span class="ag-dot success"></span> Success</span>
      <span class="ag-legend-item"><span class="ag-dot warning"></span> Needs review</span>
      <span class="ag-legend-item"><span class="ag-dot danger"></span> Failed</span>
      <span class="ag-legend-item"><span class="ag-dot info"></span> Active</span>
      <span class="ag-legend-item"><span class="ag-dot neutral"></span> Queued</span>
    </div>`;
  }

  function setView(view) {
    const prev = state.view;
    state.view = view;
    location.hash = view;
    document.querySelectorAll('.ag-nav-link').forEach(el => {
      el.classList.toggle('on', el.dataset.view === view);
    });
    document.querySelectorAll('.ag-view').forEach(el => {
      el.classList.toggle('on', el.id === `view-${view}`);
    });
    if (view === 'ask') {
      stopPolling();
      closeRunStream();
    } else if (prev === 'ask' && state.runId) {
      rehydrateActiveRun();
    }
    persist();
    if (view === 'build') {
      initBuildCanvas();
      renderBuildPalette();
    }
    renderView(view);
  }

  function buildPipelinePayload() {
    const intent = document.getElementById('intentInput');
    return {
      name: 'agent-pipeline',
      goal: (intent && intent.value.trim()) || 'Governance pipeline',
      nodes: graphNodes.map(n => ({
        id: n.id, kind: n.data.kind, label: n.data.label,
        params: n.data.params || {}, position: n.position,
      })),
      edges: graphEdges.map(e => ({ id: e.id, source: e.source, target: e.target })),
    };
  }

  function defaultGraph() {
    graphNodes = [
      { id: '1', type: 'default', position: { x: 60, y: 80 },
        data: { label: 'Source', kind: 'source', params: { engine: 'hive', table: '' }, status: 'pending' } },
      { id: '2', type: 'default', position: { x: 300, y: 80 },
        data: { label: 'Sample', kind: 'sample', params: { strategy: 'percent', amount: 10 }, status: 'pending' } },
    ];
    graphEdges = [{ id: 'e1-2', source: '1', target: '2' }];
    if (setNodesRef) setNodesRef(graphNodes.map(n => styleNode(n)));
  }

  function loadGraphFromPipeline(pipeline) {
    if (!pipeline || !pipeline.nodes) return;
    graphNodes = pipeline.nodes.map(n => ({
      id: n.id, type: 'default',
      position: n.position || { x: 60, y: 60 },
      data: { label: n.label || n.kind, kind: n.kind, params: n.params || {}, status: 'pending' },
    }));
    graphEdges = (pipeline.edges || []).map(e => ({
      id: e.id || `e-${e.source}-${e.target}`, source: e.source, target: e.target,
    }));
    if (setNodesRef) setNodesRef(graphNodes.map(n => styleNode(n)));
  }

  const STATUS_COLOR = {
    completed: '#dcfce7', failed: '#fee2e2', running: '#fef3c7',
    partial: '#e0e7ff', pending: '#fff', skipped: '#f1f5f9',
  };
  const STEP_STATUS = {
    completed: '#16a34a', failed: '#e03131', running: '#f59e0b', skipped: '#94a3b8', pending: '#cbd5e1',
  };

  function styleNode(n, status) {
    const st = status || n.data.status || 'pending';
    const sel = n.id === selectedNodeId ? '3px solid #2563eb' : `2px solid ${STEP_STATUS[st] || '#e2e8f0'}`;
    return {
      ...n,
      data: { ...n.data, status: st },
      style: {
        ...n.style,
        background: n.data.isTraceStep ? '#f8fafc' : (STATUS_COLOR[st] || '#fff'),
        border: sel, fontSize: n.data.isTraceStep ? 10 : 12,
        width: n.data.isTraceStep ? 200 : undefined, padding: n.data.isTraceStep ? 4 : 8,
      },
    };
  }

  async function renderView(view) {
    if (view === 'ask') return;
    if (view === 'results') await renderResults();
    if (view === 'dashboard') await renderDashboard();
    if (view === 'plan') await renderPlan();
    if (view === 'codegen') await renderCodegen();
    if (view === 'build') await renderBuildPalette();
  }

  async function runPickerHtml() {
    let runs = [];
    try {
      const dash = await api('/api/agents/dashboard?limit=20');
      runs = dash.runs || [];
    } catch (_) { /* empty */ }
    const opts = runs.map(r =>
      `<option value="${esc(r.run_id)}" ${r.run_id === state.runId ? 'selected' : ''}>${esc(r.run_id.slice(0, 10))}… · ${esc(r.status)}</option>`
    ).join('');
    return `<div class="ag-run-picker">
      <label style="font-size:12px;color:var(--muted)">Run</label>
      <select id="runSelect" class="ag-select" style="width:auto">${opts || '<option value="">No runs yet</option>'}</select>
      <button class="ag-btn ghost" id="btnRefreshRun">Refresh</button>
      ${state.runId ? '<button class="ag-btn ghost" id="btnDeleteRun">Delete this</button>' : ''}
      ${runs.length ? '<button class="ag-btn ghost" id="btnClearRuns">Clear jobs</button>' : ''}
    </div>`;
  }

  function bindRunPicker() {
    const sel = document.getElementById('runSelect');
    if (!sel) return;
    sel.onchange = () => {
      state.runId = sel.value || null;
      state.selectedTable = null;
      persist();
      renderView(state.view);
    };
    const btn = document.getElementById('btnRefreshRun');
    if (btn) btn.onclick = () => renderView(state.view);
    const del = document.getElementById('btnDeleteRun');
    if (del) del.onclick = async () => {
      if (!state.runId || !confirm('Delete this run from the board?')) return;
      try { await api(`/api/agents/runs/${state.runId}`, { method: 'DELETE' }); } catch (e) { alert(e.message); }
      state.runId = null; state.selectedTable = null; stopPolling(); persist();
      renderView(state.view);
    };
    const clr = document.getElementById('btnClearRuns');
    if (clr) clr.onclick = async () => {
      if (!confirm('Clear all finished jobs from the board? (running jobs are kept)')) return;
      try { await api('/api/agents/runs', { method: 'DELETE' }); } catch (e) { alert(e.message); }
      state.runId = null; state.selectedTable = null; stopPolling(); persist();
      renderView(state.view);
    };
  }

  async function renderResults() {
    const el = document.getElementById('view-results');
    if (!el) return;
    el.innerHTML = `<div class="ag-loading">Loading results…</div>`;
    if (!state.runId) {
      el.innerHTML = `${await runPickerHtml()}
        <div class="ag-empty"><div class="ag-empty-icon">◎</div>No run selected. Start from Plan or Dashboard.</div>`;
      bindRunPicker();
      return;
    }
    try {
      const [tablesRes, runRes] = await Promise.all([
        api(`/api/agents/runs/${state.runId}/tables`),
        api(`/api/agents/runs/${state.runId}`),
      ]);
      const tables = tablesRes.tables || [];
      let summary = null;
      let artifacts = [];
      let artifactsError = '';
      const artRes = await fetchRunArtifacts(state.runId);
      artifacts = artRes.artifacts;
      artifactsError = artRes.error;
      if (state.selectedTable) {
        summary = await api(`/api/agents/runs/${state.runId}/tables/${encodeURIComponent(state.selectedTable)}/summary`);
      }
      if (!runDone(runRes.status)) {
        connectRunStream(state.runId);
      } else {
        closeRunStream();
      }
      const nodesHtml = tables.map(t => {
        const sel = t.table === state.selectedTable ? ' selected' : '';
        const st = (t.status || 'queued').replace('needs_review', 'needs_review');
        return `<div class="ag-table-node status-${esc(t.status)}${sel}" data-table="${esc(t.table)}">
          ${chip(t.status)}<div style="margin-top:6px;font-weight:500">${esc(t.table)}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:4px">${t.steps_completed || 0}/${t.steps_total || 0} steps</div>
        </div>`;
      }).join('');

      let cardHtml = '<div class="ag-card"><small style="color:var(--muted)">Select a table node</small></div>';
      if (summary) {
        const steps = (summary.steps_done || []).map(s =>
          `<li class="${s.done ? 'done' : ''}"><span class="ag-check">${s.done ? '✓' : '○'}</span> ${esc(s.label)}</li>`
        ).join('');
        const m = summary.metrics || {};
        cardHtml = `<div class="ag-card">
          <div style="display:flex;justify-content:space-between;align-items:flex-start">
            <div><div style="font-weight:500;font-size:16px">${esc(summary.table)}</div>
            <div style="margin-top:4px">${chip(summary.status)}</div></div>
            <small style="color:var(--muted)">${esc(summary.run_id || '')}</small>
          </div>
          <ul class="ag-steps">${steps}</ul>
          <div class="ag-metrics">
            <div class="ag-metric"><div class="ag-metric-val">${m.columns || 0}</div><div class="ag-metric-lbl">Columns</div></div>
            <div class="ag-metric"><div class="ag-metric-val">${m.pii_columns || 0}</div><div class="ag-metric-lbl">PII columns</div></div>
            <div class="ag-metric"><div class="ag-metric-val">${m.tags_applied || 0}</div><div class="ag-metric-lbl">Tags applied</div></div>
            <div class="ag-metric"><div class="ag-metric-val" style="font-size:14px">${esc(m.contract_version)}</div><div class="ag-metric-lbl">Contract version</div></div>
          </div>
          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <button class="ag-btn primary" id="btnManage">Manage in redibis</button>
            <button class="ag-btn" id="btnViewReport" ${summary.run_id ? '' : 'disabled'}>View report</button>
            <a class="ag-btn" href="/review?table=${encodeURIComponent(summary.table)}${summary.run_id ? '&run_id=' + encodeURIComponent(summary.run_id) : ''}" target="_blank">Evidence review</a>
          </div>
          ${renderLiveLogPane(state.runId)}
          ${renderArtifactsSection(state.runId, artifacts, artifactsError, state.selectedTable)}
          <div class="ag-note">Opens the 360° view — approve, edit, audit. Nothing persists until you approve there.</div>
        </div>`;
      }

      const runLevelCard = !summary ? `
        <div class="ag-card">
          <div style="font-weight:500;font-size:16px">Run overview</div>
          <div style="margin-top:4px">${chip(runRes.status)}</div>
          ${renderLiveLogPane(state.runId)}
          ${renderArtifactsSection(state.runId, artifacts, artifactsError, state.selectedTable)}
        </div>` : '';

      el.innerHTML = `${await runPickerHtml()}
        <h1 class="ag-h1">Results · ${esc(runRes.name || state.runId.slice(0, 8))}</h1>
        ${hitlBannerHtml(runRes)}
        <p class="ag-sub">Table outcomes for this run. Deep work happens in the manual console after handoff.</p>
        ${legendHtml()}
        <div class="ag-grid-2">
          <div><div class="ag-table-grid">${nodesHtml || '<div class="ag-empty">No tables in this run</div>'}</div></div>
          <div id="summaryCard">${summary ? cardHtml : runLevelCard}</div>
        </div>`;
      bindRunPicker();
      bindHitlBanner(runRes);
      document.getElementById('btnToggleLogPane')?.addEventListener('click', () => {
        const prefs = loadRunPrefs(state.runId);
        saveRunPrefs(state.runId, { logPaneOpen: prefs.logPaneOpen === false });
        renderResults();
      });
      el.querySelectorAll('.ag-table-node').forEach(node => {
        node.onclick = () => {
          state.selectedTable = node.dataset.table;
          persist();
          renderResults();
        };
      });
      const manage = document.getElementById('btnManage');
      if (manage && state.selectedTable) {
        manage.onclick = () => handoff(state.selectedTable);
      }
      const report = document.getElementById('btnViewReport');
      if (report && summary && summary.run_id) {
        report.onclick = () => handoff(state.selectedTable);
      }
      el.querySelectorAll('[data-art-handoff]').forEach(btn => {
        btn.onclick = () => handoff(btn.dataset.artHandoff);
      });
      if (!runDone(runRes.status)) startPolling();
    } catch (e) {
      el.innerHTML = `<div class="ag-err">${esc(e.message)}</div>`;
    }
  }

  async function resumeHitl(table, approved) {
    if (!state.runId) return;
    const run = await api(`/api/agents/runs/${state.runId}`);
    const meta = run.batch_meta || {};
    const t = table || meta.paused_table || state.selectedTable || (run.tables || [])[0];
    if (!t) { alert('Select a table'); return; }
    const res = await api(`/api/agents/runs/${state.runId}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: run.session_id || meta.session_id || state.sessionId || '',
        table: t,
        approved: approved !== false,
        approved_by: 'agent-board',
      }),
    });
    if (res.session_id) state.sessionId = res.session_id;
    persist();
    await renderView(state.view);
  }

  function hitlBannerHtml(run) {
    if (!run || run.status !== 'awaiting_hitl') return '';
    const meta = run.batch_meta || {};
    const intr = (run.hitl_pending && run.hitl_pending.interrupts) || [];
    const first = intr[0] || {};
    const val = first.value || {};
    const table = val.table || meta.paused_table || state.selectedTable || (run.tables || [])[0] || '';
    return `<div class="ag-card" style="border-color:var(--warning);background:var(--warning-bg);margin-bottom:14px">
      <strong>Approval required</strong> — pipeline paused at ${esc(val.node_kind || 'gate')} for ${esc(table)}.
      <div style="margin-top:10px;display:flex;gap:8px">
        <button class="ag-btn primary" id="btnHitlApprove">Approve and continue</button>
        <button class="ag-btn" id="btnHitlReject">Reject</button>
      </div>
      <div class="ag-note" style="margin-top:8px;background:transparent;border:none;padding:0">Writes stay blocked until you approve. Deep edits still happen in the manual console after the run completes.</div>
    </div>`;
  }

  function bindHitlBanner(run) {
    document.getElementById('btnHitlApprove')?.addEventListener('click', () => {
      const table = state.selectedTable || (run.tables || [])[0];
      resumeHitl(table, true).catch(e => alert(e.message));
    });
    document.getElementById('btnHitlReject')?.addEventListener('click', () => {
      const table = state.selectedTable || (run.tables || [])[0];
      resumeHitl(table, false).catch(e => alert(e.message));
    });
  }

  async function handoff(table) {
    const sample = document.getElementById('samplePath');
    const body = { run_id: state.runId, table, sample_path: sample ? sample.value.trim() : '' };
    const h = await api('/api/agents/handoff', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    window.location.href = h.redirect_url;
  }

  async function renderDashboard() {
    const el = document.getElementById('view-dashboard');
    if (!el) return;
    if (!state.runId) {
      el.innerHTML = `${await runPickerHtml()}
        <div class="ag-empty"><div class="ag-empty-icon">◎</div>Select or start a run from Plan.</div>`;
      bindRunPicker();
      return;
    }
    try {
      const run = await api(`/api/agents/runs/${state.runId}`);
      const tables = await api(`/api/agents/runs/${state.runId}/tables`);
      const artRes = await fetchRunArtifacts(state.runId);
      const rows = tables.tables || [];
      const done = rows.filter(t => t.status === 'done').length;
      const total = rows.length || (run.tables || []).length;
      const pct = total ? Math.round((done / total) * 100) : 0;
      const listHtml = rows.map(t =>
        `<div class="ag-card" style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          ${chip(t.status)}
          <strong style="flex:1">${esc(t.table)}</strong>
          <span style="font-size:12px;color:var(--muted)">${t.steps_completed || 0} steps</span>
          <button class="ag-btn ghost" data-open="${esc(t.table)}">Open</button>
        </div>`
      ).join('');

      el.innerHTML = `${await runPickerHtml()}
        ${hitlBannerHtml(run)}
        <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
          <div>
            <h1 class="ag-h1">${esc(run.name || 'Batch run')}</h1>
            <p class="ag-sub">${done} of ${total} tables · ${esc(run.status)}</p>
          </div>
          <button class="ag-btn" id="btnCancelRun" ${run.status !== 'running' ? 'disabled' : ''}>Cancel job</button>
        </div>
        ${legendHtml()}
        <div class="ag-progress"><div class="ag-progress-bar" style="width:${pct}%"></div></div>
        ${renderLiveLogPane(state.runId)}
        ${renderArtifactsSection(state.runId, artRes.artifacts, artRes.error)}
        <div>${listHtml || '<div class="ag-empty">No table progress yet</div>'}</div>`;
      bindRunPicker();
      bindHitlBanner(run);
      document.getElementById('btnToggleLogPane')?.addEventListener('click', () => {
        const prefs = loadRunPrefs(state.runId);
        saveRunPrefs(state.runId, { logPaneOpen: prefs.logPaneOpen === false });
        renderDashboard();
      });
      el.querySelectorAll('[data-open]').forEach(btn => {
        btn.onclick = () => {
          state.selectedTable = btn.dataset.open;
          setView('results');
        };
      });
      const cancel = document.getElementById('btnCancelRun');
      if (cancel) cancel.onclick = async () => {
        await api(`/api/agents/runs/${state.runId}/cancel`, { method: 'POST' });
        renderDashboard();
      };
      if (run.status === 'running' || run.status === 'pending') {
        connectRunStream(state.runId);
        startPolling();
      } else {
        closeRunStream();
      }
    } catch (e) {
      el.innerHTML = `<div class="ag-err">${esc(e.message)}</div>`;
    }
  }

  async function renderReview() {
    const el = document.getElementById('view-review');
    if (!el) return;
    if (!state.runId) {
      el.innerHTML = `${await runPickerHtml()}<div class="ag-empty">No run selected</div>`;
      bindRunPicker();
      return;
    }
    try {
      const [q, run] = await Promise.all([
        api(`/api/agents/runs/${state.runId}/queue`),
        api(`/api/agents/runs/${state.runId}`),
      ]);
      const items = q.items || [];
      const cards = items.map(item => {
        const tags = (item.evidence && item.evidence.tags) || [];
        const tagList = Array.isArray(tags) ? tags : Object.values(item.evidence?.tags || {}).flat();
        const pills = tagList.slice(0, 8).map(t => `<span class="ag-pill">${esc(t)}</span>`).join('');
        const mem = item.memory_hint || {};
        const memHtml = mem.matches
          ? `<div class="ag-memory">Memory: ${mem.matches} similar past decision(s)${mem.prior_decisions ? ' · prior: ' + esc(mem.prior_decisions.join(', ')) : ''}</div>`
          : '';
        const col = item.column ? `${esc(item.table)}.${esc(item.column)}` : esc(item.table);
        return `<div class="ag-proposal">
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
            ${chip(item.kind)} ${chip(item.status || 'pending')}
          </div>
          <div class="ag-proposal-subject">${col}</div>
          <div class="ag-pills">${pills}</div>
          <div class="ag-evidence">${esc(item.summary)}</div>
          ${memHtml}
          <div style="font-size:11px;color:var(--muted);margin-top:8px">Routed to ${esc(item.approval_role || 'Steward')}</div>
          <div style="display:flex;gap:8px;margin-top:10px">
            <button class="ag-btn primary" data-approve="${esc(item.table)}">Approve in console</button>
            <button class="ag-btn" data-approve="${esc(item.table)}">Edit</button>
            <button class="ag-btn" data-approve="${esc(item.table)}">Reject</button>
          </div>
        </div>`;
      }).join('');

      el.innerHTML = `${await runPickerHtml()}
        ${hitlBannerHtml(run)}
        <h1 class="ag-h1">Review queue · ${items.length} items</h1>
        <p class="ag-sub">Proposals need human judgment. Nothing persists until you approve in the manual console.</p>
        ${legendHtml()}
        ${cards || '<div class="ag-empty">No items pending review — confident proposals may have auto-passed.</div>'}`;
      bindRunPicker();
      bindHitlBanner(run);
      el.querySelectorAll('[data-approve]').forEach(btn => {
        btn.onclick = () => handoff(btn.dataset.approve);
      });
    } catch (e) {
      el.innerHTML = `<div class="ag-err">${esc(e.message)}</div>`;
    }
  }

  async function renderPlan() {
    const el = document.getElementById('view-plan');
    if (!el) return;
    const preview = state.preview;
    const metrics = preview ? `
      <div class="ag-metrics">
        <div class="ag-metric"><div class="ag-metric-val">${preview.tables_total || 0}</div><div class="ag-metric-lbl">Tables</div></div>
        <div class="ag-metric"><div class="ag-metric-val">${preview.columns_estimated || 0}</div><div class="ag-metric-lbl">Columns</div></div>
        <div class="ag-metric"><div class="ag-metric-val">${preview.likely_pii_columns || 0}</div><div class="ag-metric-lbl">Likely PII</div></div>
        <div class="ag-metric"><div class="ag-metric-val" style="font-size:13px">${esc((preview.integrations || []).join(', ') || '—')}</div><div class="ag-metric-lbl">Integrations</div></div>
      </div>` : '';

    const strip = graphNodes.map((n, i) => {
      const j = JUDGMENT_KINDS.has(n.data.kind) ? ' judgment' : '';
      const arrow = i < graphNodes.length - 1 ? '<span class="ag-pipe-arrow">→</span>' : '';
      return `<span class="ag-pipe-chip${j}">${esc(n.data.label || n.data.kind)}</span>${arrow}`;
    }).join('');

    el.innerHTML = `
      <h1 class="ag-h1">Intent → plan</h1>
      <p class="ag-sub">Describe what you want or switch to Build with nodes. Preview before running.</p>
      <div class="ag-field">
        <label>Intent</label>
        <textarea id="intentInput" class="ag-textarea" placeholder="Onboard telco_cdw — classify, contract, recommend PII masking, publish to OM…"></textarea>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">
        <input id="planProvider" class="ag-input" style="max-width:160px" placeholder="LLM provider key" value="${esc(state.planProvider || '')}"/>
        <button class="ag-btn primary" id="btnPlan">Plan</button>
        <div class="ag-toggle">
          <button id="modeDescribe" class="${state.planMode === 'describe' ? 'on' : ''}">Describe</button>
          <button id="modeBuild" class="${state.planMode === 'build' ? 'on' : ''}">Build with nodes</button>
        </div>
      </div>
      <div class="ag-pipeline-strip">${strip || '<span class="ag-pipe-chip">Add nodes in Build view</span>'}</div>
      <p style="font-size:12px;color:var(--muted);margin-bottom:12px">Judgment nodes (classify, mask) use an LLM at a judgment point — every result is reviewed.</p>
      ${metrics}
      <div class="ag-field"><label>Table</label><input id="tableInput" class="ag-input" value="telecom.customers"/></div>
      <div class="ag-field"><label>Sample path (optional)</label><input id="samplePath" class="ag-input" placeholder="/path/to/sample.csv"/></div>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button class="ag-btn primary" id="btnPreview">Preview scope</button>
        <button class="ag-btn" id="btnTry3" ${AGENTS_ENABLED ? '' : 'disabled'}>Try on 3 tables</button>
        <button class="ag-btn" id="btnRunAll" ${AGENTS_ENABLED ? '' : 'disabled'}>Run all</button>
      </div>
      <div class="ag-note">Nothing persists until you approve in the review queue and manual console.</div>
      <div id="planErrors" class="ag-err"></div>`;

    document.getElementById('modeDescribe').onclick = () => { state.planMode = 'describe'; renderPlan(); };
    document.getElementById('modeBuild').onclick = () => setView('build');
    document.getElementById('btnPlan').onclick = () => planFromIntent().catch(e => alert(e.message));
    document.getElementById('btnPreview').onclick = () => showPreview(0).catch(e => alert(e.message));
    document.getElementById('btnTry3').onclick = () => runBatch(3).catch(e => alert(e.message));
    document.getElementById('btnRunAll').onclick = () => runBatch(0).catch(e => alert(e.message));

    const intentEl = document.getElementById('intentInput');
    const providerEl = document.getElementById('planProvider');
    const payload = buildPipelinePayload();
    if (intentEl && payload.goal && !intentEl.value) intentEl.value = payload.goal;
    if (providerEl) providerEl.oninput = () => { state.planProvider = providerEl.value.trim(); };
  }

  async function renderCodegen() {
    const el = document.getElementById('view-codegen');
    if (!el) return;
    el.innerHTML = '<div class="ag-loading">Loading codegen…</div>';
    try {
      if (!state.codegenStatus) {
        state.codegenStatus = await api('/api/agents/codegen/status');
      }
      const st = state.codegenStatus;
      const result = state.codegen || {};
      const form = state.codegenForm || {};
      const req = result.request || {};
      const judge = result.judge_verdict || {};
      const vuln = result.vuln_report || {};
      let statusLine = 'Local codegen engine (template / optional local LLM).';
      if (st.method === 'remote') {
        statusLine = 'Remote hosted codegen configured (URL + token).';
      } else if (st.method === 'misconfigured_remote') {
        statusLine = 'Remote mode selected but URL or REDIBIS_CODEGEN_TOKEN is missing.';
      } else if (st.service_url_configured && !st.token_configured) {
        statusLine = 'Service URL set without token — local engine remains the default.';
      }

      el.innerHTML = `
        <h1 class="ag-h1">Policy codegen</h1>
        <p class="ag-sub">Propose Apache Ranger (or other) artifacts from contract metadata. Nothing executes on-prem.</p>
        <div class="ag-note">${esc(statusLine)} External egress: ${st.allow_external_codegen ? 'allowed' : 'blocked by config'}.</div>
        <div class="ag-field"><label>Table</label><input id="codegenTable" class="ag-input" value="${esc(form.table || 'telecom.customers')}"/></div>
        <div class="ag-field"><label>Intent</label>
          <textarea id="codegenIntent" class="ag-textarea" placeholder="Generate Ranger row-filter from maskingPolicy columns…">${esc(form.intent || 'Generate Apache Ranger policy from maskingPolicy')}</textarea>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <select id="codegenTarget" class="ag-select">
            <option value="ranger" ${(form.target || st.default_target) === 'ranger' ? 'selected' : ''}>ranger</option>
          </select>
          <select id="codegenResidency" class="ag-select">
            <option value="local" ${form.residency === 'local' ? 'selected' : ''}>local</option>
            <option value="private" ${form.residency === 'private' ? 'selected' : ''}>private</option>
          </select>
          <button class="ag-btn" id="btnCodegenPreview">Preview egress request</button>
          <button class="ag-btn primary" id="btnCodegenSubmit" ${AGENTS_ENABLED ? '' : 'disabled'}>Submit to service</button>
        </div>
        <div id="codegenErrors" class="ag-err"></div>
        ${result.status ? `
          <div class="ag-card" style="margin-top:12px">
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
              <strong>Status:</strong> ${esc(result.status)} ${chip(judge.approved ? 'completed' : (vuln.passed === false ? 'failed' : 'needs_review'))}
            </div>
            ${result.message ? `<p class="ag-sub">${esc(result.message)}</p>` : ''}
            ${judge.rationale ? `<p style="font-size:12px"><strong>Judge:</strong> ${esc(judge.rationale)}</p>` : ''}
            ${vuln.scanner ? `<p style="font-size:12px"><strong>SAST:</strong> ${vuln.passed ? 'passed' : 'failed'} (${esc(vuln.scanner)})</p>` : ''}
            ${result.provenance && result.provenance.payload_sha256 ? `<p style="font-size:11px;color:var(--muted)">Provenance sha256: ${esc(result.provenance.payload_sha256.slice(0, 16))}…</p>` : ''}
            ${result.code ? `<pre class="ag-codegen-pre">${esc(result.code)}</pre>` : ''}
            ${req.request_id ? `<p style="font-size:11px;color:var(--muted)">Request ${esc(req.request_id)}</p>` : ''}
          </div>` : ''}`;

      document.getElementById('btnCodegenPreview').onclick = () => previewCodegen().catch(e => alert(e.message));
      document.getElementById('btnCodegenSubmit').onclick = () => submitCodegen().catch(e => alert(e.message));
    } catch (e) {
      el.innerHTML = `<div class="ag-err">${esc(e.message)}</div>`;
    }
  }

  async function previewCodegen() {
    const table = document.getElementById('codegenTable').value.trim();
    const intent = document.getElementById('codegenIntent').value.trim();
    const target_system = document.getElementById('codegenTarget').value;
    const residency = document.getElementById('codegenResidency').value;
    if (!table || !intent) { alert('Table and intent required'); return; }
    state.codegenForm = { table, intent, target: target_system, residency };
    const res = await api('/api/agents/codegen/request', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intent, table, target_system, residency }),
    });
    state.codegen = { status: 'preview', request: res.request, egress: res };
    document.getElementById('codegenErrors').textContent = '';
    renderCodegen();
  }

  async function submitCodegen() {
    if (!AGENTS_ENABLED) { alert('agents.enabled is false'); return; }
    const table = document.getElementById('codegenTable').value.trim();
    const intent = document.getElementById('codegenIntent').value.trim();
    const target_system = document.getElementById('codegenTarget').value;
    const residency = document.getElementById('codegenResidency').value;
    if (!table || !intent) { alert('Table and intent required'); return; }
    state.codegenForm = { table, intent, target: target_system, residency };
    const res = await api('/api/agents/codegen/submit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intent, table, target_system, residency }),
    });
    state.codegen = res;
    const errBox = document.getElementById('codegenErrors');
    if (res.status === 'commercial_required' && errBox) {
      errBox.textContent = res.message || 'Commercial codegen service required';
    } else if (errBox) {
      errBox.textContent = '';
    }
    renderCodegen();
  }

  async function planFromIntent() {
    const intent = document.getElementById('intentInput').value.trim();
    const provider = document.getElementById('planProvider').value.trim() || state.planProvider || '';
    if (!intent) { alert('Enter intent'); return; }
    const res = await api('/api/agents/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intent, provider, policy_pack: 'telecom' }),
    });
    if (!res.valid) {
      document.getElementById('planErrors').textContent = (res.errors || []).join('; ');
      return;
    }
    loadGraphFromPipeline(res.pipeline);
    state.preview = null;
    persist();
    renderPlan();
    await compilePlan();
  }

  async function showPreview(tryOnN) {
    const table = document.getElementById('tableInput').value.trim();
    state.preview = await api('/api/agents/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pipeline: buildPipelinePayload(),
        tables: table ? [table] : [],
        try_on_n: tryOnN || 0,
      }),
    });
    renderPlan();
    const modal = document.getElementById('previewModal');
    const content = document.getElementById('previewContent');
    let html = `<p><strong>${state.preview.tables_total}</strong> tables · <strong>${state.preview.columns_estimated}</strong> columns</p>`;
    html += '<table style="width:100%;font-size:12px;margin-top:8px"><tr><th>Table</th><th>Cols</th><th>PII hints</th></tr>';
    (state.preview.tables || []).forEach(t => {
      html += `<tr><td>${esc(t.table)}</td><td>${t.columns}</td><td>${esc((t.likely_pii_columns || []).join(', '))}</td></tr>`;
    });
    html += '</table>';
    content.innerHTML = html;
    modal.classList.add('open');
    previewAction = { tryOnN: tryOnN || 0 };
  }

  async function runBatch(tryOnN) {
    if (!AGENTS_ENABLED) { alert('agents.enabled is false'); return; }
    const table = document.getElementById('tableInput')?.value.trim() || '';
    const pipeline = buildPipelinePayload();
    if (!tryOnN && table) {
      const sample = document.getElementById('samplePath')?.value.trim() || '';
      const run = await api('/api/agents/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pipeline, table, sample_path: sample, dry_run: false }),
      });
      state.runId = run.run_id;
      if (run.session_id) state.sessionId = run.session_id;
      state.selectedTable = table;
      liveLogLines = [];
      persist();
      connectRunStream(state.runId);
      if (run.status === 'awaiting_hitl') setView('dashboard');
      else setView('dashboard');
      return;
    }
    const res = await api('/api/agents/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pipeline: buildPipelinePayload(),
        tables: table ? [table] : [],
        dry_run: false,
      }),
    });
    state.runId = res.run_id;
    liveLogLines = [];
    persist();
    connectRunStream(state.runId);
    setView('dashboard');
    startPolling();
  }

  async function renderBuildPalette() {
    const pal = document.getElementById('palette');
    if (!pal) return;
    if (!registry.length) {
      try {
        const data = await api('/api/agents/nodes');
        registry = data.nodes || [];
      } catch (_) { return; }
    }
    const byCat = {};
    registry.forEach(n => {
      const c = n.category || 'core';
      (byCat[c] = byCat[c] || []).push(n);
    });
    let html = '';
    Object.keys(byCat).sort().forEach(cat => {
      html += `<div class="ag-section-title">${esc(cat)}</div>`;
      byCat[cat].forEach(n => {
        html += `<div class="ag-palette-item" draggable="true" data-kind="${esc(n.kind)}">${esc(n.label)}<small>${esc(n.tool)}</small></div>`;
      });
    });
    pal.innerHTML = html;
    pal.querySelectorAll('.ag-palette-item').forEach(item => {
      item.addEventListener('dragstart', e => e.dataTransfer.setData('kind', item.dataset.kind));
    });
    renderConfigCard();
    const plan = document.getElementById('planText');
    if (plan && !plan.value) compilePlan().catch(() => {});
  }

  function renderConfigCard() {
    const box = document.getElementById('configCard');
    if (!box) return;
    if (!selectedNodeId) {
      box.innerHTML = '<small style="color:var(--muted)">Click a node to edit config</small>';
      return;
    }
    const node = graphNodes.find(n => n.id === selectedNodeId);
    if (!node) return;
    const spec = registry.find(r => r.kind === node.data.kind) || {};
    const params = node.data.params || {};
    let html = `<h3 style="font-weight:500;font-size:14px">${esc(node.data.label)} <small style="color:var(--muted)">(${esc(node.data.kind)})</small></h3>`;
    html += `<p style="font-size:11px;color:var(--muted)">${esc(spec.description || '')}</p>`;
    (spec.params || []).forEach(p => {
      const val = params[p.name] !== undefined ? params[p.name] : (p.default ?? '');
      html += `<div class="ag-field"><label>${esc(p.name)}</label>`;
      if (p.choices && p.choices.length) {
        html += `<select class="ag-select" data-param="${esc(p.name)}">${p.choices.map(c =>
          `<option value="${esc(c)}" ${String(val) === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
      } else if (p.type === 'boolean') {
        html += `<select class="ag-select" data-param="${esc(p.name)}"><option value="true" ${val ? 'selected' : ''}>true</option><option value="false" ${!val ? 'selected' : ''}>false</option></select>`;
      } else {
        html += `<input class="ag-input" data-param="${esc(p.name)}" value="${esc(val)}"/>`;
      }
      html += '</div>';
    });
    box.innerHTML = html;
    box.querySelectorAll('[data-param]').forEach(el => {
      const update = () => {
        const name = el.dataset.param;
        const specP = (spec.params || []).find(x => x.name === name);
        let v = el.value;
        if (specP && specP.type === 'boolean') v = v === 'true';
        node.data.params = { ...node.data.params, [name]: v };
        if (setNodesRef) setNodesRef(nds => nds.map(n => n.id === node.id ? { ...n, data: { ...node.data } } : n));
        compilePlan().catch(() => {});
        persist();
      };
      el.onchange = el.oninput = update;
    });
  }

  async function compilePlan() {
    const plan = await api('/api/agents/compile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pipeline: buildPipelinePayload() }),
    });
    const el = document.getElementById('planText');
    if (el) el.value = plan.text || '';
  }

  async function renderAudit() {
    const el = document.getElementById('view-audit');
    const canvas = document.getElementById('auditCanvas');
    if (!el || !canvas) return;
    if (!state.runId) {
      el.innerHTML = `${await runPickerHtml()}<div class="ag-empty">Select a run to view audit DAG</div>`;
      bindRunPicker();
      return;
    }
    try {
      const audit = await api(`/api/agents/runs/${state.runId}/audit`);
      el.innerHTML = `${await runPickerHtml()}
        <h1 class="ag-h1">Audit trace</h1>
        <p class="ag-sub">Steps, tool calls, and RAI decisions for this run.</p>
        ${legendHtml()}
        <div id="auditCanvas" style="height:480px;border:0.5px solid var(--border);border-radius:10px;background:var(--bg)"></div>`;
      bindRunPicker();
      const mount = document.getElementById('auditCanvas');
      const graph = audit.graph || {};
      if (graph.nodes && graph.nodes.length && RF) {
        const nodes = graph.nodes.map(n => ({
          id: n.id, type: 'default', position: n.position || { x: 0, y: 0 },
          data: { label: n.data?.label || n.id },
          style: { fontSize: 10, padding: 6, border: '0.5px solid var(--border)' },
        }));
        const edges = (graph.edges || []).map(e => ({ id: e.id, source: e.source, target: e.target }));
        const AuditFlow = () => React.createElement(RF.ReactFlow, {
          nodes, edges, fitView: true, nodesDraggable: false,
        }, React.createElement(RF.Background));
        ReactDOM.createRoot(mount).render(
          React.createElement(RF.ReactFlowProvider, null, React.createElement(AuditFlow))
        );
      } else {
        mount.innerHTML = '<div class="ag-empty">No audit graph available</div>';
      }
    } catch (e) {
      el.innerHTML = `<div class="ag-err">${esc(e.message)}</div>`;
    }
  }

  function stopPolling() {
    if (state.polling) { clearInterval(state.polling); state.polling = null; }
  }

  async function rehydrateActiveRun() {
    if (!state.runId) return;
    if (state.view === 'ask') {
      return;
    }
    try {
      const run = await api(`/api/agents/runs/${state.runId}`);
      if (!runDone(run.status)) {
        connectRunStream(state.runId);
        if (!state.polling) startPolling();
      } else {
        closeRunStream();
        const artRes = await fetchRunArtifacts(state.runId);
        if (Array.isArray(run.logs) && run.logs.length && !liveLogLines.length) {
          liveLogLines = run.logs.slice();
        }
      }
    } catch (_) {
      state.runId = null;
      persist();
    }
  }

  function startPolling() {
    if (state.view === 'ask') return;
    if (state.polling) clearInterval(state.polling);
    state.polling = setInterval(async () => {
      if (!state.runId) return;
      try {
        const run = await api(`/api/agents/runs/${state.runId}`);
        if (run.logs && run.logs.length) {
          const tail = run.logs.slice(liveLogLines.length);
          tail.forEach(appendLiveLog);
        }
        if (run.status !== 'running' && run.status !== 'pending') {
          clearInterval(state.polling);
          state.polling = null;
          closeRunStream();
          if (state.view === 'dashboard') renderDashboard();
          else if (state.view === 'results') renderResults();
        } else if (state.view === 'dashboard' || state.view === 'results') {
          if (state.view === 'dashboard') renderDashboard();
          else renderResults();
        }
      } catch (_) { /* retry */ }
    }, 2000);
  }

  function FlowBoard() {
    const [nodes, setNodes] = useState(graphNodes.map(n => styleNode(n)));
    const [edges, setEdges] = useState(graphEdges);
    setNodesRef = setNodes;
    const onNodesChange = useCallback(changes => {
      setNodes(nds => { const u = RF.applyNodeChanges(changes, nds); graphNodes = u; persist(); return u; });
    }, []);
    const onEdgesChange = useCallback(changes => {
      setEdges(eds => { const u = RF.applyEdgeChanges(changes, eds); graphEdges = u; persist(); return u; });
    }, []);
    const onConnect = useCallback(params => {
      setEdges(eds => { const u = RF.addEdge(params, eds); graphEdges = u; return u; });
    }, []);
    const onNodeClick = useCallback((_, node) => {
      selectedNodeId = node.id;
      setNodes(nds => nds.map(n => styleNode(n)));
      renderConfigCard();
    }, []);
    const onDrop = useCallback(e => {
      e.preventDefault();
      const kind = e.dataTransfer.getData('kind');
      const spec = registry.find(n => n.kind === kind);
      if (!spec) return;
      const id = String(++nodeSeq + 10);
      const node = {
        id, type: 'default',
        position: { x: e.clientX - 260, y: e.clientY - 100 },
        data: { label: spec.label, kind: spec.kind, params: { ...(spec.default_params || {}) }, status: 'pending' },
      };
      setNodes(nds => { const u = [...nds, styleNode(node)]; graphNodes = u; persist(); return u; });
      compilePlan().catch(() => {});
    }, []);
    const onDragOver = useCallback(e => e.preventDefault(), []);
    return React.createElement('div', { style: { height: '100%' }, onDrop, onDragOver },
      React.createElement(RF.ReactFlow, {
        nodes, edges, onNodesChange, onEdgesChange, onConnect, onNodeClick, fitView: true,
      }, React.createElement(RF.Controls), React.createElement(RF.Background))
    );
  }

  function initBuildCanvas() {
    const root = document.getElementById('canvas');
    if (!root || root.dataset.mounted) return;
    root.dataset.mounted = '1';
    if (!graphNodes.length) defaultGraph();
    ReactDOM.createRoot(root).render(
      React.createElement(RF.ReactFlowProvider, null, React.createElement(FlowBoard))
    );
    document.getElementById('btnCompile')?.addEventListener('click', () => compilePlan().catch(e => alert(e.message)));
    document.getElementById('btnValidate')?.addEventListener('click', () => validateGraph().catch(e => alert(e.message)));
  }

  async function validateGraph() {
    const res = await api('/api/agents/validate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pipeline: buildPipelinePayload() }),
    });
    const box = document.getElementById('validateBox');
    if (box) box.textContent = res.valid ? '' : (res.errors || []).map(e => `${e.node_id}: ${e.message}`).join(' | ');
    return res.valid;
  }

  function init() {
    loadPersisted();
    const hash = (location.hash || '#ask').replace('#', '');
    if (['results', 'dashboard', 'composer', 'build', 'ask'].includes(hash)) {
      state.view = hash;
    }
    document.querySelectorAll('.ag-nav-link').forEach(link => {
      link.onclick = () => setView(link.dataset.view);
    });
    setView(state.view);
    initBuildCanvas();
    renderBuildPalette();
    rehydrateActiveRun();

    document.getElementById('btnPreviewClose')?.addEventListener('click', () => {
      document.getElementById('previewModal').classList.remove('open');
    });
    document.getElementById('btnPreviewRun')?.addEventListener('click', () => {
      document.getElementById('previewModal').classList.remove('open');
      runBatch(previewAction?.tryOnN || 0).catch(e => alert(e.message));
    });
    api('/api/agents/defaults').then((d) => {
      const planner = (d.defaults || {}).planner || {};
      if (planner.provider) {
        state.planProvider = planner.provider;
        if (state.view === 'plan') renderPlan();
      }
    }).catch(() => {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.REDIBIS_AGENTS = window.REDIBIS_AGENTS || {};
  window.REDIBIS_AGENTS.setView = setView;
  window.REDIBIS_AGENTS.stopRunTracking = function () {
    stopPolling();
    closeRunStream();
  };
  window.REDIBIS_AGENTS.setRunId = function (runId) {
    state.runId = runId;
    liveLogLines = [];
    persist();
    connectRunStream(runId);
    setView('dashboard');
  };
  // Set the active run WITHOUT navigating — used by the composer so Send stays on the board.
  window.REDIBIS_AGENTS.setRunIdQuiet = function (runId) {
    state.runId = runId;
    liveLogLines = [];
    persist();
    if (runId) connectRunStream(runId);
  };
  window.REDIBIS_AGENTS.loadPipeline = function (pipeline, opts) {
    if (!pipeline || !pipeline.nodes) return;
    loadGraphFromPipeline(pipeline);
    state.pipeline = pipeline;
    state.preview = null;
    persist();
    if (!(opts && opts.silent)) {
      setView('build');
    }
  };

  /** Ask / Composer — attach an in-flight run to Dashboard polling + URL. */
  window.redibisAgentsBindRun = function (runId, opts) {
    opts = opts || {};
    if (!runId) return;
    state.runId = runId;
    if (opts.table) state.selectedTable = opts.table;
    persist();
    syncUrlRunPointer();
    if (opts.askOwned) {
      return;
    }
    liveLogLines = [];
    connectRunStream(runId);
    startPolling();
    if (opts.view) setView(opts.view);
    else if (!opts.silent && state.view === 'ask') {
      /* stay on Ask; Dashboard can be opened manually */
    }
  };
})();
