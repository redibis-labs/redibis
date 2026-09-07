/* redibis agent composer — Claude-style intent composer over /api/agents/* REST.
 * Vanilla JS (same idiom as agents.js). Mounts into #view-composer.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'redibis_composer_v2';

  const ACTIONS = [
    { kind: 'source',   icon: 'database',     label: 'Source',   config: true },
    { kind: 'profile',  icon: 'chart-bar',    label: 'Profile' },
    { kind: 'quality',  icon: 'checklist',    label: 'Quality',  config: true },
    { kind: 'pii',      icon: 'shield-lock',  label: 'PII',      config: true },
    { kind: 'classify', icon: 'tags',         label: 'Classify', config: true },
    { kind: 'mask',     icon: 'eye-off',      label: 'Mask' },
    { kind: 'contract', icon: 'file-text',    label: 'Contract', config: true },
    { kind: 'publish',  icon: 'cloud-upload', label: 'Publish',  config: true },
  ];

  const LEGACY_KIND = {
    source_table: 'source', profile_scan: 'profile', quality_scan: 'quality',
    pii_scan: 'pii', catalog_push: 'publish', contract_write: 'publish',
  };

  const state = {
    providers: [], providerMeta: [], provider: '', model: '', providerReady: false,
    packs: { policy: ['telecom', 'general'], classification: ['telecom', 'general'], all: [] },
    policyPack: 'telecom', classificationPack: 'telecom',
    defaults: {},
    recipes: [],
    extraNotes: '',
    active: new Set(['source']),
    configTab: 'setup',
    showConfig: false,
    source: {
      engine: 'local',
      scope: 'selected',
      sampleRows: 5000,
      sampleDir: '',
      sessionId: '',
      local: { tables: [], files: [], selected: new Set() },
      hive: { metastore_uri: '', use_spark: true, database: 'default' },
      oracle: { host: '', port: '1521', service: '', owner: '', credential_ref: '' },
      remote: { connected: false, schema: '', schemas: [], tables: [], selected: new Set(), error: '' },
    },
    cfg: {
      pii: { engines: 'both', equation_mode: 'balanced', sample_rows: 5000 },
      quality: { sample_rows: 10000, strategy: 'fixed_rows', fixed_row_count: 10000, sample_fraction: 0.1 },
      classify: { jurisdiction: '' },
      contract: { pii: true, quality: true, mask: false, definitions: false, enrich: false },
      publish: { target: 'openmetadata' },
    },
    pipeline: null,
    planValid: true,
    planErrors: [],
    prompt: '',
    run: null,        // { id, status, tables:[], steps:[], error, sessionId } — live batch run
    polling: false,
    debug: false,     // Debug log drawer open
    debugLogs: [],    // captured SSE log lines
    codegen: null,
    packEditor: null,
    modelEditor: null,
    syncing: false,
    sendNotice: '',
  };

  let rootEl = null;
  let uploadInputEl = null;
  let uploadFolderInputEl = null;
  let monaco = null, monacoEditor = null;
  let rootClickBound = false;

  function ensureUploadInput() {
    if (!uploadInputEl) {
      uploadInputEl = document.createElement('input');
      uploadInputEl.type = 'file';
      uploadInputEl.multiple = true;
      uploadInputEl.accept = '.csv,.parquet';
      uploadInputEl.style.display = 'none';
      uploadInputEl.addEventListener('change', () => {
        uploadSamples(uploadInputEl);
        uploadInputEl.value = '';
      });
      document.body.appendChild(uploadInputEl);
    }
    return uploadInputEl;
  }

  function ensureFolderUploadInput() {
    if (!uploadFolderInputEl) {
      uploadFolderInputEl = document.createElement('input');
      uploadFolderInputEl.type = 'file';
      uploadFolderInputEl.multiple = true;
      uploadFolderInputEl.accept = '.csv,.parquet';
      uploadFolderInputEl.setAttribute('webkitdirectory', '');
      uploadFolderInputEl.setAttribute('directory', '');
      uploadFolderInputEl.style.display = 'none';
      uploadFolderInputEl.addEventListener('change', () => {
        uploadSamples(uploadFolderInputEl);
        uploadFolderInputEl.value = '';
      });
      document.body.appendChild(uploadFolderInputEl);
    }
    return uploadFolderInputEl;
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    return r.json();
  }
  function jpost(path, body) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  }
  const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const ti = (n) => `<i class="ti ti-${n}" aria-hidden="true"></i>`;
  const uid = () => Math.random().toString(36).slice(2, 10);
  const ARTIFACT_KIND_LABEL = {
    report: 'Reports',
    contract: 'Contract',
    diff: 'Diff',
    evidence: 'Evidence',
    masked_csv: 'Masked',
    log: 'Logs',
  };
  const LIVE_LOG_MAX_LINES = 300;
  const ARTIFACT_KIND_ORDER = { report: 0, contract: 1, diff: 2, evidence: 3, masked_csv: 4, log: 5 };

  function canonicalKind(kind) {
    return LEGACY_KIND[kind] || kind;
  }

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        provider: state.provider, policyPack: state.policyPack,
        classificationPack: state.classificationPack, extraNotes: state.extraNotes,
        active: [...state.active], sourceEngine: state.source.engine,
        showConfig: state.showConfig,
        cfg: state.cfg,
        runId: state.run && state.run.id ? state.run.id : '',
        debug: state.debug,
      }));
      syncUrlRunPointer();
    } catch (_) { /* ignore */ }
  }

  function readUrlRunPointer() {
    const p = new URLSearchParams(location.search);
    return { runId: p.get('run') || null };
  }

  function syncUrlRunPointer() {
    const url = new URL(location.href);
    const rid = state.run && state.run.id;
    if (rid) url.searchParams.set('run', rid);
    else url.searchParams.delete('run');
    const next = url.pathname + url.search + location.hash;
    if (location.pathname + location.search + location.hash !== next) {
      history.replaceState(null, '', next);
    }
  }

  function restore() {
    try {
      const s = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      if (s.provider) state.provider = s.provider;
      if (s.policyPack) state.policyPack = s.policyPack;
      if (s.classificationPack) state.classificationPack = s.classificationPack;
      if (s.extraNotes) state.extraNotes = s.extraNotes;
      else if (s.intent) state.extraNotes = s.intent;
      if (Array.isArray(s.active)) state.active = new Set(s.active);
      if (s.sourceEngine) state.source.engine = s.sourceEngine;
      if (typeof s.showConfig === 'boolean') state.showConfig = s.showConfig;
      if (typeof s.debug === 'boolean') state.debug = s.debug;
      if (s.cfg) Object.assign(state.cfg, s.cfg);
      state._restoredRunId = s.runId || readUrlRunPointer().runId || '';
    } catch (_) { /* ignore */ }
  }

  /* ── PipelineSpec ↔ UI sync ─────────────────────────────────────────────── */

  function contractActive() {
    return state.active.has('contract') || state.active.has('quality')
      || state.active.has('pii') || state.active.has('classify');
  }

  function buildSpecFromState() {
    const nodes = [];
    const edges = [];
    let y = 60;
    const add = (kind, label, params) => {
      const id = uid();
      nodes.push({ id, kind, label, params: params || {}, position: { x: 60, y } });
      y += 80;
      return id;
    };
    const wire = (src, tgt) => {
      if (src && tgt) edges.push({ id: `e-${src}-${tgt}`, source: src, target: tgt });
    };

    let srcId = null, profId = null, contractId = null;

    if (state.active.has('source')) {
      const eng = state.source.engine;
      const params = { sample_rows: +state.source.sampleRows || 5000 };
      if (eng === 'local') {
        params.engine = 'folder';
        const tbl = [...currentSelectedTables()][0] || '';
        if (tbl) params.table = tbl;
        if (state.source.sampleDir) params.sample_dir = state.source.sampleDir;
      } else if (eng === 'hive') {
        params.engine = 'hive';
        params.database = state.source.hive.database;
      } else if (eng === 'oracle') {
        params.engine = 'oracle';
        const o = state.source.oracle;
        params.jdbc = { host: o.host, port: o.port, database: o.service, credential_ref: o.credential_ref };
      }
      srcId = add('source', 'Source', params);
    }

    if (state.active.has('profile') || contractActive()) {
      profId = add('profile', 'Profile', {});
    }

    if (contractActive()) {
      const c = state.cfg.contract;
      const pii = state.cfg.pii;
      const qual = state.cfg.quality;
      contractId = add('contract', 'Contract', {
        pii: state.active.has('pii') || c.pii,
        quality: state.active.has('quality') || c.quality,
        classify: state.active.has('classify') || c.classify,
        mask_rules: c.mask || state.active.has('mask'),
        enrich: c.enrich,
        policy_pack: state.policyPack,
        jurisdiction: state.cfg.classify.jurisdiction || '',
        provider: state.provider,
        model: state.model,
        pii_engines: pii.engines,
        equation_mode: pii.equation_mode,
        pii_sample_rows: +pii.sample_rows || 5000,
        quality_sample_rows: +qual.sample_rows || 10000,
        quality_strategy: qual.strategy,
        quality_fixed_row_count: +qual.fixed_row_count || 10000,
        quality_sample_fraction: +qual.sample_fraction || 0.1,
        sample_rows: +state.source.sampleRows || +pii.sample_rows || 5000,
      });
    }

    let maskId = null;
    if (state.active.has('mask')) {
      maskId = add('mask', 'Mask', {});
    }

    let pubId = null;
    if (state.active.has('publish')) {
      pubId = add('publish', 'Publish', { backend: state.cfg.publish.target || 'openmetadata' });
    }

    if (srcId && profId) wire(srcId, profId);
    if (srcId && contractId) wire(srcId, contractId);
    if (profId && contractId) wire(profId, contractId);
    if (contractId && maskId) wire(contractId, maskId);
    if (contractId && pubId) wire(contractId, pubId);

    return {
      name: 'composer-pipeline',
      goal: state.extraNotes.trim() || state.prompt.trim() || 'Governance pipeline',
      source: {
        engine: state.source.engine,
        scope: state.source.scope,
        policy_pack: state.policyPack,
        classification_pack: state.classificationPack,
      },
      nodes,
      edges,
      metadata: { provider: state.provider },
    };
  }

  function syncFromSpec(spec) {
    if (!spec || !spec.nodes) return;
    state.syncing = true;
    state.pipeline = spec;
    const kinds = new Set(spec.nodes.map((n) => canonicalKind(n.kind)));
    state.active = new Set();
    if (kinds.has('source')) state.active.add('source');
    if (kinds.has('profile')) state.active.add('profile');
    if (kinds.has('mask')) state.active.add('mask');
    if (kinds.has('publish')) state.active.add('publish');

    const contract = spec.nodes.find((n) => canonicalKind(n.kind) === 'contract');
    if (contract) {
      state.active.add('contract');
      const p = contract.params || {};
      if (p.pii) state.active.add('pii');
      if (p.quality) state.active.add('quality');
      if (p.classify) state.active.add('classify');
      state.cfg.contract.pii = !!p.pii;
      state.cfg.contract.quality = !!p.quality;
      state.cfg.contract.classify = !!p.classify;
      state.cfg.contract.enrich = !!p.enrich;
      state.cfg.contract.mask = !!p.mask_rules;
      if (p.jurisdiction) state.cfg.classify.jurisdiction = p.jurisdiction;
      if (p.policy_pack) state.policyPack = p.policy_pack;
      if (p.pii_engines) state.cfg.pii.engines = p.pii_engines;
      if (p.equation_mode) state.cfg.pii.equation_mode = p.equation_mode;
      if (p.pii_sample_rows) state.cfg.pii.sample_rows = p.pii_sample_rows;
      if (p.quality_sample_rows) state.cfg.quality.sample_rows = p.quality_sample_rows;
      if (p.provider) state.provider = p.provider;
      if (p.model) state.model = p.model;
    }
    if (kinds.has('quality') && !contract) state.active.add('quality');
    if (kinds.has('pii') && !contract) state.active.add('pii');

    const src = spec.nodes.find((n) => canonicalKind(n.kind) === 'source');
    if (src && src.params) {
      const p = src.params;
      if (p.engine === 'folder' || p.engine === 'local') state.source.engine = 'local';
      else if (p.engine) state.source.engine = p.engine;
      if (p.sample_rows) state.source.sampleRows = p.sample_rows;
    }
    if (spec.metadata && spec.metadata.provider) state.provider = spec.metadata.provider;
    state.syncing = false;
  }

  async function compilePrompt() {
    if (!state.pipeline) return;
    try {
      const c = await jpost('/api/agents/compile', { pipeline: state.pipeline });
      state.prompt = c.text || '';
    } catch (_) { state.prompt = ''; }
  }

  async function validatePipeline() {
    if (!state.pipeline) return true;
    try {
      const v = await jpost('/api/agents/validate', { pipeline: state.pipeline });
      state.planValid = !!v.valid;
      state.planErrors = (v.errors || []).map((e) => `${e.node_id}: ${e.message}`);
      return state.planValid;
    } catch (e) {
      state.planValid = false;
      state.planErrors = [String(e.message || e)];
      return false;
    }
  }

  async function rebuildFromIcons() {
    state.pipeline = buildSpecFromState();
    await compilePrompt();
    await validatePipeline();
    if (window.REDIBIS_AGENTS && window.REDIBIS_AGENTS.loadPipeline) {
      window.REDIBIS_AGENTS.loadPipeline(state.pipeline, { silent: true });
    }
    render();
  }

  /* ── data loads ─────────────────────────────────────────────────────────── */

  function applyDefaultsToState(defaults) {
    if (!defaults || typeof defaults !== 'object') return;
    state.defaults = defaults;
    const pii = defaults.pii || {};
    const qual = defaults.quality || {};
    const planner = defaults.planner || {};
    if (pii.engines) state.cfg.pii.engines = pii.engines;
    if (pii.equation_mode) state.cfg.pii.equation_mode = pii.equation_mode;
    if (pii.sample_rows) state.cfg.pii.sample_rows = pii.sample_rows;
    if (qual.sample_rows) state.cfg.quality.sample_rows = qual.sample_rows;
    if (qual.strategy) state.cfg.quality.strategy = qual.strategy;
    if (qual.fixed_row_count) state.cfg.quality.fixed_row_count = qual.fixed_row_count;
    if (qual.sample_fraction != null) state.cfg.quality.sample_fraction = qual.sample_fraction;
    if (planner.provider && !state.provider) state.provider = planner.provider;
    if (planner.model) state.model = planner.model;
    if (pii.sample_rows) state.source.sampleRows = pii.sample_rows;
  }

  async function loadProviders() {
    try {
      const [d, gs] = await Promise.all([
        api('/api/llm-providers'),
        api('/api/settings/global').catch(() => ({})),
      ]);
      state.providerMeta = d.providers || [];
      state.providers = state.providerMeta.map((p) => p.name).filter(Boolean);
      state.defaultProvider = (d.default_provider || '').trim()
        || (gs.llm_defaults && gs.llm_defaults.provider)
        || (gs.agentic_defaults && gs.agentic_defaults.planner_provider)
        || 'gemini';
      if (!state.provider) {
        state.provider = (gs.agentic_defaults && gs.agentic_defaults.planner_provider)
          || (gs.llm_defaults && gs.llm_defaults.provider)
          || state.defaultProvider;
      }
      if (!state.model) {
        state.model = (gs.agentic_defaults && gs.agentic_defaults.planner_model)
          || (gs.llm_defaults && gs.llm_defaults.model)
          || '';
      }
    } catch (_) { state.providers = []; state.providerMeta = []; state.defaultProvider = 'gemini'; }
  }

  async function validateProvider() {
    // Send is gated on a configured + validated model provider (cheap config check, no model call).
    state.providerReady = false;
    state.providerReason = '';
    if (!state.provider) {
      if (state.providers.length) {
        const pref = state.defaultProvider || 'gemini';
        state.provider = state.providers.includes(pref) ? pref : state.providers[0];
      } else {
        state.providerReason = 'No model providers configured — add one in Setup → Model & keys.';
        render();
        return;
      }
    }
    try {
      const d = await jpost('/api/agents/providers/validate', { provider: state.provider, model: state.model });
      if (d.valid) {
        state.providerReady = true;
        state.providerReason = '';
      } else {
        state.providerReason = d.reason || 'Provider validation failed';
      }
    } catch (e) {
      state.providerReason = String(e.message || e);
    }
    render();
  }

  function showSendNotice(msg) {
    state.sendNotice = String(msg || '').trim();
  }

  async function loadPacks() {
    try {
      const d = await api('/api/agents/packs');
      if (Array.isArray(d.policy)) state.packs.policy = d.policy;
      if (Array.isArray(d.classification)) state.packs.classification = d.classification;
      state.packs.all = d.all || state.packs.policy.map((n) => ({ name: n, kind: 'classification' }))
        .concat([{ name: 'defaults', kind: 'defaults' }]);
    } catch (_) { /* keep defaults */ }
  }

  async function loadDefaults() {
    try {
      const d = await api('/api/agents/defaults');
      applyDefaultsToState(d.defaults || {});
    } catch (_) { /* optional */ }
  }

  function applySampleWorkspace(d) {
    state.source.sessionId = d.session_id || state.source.sessionId || '';
    state.source.sampleDir = d.sample_dir || '';
    state.source.local.files = d.file_entries || (d.files || []).map((name) => ({ name, table: name.replace(/\.[^.]+$/, ''), size: 0 }));
    state.source.local.tables = d.tables || state.source.local.files.map((f) => f.table);
    const names = new Set(state.source.local.tables);
    state.source.local.selected = new Set([...state.source.local.selected].filter((t) => names.has(t)));
  }

  async function newSourceSession() {
    try {
      const d = await jpost('/api/agents/source/samples/session', {});
      applySampleWorkspace(d);
      state.source.local.selected = new Set();
    } catch (_) {
      state.source.sessionId = uid();
      state.source.sampleDir = '';
      state.source.local.tables = [];
      state.source.local.files = [];
      state.source.local.selected = new Set();
    }
  }

  async function loadSamples() {
    if (!state.source.sessionId) return;
    try {
      const d = await api(`/api/agents/source/samples?session_id=${encodeURIComponent(state.source.sessionId)}`);
      applySampleWorkspace(d);
    } catch (_) { /* optional */ }
  }

  async function loadRecipes() {
    try {
      const d = await api('/api/agents/recipes');
      state.recipes = d.recipes || [];
    } catch (_) { state.recipes = []; }
  }

  async function applyRecipe(id) {
    let recipe;
    try { recipe = await api(`/api/agents/recipes/${encodeURIComponent(id)}`); }
    catch (_) { return; }
    const plan = recipe.plan || {};
    const active = new Set();
    for (const n of (plan.nodes || [])) {
      const k = n.kind, p = n.params || {};
      if (k === 'source') {
        active.add('source');
        const eng = (p.engine === 'folder') ? 'local' : p.engine;
        if (['local', 'hive', 'oracle'].includes(eng)) state.source.engine = eng;
      } else if (k === 'profile') {
        active.add('profile');
      } else if (k === 'contract') {
        active.add('contract');
        if (p.pii) { active.add('pii'); state.cfg.contract.pii = true; }
        if (p.classify) active.add('classify');
        if (p.quality) { active.add('quality'); state.cfg.contract.quality = true; }
        if (p.mask_rules) { active.add('mask'); state.cfg.contract.mask = true; }
        state.cfg.contract.enrich = !!p.enrich;
      } else if (k === 'mask') {
        active.add('mask');
      } else if (k === 'publish') {
        active.add('publish');
        if (p.backend) state.cfg.publish.target = p.backend;
      }
      // 'sample' and 'gate' are implicit in the composer
    }
    state.active = active.size ? active : new Set(['source']);
    state.extraNotes = recipe.intent || state.extraNotes;
    await rebuildFromIcons();
    persist();
    render();
  }

  /* ── render ─────────────────────────────────────────────────────────────── */

  function render() {
    if (!rootEl) return;
    rootEl.innerHTML = `
      <div class="cmp-feed">${feedHTML()}</div>
      <div class="cmp-dock">
        ${state.debug ? renderDebugPanel() : ''}
        ${state.showConfig ? renderConfigTabs() : ''}
        ${renderComposer()}
      </div>
      ${state.packEditor ? renderPackModal() : ''}
      ${state.modelEditor ? renderModelModal() : ''}
    `;
    bind();
    if (state.codegen) mountMonaco();
  }

  /* ── results feed (above the dock) ──────────────────────────────────────── */

  function feedHTML() {
    const parts = [];
    if (state.run) parts.push(renderRunFeed());
    if (state.codegen) parts.push(renderCodegen());
    if (!parts.length) parts.push(renderEmptyFeed());
    return parts.join('');
  }

  function renderEmptyFeed() {
    return `<div class="cmp-empty">
      ${ti('sparkles')}
      <p>Describe what you want, toggle the steps, then Send.</p>
      <p class="cmp-note">Reports and artifacts from each run appear here with links to the full detail pages.</p>
    </div>`;
  }

  const RUN_STATE_CHIP = {
    done: 'success', completed: 'success', running: 'info', queued: 'neutral',
    partial: 'warning', needs_review: 'warning', awaiting_hitl: 'warning',
    failed: 'danger', cancelled: 'neutral',
  };
  const chipClass = (s) => RUN_STATE_CHIP[s] || 'neutral';
  const RUN_DONE = (s) => ['completed', 'failed', 'cancelled', 'awaiting_hitl'].includes(s);

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

  async function ensureRunArtifacts(force) {
    const run = state.run;
    if (!run || !run.id) return;
    run.artifacts = run.artifacts || { items: [], loaded: false, loading: false, error: '' };
    if (run.artifacts.loading || (run.artifacts.loaded && !force)) return;
    run.artifacts.loading = true;
    refreshFeed();
    try {
      const detail = await api(`/api/agents/runs/${encodeURIComponent(run.id)}/artifacts`);
      run.artifacts.items = detail.artifacts || [];
      run.artifacts.loaded = true;
      run.artifacts.error = '';
    } catch (e) {
      run.artifacts.items = [];
      run.artifacts.loaded = true;
      run.artifacts.error = String(e.message || e);
    } finally {
      run.artifacts.loading = false;
      refreshFeed();
    }
  }

  function renderDebugTraceButtons(run) {
    const art = (run && run.artifacts && run.artifacts.items) || [];
    const trace = art.find((a) => a.name === 'run_trace.json');
    const fullLog = art.find((a) => a.name === 'run.log')
      || art.find((a) => a.name === 'run_debug.log');
    if (!trace && !fullLog) return '';
    const btns = [];
    if (fullLog) {
      btns.push(`<a class="cmp-link" href="${artifactDownloadUrl(run.id, fullLog.id)}" download>${ti('download')} Full log</a>`);
    }
    if (trace) {
      btns.push(`<a class="cmp-link" href="${artifactDownloadUrl(run.id, trace.id)}" target="_blank" rel="noopener">${ti('file-code')} Trace</a>`);
    }
    return `<div class="cmp-rt-links" style="margin-top:8px">${btns.join('')}</div>`;
  }

  function renderArtifactsSection(run, highlightTable) {
    const art = run && run.artifacts;
    if (!art || (!art.loaded && !art.loading)) return '';
    if (art.loading) {
      return `<div class="cmp-rt-card" style="margin-top:10px"><span class="cmp-note">Loading artifacts…</span></div>`;
    }
    if (art.error) {
      return `<div class="cmp-rt-card" style="margin-top:10px"><span class="cmp-rt-err">${ti('alert-triangle')} ${esc(art.error)}</span></div>`;
    }
    const items = art.items || [];
    if (!items.length) {
      return `<div class="cmp-rt-card" style="margin-top:10px"><span class="cmp-note">No artifacts recorded yet.</span></div>`;
    }
    const byTable = {};
    items.forEach((item) => {
      const tbl = item.table || '(run-level)';
      (byTable[tbl] = byTable[tbl] || []).push(item);
    });
    const tableKeys = Object.keys(byTable).sort((a, b) => {
      if (a === '(run-level)') return -1;
      if (b === '(run-level)') return 1;
      return a.localeCompare(b);
    });
    const content = tableKeys.map((tbl) => {
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
              ? `<button type="button" class="cmp-link" data-feed-act="handoff" data-table="${esc(handoffTable)}">${ti('external-link')} Open in redibis</button>`
              : '';
            return `<div class="cmp-rt-row" style="padding:6px 0">
              <span class="cmp-rt-name">${esc(item.name)}</span>
              <span class="cmp-note">${esc(item.type || '')}${item.size ? ` · ${esc(formatBytes(item.size))}` : ''}</span>
              <span class="cmp-rt-links">
                <a class="cmp-link" href="${artifactDownloadUrl(run.id, item.id)}" target="_blank" rel="noopener">${ti('download')} Download</a>
                ${open}
              </span>
            </div>`;
          }).join('');
          return `<div style="margin-top:8px">
            <div class="cmp-note" style="font-weight:600">${esc(ARTIFACT_KIND_LABEL[kind] || kind)}</div>
            ${lines}
          </div>`;
        }).join('');
      const title = tbl === '(run-level)' ? 'Run-level' : tbl;
      return `<div style="margin-top:10px"><div style="font-weight:600;font-size:13px">${esc(title)}</div>${kindHtml}</div>`;
    }).join('');
    return `<div class="cmp-rt-card" style="margin-top:10px">
      <div class="cmp-prompt-label"><span>${ti('paperclip')} Artifacts</span><span class="cmp-note">All tables and kinds — partial artifacts appear while the run is in progress.</span></div>
      ${renderDebugTraceButtons(run)}
      ${content}
    </div>`;
  }

  function renderRunFeed() {
    const run = state.run;
    const status = run.status || 'running';
    const chip = chipClass(status);
    const live = !RUN_DONE(status);
    const last = lastLogLine(run);
    const tables = run.tables || [];
    run.summaries = run.summaries || {};
    const rows = tables.map((t) => {
      const c = chipClass(t.status);
      const prog = t.steps_total ? `${t.steps_completed}/${t.steps_total}` : '';
      const isOpen = run.open === t.table;
      const links = run.id ? `
        <span class="cmp-rt-links">
          <button type="button" class="cmp-link ${isOpen ? 'on' : ''}" data-feed-act="view-report" data-table="${esc(t.table)}">${ti(isOpen ? 'chevron-up' : 'report')} Reports</button>
          <button type="button" class="cmp-link" data-feed-act="handoff" data-table="${esc(t.table)}">${ti('external-link')} Open in console</button>
        </span>` : '';
      const card = isOpen ? summaryCardHtml(run, t.table, run.summaries[t.table]) : '';
      return `<div class="cmp-rt-row">
        <span class="ag-chip ${c}">${esc(t.status)}</span>
        <span class="cmp-rt-name">${esc(t.table)}</span>
        ${prog ? `<span class="cmp-note">${esc(prog)} steps</span>` : ''}
        ${t.last_error ? `<span class="cmp-rt-err" title="${esc(t.last_error)}">${ti('alert-triangle')} ${esc(t.last_error)}</span>` : ''}
        ${links}
      </div>${card}`;
    }).join('');
    return `<div class="cmp-run">
      <div class="cmp-logbar">
        <span class="ag-chip ${chip}">${live ? `<span class="cmp-spin">${ti('loader-2')}</span>` : ti('circle-check')} ${esc(status)}</span>
        <span class="cmp-log-text">${esc(last)}</span>
        <span class="cmp-logbar-right">
          ${run.id ? `<span class="cmp-note cmp-runid">run ${esc(run.id)}</span>` : ''}
          ${live && run.id ? `<button type="button" class="ag-btn ghost" data-feed-act="cancel-run">${ti('player-stop')} Stop</button>` : ''}
          ${run.id ? `<button type="button" class="cmp-link" data-feed-act="open-dashboard">${ti('layout-dashboard')} Dashboard</button>` : ''}
        </span>
      </div>
      ${run.error ? `<div class="cmp-run-err">${ti('alert-triangle')} ${esc(run.error)}</div>` : ''}
      ${renderDebugTraceButtons(run)}
      ${renderArtifactsSection(run, run.open)}
      ${rows ? `<div class="cmp-rt-list">${rows}</div>` : `<div class="cmp-note" style="padding:8px 12px">Starting…</div>`}
    </div>`;
  }

  function lastLogLine(run) {
    if (state.debugLogs.length) return state.debugLogs[state.debugLogs.length - 1];
    const steps = run.steps || [];
    for (let i = steps.length - 1; i >= 0; i--) {
      const s = steps[i];
      if (!s) continue;
      const where = s.table ? `${s.table} · ` : '';
      return `${where}${s.node_kind || s.label || 'step'} — ${s.status || ''}`.trim();
    }
    if (run.error) return run.error;
    return RUN_DONE(run.status) ? 'Run finished.' : 'Submitting run…';
  }

  /* ── Debug log drawer — reuses the scan console SSE (/api/sessions/{sid}/stream) ── */

  function renderDebugPanel() {
    const rid = state.run && state.run.id;
    const src = rid ? `run ${esc(rid)} · live SSE` : 'no run yet — start one to stream logs';
    const body = state.debugLogs.length ? esc(state.debugLogs.join('\n')) : 'Waiting for log output…';
    return `<div class="cmp-debug">
      <div class="cmp-debug-head">
        ${ti('bug')} Debug log <span class="cmp-note">${src} · last ${LIVE_LOG_MAX_LINES} lines</span>
        <span class="cmp-debug-right">
          <button type="button" class="cmp-link" data-act="debug-clear">${ti('eraser')} Clear</button>
          <button type="button" class="cmp-link" data-act="toggle-debug">${ti('x')} Close</button>
        </span>
      </div>
      <div class="cmp-note" style="margin-top:6px">Download the complete log from Results artifacts when the run finishes.</div>
      <pre class="cmp-debug-body" id="cmp-debug-body">${body}</pre>
    </div>`;
  }

  function appendDebug(line) {
    if (line == null || line === '') return;
    state.debugLogs.push(String(line));
    if (state.debugLogs.length > LIVE_LOG_MAX_LINES) {
      state.debugLogs = state.debugLogs.slice(-LIVE_LOG_MAX_LINES);
    }
    const body = document.getElementById('cmp-debug-body');
    if (body) { body.textContent = state.debugLogs.join('\n'); body.scrollTop = body.scrollHeight; }
    const bar = rootEl && rootEl.querySelector('.cmp-log-text');
    if (bar) bar.textContent = String(line);
  }

  function closeDebugStream() {
    if (state._es) { try { state._es.close(); } catch (_) { /* noop */ } state._es = null; }
  }

  function connectDebug() {
    closeDebugStream();
    const rid = state.run && state.run.id;
    if (rid && window.EventSource) {
      try {
        const es = new EventSource(`/api/agents/runs/${encodeURIComponent(rid)}/stream`);
        state._es = es;
        es.onmessage = (e) => {
          try {
            const d = JSON.parse(e.data);
            if (d.type === 'done' || d.type === 'failed') {
              appendDebug(`— ${d.message || 'run finished'} —`);
              closeDebugStream();
              return;
            }
            if (d.type === 'error') {
              appendDebug(d.message || 'stream error');
              return;
            }
            appendDebug(d.message != null ? d.message : e.data);
          } catch (_) { appendDebug(e.data); }
        };
        es.onerror = () => { /* SSE auto-reconnects; leave open */ };
        return;
      } catch (_) { /* fall through to telemetry */ }
    }
    refreshDebugFromRun();
  }

  // Fallback when there is no live SSE (no run id yet): render persisted steps/telemetry.
  function refreshDebugFromRun() {
    if (!state.debug || state._es) return;
    const run = state.run || {};
    if (Array.isArray(run.logs) && run.logs.length) {
      state.debugLogs = run.logs.slice();
      const body = document.getElementById('cmp-debug-body');
      if (body) { body.textContent = state.debugLogs.join('\n'); body.scrollTop = body.scrollHeight; }
      return;
    }
    const lines = (run.steps || []).map((s) => {
      const out = s.output || {};
      const tail = s.error ? `ERROR ${s.error}`
        : (out.skipped && out.reason) ? `skipped — ${out.reason}`
        : [out.columns != null ? `${out.columns} cols` : '',
           out.columns_detected != null ? `${out.columns_detected} PII` : '',
           out.expectations != null ? `${out.expectations} rules` : ''].filter(Boolean).join(' · ');
      const t = (s.finished_at || s.started_at || '').replace('T', ' ').slice(0, 19);
      return `${t}  [${s.status}] ${s.table || '—'} · ${s.node_kind || ''} ${tail ? '— ' + tail : ''}`.trim();
    });
    state.debugLogs = lines;
    const body = document.getElementById('cmp-debug-body');
    if (body) { body.textContent = lines.join('\n') || 'No steps yet…'; body.scrollTop = body.scrollHeight; }
  }

  function summaryCardHtml(run, table, s) {
    if (!s) return `<div class="cmp-rt-card"><span class="cmp-note">Loading report…</span></div>`;
    if (s._error) return `<div class="cmp-rt-card"><span class="cmp-rt-err">${ti('alert-triangle')} ${esc(s._error)}</span></div>`;
    const steps = (s.steps_done || []).map((x) =>
      `<li class="${x.done ? 'done' : ''}">${x.done ? ti('circle-check') : ti('circle')} ${esc(x.label)}</li>`).join('');
    const m = s.metrics || {};
    const metric = (v, l) => `<div class="cmp-metric"><div class="cmp-metric-val">${esc(v)}</div><div class="cmp-metric-lbl">${esc(l)}</div></div>`;
    // Deep report HTML (PII / quality / contract) is the existing redibis artifact — open it
    // in the 360° console via handoff rather than re-rendering it here.
    return `<div class="cmp-rt-card">
      <ul class="cmp-steps">${steps || '<li class="cmp-note">No steps recorded</li>'}</ul>
      <div class="cmp-metrics">
        ${metric(m.columns || 0, 'Columns')}
        ${metric(m.pii_columns || 0, 'PII columns')}
        ${metric(m.tags_applied || 0, 'Tags')}
        ${metric(m.contract_version || '—', 'Contract')}
      </div>
      ${renderArtifactsSection(run, table)}
    </div>`;
  }

  function currentProviderMeta() {
    return state.providerMeta.find((p) => p.name === state.provider) || {};
  }

  function renderSetupConfig() {
    const sel = (id, label, value, opts) => `
      <label>${esc(label)}
        <select data-sel="${id}">${(opts || []).map((o) =>
          `<option value="${esc(o)}" ${o === value ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>
      </label>`;
    const meta = currentProviderMeta();
    const models = [state.model || meta.model || ''].concat(meta.known_models || []).filter(Boolean);
    const uniqModels = [...new Set(models)];
    const recipeOpts = (state.recipes || []).map((r) => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
    const ready = !!state.providerReady;
    const status = ready
      ? `<span class="ag-chip success">${ti('circle-check')} model ready</span>`
      : `<span class="ag-chip warning">${ti('alert-triangle')} ${esc(state.providerReason || 'select & validate a model')}</span>`;
    const keyHint = (meta.cloud || meta.residency === 'public') && !meta.api_key_env_set && !meta.api_key_saved
      ? `<div class="cmp-note" style="color:#92400e;background:#fef3c7;padding:8px;border-radius:6px;margin-top:8px">
          Cloud provider <strong>${esc(state.provider)}</strong> needs
          <code>${esc(meta.api_key_env || 'GEMINI_API_KEY')}</code> on the server
          (export + restart webapp). Review the active provider in <a href="/settings">Settings</a>.
        </div>`
      : '';
    return `<div class="cmp-setup">
      <div class="cmp-row">
        ${sel('provider', 'Model provider', state.provider, state.providers.length ? state.providers : [state.provider || 'gemini'])}
        ${uniqModels.length ? sel('model', 'Variant', state.model || meta.model || uniqModels[0], uniqModels) : ''}
        <button type="button" class="ag-btn ghost" data-act="edit-model">${ti('key')} Model &amp; keys</button>
        ${status}
      </div>
      ${keyHint}
      <div class="cmp-row">
        ${sel('policy', 'Policy pack', state.policyPack, state.packs.policy)}
        ${sel('classification', 'Classification pack', state.classificationPack, state.packs.classification)}
        <button type="button" class="ag-btn ghost" data-act="edit-packs">${ti('pencil')} Edit packs</button>
      </div>
      <div class="cmp-row">
        <label>Start from a recipe
          <select data-recipe><option value="">Choose a recipe…</option>${recipeOpts}</select></label>
      </div>
      <div class="cmp-note">Model, keys, packs, and recipes live here in Setup. They apply to every Send.</div>
    </div>`;
  }

  function renderComposer() {
    const icons = ACTIONS.map((a) => {
      const on = state.active.has(a.kind);
      const cfgHint = a.config ? `<span class="cmp-cfg-badge" title="Has settings">${ti('settings')}</span>` : '';
      return `<button type="button" class="cmp-action ${on ? 'on' : ''}${a.config ? ' has-config' : ''}" data-action="${a.kind}" aria-pressed="${on}">
        ${ti(a.icon)} ${a.label}${cfgHint}</button>`;
    }).join('');
    const ready = !!state.providerReady;
    const sendTitle = ready
      ? 'Send pipeline'
      : (state.providerReason || 'Configure and validate a model provider in Setup');
    const cfgOn = state.showConfig && hasConfigTabs();
    const promptText = state.prompt.trim() || 'Toggle steps or open Config — the instruction updates automatically.';
    return `<div class="cmp-box">
      <div class="cmp-actionbar cmp-actionbar-top">
        <div class="cmp-actions">${icons}</div>
      </div>
      <div class="cmp-prompt-panel">
        <div class="cmp-prompt-label">
          <span>${ti('script')} Generated instruction</span>
          <span class="cmp-note">Read-only · updates when steps or config change</span>
        </div>
        <textarea class="cmp-prompt-auto" readonly rows="6" aria-readonly="true">${esc(promptText)}</textarea>
        <div class="cmp-prompt-label">
          <span>${ti('message-plus')} Additional notes</span>
          <span class="cmp-note">Optional · sent with the instruction</span>
        </div>
        <textarea class="cmp-input cmp-notes" data-input="extraNotes" rows="2"
          placeholder="Extra context — jurisdiction hints, table names, masking goals…">${esc(state.extraNotes)}</textarea>
      </div>
      <div class="cmp-actionbar">
        <div class="cmp-actionbar-right" style="margin-left:0;width:100%;justify-content:flex-end">
          <button type="button" class="ag-btn cmp-debug-toggle ${state.debug ? 'on' : ''}" data-act="toggle-debug"
            title="Live debug log (streams the run's SSE, same as the scan console)" aria-pressed="${state.debug}">
            ${ti('bug')} Debug
          </button>
          <button type="button" class="ag-btn cmp-config-toggle ${cfgOn ? 'on' : ''}" data-act="toggle-config"
            title="${cfgOn ? 'Hide step configuration' : 'Show step configuration'}" aria-pressed="${cfgOn}">
            ${ti('settings')} Config
          </button>
          <button type="button" class="cmp-send ${ready ? 'ready' : ''}" data-act="send" aria-label="Send" title="${esc(sendTitle)}">${ti('send')}</button>
        </div>
      </div>
    </div>
    <div class="cmp-hint">${ti('info-circle')} Green steps are active. Config opens per-step settings.${state.sendNotice ? ` <span class="ag-chip danger">${esc(state.sendNotice)}</span>` : ''}${!state.sendNotice && state.planErrors.length ? ` <span class="ag-chip danger">${esc(state.planErrors[0])}</span>` : ''}</div>`;
  }

  // Setup is always available; step tabs appear only when their step is active.
  const hasConfigTabs = () => true;

  function configTabList() {
    return [{ kind: 'setup', label: 'Setup' }]
      .concat(ACTIONS.filter((a) => a.config && state.active.has(a.kind)));
  }

  function renderConfigTabs() {
    const tabs = configTabList();
    if (!tabs.some((t) => t.kind === state.configTab)) state.configTab = 'setup';
    const strip = tabs.map((t) =>
      `<button type="button" class="cmp-tab ${t.kind === state.configTab ? 'on' : ''}" data-tab="${t.kind}">${esc(t.label)}</button>`).join('');
    return `<div class="cmp-config">
      <div class="cmp-tabstrip">${strip}</div>
      <div class="cmp-tabbody">${renderConfigBody(state.configTab)}</div>
    </div>`;
  }

  function renderConfigBody(kind) {
    if (kind === 'setup') return renderSetupConfig();
    if (kind === 'source') return renderSourceConfig();
    if (kind === 'pii') {
      const p = state.cfg.pii;
      return `<div class="cmp-row"><label>PII engines
        <select data-cfg="pii.engines">
          <option value="regex" ${p.engines === 'regex' ? 'selected' : ''}>Regex only (Presidio)</option>
          <option value="ner" ${p.engines === 'ner' ? 'selected' : ''}>NER only (GLiNER)</option>
          <option value="both" ${p.engines === 'both' ? 'selected' : ''}>Both regex + NER</option>
        </select></label>
        <label>Equation mode
        <select data-cfg="pii.equation_mode">
          ${['strict', 'balanced', 'lenient'].map((m) =>
            `<option value="${m}" ${p.equation_mode === m ? 'selected' : ''}>${m}</option>`).join('')}
        </select></label>
        <label>PII sample rows <input data-cfg="pii.sample_rows" value="${esc(p.sample_rows)}" style="width:100px"/></label>
      </div>
      <div class="cmp-note">Used when PII scan runs — separate from quality sampling. Defaults from Run defaults pack when unset.</div>`;
    }
    if (kind === 'quality') {
      const q = state.cfg.quality;
      return `<div class="cmp-row">
        <label>Quality sample rows <input data-cfg="quality.sample_rows" value="${esc(q.sample_rows)}" style="width:100px"/></label>
        <label>Strategy
        <select data-cfg="quality.strategy">
          ${['fixed_rows', 'statistical', 'column_first', 'partition_picker'].map((s) =>
            `<option value="${s}" ${q.strategy === s ? 'selected' : ''}>${s}</option>`).join('')}
        </select></label>
        <label>Fixed row count <input data-cfg="quality.fixed_row_count" value="${esc(q.fixed_row_count)}" style="width:100px"/></label>
        <label>Sample fraction <input data-cfg="quality.sample_fraction" value="${esc(q.sample_fraction)}" style="width:80px"/></label>
      </div>
      <div class="cmp-note">Quality scan uses its own row budget — independent of PII sample size.</div>`;
    }
    if (kind === 'classify') return `
      <div class="cmp-row"><label>Jurisdiction <input data-cfg="classify.jurisdiction" value="${esc(state.cfg.classify.jurisdiction)}" placeholder="EU / IN / US"/></label></div>
      <div class="cmp-note">Uses classification pack <strong>${esc(state.classificationPack)}</strong>.</div>`;
    if (kind === 'contract') {
      const c = state.cfg.contract;
      return `
      <div class="cmp-checks">
        ${['pii', 'quality', 'mask', 'definitions'].map((k) =>
          `<label><input type="checkbox" data-cfg="contract.${k}" ${c[k] ? 'checked' : ''}/> ${esc(k)}</label>`).join('')}
      </div>
      <label class="cmp-enrich"><input type="checkbox" data-cfg="contract.enrich" ${c.enrich ? 'checked' : ''}/> Enrich with LLM</label>
      <div class="cmp-note">Profiling runs automatically when contract is on.</div>`;
    }
    if (kind === 'publish') return `
      <div class="cmp-row"><label>Target
        <select data-cfg="publish.target">
          <option value="openmetadata" ${state.cfg.publish.target === 'openmetadata' ? 'selected' : ''}>OpenMetadata</option>
          <option value="atlas" disabled>Atlas (coming soon)</option>
        </select>
      </label></div>`;
    return '';
  }

  function renderSourceConfig() {
    const eng = state.source.engine;
    const engBtn = (k, icon, label) =>
      `<button type="button" class="cmp-eng ${eng === k ? 'on' : ''}" data-engine="${k}">${ti(icon)} ${label}</button>`;
    let form = '';
    if (eng === 'local') form = renderLocalSource();
    else if (eng === 'hive') form = renderHiveSource();
    else if (eng === 'oracle') form = renderOracleSource();
    return `<div class="cmp-source">
      <div class="cmp-engs">${engBtn('local', 'folder', 'Local folder')}${engBtn('hive', 'server', 'Hive metastore')}${engBtn('oracle', 'database', 'Oracle')}</div>
      ${form}
    </div>`;
  }

  function renderLocalSource() {
    const t = state.source.local;
    const list = t.files.length
      ? t.files.map((f) => {
          const name = f.name || f;
          const table = f.table || String(name).replace(/\.[^.]+$/, '');
          const size = f.size ? ` · ${formatBytes(f.size)}` : '';
          return `<div class="cmp-file-row">
            <label><input type="checkbox" data-localtbl="${esc(table)}" ${t.selected.has(table) ? 'checked' : ''}/> ${esc(table)}</label>
            <span class="cmp-note">${esc(name)}${size}</span>
            <button type="button" class="cmp-link cmp-file-del" data-act="src-del" data-file="${esc(name)}" title="Remove file">${ti('trash')}</button>
          </div>`;
        }).join('')
      : `<div class="cmp-note">No files yet — upload CSV or Parquet files, or pick a folder.</div>`;
    const sid = state.source.sessionId ? `<span class="cmp-note">workspace ${esc(state.source.sessionId)}</span>` : '';
    return `<div class="cmp-row cmp-src-actions">
        <button type="button" class="ag-btn" data-act="src-upload">${ti('upload')} Upload files</button>
        <button type="button" class="ag-btn ghost" data-act="src-upload-folder">${ti('folder')} Upload folder</button>
        <button type="button" class="ag-btn ghost" data-act="src-new">${ti('refresh')} New workspace</button>
        ${t.files.length ? `<button type="button" class="cmp-link" data-act="src-clear">${ti('eraser')} Clear all</button>` : ''}
        ${sid}
      </div>
      ${scopeRow()}
      <div class="cmp-tbllist cmp-filelist">${list}</div>`;
  }

  function renderHiveSource() {
    const h = state.source.hive;
    const r = state.source.remote;
    return `<div class="cmp-row">
        <label><input type="checkbox" data-cfg="hive.use_spark" ${h.use_spark ? 'checked' : ''}/> Use injected Spark session</label>
      </div>
      <div class="cmp-row">
        <label>Metastore URI <input data-cfg="hive.metastore_uri" value="${esc(h.metastore_uri)}" placeholder="thrift://hms:9083" ${h.use_spark ? 'disabled' : ''}/></label>
        <label>Database <input data-cfg="hive.database" value="${esc(h.database)}"/></label>
      </div>
      ${connectRow()}
      ${r.connected ? schemaBrowseRow() : ''}
      ${r.connected ? browseRow() : ''}
      <div class="cmp-note">Catalog via metastore; sample rows via Spark session.</div>`;
  }

  function renderOracleSource() {
    const o = state.source.oracle;
    const r = state.source.remote;
    return `<div class="cmp-row">
        <label>Host <input data-cfg="oracle.host" value="${esc(o.host)}"/></label>
        <label>Port <input data-cfg="oracle.port" value="${esc(o.port)}"/></label>
        <label>Service / SID <input data-cfg="oracle.service" value="${esc(o.service)}"/></label>
        <label>Schema / owner <input data-cfg="oracle.owner" value="${esc(o.owner)}" placeholder="HR — defaults to credential username"/></label>
      </div>
      <div class="cmp-row">
        <label>Credential reference <input data-cfg="oracle.credential_ref" value="${esc(o.credential_ref)}" placeholder="env var or file path"/></label>
      </div>
      ${connectRow()}
      ${r.connected ? schemaBrowseRow() : ''}
      ${r.connected ? browseRow() : ''}
      <div class="cmp-note">Credentials resolved at runtime — never stored in session or contracts.</div>`;
  }

  function connectRow() {
    const r = state.source.remote;
    const status = r.error
      ? `<span class="ag-chip danger">${esc(r.error)}</span>`
      : r.connected ? `<span class="ag-chip success">${ti('circle-check')} connected · ${r.tables.length} tables</span>` : '';
    return `<div class="cmp-row"><button type="button" class="ag-btn" data-act="src-test">${ti('plug-connected')} Test connection</button>${status}</div>`;
  }

  function schemaBrowseRow() {
    const r = state.source.remote;
    const opts = (r.schemas.length ? r.schemas : [r.schema]).filter(Boolean)
      .map((s) => `<option value="${esc(s)}" ${s === r.schema ? 'selected' : ''}>${esc(s)}</option>`).join('');
    return `<div class="cmp-row"><label>Schema
      <select data-cfg="remote.schema">${opts || `<option value="">—</option>`}</select>
      <button type="button" class="ag-btn" data-act="src-load-tables" style="margin-top:4px">${ti('refresh')} Load tables</button>
    </label></div>`;
  }

  function scopeRow() {
    const s = state.source.scope;
    const b = (k, l) => `<button type="button" class="cmp-scope ${s === k ? 'on' : ''}" data-scope="${k}">${esc(l)}</button>`;
    return `<div class="cmp-row cmp-scoperow">Scope: ${b('one', 'One table')}${b('selected', 'Selected')}${b('schema', 'Entire schema')}
      <span class="cmp-sample">Sample <input data-cfg="source.sampleRows" value="${esc(state.source.sampleRows)}" style="width:80px"/> rows</span></div>`;
  }

  function browseRow() {
    const r = state.source.remote;
    const list = r.tables.map((n) =>
      `<label><input type="checkbox" data-remotetbl="${esc(n)}" ${r.selected.has(n) ? 'checked' : ''}/> ${esc(n)}</label>`).join('');
    return `${scopeRow()}<div class="cmp-tbllist">${list || '<div class="cmp-note">No tables in schema.</div>'}</div>`;
  }

  function renderCodegen() {
    const cg = state.codegen;
    if (cg._native) {
      const dec = cg._native.decision || {};
      const caps = (dec.native_matches || []).map((m) =>
        `<li><strong>${esc(m.label || m.id)}</strong> <span class="cmp-note">${esc(m.via || '')}</span>
          ${m.matched_on && m.matched_on.length ? `<span class="cmp-note">· matched: ${esc(m.matched_on.join(', '))}</span>` : ''}</li>`).join('');
      return `<div class="cmp-section"><div class="cmp-section-head">${ti('bulb')} redibis can already do this</div>
        <div class="cmp-rt-card">
          <p>${esc(cg._native.message || 'This is native redibis functionality — no external code needed.')}</p>
          <ul class="cmp-steps cmp-native-list">${caps || '<li class="cmp-note">native pipeline step</li>'}</ul>
          <p class="cmp-note">${esc(cg._native.hint || '')}</p>
          <div class="cmp-row" style="margin-top:10px">
            <button type="button" class="ag-btn" data-act="cg-force">${ti('code')} Generate code anyway</button>
          </div>
        </div></div>`;
    }
    const files = Object.keys(cg.files || {});
    const tabs = files.map((f, i) =>
      `<button type="button" class="cmp-filetab ${i === (cg._active || 0) ? 'on' : ''}" data-file="${i}">${esc(f)}</button>`).join('');
    return `<div class="cmp-section"><div class="cmp-section-head">${ti('code')} Generated code <span class="cmp-note">${files.length} files</span></div>
      <div class="cmp-codecard">
        <div class="cmp-filetabs">${tabs}</div>
        <div id="cmp-monaco" class="cmp-monaco"></div>
        <pre id="cmp-codefallback" class="cmp-codefallback" hidden></pre>
        <div class="cmp-codebar">
          <span class="ag-chip ${cg.vuln === 'clean' ? 'success' : 'warning'}">${ti('shield-check')} vuln: ${esc(cg.vuln || 'pending')}</span>
          <span class="ag-chip ${cg.judge === 'approve' ? 'success' : 'warning'}">${ti('gavel')} judge: ${esc(cg.judge || 'pending')}</span>
          <span class="ag-chip warning">${ti('player-pause')} not executed</span>
          <span class="cmp-codebar-right">
            <button type="button" class="ag-btn" data-act="cg-download">${ti('download')} This file</button>
            <button type="button" class="ag-btn" data-act="cg-zip">${ti('file-zip')} Download all</button>
            <button type="button" class="ag-btn primary" data-act="cg-register">${ti('check')} Approve &amp; register</button>
          </span>
        </div>
      </div></div>`;
  }

  function renderPackModal() {
    const pe = state.packEditor;
    const list = (pe.packList || []).map((p) => {
      const on = pe.name === p.name && pe.kind === p.kind;
      const badge = p.kind === 'defaults' ? 'Run defaults' : 'Classification';
      return `<button type="button" class="cmp-pack-item ${on ? 'on' : ''}" data-pack="${esc(p.name)}" data-pack-kind="${esc(p.kind)}">${esc(p.name)} <small>${badge}</small></button>`;
    }).join('');
    return `<div class="cmp-modal open"><div class="cmp-modal-box cmp-modal-wide">
      <h3>Packs library</h3>
      <div class="cmp-pack-layout">
        <div class="cmp-pack-list">${list || '<div class="cmp-note">Loading…</div>'}
          <button type="button" class="ag-btn" data-act="pack-new" style="margin-top:8px">${ti('plus')} New classification pack</button>
        </div>
        <div class="cmp-pack-editor">
          <div class="cmp-note">${pe.kind === 'defaults' ? 'Run defaults — PII engines, sampling, planner model' : `Classification policy · ${esc(pe.name)}`}</div>
          <textarea class="cmp-pack-text" data-input="pack-text" rows="18">${esc(pe.text || '')}</textarea>
        </div>
      </div>
      <div class="cmp-row" style="margin-top:10px">
        <button type="button" class="ag-btn primary" data-act="pack-save">${ti('device-floppy')} Save</button>
        <button type="button" class="ag-btn" data-act="pack-close">Close</button>
      </div></div></div>`;
  }

  function validateApiBase(value, providerName) {
    const v = (value || '').trim();
    if (!v) return null;
    if (/^https?:\/\/.+/i.test(v)) return null;
    const hosted = { gemini: 1, claude: 1, openai: 1, openrouter: 1 };
    const prov = (providerName || 'provider').trim() || 'provider';
    let msg = `${prov} has an invalid api_base configuration. api_base must be a full http:// or https:// URL.`;
    if (hosted[prov.toLowerCase()]) {
      msg += ' For hosted providers, leave api_base empty unless you are using a custom gateway.';
    }
    if (/^[A-Za-z0-9._-]{20,}$/.test(v) && !v.includes('://') && !v.includes('/')) {
      msg += ' The current value looks like an API key, not a URL; put it in the provider API key field or the matching *_API_KEY environment variable instead.';
    }
    return msg;
  }

  function renderModelModal() {
    const me = state.modelEditor;
    const cfg = me.config || {};
    const hosted = ['gemini', 'claude', 'openai', 'openrouter'].includes((me.name || '').toLowerCase());
    return `<div class="cmp-modal open"><div class="cmp-modal-box">
      <h3>Model provider · ${esc(me.name)}</h3>
      <div class="cmp-row"><label>Default model <input data-model="litellm_model" value="${esc(cfg.litellm_model || '')}"/></label></div>
      <div class="cmp-row"><label>API base ${hosted ? '(advanced, optional)' : ''} <input data-model="api_base" value="${esc(cfg.api_base || '')}" placeholder="${hosted ? 'leave empty for hosted providers; keys go in API key env var' : 'http://host:port/v1'}"/></label></div>
      <div class="cmp-row"><label>API key env var <input data-model="api_key_env" value="${esc(cfg.api_key_env || '')}" placeholder="OPENAI_API_KEY — never paste raw keys"/></label></div>
      <div class="cmp-row"><label>Description <input data-model="description" value="${esc(cfg.description || '')}"/></label></div>
      <div class="cmp-note">Registry metadata editor only. Configure defaults in <a href="/settings">Settings</a>; keep reusable credentials in environment variables. Registry file: ${esc(me.path || './llm_providers.json')}</div>
      <div class="cmp-row" style="margin-top:10px">
        <button type="button" class="ag-btn primary" data-act="model-save">${ti('device-floppy')} Save provider</button>
        <button type="button" class="ag-btn" data-act="model-close">Close</button>
      </div></div></div>`;
  }

  /* ── Monaco ─────────────────────────────────────────────────────────────── */

  function mountMonaco() {
    const host = document.getElementById('cmp-monaco');
    if (!host) return;
    const showFallback = () => {
      const fb = document.getElementById('cmp-codefallback');
      host.hidden = true;
      if (fb) { fb.hidden = false; fb.textContent = currentCode(); }
    };
    const setModel = () => {
      const f = currentFile();
      if (!monacoEditor) {
        monacoEditor = monaco.editor.create(host, {
          value: currentCode(), language: langFor(f), readOnly: true,
          minimap: { enabled: false }, automaticLayout: true, fontSize: 12,
        });
      } else {
        monacoEditor.setValue(currentCode());
        monaco.editor.setModelLanguage(monacoEditor.getModel(), langFor(f));
      }
    };
    if (monaco) { setModel(); return; }
    ensureMonacoLoader().then(() => {
      window.require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
      window.require(['vs/editor/editor.main'], () => { monaco = window.monaco; setModel(); }, showFallback);
    }).catch(showFallback);
  }

  function ensureMonacoLoader() {
    return new Promise((res, rej) => {
      if (window.require && window.require.config) return res();
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs/loader.js';
      s.onload = res; s.onerror = rej; document.head.appendChild(s);
    });
  }

  const currentFile = () => Object.keys(state.codegen.files || {})[state.codegen._active || 0] || '';
  const currentCode = () => (state.codegen.files || {})[currentFile()] || '';
  function langFor(f) {
    if (/\.json$/.test(f)) return 'json';
    if (/\.(sh|bash)$/.test(f)) return 'shell';
    if (/\.md$/.test(f)) return 'markdown';
    if (/\.py$/.test(f)) return 'python';
    if (/\.sql$/.test(f)) return 'sql';
    return 'plaintext';
  }

  /* ── actions ────────────────────────────────────────────────────────────── */

  function fullIntent() {
    const parts = [state.prompt.trim(), state.extraNotes.trim()].filter(Boolean);
    return parts.join('\n\n');
  }

  function isCodegenIntent(text) {
    return /generate\s+(code|script|ranger|policy)|write\s+(a\s+)?(python|sql|ranger)|codegen/i.test(text || '');
  }

  async function send() {
    state.sendNotice = '';
    if (!state.extraNotes.trim() && state.active.size === 0) {
      showSendNotice('Toggle at least one step or add notes before sending.');
      render();
      return;
    }
    if (!state.providerReady) {
      state.showConfig = true;
      state.configTab = 'setup';
      showSendNotice(state.providerReason || 'Configure and validate a model provider in Setup.');
      render();
      return;
    }
    if (isCodegenIntent(fullIntent())) {
      await requestCode();
      return;
    }
    try {
      state.pipeline = buildSpecFromState();
      await compilePrompt();
      await validatePipeline();
      persist();
      if (!state.planValid) {
        showSendNotice(state.planErrors[0] || 'Pipeline validation failed — check step configuration.');
        render();
        return;
      }
      if (!window.REDIBIS_AGENTS_ENABLED) {
        showSendNotice('Agent execution is disabled — set agents.enabled: true in config to run.');
        render();
        return;
      }
      render();
      await startRun();
    } catch (e) {
      showSendNotice(String(e.message || e));
      render();
    }
  }

  async function requestCode(forceExternal) {
    try {
      const tables = [...currentSelectedTables()];
      const d = await jpost('/api/agents/codegen/submit', {
        table: tables[0] || 'telecom.customers',
        intent: fullIntent(),
        target_system: 'ranger',
        residency: 'local',
        provider: state.provider,
        force_external: !!forceExternal,
      });
      if (d.status === 'native_capability_available') {
        state.codegen = { _native: d };
      } else {
        state.codegen = normalizeCodegen(d);
      }
    } catch (e) {
      state.codegen = { files: { 'error.txt': String(e.message || e) }, vuln: 'n/a', judge: 'n/a' };
    }
    render();
  }

  function normalizeCodegen(d) {
    const files = d.files || (d.code ? { [(d.filename || 'generated.txt')]: d.code } : {});
    const judge = d.judge || {};
    return {
      files,
      vuln: d.vuln || d.vuln_scan || (d.vulnerability_scan && d.vulnerability_scan.status) || 'clean',
      judge: d.judge_verdict || (judge.approved ? 'approve' : 'review') || 'pending',
      _active: 0,
      raw: d,
    };
  }

  function currentSelectedTables() {
    const s = state.source;
    if (s.engine === 'local') {
      if (s.scope === 'schema') return s.local.tables;
      if (s.scope === 'one') return s.local.selected.size ? [[...s.local.selected][0]] : s.local.tables.slice(0, 1);
      return s.local.selected.size ? s.local.selected : s.local.tables;
    }
    if (s.scope === 'schema') return s.remote.tables;
    if (s.scope === 'one') return s.remote.selected.size ? [[...s.remote.selected][0]] : s.remote.tables.slice(0, 1);
    return s.remote.selected.size ? s.remote.selected : s.remote.tables;
  }

  function sourceTestBody() {
    const s = state.source;
    if (s.engine === 'oracle') {
      return {
        engine: 'oracle',
        jdbc: {
          host: s.oracle.host, port: s.oracle.port, database: s.oracle.service,
          credential_ref: s.oracle.credential_ref,
        },
        schema_name: s.remote.schema || s.oracle.owner || '',
      };
    }
    if (s.engine === 'hive') {
      return { engine: 'hive', hive: s.hive, schema_name: s.hive.database };
    }
    return {};
  }

  async function testConnection() {
    const s = state.source;
    s.remote.error = '';
    try {
      const d = await jpost('/api/agents/source/test', sourceTestBody());
      s.remote.connected = !!d.connected;
      if (!d.connected) {
        s.remote.error = d.error || 'connection failed';
      } else {
        s.remote.tables = d.tables || [];
        if (!s.remote.schema && s.engine === 'oracle') s.remote.schema = s.oracle.owner || '';
        if (!s.remote.schema && s.engine === 'hive') s.remote.schema = s.hive.database;
        await loadSchemas();
      }
    } catch (e) {
      s.remote.connected = false;
      s.remote.error = String(e.message || e);
    }
    render();
  }

  async function loadSchemas() {
    const s = state.source;
    if (s.engine === 'oracle') {
      try {
        const q = new URLSearchParams({
          engine: 'oracle',
          host: s.oracle.host,
          port: s.oracle.port,
          database: s.oracle.service,
          credential_ref: s.oracle.credential_ref,
        });
        const d = await api(`/api/agents/source/schemas?${q}`);
        s.remote.schemas = d.schemas || [];
      } catch (_) { s.remote.schemas = [s.oracle.service].filter(Boolean); }
    }
  }

  async function loadRemoteTables() {
    const s = state.source;
    try {
      const d = await jpost('/api/agents/source/tables', {
        ...sourceTestBody(),
        schema_name: s.remote.schema || s.oracle.owner || s.hive.database,
      });
      s.remote.tables = d.tables || [];
    } catch (e) {
      s.remote.error = String(e.message || e);
    }
    render();
  }

  async function uploadSamples(input) {
    const el = input || uploadInputEl;
    if (!el || !el.files || !el.files.length) return;
    if (!state.source.sessionId) await newSourceSession();
    const fd = new FormData();
    [...el.files].forEach((f) => fd.append('files', f));
    try {
      const r = await fetch(`/api/agents/source/upload?session_id=${encodeURIComponent(state.source.sessionId)}`, { method: 'POST', body: fd });
      const text = await r.text();
      if (!r.ok) throw new Error(text || r.statusText);
      const d = JSON.parse(text);
      applySampleWorkspace(d);
      (d.tables || []).forEach((tbl) => state.source.local.selected.add(tbl));
    } catch (e) {
      alert(String(e.message || e));
    }
    render();
  }

  async function deleteSampleFile(name) {
    if (!state.source.sessionId || !name) return;
    try {
      const r = await fetch(
        `/api/agents/source/samples/${encodeURIComponent(name)}?session_id=${encodeURIComponent(state.source.sessionId)}`,
        { method: 'DELETE' },
      );
      if (!r.ok) throw new Error(await r.text() || r.statusText);
      const d = await r.json();
      applySampleWorkspace(d);
      const names = new Set(state.source.local.tables);
      state.source.local.selected = new Set([...state.source.local.selected].filter((t) => names.has(t)));
    } catch (e) {
      alert(String(e.message || e));
    }
    render();
  }

  async function clearSourceWorkspace() {
    try {
      const d = await jpost('/api/agents/source/samples/clear', {
        session_id: state.source.sessionId || '',
        new_session: true,
      });
      applySampleWorkspace(d);
      state.source.local.selected = new Set();
    } catch (e) {
      alert(String(e.message || e));
    }
    render();
  }

  async function openPackEditor() {
    await loadPacks();
    const list = state.packs.all.length ? state.packs.all
      : state.packs.policy.map((n) => ({ name: n, kind: 'classification' }))
        .concat([{ name: 'defaults', kind: 'defaults' }]);
    state.packEditor = { name: 'defaults', kind: 'defaults', text: '', packList: list };
    await selectPack('defaults', 'defaults');
    render();
  }

  async function selectPack(name, kind) {
    if (!state.packEditor) return;
    state.packEditor.name = name;
    state.packEditor.kind = kind || 'classification';
    try {
      if (kind === 'defaults') {
        const d = await api('/api/agents/defaults');
        state.packEditor.text = d.text || '';
      } else {
        const d = await api(`/api/agents/packs/${encodeURIComponent(name)}`);
        state.packEditor.text = d.text || '';
      }
    } catch (e) {
      state.packEditor.text = kind === 'defaults'
        ? '# Run defaults\nversion: "1.0.0"\nname: defaults\n'
        : `# ${name}\nversion: "1.0.0"\nname: ${name}\n`;
    }
    render();
  }

  async function savePack() {
    const pe = state.packEditor;
    if (!pe) return;
    try {
      if (pe.kind === 'defaults') {
        const r = await fetch('/api/agents/defaults', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: pe.text }),
        });
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        applyDefaultsToState(d.defaults || {});
      } else {
        const r = await fetch(`/api/agents/packs/${encodeURIComponent(pe.name)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: pe.text }),
        });
        if (!r.ok) throw new Error(await r.text());
      }
      await loadPacks();
      await rebuildFromIcons();
      state.packEditor = null;
      render();
    } catch (e) {
      alert(String(e.message || e));
    }
  }

  async function createPack() {
    const name = prompt('New classification pack name (a-z, 0-9, _-):', 'my_pack');
    if (!name) return;
    state.packEditor = state.packEditor || { packList: [] };
    await selectPack(name.trim().toLowerCase(), 'classification');
    if (!state.packEditor.text.trim()) {
      state.packEditor.text = `# ${name}\nversion: "1.0.0"\nname: ${name.trim().toLowerCase()}\ndescription: Custom pack\n\ndomains:\n  DataSensitivity:\n    tags:\n      PII:\n        attributes: {}\n`;
    }
    render();
  }

  async function openModelEditor() {
    try {
      const reg = await api('/api/llm-providers/registry');
      const name = state.provider || 'demo';
      state.modelEditor = {
        name,
        path: reg.path,
        registry: reg.providers || {},
        config: { ...(reg.providers || {})[name] },
      };
    } catch (e) {
      alert(String(e.message || e));
    }
    render();
  }

  async function saveModelEditor() {
    const me = state.modelEditor;
    if (!me) return;
    const apiBaseErr = validateApiBase((me.config || {}).api_base, me.name);
    if (apiBaseErr) {
      alert(apiBaseErr);
      return;
    }
    try {
      const registry = { ...(me.registry || {}) };
      registry[me.name] = { ...(me.config || {}) };
      const r = await fetch('/api/llm-providers/registry', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ providers: registry }),
      });
      if (!r.ok) {
        let detail = await r.text();
        try {
          const j = JSON.parse(detail);
          detail = j.detail || detail;
        } catch (_) { /* keep raw text */ }
        throw new Error(detail);
      }
      await loadProviders();
      state.modelEditor = null;
      await validateProvider();
      render();
    } catch (e) {
      alert(String(e.message || e));
    }
  }

  async function startRun() {
    if (!state.pipeline) return;
    const tables = [...currentSelectedTables()];
    if (!tables.length) {
      showSendNotice('Select at least one source table (upload samples or load from a connection).');
      render();
      return;
    }
    state.sendNotice = '';
    state.run = { id: '', status: 'queued', tables: [], steps: [], error: '', artifacts: { items: [], loaded: false, loading: false, error: '' } };
    refreshFeed();
    try {
      const d = await jpost('/api/agents/batch', {
        pipeline: state.pipeline,
        tables,
        sample_dir: state.source.sampleDir || '',
        sample_session_id: state.source.sessionId || '',
        source: sourceTestBody(),   // live connection so remote scans hit real data
      });
      state.run.id = d.run_id || '';
      persist();
      if (window.REDIBIS_AGENTS && window.REDIBIS_AGENTS.setRunIdQuiet && state.run.id) {
        window.REDIBIS_AGENTS.setRunIdQuiet(state.run.id);
      }
      connectDebug();
      ensureRunArtifacts().catch(() => {});
      pollRun();
    } catch (e) {
      state.run.status = 'failed';
      state.run.error = String(e.message || e);
      refreshFeed();
    }
  }

  async function pollRun() {
    if (!state.run || !state.run.id || state.polling) return;
    state.polling = true;
    try {
      while (state.run && state.run.id) {
        let detail = null;
        try { detail = await api(`/api/agents/runs/${encodeURIComponent(state.run.id)}`); } catch (_) { /* transient */ }
        if (detail) {
          state.run.status = detail.status || state.run.status;
          state.run.steps = detail.steps || [];
          state.run.error = detail.error || '';
          if (detail.logs) state.run.logs = detail.logs;
          if (detail.session_id) state.run.sessionId = detail.session_id;  // agent session (for handoff)
        }
        try {
          const td = await api(`/api/agents/runs/${encodeURIComponent(state.run.id)}/tables`);
          state.run.tables = td.tables || [];
        } catch (_) { /* transient */ }
        refreshFeed();
        refreshDebugFromRun();
        ensureRunArtifacts().catch(() => {});
        if (detail && RUN_DONE(detail.status)) {
          await ensureRunArtifacts(true);
          closeDebugStream();
          break;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    } finally {
      state.polling = false;
    }
  }

  async function cancelRun() {
    if (!state.run || !state.run.id) return;
    try { await jpost(`/api/agents/runs/${encodeURIComponent(state.run.id)}/cancel`, {}); } catch (e) { alert(String(e.message || e)); }
  }

  async function handoffTable(table) {
    if (!state.run || !state.run.id) return;
    try {
      const d = await jpost('/api/agents/handoff', { run_id: state.run.id, table });
      const url = d.redirect_url || (d.session_id ? `/?session=${encodeURIComponent(d.session_id)}` : '');
      if (url) window.open(url, '_blank');
      else alert('Handoff session created' + (d.session_id ? `: ${d.session_id}` : ''));
    } catch (e) {
      alert(String(e.message || e));
    }
  }

  function openInDashboard() {
    if (!(window.REDIBIS_AGENTS && window.REDIBIS_AGENTS.setView)) return;
    if (state.run && state.run.id && window.REDIBIS_AGENTS.setRunId) {
      window.REDIBIS_AGENTS.setRunId(state.run.id);  // explicit navigation only
    } else {
      window.REDIBIS_AGENTS.setView('dashboard');
    }
  }

  async function viewReport(table) {
    const run = state.run;
    if (!run || !run.id || !table) return;
    if (run.open === table) { run.open = ''; refreshFeed(); return; }
    run.open = table;
    run.summaries = run.summaries || {};
    refreshFeed();
    ensureRunArtifacts().catch(() => {});
    try {
      run.summaries[table] = await api(`/api/agents/runs/${encodeURIComponent(run.id)}/tables/${encodeURIComponent(table)}/summary`);
    } catch (e) {
      run.summaries[table] = { _error: String(e.message || e) };
    }
    refreshFeed();
  }

  /* update only the feed region (during polling) without disturbing the dock */
  function refreshFeed() {
    const feed = rootEl && rootEl.querySelector('.cmp-feed');
    if (!feed) { render(); return; }
    feed.innerHTML = feedHTML();
    bindFeed();
    if (state.codegen) mountMonaco();
  }

  function bindFeed() {
    if (!rootEl) return;
    const fa = {
      'cancel-run': cancelRun,
      'open-dashboard': () => openInDashboard(),
      'view-report': (b) => viewReport(b.dataset.table || ''),
      'handoff': (b) => handoffTable(b.dataset.table || ''),
    };
    rootEl.querySelectorAll('[data-feed-act]').forEach((b) => {
      const f = fa[b.dataset.feedAct];
      if (f) b.onclick = () => f(b);
    });
    // codegen buttons live in the feed too — rebind after a feed refresh
    const cgActs = {
      'cg-force': () => requestCode(true),
      'cg-zip': downloadZip,
      'cg-download': downloadFile,
      'cg-register': registerCodegenTool,
    };
    rootEl.querySelectorAll('.cmp-feed [data-act]').forEach((b) => {
      const f = cgActs[b.dataset.act];
      if (f) b.onclick = f;
    });
    rootEl.querySelectorAll('[data-file]').forEach((b) => b.onclick = () => { state.codegen._active = +b.dataset.file; refreshFeed(); });
  }

  /* ── event binding ──────────────────────────────────────────────────────── */

  function bindRootClicks() {
    if (!rootEl || rootClickBound) return;
    rootClickBound = true;
    rootEl.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-act="send"]');
      if (btn) {
        ev.preventDefault();
        send();
      }
    });
  }

  function bind() {
    bindRootClicks();
    rootEl.querySelectorAll('[data-action]').forEach((b) => {
      b.onclick = async () => {
        const k = b.dataset.action;
        if (state.active.has(k)) state.active.delete(k); else state.active.add(k);
        const a = ACTIONS.find((x) => x.kind === k);
        if (a && a.config && state.active.has(k)) {
          state.configTab = k;
          state.showConfig = true;
        }
        await rebuildFromIcons();
        persist();
        render();
      };
    });
    rootEl.querySelectorAll('[data-tab]').forEach((b) => b.onclick = () => { state.configTab = b.dataset.tab; render(); });
    rootEl.querySelectorAll('[data-engine]').forEach((b) => b.onclick = async () => {
      state.source.engine = b.dataset.engine;
      state.source.remote.connected = false;
      await rebuildFromIcons();
      render();
    });
    rootEl.querySelectorAll('[data-scope]').forEach((b) => b.onclick = async () => {
      state.source.scope = b.dataset.scope;
      await rebuildFromIcons();
    });
    rootEl.querySelectorAll('[data-file]').forEach((b) => b.onclick = () => { state.codegen._active = +b.dataset.file; render(); });
    rootEl.querySelectorAll('[data-sel]').forEach((s) => s.onchange = async () => {
      const id = s.dataset.sel;
      let revalidated = false;
      if (id === 'provider') { state.provider = s.value; await validateProvider(); revalidated = true; }
      if (id === 'model') { state.model = s.value; await validateProvider(); revalidated = true; }
      if (id === 'policy') state.policyPack = s.value;
      if (id === 'classification') state.classificationPack = s.value;
      await rebuildFromIcons();
      persist();
      if (revalidated) render();  // refresh the Setup status chip + Send readiness
    });
    const recipeSel = rootEl.querySelector('[data-recipe]');
    if (recipeSel) recipeSel.onchange = () => { if (recipeSel.value) applyRecipe(recipeSel.value); };
    const extraNotes = rootEl.querySelector('[data-input="extraNotes"]');
    if (extraNotes) {
      extraNotes.oninput = () => { state.extraNotes = extraNotes.value; persist(); };
    }
    const packText = rootEl.querySelector('[data-input="pack-text"]');
    if (packText) packText.oninput = () => { if (state.packEditor) state.packEditor.text = packText.value; };
    rootEl.querySelectorAll('[data-cfg]').forEach((el) => {
      el.onchange = async () => {
        setCfg(el.dataset.cfg, el.type === 'checkbox' ? el.checked : el.value);
        await rebuildFromIcons();
      };
      if (el.tagName === 'INPUT' && el.type !== 'checkbox') {
        el.oninput = () => {
          setCfg(el.dataset.cfg, el.value);
          scheduleRebuild();
        };
      }
    });
    rootEl.querySelectorAll('[data-localtbl]').forEach((el) => el.onchange = async () => {
      toggleSet(state.source.local.selected, el.dataset.localtbl, el.checked);
      await rebuildFromIcons();
    });
    rootEl.querySelectorAll('[data-remotetbl]').forEach((el) => el.onchange = async () => {
      toggleSet(state.source.remote.selected, el.dataset.remotetbl, el.checked);
      await rebuildFromIcons();
    });

    rootEl.querySelectorAll('[data-pack]').forEach((b) => b.onclick = () => selectPack(b.dataset.pack, b.dataset.packKind));
    rootEl.querySelectorAll('[data-model]').forEach((el) => {
      el.oninput = () => {
        if (state.modelEditor) state.modelEditor.config[el.dataset.model] = el.value;
      };
    });

    const act = {
      'toggle-debug': () => {
        state.debug = !state.debug;
        persist();
        render();
        if (state.debug) connectDebug();
      },
      'debug-clear': () => { state.debugLogs = []; const b = document.getElementById('cmp-debug-body'); if (b) b.textContent = ''; },
      'toggle-config': () => {
        state.showConfig = !state.showConfig;
        if (state.showConfig && hasConfigTabs() && !state.configTab) {
          state.configTab = ACTIONS.find((a) => a.config && state.active.has(a.kind))?.kind || 'source';
        }
        persist();
        render();
      },
      'src-test': testConnection,
      'src-load-tables': loadRemoteTables,
      'src-upload': () => ensureUploadInput().click(),
      'src-upload-folder': () => ensureFolderUploadInput().click(),
      'src-new': () => newSourceSession().then(() => render()),
      'src-clear': () => clearSourceWorkspace(),
      'src-del': (ev) => { deleteSampleFile(ev.currentTarget.dataset.file); },
      'cg-force': () => requestCode(true),
      'cg-zip': downloadZip,
      'cg-download': downloadFile,
      'cg-register': registerCodegenTool,
      'edit-packs': openPackEditor,
      'edit-model': openModelEditor,
      'pack-save': savePack,
      'pack-close': () => { state.packEditor = null; render(); },
      'pack-new': createPack,
      'model-save': saveModelEditor,
      'model-close': () => { state.modelEditor = null; render(); },
    };
    rootEl.querySelectorAll('[data-act]').forEach((b) => {
      const f = act[b.dataset.act];
      if (f) b.onclick = f;
    });
    bindFeed();
  }

  function setCfg(path, val) {
    const parts = path.split('.');
    if (parts[0] === 'source' && parts[1] === 'sampleRows') state.source.sampleRows = val;
    else if (parts[0] === 'remote' && parts[1] === 'schema') state.source.remote.schema = val;
    else if (parts[0] === 'oracle' || parts[0] === 'hive') state.source[parts[0]][parts[1]] = val;
    else if (parts[0] === 'pii' || parts[0] === 'quality' || parts[0] === 'classify' || parts[0] === 'contract' || parts[0] === 'publish') {
      state.cfg[parts[0]][parts[1]] = val;
    }
  }

  let rebuildTimer = null;
  function scheduleRebuild() {
    clearTimeout(rebuildTimer);
    rebuildTimer = setTimeout(() => rebuildFromIcons(), 400);
  }

  function toggleSet(set, key, on) { if (on) set.add(key); else set.delete(key); }

  function downloadFile() {
    blobDownload(currentFile(), currentCode());
  }

  async function downloadZip() {
    const files = state.codegen.files || {};
    if (window.JSZip) {
      const zip = new window.JSZip();
      Object.entries(files).forEach(([n, c]) => zip.file(n, c));
      const blob = await zip.generateAsync({ type: 'blob' });
      blobDownload('generated-code.zip', blob, true);
      return;
    }
    Object.entries(files).forEach(([n, c]) => blobDownload(n, c));
  }

  function blobDownload(name, content, isBlob) {
    const a = document.createElement('a');
    a.href = isBlob ? URL.createObjectURL(content) : URL.createObjectURL(new Blob([content], { type: 'text/plain' }));
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function registerCodegenTool() {
    const cg = state.codegen;
    if (!cg || !cg.raw) { alert('No codegen result'); return; }
    const name = prompt('Tool name (registered, not executed until approved):', 'custom_codegen_tool');
    if (!name) return;
    const approvedBy = prompt('Your name (required for registration):', '');
    if (!approvedBy) return;
    try {
      await jpost('/api/agents/dynamic-tools/register', {
        name,
        description: fullIntent().slice(0, 200),
        approved_by: approvedBy,
        source_path: cg.raw.artifact_path || '',
      });
      alert('Tool registered (sandboxed — not executed).');
    } catch (e) {
      alert(String(e.message || e));
    }
  }

  /* ── mount ──────────────────────────────────────────────────────────────── */

  async function rehydrateRun() {
    const rid = state._restoredRunId;
    if (!rid || (state.run && state.run.id)) return;
    try {
      const detail = await api(`/api/agents/runs/${encodeURIComponent(rid)}`);
      const td = await api(`/api/agents/runs/${encodeURIComponent(rid)}/tables`);
      state.run = {
        id: rid,
        status: detail.status || 'running',
        tables: td.tables || [],
        steps: detail.steps || [],
        error: detail.error || '',
        sessionId: detail.session_id || '',
        artifacts: { items: [], loaded: false, loading: false, error: '' },
        summaries: {},
        logs: detail.logs || [],
      };
      if (!RUN_DONE(state.run.status)) {
        connectDebug();
        pollRun();
      } else {
        await ensureRunArtifacts(true);
      }
      refreshFeed();
    } catch (_) {
      state._restoredRunId = '';
    }
  }

  async function init() {
    rootEl = document.getElementById('view-composer');
    if (!rootEl) return;
    ensureUploadInput();
    ensureFolderUploadInput();
    restore();
    await Promise.all([loadProviders(), loadPacks(), loadDefaults(), loadRecipes()]);
    await newSourceSession();
    state.pipeline = buildSpecFromState();
    render();
    await validateProvider();
    if (!state.providerReady) { state.showConfig = true; state.configTab = 'setup'; }
    await compilePrompt();
    await validatePipeline();
    await rehydrateRun();
    render();
  }

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);

  window.RedibisComposer = { init, state, buildSpecFromState, syncFromSpec };
})();
