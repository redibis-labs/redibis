(function () {
  'use strict';

  const state = { runtime: null, global: {}, providers: [] };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

  async function api(path, options) {
    const response = await fetch(path, options);
    const text = await response.text();
    let body = {};
    try { body = text ? JSON.parse(text) : {}; } catch (_) { body = { detail: text }; }
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  }

  function optionRows(selected) {
    const names = state.providers.map((p) => p.name).filter(Boolean);
    if (selected && !names.includes(selected)) names.unshift(selected);
    return ['<option value="">Heuristic / none</option>'].concat(
      names.map((name) => `<option value="${esc(name)}"${name === selected ? ' selected' : ''}>${esc(name)}</option>`),
    ).join('');
  }

  function renderDefaults() {
    const llm = state.global.llm_defaults || {};
    const agentic = state.global.agentic_defaults || {};
    $('llmProvider').innerHTML = optionRows(llm.provider || '');
    $('plannerProvider').innerHTML = optionRows(agentic.planner_provider || '');
    $('llmModel').value = llm.model || '';
    $('plannerModel').value = agentic.planner_model || '';
    $('llmEnabled').checked = Boolean(llm.enabled);
    const pii = state.global.pii || {};
    const ui = state.global.ui_prefs || {};
    $('piiSampleSize').value = pii.sample_size || 100;
    $('scanWaitTimeout').value = ui.scan_wait_timeout_sec || 360;
    $('sseTimeout').value = ui.sse_ping_timeout_sec || 30;
  }

  function renderProviders() {
    const root = $('providerList');
    if (!state.providers.length) {
      root.innerHTML = '<div class="call-empty">No providers configured.</div>';
      return;
    }
    root.innerHTML = state.providers.map((provider) => {
      const model = provider.default_model_bare || provider.model || 'model selected at run time';
      const source = provider.source || (provider.editable ? 'custom' : 'packaged');
      const key = provider.needs_key
        ? (provider.api_key_env_set ? 'credential ready' : `needs ${provider.api_key_env || 'credential'}`)
        : 'no key required';
      return `<div class="provider-row"><strong>${esc(provider.name)}</strong>`
        + `<div class="settings-muted">${esc(provider.description || '')}</div>`
        + `<div class="provider-meta"><span class="settings-chip">${esc(model)}</span>`
        + `<span class="settings-chip">${esc(source)}</span><span class="settings-chip">${esc(key)}</span></div></div>`;
    }).join('');
  }

  function displayValue(value) {
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    if (value === '' || value == null) return 'not set';
    return String(value);
  }

  function renderRuntime() {
    const runtime = state.runtime || {};
    const sections = runtime.sections || {};
    const hasGlobals = ['planner', 'enrichment'].some((name) =>
      sections[name] && sections[name].source === 'global_settings');
    $('settingsSource').textContent = runtime.config_source === 'REDIBIS_CONFIG'
      ? 'Deployment config active'
      : (hasGlobals ? 'Global settings + built-in runtime' : 'Built-in defaults');
    const root = $('runtimeSections');
    const plannerOwnedByDeployment = sections.planner && sections.planner.source === 'redibis_config';
    ['plannerProvider', 'plannerModel'].forEach((id) => {
      const field = $(id);
      if (field) {
        field.disabled = plannerOwnedByDeployment;
        field.title = plannerOwnedByDeployment
          ? 'This value is controlled by REDIBIS_CONFIG and requires a restart.'
          : '';
      }
    });
    root.innerHTML = Object.entries(sections).map(([name, values]) => {
      const restart = Boolean(values.restart_required);
      const rows = Object.entries(values)
        .filter(([key]) => !['editable', 'restart_required'].includes(key))
        .map(([key, value]) => `<div class="runtime-row"><span class="runtime-key">${esc(key.replace(/_/g, ' '))}</span>`
          + `<span class="runtime-value">${esc(displayValue(value))}</span></div>`).join('');
      return `<article class="runtime-card"><div class="settings-card-head"><h3>${esc(name)}</h3>`
        + `<span class="settings-chip ${restart ? 'restart' : 'hot'}">${restart ? 'Restart required' : 'Hot setting'}</span></div>${rows}</article>`;
    }).join('');
  }

  async function saveDefaults() {
    const status = $('saveStatus');
    status.textContent = 'Saving…';
    try {
      const body = {
        settings: {
          llm_defaults: {
            provider: $('llmProvider').value,
            model: $('llmModel').value.trim(),
            enabled: $('llmEnabled').checked,
          },
          agentic_defaults: {
            planner_mode: $('plannerProvider').value ? 'llm' : 'heuristic',
            planner_provider: $('plannerProvider').value,
            planner_model: $('plannerModel').value.trim(),
          },
        },
      };
      const result = await api('/api/settings/global', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      state.global = result.settings || state.global;
      state.runtime = await api('/api/settings/runtime');
      renderRuntime();
      status.textContent = 'Saved.';
    } catch (error) {
      status.textContent = `Save failed: ${error.message}`;
    }
  }

  async function saveScanPreferences() {
    const status = $('scanSaveStatus');
    status.textContent = 'Saving…';
    try {
      const body = {
        settings: {
          pii: { sample_size: Number($('piiSampleSize').value || 100) },
          ui_prefs: {
            scan_wait_timeout_sec: Number($('scanWaitTimeout').value || 360),
            sse_ping_timeout_sec: Number($('sseTimeout').value || 30),
          },
        },
      };
      const result = await api('/api/settings/global', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      state.global = result.settings || state.global;
      status.textContent = 'Saved.';
    } catch (error) {
      status.textContent = `Save failed: ${error.message}`;
    }
  }

  async function testProvider() {
    const status = $('saveStatus');
    const usePlanner = Boolean($('plannerProvider').value);
    const name = usePlanner ? $('plannerProvider').value : $('llmProvider').value;
    const model = usePlanner ? $('plannerModel').value.trim() : $('llmModel').value.trim();
    if (!name) {
      status.textContent = 'Select an LLM provider first.';
      return;
    }
    status.textContent = `Testing ${name}…`;
    try {
      const result = await api(`/api/llm-providers/${encodeURIComponent(name)}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      status.textContent = result.ok === false ? `Probe failed: ${result.error || 'unknown error'}` : 'Provider probe passed.';
    } catch (error) {
      status.textContent = `Probe failed: ${error.message}`;
    }
  }

  async function saveCustomProvider() {
    const status = $('providerSaveStatus');
    const name = $('customProviderName').value.trim().toLowerCase();
    const litellmModel = $('customProviderModel').value.trim();
    if (!name || !litellmModel) {
      status.textContent = 'Registry name and LiteLLM model are required.';
      return;
    }
    status.textContent = 'Saving…';
    try {
      await api('/api/llm/providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          description: `Custom provider ${name}`,
          profile_type: 'openai_compatible',
          litellm_model: litellmModel,
          model: litellmModel.includes('/') ? litellmModel.split('/').slice(1).join('/') : litellmModel,
          model_prefix: litellmModel.includes('/') ? litellmModel.split('/')[0] : 'openai',
          api_base: $('customProviderBase').value.trim() || null,
          api_key_env: $('customProviderKeyEnv').value.trim(),
          supports_json: true,
          residency: $('customProviderResidency').value,
          params: {},
          known_models: [],
        }),
      });
      const providerData = await api('/api/llm-providers');
      state.providers = providerData.providers || [];
      renderProviders();
      renderDefaults();
      status.textContent = 'Provider saved.';
    } catch (error) {
      status.textContent = `Save failed: ${error.message}`;
    }
  }

  async function loadCalls() {
    const root = $('llmCalls');
    root.innerHTML = '<div class="call-empty">Loading…</div>';
    try {
      const result = await api('/api/llm/calls?limit=25');
      const calls = result.calls || [];
      root.innerHTML = calls.length ? calls.map((call) =>
        `<div class="call-row"><strong>${esc(call.provider || '-')}</strong><span>${esc(call.model || '-')}</span>`
        + `<span>${esc(call.status || '-')}</span><span>${esc(call.latency_ms == null ? '-' : `${Math.round(call.latency_ms)} ms`)}</span></div>`
      ).join('') : '<div class="call-empty">No model calls recorded in this process.</div>';
    } catch (error) {
      root.innerHTML = `<div class="call-empty">Could not load calls: ${esc(error.message)}</div>`;
    }
  }

  async function init() {
    try {
      const [runtime, global, providerData] = await Promise.all([
        api('/api/settings/runtime'), api('/api/settings/global'), api('/api/llm-providers'),
      ]);
      state.runtime = runtime;
      state.global = global;
      state.providers = providerData.providers || [];
      renderDefaults();
      renderProviders();
      renderRuntime();
      loadCalls();
    } catch (error) {
      $('settingsNotice').hidden = false;
      $('settingsNotice').textContent = `Settings failed to load: ${error.message}`;
    }
    $('saveDefaults').onclick = saveDefaults;
    $('saveScanPrefs').onclick = saveScanPreferences;
    $('testProvider').onclick = testProvider;
    $('saveCustomProvider').onclick = saveCustomProvider;
    $('refreshCalls').onclick = loadCalls;
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
