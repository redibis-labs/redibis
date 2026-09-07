/**
 * Shared run-centric Evidence Review explorer.
 *
 * Reused (unchanged manual-scan behavior) across the standalone /review page,
 * the v2 "Evidence Review" tab, agent results, and the enterprise reports
 * addon. Dependency-light: plain DOM, no framework required.
 *
 * Public API:
 *   EvidenceReviewer.mount(container, reviewData, opts)
 *     Render one already-fetched review DTO (either the run-scoped
 *     ``EvidenceReviewService.from_bundle`` shape, or the legacy Final
 *     Review checkpoint shape — both are normalized transparently).
 *   EvidenceReviewer.mountRunExplorer(container, { table, runId, apiBase, readOnly })
 *     Self-driving explorer: run selector/timeline + phase tabs, backed by
 *     the /api/evidence/{table}/runs* endpoints.
 *   EvidenceReviewer.fetchAndMount(container, url, opts)
 *     Back-compat: fetch one URL and mount() the result.
 */
(function (global) {
  "use strict";

  // ── helpers ────────────────────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtNum(n) {
    if (n == null || n === "") return "";
    var v = Number(n);
    if (Number.isNaN(v)) return String(n);
    return (Math.abs(v) < 1 && v !== 0) ? v.toFixed(3) : String(Math.round(v * 1000) / 1000);
  }

  function fmtBytes(n) {
    var v = Number(n) || 0;
    if (v < 1024) return v + " B";
    if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
    return (v / 1024 / 1024).toFixed(1) + " MB";
  }

  var STYLE_INJECTED = false;
  function injectStyle() {
    if (STYLE_INJECTED || typeof document === "undefined") return;
    STYLE_INJECTED = true;
    var css = "" +
      ".er-root{font-family:Inter,system-ui,-apple-system,sans-serif;color:#0f172a;font-size:13px}" +
      ".er-header{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin-bottom:12px}" +
      ".er-header h2{font-size:15px;margin:0 0 6px}" +
      ".er-runbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px}" +
      ".er-runbar select,.er-runbar input{font:inherit;border:1px solid #e2e8f0;border-radius:8px;padding:5px 8px}" +
      ".er-meta{font-size:.85em;color:#64748b}" +
      ".er-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}" +
      ".er-badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.78em;font-weight:600}" +
      ".er-b-evaluated{background:#dcfce7;color:#166534}" +
      ".er-b-skipped{background:#f1f5f9;color:#475569}" +
      ".er-b-error{background:#fee2e2;color:#991b1b}" +
      ".er-b-not_run{background:#f1f5f9;color:#94a3b8}" +
      ".er-engine{background:#eef2ff;color:#3730a3}" +
      ".er-steward{background:#dcfce7;color:#166534}" +
      ".er-supplied{background:#fef3c7;color:#92400e}" +
      ".er-stale-badge{background:#fee2e2;color:#991b1b}" +
      ".er-tabs{display:flex;gap:4px;border-bottom:1px solid #e2e8f0;margin:10px 0;flex-wrap:wrap}" +
      ".er-tab{padding:7px 12px;cursor:pointer;border-bottom:2px solid transparent;color:#64748b;font-weight:600;font-size:.9em}" +
      ".er-tab.active{color:#2563eb;border-color:#2563eb}" +
      ".er-search{width:100%;max-width:320px;margin-bottom:8px}" +
      ".er-table{width:100%;border-collapse:collapse}" +
      ".er-table th{text-align:left;font-size:.72em;text-transform:uppercase;color:#64748b;padding:6px 8px;border-bottom:2px solid #e2e8f0}" +
      ".er-table td{padding:6px 8px;border-bottom:1px solid #eef2f6;vertical-align:top}" +
      ".er-row{cursor:pointer}" +
      ".er-row:hover{background:#f8fafc}" +
      ".er-row.er-stale{background:#fffbe6}" +
      ".er-drawer{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin:4px 0 12px}" +
      ".er-drawer h4{font-size:.8em;text-transform:uppercase;color:#64748b;margin:8px 0 4px}" +
      ".er-drawer h4:first-child{margin-top:0}" +
      ".er-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}" +
      ".er-mini-card{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:8px}" +
      ".er-mini-card .er-title{font-weight:700;font-size:.85em}" +
      ".er-pre{background:#0f172a;color:#e2e8f0;padding:10px;border-radius:8px;overflow:auto;font-size:.78em;max-height:280px;white-space:pre-wrap}" +
      ".er-btn{font:inherit;border:1px solid #e2e8f0;background:#fff;border-radius:8px;padding:5px 10px;cursor:pointer;font-weight:600;font-size:.85em}" +
      ".er-btn:hover{background:#f8fafc}" +
      ".er-btn-danger{border-color:#fca5a5;color:#991b1b}" +
      ".er-btn-restricted{border-color:#f59e0b;color:#92400e}" +
      ".er-artifact-row{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #eef2f6}" +
      ".er-empty{color:#64748b;font-style:italic;padding:16px;text-align:center}" +
      ".er-restricted-form{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}" +
      ".er-restricted-form input{font:inherit;border:1px solid #e2e8f0;border-radius:6px;padding:4px 7px}" +
      "";
    var tag = document.createElement("style");
    tag.setAttribute("data-evidence-reviewer", "1");
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  // ── badges ─────────────────────────────────────────────────────────────
  function sourceBadge(source) {
    var cls = "er-engine";
    if (source === "steward") cls = "er-steward";
    if (source === "supplied") cls = "er-supplied";
    return "<span class='er-badge " + cls + "'>" + esc(source || "engine") + "</span>";
  }

  function coverageBadge(entry) {
    entry = entry || {};
    var status = entry.status || "not_run";
    var cls = "er-b-" + status;
    var label = status.replace(/_/g, " ");
    var title = esc(entry.reason || entry.error || "");
    return "<span class='er-badge " + cls + "' title='" + title + "'>" + esc(label) + "</span>";
  }

  function driftBadge(drift) {
    drift = drift || {};
    if (drift.state === "stale") {
      return "<span class='er-badge er-stale-badge' title='" +
        esc((drift.reasons || []).join(", ")) + "'>stale</span>";
    }
    if (drift.state && drift.state !== "none") {
      return "<span class='er-badge er-b-not_run'>" + esc(drift.state) + "</span>";
    }
    return "";
  }

  // ── legacy-shape adapter (Final Review checkpoint response) ─────────────
  function normalizeReviewData(data) {
    if (!data || !data.columns || !data.columns.length) return data;
    var first = data.columns[0];
    if (first.effective_verdict) return data;
    return {
      table: data.table,
      run_id: data.run_id || "",
      header: data.header || {},
      columns: data.columns.map(function (c) {
        var pii = c.pii || {};
        return {
          column: c.column,
          review_status: (c.review && c.review.status) || "pending",
          review: c.review || {},
          engine_proposal: {
            detected: !!pii.flag,
            entity_type: pii.entity_type,
            confidence: pii.confidence,
          },
          effective_verdict: {
            detected: !!pii.flag,
            entity_type: pii.entity_type,
            confidence: pii.confidence,
            source: "contract",
            authority_reason: "active_contract",
          },
          drift: { state: "none", reasons: [] },
          coverage: {},
          engine_evidence: {},
          rules_fired: [],
          negative_signals_fired: [],
          quality: null,
          profile: null,
          profile_summary: {},
        };
      }),
      progress: data.progress || {
        total_columns: data.total_columns || data.columns.length,
        reviewed_count: data.approved_count || 0,
        status: data.fully_approved ? "reviewed" : "partially_reviewed",
      },
      artifacts: data.artifacts || {},
      llm_calls: data.llm_calls || [],
    };
  }

  // ── header ────────────────────────────────────────────────────────────
  function renderHeader(data) {
    var header = data.header || {};
    var prov = header.provenance || {};
    var html = "<div class='er-header'>";
    html += "<h2>" + esc(data.table || "") + "</h2>";
    html += "<div class='er-meta'>run: <strong>" + esc(data.run_id || "—") + "</strong>";
    if (data.mode) html += " · mode: " + esc(data.mode);
    if (data.schema_version) html += " · bundle schema " + esc(data.schema_version);
    if (data.review_schema_version) html += " · review schema " + esc(data.review_schema_version);
    if (data.run_status) html += " · status: " + esc(data.run_status);
    html += "</div>";
    if (prov.redibis_version || prov.redibis_git_sha) {
      html += "<div class='er-meta'>redibis " + esc(prov.redibis_version || "") +
        (prov.redibis_git_sha ? " (" + esc(String(prov.redibis_git_sha).slice(0, 10)) + ")" : "") +
        (prov.config_sha256 ? " · config " + esc(String(prov.config_sha256).slice(0, 10)) : "") +
        "</div>";
    }
    var packEntry = (prov.pack_stack && prov.pack_stack.packs && prov.pack_stack.packs[0]) || null;
    if (packEntry) {
      html += "<div class='er-meta'>pack: " + esc(packEntry.id || "") +
        (packEntry.version ? " v" + esc(packEntry.version) : "") +
        (prov.pack_stack.stack_uuid ? " (" + esc(String(prov.pack_stack.stack_uuid).slice(0, 8)) + ")" : "") + "</div>";
    }
    var p = data.progress || {};
    html += "<div class='er-badges'>";
    html += "<span class='er-badge er-b-evaluated'>" + (p.status || "unknown") + "</span>";
    html += "<span class='er-badge er-b-not_run'>" + (p.reviewed_count || 0) + "/" + (p.total_columns || 0) + " reviewed</span>";
    if (p.stale_count) html += "<span class='er-badge er-stale-badge'>" + p.stale_count + " stale</span>";
    var cov = data.coverage || {};
    ["profile", "quality", "pii", "llm", "agentic"].forEach(function (phase) {
      if (cov[phase]) html += coverageBadge(Object.assign({}, cov[phase], {})) ;
    });
    html += "</div>";
    if (data.errors && data.errors.length) {
      html += "<div class='er-meta' style='color:#991b1b;margin-top:6px'>" +
        data.errors.map(function (e) { return esc(e.phase || "") + ": " + esc(e.error || e.message || ""); }).join(" · ") +
        "</div>";
    }
    html += "</div>";
    return html;
  }

  // ── per-column drawer (all-phase detail) ─────────────────────────────────
  function renderEngineMatrix(col) {
    var ev = col.engine_evidence || {};
    var keys = Object.keys(ev);
    if (!keys.length) return "<p class='er-meta'>No per-engine evidence recorded.</p>";
    var html = "<div class='er-grid'>";
    keys.sort().forEach(function (name) {
      var e = ev[name] || {};
      var ran = e.ran !== false;
      var badgeCls = e.error ? "er-b-error" : (ran ? "er-b-evaluated" : "er-b-skipped");
      var badgeLbl = e.error ? "error" : (ran ? "ran" : "skipped");
      html += "<div class='er-mini-card'>";
      html += "<div class='er-title'>" + esc(name) + " <span class='er-badge " + badgeCls + "'>" + badgeLbl + "</span></div>";
      if (e.score != null) html += "<div class='er-meta'>score " + fmtNum(e.score) + "</div>";
      if (e.match_rate != null) html += "<div class='er-meta'>match rate " + fmtNum(e.match_rate) + "</div>";
      if (e.entity) html += "<div class='er-meta'>entity: " + esc(e.entity) + "</div>";
      if (e.label) html += "<div class='er-meta'>label: " + esc(e.label) + "</div>";
      if (e.rates && Object.keys(e.rates).length) {
        html += "<div class='er-meta'>" + Object.keys(e.rates).map(function (k) {
          return esc(k) + "=" + fmtNum(e.rates[k]);
        }).join(" · ") + "</div>";
      }
      if (e.hits && e.hits.length) html += "<div class='er-meta'>hits: " + e.hits.length + "</div>";
      if (e.version) html += "<div class='er-meta'>v" + esc(e.version) + "</div>";
      if (e.config_hash) html += "<div class='er-meta'>cfg " + esc(String(e.config_hash).slice(0, 10)) + "</div>";
      if (e.duration_ms != null) html += "<div class='er-meta'>" + esc(e.duration_ms) + " ms</div>";
      if (e.reason) html += "<div class='er-meta'>" + (ran ? "" : "skipped: ") + esc(e.reason) + "</div>";
      if (e.extra && Object.keys(e.extra).length) {
        html += "<div class='er-meta'>" + esc(JSON.stringify(e.extra)) + "</div>";
      }
      if (e.error) html += "<div class='er-meta' style='color:#991b1b'>" + esc(e.error) + "</div>";
      html += "</div>";
    });
    html += "</div>";
    return html;
  }

  function renderColumnDrawer(col) {
    var html = "<div class='er-drawer'>";

    html += "<h4>Effective vs engine</h4>";
    var ev = col.effective_verdict || {};
    var prop = col.engine_proposal || {};
    html += "<div class='er-grid'>";
    html += "<div class='er-mini-card'><div class='er-title'>Engine proposal</div>" +
      "<div>" + (prop.detected ? "PII" : "not PII") + (prop.entity_type ? " (" + esc(prop.entity_type) + ")" : "") + "</div></div>";
    html += "<div class='er-mini-card'><div class='er-title'>Effective " + sourceBadge(ev.source) + "</div>" +
      "<div>" + (ev.detected ? "PII" : "not PII") + (ev.entity_type ? " (" + esc(ev.entity_type) + ")" : "") + "</div>" +
      "<div class='er-meta'>" + esc(ev.authority_reason || "") + "</div></div>";
    html += "</div>";

    if (col.steward_decision || col.supplied_decision) {
      html += "<h4>Decisions</h4>";
      if (col.steward_decision) {
        html += "<div class='er-meta'>Steward: " + esc(col.steward_decision.status) +
          " by " + esc(col.steward_decision.decided_by || "?") +
          (col.steward_decision.reason ? " — " + esc(col.steward_decision.reason) : "") + "</div>";
      }
      if (col.supplied_decision) {
        html += "<div class='er-meta'>Supplied: " + esc(col.supplied_decision.status) + "</div>";
      }
    }

    if (col.drift && col.drift.reasons && col.drift.reasons.length) {
      html += "<h4>Drift</h4><div class='er-meta'>" + esc(col.drift.reasons.join(", ")) + "</div>";
    }

    html += "<h4>Profile</h4>";
    var pf = col.profile_summary || {};
    html += "<div class='er-meta'>class=" + esc(pf.inferred_class || col.inferred_class || "?") +
      " · null_rate=" + fmtNum(pf.null_rate) + " · distinct_rate=" + fmtNum(pf.distinct_rate) +
      " · logical=" + esc(col.logical_type || "") + " · physical=" + esc(col.physical_type || "") + "</div>";

    html += "<h4>Engines (regex / NER / phone / custom rules / LLM)</h4>";
    html += renderEngineMatrix(col);

    if (col.rules_fired && col.rules_fired.length) {
      html += "<h4>Rules fired</h4><div class='er-meta'>" + col.rules_fired.map(esc).join(", ") + "</div>";
    }
    if (col.negative_signals_fired && col.negative_signals_fired.length) {
      html += "<h4>Negative signals</h4><div class='er-meta'>" + col.negative_signals_fired.map(esc).join(", ") + "</div>";
    }
    if (col.quality) {
      html += "<h4>Quality</h4><pre class='er-pre'>" + esc(JSON.stringify(col.quality, null, 2)) + "</pre>";
    }
    if (col.validator_results && Object.keys(col.validator_results).length) {
      html += "<h4>Validators</h4><pre class='er-pre'>" + esc(JSON.stringify(col.validator_results, null, 2)) + "</pre>";
    }
    if (col.samples) {
      html += "<h4>Samples</h4><pre class='er-pre'>" + esc(JSON.stringify(col.samples, null, 2)) + "</pre>";
    }

    if (col._actions) html += col._actions;

    html += "</div>";
    return html;
  }

  // ── Overview tab: searchable column matrix ───────────────────────────────
  function renderOverviewTab(data, opts) {
    var cols = data.columns || [];
    var html = "<input type='text' class='er-search' placeholder='Filter columns…' data-er-search/>";
    html += "<table class='er-table'><thead><tr>" +
      "<th>Column</th><th>Type</th><th>Effective</th><th>Engine</th><th>Coverage</th><th>Review</th><th>Drift</th>" +
      "</tr></thead><tbody data-er-body>";
    cols.forEach(function (col) {
      var ev = col.effective_verdict || {};
      var prop = col.engine_proposal || {};
      var stale = (col.drift || {}).state === "stale";
      html += "<tr class='er-row" + (stale ? " er-stale" : "") + "' data-column='" + esc(col.column) + "'>";
      html += "<td class='er-mono'>" + esc(col.column) + "</td>";
      html += "<td>" + esc(col.logical_type || "") + "</td>";
      html += "<td>" + (ev.detected ? "PII" : "not PII") + (ev.entity_type ? " · " + esc(ev.entity_type) : "") +
        " " + sourceBadge(ev.source) + "</td>";
      html += "<td>" + (prop.detected ? "PII" : "not PII") + (prop.entity_type ? " · " + esc(prop.entity_type) : "") + "</td>";
      var covKeys = Object.keys(col.coverage || {});
      html += "<td>" + (covKeys.length
        ? covKeys.map(function (k) { return coverageBadge(col.coverage[k]); }).join(" ")
        : "") + "</td>";
      html += "<td>" + esc(col.review_status || "pending") + "</td>";
      html += "<td>" + driftBadge(col.drift) + "</td>";
      html += "</tr>";
      html += "<tr class='er-drawer-row' data-drawer-for='" + esc(col.column) + "' style='display:none'>" +
        "<td colspan='7'>" + renderColumnDrawer(withActions(col, data, opts)) + "</td></tr>";
    });
    html += "</tbody></table>";
    if (!cols.length) html += "<div class='er-empty'>No columns to review.</div>";
    return html;
  }

  function withActions(col, data, opts) {
    if (opts.readOnly || !opts.onApprove) return col;
    var out = Object.assign({}, col);
    out._actions = "<h4>Actions</h4><button type='button' class='er-btn er-act-approve' data-column='" +
      esc(col.column) + "'>Approve</button> " +
      "<button type='button' class='er-btn er-btn-danger er-act-reject' data-column='" +
      esc(col.column) + "'>Reject</button> " +
      "<button type='button' class='er-btn er-act-reset' data-column='" +
      esc(col.column) + "'>Reset</button>";
    return out;
  }

  function renderProfilingTab(data) {
    var cols = data.columns || [];
    var html = "<table class='er-table'><thead><tr><th>Column</th><th>Class</th><th>Null rate</th>" +
      "<th>Distinct rate</th><th>Logical</th><th>Physical</th></tr></thead><tbody>";
    cols.forEach(function (col) {
      var pf = col.profile_summary || {};
      html += "<tr><td class='er-mono'>" + esc(col.column) + "</td>" +
        "<td>" + esc(pf.inferred_class || col.inferred_class || "") + "</td>" +
        "<td>" + fmtNum(pf.null_rate) + "</td>" +
        "<td>" + fmtNum(pf.distinct_rate) + "</td>" +
        "<td>" + esc(col.logical_type || "") + "</td>" +
        "<td>" + esc(col.physical_type || "") + "</td></tr>";
    });
    html += "</tbody></table>";
    if (!cols.length) html += "<div class='er-empty'>No profiling data.</div>";
    return html;
  }

  function renderQualityTab(data) {
    var cols = (data.columns || []).filter(function (c) { return c.quality; });
    if (!cols.length) return "<div class='er-empty'>No quality rule results for this run.</div>";
    var html = "";
    cols.forEach(function (col) {
      html += "<div class='er-mini-card' style='margin-bottom:8px'>" +
        "<div class='er-title'>" + esc(col.column) + "</div>" +
        "<pre class='er-pre'>" + esc(JSON.stringify(col.quality, null, 2)) + "</pre></div>";
    });
    return html;
  }

  function renderPiiTab(data) {
    var cols = data.columns || [];
    var html = "<table class='er-table'><thead><tr><th>Column</th><th>Engines</th></tr></thead><tbody>";
    cols.forEach(function (col) {
      html += "<tr><td class='er-mono' style='white-space:nowrap'>" + esc(col.column) + "</td><td>" +
        renderEngineMatrix(col) + "</td></tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function llmRestrictedForm(table, runId, callId) {
    var id = "er-restrict-" + Math.random().toString(36).slice(2);
    return "<div>" +
      "<button type='button' class='er-btn er-btn-restricted' data-toggle-restricted='" + id + "'>View exact evidence</button>" +
      "<div id='" + id + "' style='display:none' class='er-restricted-form'>" +
      "<input type='text' placeholder='actor' data-restricted-actor/>" +
      "<input type='text' placeholder='reason' data-restricted-reason/>" +
      "<button type='button' class='er-btn' data-reveal-restricted data-table='" + esc(table) + "' " +
      "data-run='" + esc(runId) + "' data-call='" + esc(callId) + "'>Reveal (audited)</button>" +
      "<div class='er-meta' data-restricted-result></div>" +
      "</div></div>";
  }

  function renderLlmTab(data, opts) {
    var calls = data.llm_calls || [];
    if (!calls.length) return "<div class='er-empty'>No guarded model calls recorded for this run.</div>";
    var html = "<table class='er-table'><thead><tr><th>Call</th><th>Step</th><th>Model</th>" +
      "<th>Tokens</th><th>Latency</th><th>Cost</th><th>Validation</th><th>Exact</th></tr></thead><tbody>";
    calls.forEach(function (c) {
      var restricted = (c.sensitivity === "restricted") || c.restricted;
      html += "<tr>" +
        "<td class='er-mono'>" + esc(c.call_id || "") + (c.parent_call_id ? " ← " + esc(c.parent_call_id) : "") + "</td>" +
        "<td>" + esc(c.step_id || "") + "</td>" +
        "<td>" + esc(c.model || "") + "</td>" +
        "<td>" + esc(c.total_tokens || c.tokens || "") + "</td>" +
        "<td>" + esc(c.latency_ms || "") + " ms</td>" +
        "<td>" + esc(c.cost != null ? c.cost : "") + "</td>" +
        "<td>" + esc(c.validation_status || c.rai_status || "") + "</td>" +
        "<td>" + (restricted && opts.table
          ? llmRestrictedForm(opts.table, data.run_id, c.call_id)
          : "<span class='er-meta'>shareable</span>") + "</td>" +
        "</tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function artifactHref(opts, data, name) {
    var table = opts.table || data.table;
    var runId = opts.runId || data.run_id;
    return "/api/evidence/" + encodeURIComponent(table) + "/runs/" + encodeURIComponent(runId) +
      "/artifacts/" + encodeURIComponent(name);
  }

  function renderArtifactsTab(data, opts) {
    var artifacts = data.artifacts || {};
    var names = Object.keys(artifacts);
    if (!names.length) return "<div class='er-empty'>No artifacts recorded in this run's manifest.</div>";
    var html = "";
    names.sort().forEach(function (name) {
      var a = artifacts[name] || {};
      var restricted = a.sensitivity === "restricted";
      html += "<div class='er-artifact-row'>";
      html += "<div><strong>" + esc(name) + "</strong> <span class='er-meta'>" + esc(a.kind || "") +
        (a.size ? " · " + fmtBytes(a.size) : "") +
        (a.sha256 ? " · " + esc(String(a.sha256).slice(0, 10)) : "") + "</span></div>";
      if (restricted) {
        html += "<span class='er-badge er-b-error'>restricted</span>";
      } else {
        html += "<a class='er-btn' target='_blank' rel='noopener' href='" + artifactHref(opts, data, name) + "'>Open</a>";
      }
      html += "</div>";
    });
    return html;
  }

  function decideForm(table, column) {
    var id = "er-decide-" + Math.random().toString(36).slice(2);
    return "<button type='button' class='er-btn' data-toggle-restricted='" + id + "'>Decide…</button>" +
      "<button type='button' class='er-btn' data-toggle-history='" + column.replace(/'/g, "") + "'>History</button>" +
      "<div id='" + id + "' style='display:none' class='er-restricted-form'>" +
      "<select data-decide-status><option value='not_pii'>not PII</option><option value='pii'>PII</option></select>" +
      "<input type='text' placeholder='entity type (if PII)' data-decide-entity/>" +
      "<input type='text' placeholder='reviewer' data-decide-reviewer/>" +
      "<input type='text' placeholder='reason' data-decide-reason/>" +
      "<button type='button' class='er-btn' data-decide-submit data-table='" + esc(table) + "' data-column='" + esc(column) + "'>Save</button>" +
      "<div class='er-meta' data-decide-result></div>" +
      "</div>" +
      "<div data-history-for='" + esc(column) + "' style='display:none;margin-top:6px'></div>";
  }

  function renderVerdictPanel(data, opts) {
    var table = opts.table || data.table;
    var runId = opts.runId || data.run_id;
    return "<div class='er-drawer' data-er-verdict-panel data-table='" + esc(table) + "' data-run='" + esc(runId) + "'>" +
      "<h4>Verdict package (export / preview / import / replay)</h4>" +
      "<div class='er-meta'>Preview and replay never write. Import requires an actor + reason and records " +
      "one audit event.</div>" +
      "<textarea data-vp-json rows='4' style='width:100%;font:inherit;margin-top:6px' " +
      "placeholder='Paste a verdict package JSON ({\"entries\":[...]})'></textarea>" +
      "<div class='er-restricted-form' style='margin-top:6px'>" +
      "<input type='text' placeholder='actor' data-vp-actor/>" +
      "<input type='text' placeholder='reason' data-vp-reason/>" +
      "<select data-vp-merge-policy>" +
      "<option value='matching_only'>matching only</option>" +
      "<option value='overwrite_conflicts'>overwrite conflicts</option>" +
      "</select>" +
      "<button type='button' class='er-btn' data-vp-action='preview'>Preview</button>" +
      "<button type='button' class='er-btn' data-vp-action='replay'>Replay in this run</button>" +
      "<button type='button' class='er-btn er-btn-restricted' data-vp-action='import'>Import (durable)</button>" +
      "</div>" +
      "<div data-vp-result style='margin-top:8px'></div>" +
      "<h4 style='margin-top:14px'>Import history</h4>" +
      "<div data-vp-history class='er-meta'>Loading…</div>" +
      "</div>";
  }

  function classifyCounts(preview) {
    if (!preview) return "";
    var cats = ["matching", "stale", "missing", "conflicting", "invalid"];
    return cats.map(function (c) {
      return esc(c) + ": " + ((preview[c] || []).length);
    }).join(" · ");
  }

  function bindVerdictPanel(container, opts, onImported) {
    var panel = container.querySelector("[data-er-verdict-panel]");
    if (!panel) return;
    var table = panel.getAttribute("data-table");
    var runId = panel.getAttribute("data-run");
    var resultEl = panel.querySelector("[data-vp-result]");
    var historyEl = panel.querySelector("[data-vp-history]");

    function loadHistory() {
      fetch("/api/evidence/" + encodeURIComponent(table) + "/verdicts/imports")
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var imports = data.imports || [];
          if (!imports.length) { historyEl.textContent = "No verdict imports recorded yet."; return; }
          historyEl.innerHTML = "<pre class='er-pre'>" + esc(JSON.stringify(imports, null, 2)) + "</pre>";
        }).catch(function () { historyEl.textContent = "Failed to load import history."; });
    }
    loadHistory();

    panel.querySelectorAll("[data-vp-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var action = btn.getAttribute("data-vp-action");
        var raw = panel.querySelector("[data-vp-json]").value.trim();
        var pkg;
        try {
          pkg = JSON.parse(raw);
        } catch (e) {
          resultEl.innerHTML = "<div style='color:#991b1b'>Invalid JSON: " + esc(e.message) + "</div>";
          return;
        }
        var actor = panel.querySelector("[data-vp-actor]").value.trim();
        var reason = panel.querySelector("[data-vp-reason]").value.trim();
        var mergePolicy = panel.querySelector("[data-vp-merge-policy]").value;
        resultEl.textContent = "Working…";

        var url, body;
        if (action === "preview") {
          url = "/api/evidence/" + encodeURIComponent(table) + "/verdicts/preview";
          body = { package: pkg, run_id: runId };
        } else if (action === "replay") {
          url = "/api/evidence/" + encodeURIComponent(table) + "/runs/" + encodeURIComponent(runId) + "/verdicts/replay";
          body = { package: pkg };
        } else {
          url = "/api/evidence/" + encodeURIComponent(table) + "/verdicts/import";
          body = { package: pkg, run_id: runId, actor: actor, reason: reason, merge_policy: mergePolicy };
        }
        fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function (r) {
          return r.json().then(function (data) { return { ok: r.ok, data: data }; });
        }).then(function (res) {
          if (!res.ok) {
            resultEl.innerHTML = "<div style='color:#991b1b'>Failed: " + esc(res.data.detail || "error") + "</div>";
            return;
          }
          var summary = "";
          if (action === "preview") summary = "Preview — " + classifyCounts(res.data);
          if (action === "replay") {
            summary = "Replay (non-mutating, this run only) — would-apply: " +
              (res.data.applied || []).length +
              " · stale: " + (res.data.stale_skipped || []).length +
              " · missing: " + (res.data.missing_skipped || []).length +
              " · conflicting: " + (res.data.conflicting || []).length +
              " · invalid: " + (res.data.invalid_skipped || []).length;
          }
          if (action === "import") {
            summary = "Imported " + (res.data.applied || []).length + " verdict(s).";
            loadHistory();
            if (onImported) onImported();
          }
          resultEl.innerHTML = "<div style='color:#166534;margin-bottom:4px'>" + esc(summary) + "</div>" +
            "<pre class='er-pre'>" + esc(JSON.stringify(res.data, null, 2)) + "</pre>";
        }).catch(function (e) {
          resultEl.innerHTML = "<div style='color:#991b1b'>Request failed: " + esc(e.message) + "</div>";
        });
      });
    });
  }

  function renderDecisionsTab(data, opts) {
    var cols = data.columns || [];
    var table = opts.table || data.table;
    var html = opts.readOnly ? "" : renderVerdictPanel(data, opts);
    html += "<table class='er-table'><thead><tr><th>Column</th><th>Effective</th>" +
      "<th>Steward</th><th>Supplied</th><th>Drift</th>" + (opts.readOnly ? "" : "<th>Actions</th>") + "</tr></thead><tbody>";
    cols.forEach(function (col) {
      var ev = col.effective_verdict || {};
      html += "<tr data-column='" + esc(col.column) + "'>";
      html += "<td class='er-mono'>" + esc(col.column) + "</td>";
      html += "<td>" + (ev.detected ? "PII" : "not PII") + (ev.entity_type ? " (" + esc(ev.entity_type) + ")" : "") +
        " " + sourceBadge(ev.source) + "</td>";
      html += "<td>" + (col.steward_decision
        ? esc(col.steward_decision.status) + " — " + esc(col.steward_decision.decided_by || "")
        : "<span class='er-meta'>none</span>") + "</td>";
      html += "<td>" + (col.supplied_decision ? esc(col.supplied_decision.status) : "<span class='er-meta'>none</span>") + "</td>";
      html += "<td>" + driftBadge(col.drift) + "</td>";
      if (!opts.readOnly) {
        html += "<td>" + decideForm(table, col.column) + "</td>";
      }
      html += "</tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function bindDecideForms(container, opts, onSaved) {
    container.querySelectorAll("[data-decide-submit]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var wrap = btn.closest(".er-restricted-form");
        var table = btn.getAttribute("data-table");
        var column = btn.getAttribute("data-column");
        var status = wrap.querySelector("[data-decide-status]").value;
        var entity = wrap.querySelector("[data-decide-entity]").value.trim();
        var reviewer = wrap.querySelector("[data-decide-reviewer]").value.trim();
        var reason = wrap.querySelector("[data-decide-reason]").value.trim();
        var result = wrap.querySelector("[data-decide-result]");
        result.textContent = "Saving…";
        fetch("/api/contracts/" + encodeURIComponent(table) + "/review/columns/" + encodeURIComponent(column), {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            pii_status: status, entity_type: entity || null,
            reviewer: reviewer, reason: reason, run_id: opts.runId || "",
          }),
        }).then(function (r) {
          return r.json().then(function (data) { return { ok: r.ok, data: data }; });
        }).then(function (res) {
          if (!res.ok) {
            result.textContent = "Failed: " + (res.data.detail || "error");
            result.style.color = "#991b1b";
            return;
          }
          result.textContent = "Saved.";
          result.style.color = "#166534";
          if (onSaved) onSaved();
        }).catch(function (e) {
          result.textContent = "Request failed: " + e.message;
          result.style.color = "#991b1b";
        });
      });
    });
    container.querySelectorAll("[data-toggle-history]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var column = btn.getAttribute("data-toggle-history");
        var target = container.querySelector("[data-history-for='" + CSS.escape(column) + "']");
        if (!target) return;
        var show = target.style.display === "none";
        target.style.display = show ? "" : "none";
        if (show && !target._loaded) {
          target._loaded = true;
          target.textContent = "Loading…";
          fetch("/api/contracts/" + encodeURIComponent(opts.table || "") + "/review/columns/" + encodeURIComponent(column) + "/history")
            .then(function (r) { return r.json(); })
            .then(function (data) {
              var hist = data.history || [];
              if (!hist.length) { target.textContent = "No decision history."; return; }
              target.innerHTML = "<pre class='er-pre'>" + esc(JSON.stringify(hist, null, 2)) + "</pre>";
            }).catch(function (e) { target.textContent = "Failed to load history: " + e.message; });
        }
      });
    });
  }

  var TABS = [
    { id: "overview", label: "Overview", render: renderOverviewTab },
    { id: "profiling", label: "Profiling", render: renderProfilingTab },
    { id: "quality", label: "Quality", render: renderQualityTab },
    { id: "pii", label: "PII", render: renderPiiTab },
    { id: "llm", label: "LLM", render: renderLlmTab },
    { id: "artifacts", label: "Artifacts", render: renderArtifactsTab },
    { id: "decisions", label: "Decisions", render: renderDecisionsTab },
  ];

  function bindOverviewEvents(container, opts) {
    var body = container.querySelector("[data-er-body]");
    if (body) {
      body.querySelectorAll(".er-row").forEach(function (row) {
        row.addEventListener("click", function (evt) {
          if (evt.target.closest("button") || evt.target.closest("input")) return;
          var col = row.getAttribute("data-column");
          var drawer = body.querySelector("[data-drawer-for='" + CSS.escape(col) + "']");
          if (drawer) drawer.style.display = drawer.style.display === "none" ? "" : "none";
        });
      });
    }
    var search = container.querySelector("[data-er-search]");
    if (search) {
      search.addEventListener("input", function () {
        var q = search.value.trim().toLowerCase();
        body.querySelectorAll(".er-row").forEach(function (row) {
          var col = (row.getAttribute("data-column") || "").toLowerCase();
          var show = !q || col.indexOf(q) !== -1;
          row.style.display = show ? "" : "none";
          var drawer = body.querySelector("[data-drawer-for='" + CSS.escape(row.getAttribute("data-column")) + "']");
          if (drawer && !show) drawer.style.display = "none";
        });
      });
    }
    bindActionButtons(container, opts);
  }

  function bindActionButtons(container, opts) {
    if (opts.readOnly || !opts.onApprove) return;
    container.querySelectorAll(".er-act-approve").forEach(function (btn) {
      btn.addEventListener("click", function (evt) {
        evt.stopPropagation();
        opts.onApprove(btn.getAttribute("data-column"));
      });
    });
    container.querySelectorAll(".er-act-reject").forEach(function (btn) {
      btn.addEventListener("click", function (evt) {
        evt.stopPropagation();
        if (opts.onReject) opts.onReject(btn.getAttribute("data-column"));
      });
    });
    container.querySelectorAll(".er-act-reset").forEach(function (btn) {
      btn.addEventListener("click", function (evt) {
        evt.stopPropagation();
        if (opts.onReset) opts.onReset(btn.getAttribute("data-column"));
      });
    });
  }

  function bindRestrictedForms(container) {
    container.querySelectorAll("[data-toggle-restricted]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var target = container.querySelector("#" + btn.getAttribute("data-toggle-restricted"));
        if (target) target.style.display = target.style.display === "none" ? "flex" : "none";
      });
    });
    container.querySelectorAll("[data-reveal-restricted]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var wrap = btn.closest(".er-restricted-form");
        var actor = wrap.querySelector("[data-restricted-actor]").value.trim();
        var reason = wrap.querySelector("[data-restricted-reason]").value.trim();
        var result = wrap.querySelector("[data-restricted-result]");
        var table = btn.getAttribute("data-table");
        var runId = btn.getAttribute("data-run");
        var callId = btn.getAttribute("data-call");
        result.textContent = "Requesting…";
        fetch("/api/evidence/" + encodeURIComponent(table) + "/restricted/llm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: runId, call_id: callId, actor: actor, reason: reason }),
        }).then(function (r) {
          return r.json().then(function (data) { return { ok: r.ok, data: data }; });
        }).then(function (res) {
          if (!res.ok) {
            result.textContent = "Denied: " + (res.data.detail || "access refused");
            result.style.color = "#991b1b";
            return;
          }
          var pre = document.createElement("pre");
          pre.className = "er-pre";
          pre.textContent = JSON.stringify(res.data, null, 2);
          result.innerHTML = "";
          result.style.color = "";
          result.appendChild(pre);
        }).catch(function (e) {
          result.textContent = "Request failed: " + e.message;
          result.style.color = "#991b1b";
        });
      });
    });
  }

  // ── mount() — render one already-fetched review payload ─────────────────
  function mount(container, reviewData, opts) {
    opts = opts || {};
    injectStyle();
    if (!container || !reviewData) return;
    var data = normalizeReviewData(reviewData);
    opts.table = opts.table || data.table;
    opts.runId = opts.runId || data.run_id;

    var activeTab = container._erActiveTab || "overview";
    var wrap = document.createElement("div");
    wrap.className = "er-root";
    wrap.innerHTML = renderHeader(data) +
      "<div class='er-tabs' data-er-tabs>" +
      TABS.map(function (t) {
        return "<div class='er-tab" + (t.id === activeTab ? " active" : "") + "' data-tab='" + t.id + "'>" + t.label + "</div>";
      }).join("") +
      "</div><div data-er-tabbody></div>";
    container.innerHTML = "";
    container.appendChild(wrap);

    function renderTab(tabId) {
      activeTab = tabId;
      container._erActiveTab = tabId;
      wrap.querySelectorAll("[data-tab]").forEach(function (el) {
        el.classList.toggle("active", el.getAttribute("data-tab") === tabId);
      });
      var body = wrap.querySelector("[data-er-tabbody]");
      var tab = TABS.filter(function (t) { return t.id === tabId; })[0] || TABS[0];
      body.innerHTML = tab.render(data, opts);
      if (tabId === "overview") bindOverviewEvents(body, opts);
      if (tabId === "decisions") {
        bindActionButtons(body, opts);
        bindDecideForms(body, opts, opts.onDecided);
        bindVerdictPanel(body, opts, opts.onDecided);
      }
      if (tabId === "llm") bindRestrictedForms(body);
    }

    wrap.querySelectorAll("[data-tab]").forEach(function (el) {
      el.addEventListener("click", function () { renderTab(el.getAttribute("data-tab")); });
    });
    renderTab(activeTab);
  }

  // ── Final-Review checkpoint actions (approve/reject/reset) ───────────────
  // Shared default wiring so every embed (single-table, v2, enterprise
  // reports) gets working steward checkpoint actions without each caller
  // re-implementing the fetch calls (plan §5: approve/reject/reset).
  function defaultCheckpointAction(table, column, kind, reload) {
    var reviewer = (global.prompt && global.prompt(
      (kind === "reject" ? "Reject" : "Approve") + " " + column + " — reviewer name:",
    )) || "";
    if (kind !== "reset" && !reviewer.trim()) return;
    var url = "/api/contracts/" + encodeURIComponent(table) + "/review/columns/" +
      encodeURIComponent(column) + "/" + kind;
    var body = kind === "reset" ? undefined : JSON.stringify({ reviewer: reviewer });
    fetch(url, {
      method: "POST",
      headers: kind === "reset" ? undefined : { "Content-Type": "application/json" },
      body: body,
    }).then(function () { reload(); }).catch(function () { reload(); });
  }

  // ── mountRunExplorer() — self-driving: run selector + timeline ──────────
  function mountRunExplorer(container, opts) {
    opts = opts || {};
    injectStyle();
    if (!container || !opts.table) return Promise.resolve();
    var table = opts.table;
    var shell = document.createElement("div");
    shell.innerHTML = "<div class='er-runbar'>" +
      "<label class='er-meta'>Run: <select data-er-run-select></select></label>" +
      "<span class='er-meta' data-er-run-count></span>" +
      "</div><div data-er-body class='er-empty'>Loading…</div>";
    container.innerHTML = "";
    container.appendChild(shell);
    var body = shell.querySelector("[data-er-body]");
    var select = shell.querySelector("[data-er-run-select]");

    function loadRun(runId) {
      body.innerHTML = "<div class='er-empty'>Loading run…</div>";
      var url = "/api/evidence/" + encodeURIComponent(table) + "/review";
      if (runId) url += "?run_id=" + encodeURIComponent(runId);
      return fetch(url).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error((data && data.detail) || ("HTTP " + r.status));
          return data;
        });
      }).then(function (data) {
        var mountOpts = Object.assign({}, opts, { table: table, runId: data.run_id });
        mountOpts.onDecided = function () { loadRun(select.value || runId); };
        if (!opts.readOnly) {
          if (!mountOpts.onApprove) {
            mountOpts.onApprove = function (column) {
              defaultCheckpointAction(table, column, "approve", function () { loadRun(select.value || runId); });
            };
          }
          if (!mountOpts.onReject) {
            mountOpts.onReject = function (column) {
              defaultCheckpointAction(table, column, "reject", function () { loadRun(select.value || runId); });
            };
          }
          if (!mountOpts.onReset) {
            mountOpts.onReset = function (column) {
              defaultCheckpointAction(table, column, "reset", function () { loadRun(select.value || runId); });
            };
          }
        }
        mount(body, data, mountOpts);
        return data;
      }).catch(function (e) {
        body.innerHTML = "<div class='er-empty'>Failed to load review: " + esc(e.message) + "</div>";
      });
    }

    return fetch("/api/evidence/" + encodeURIComponent(table) + "/runs").then(function (r) {
      return r.ok ? r.json() : { runs: [] };
    }).then(function (data) {
      var runs = (data.runs || []).slice().reverse(); // newest first
      shell.querySelector("[data-er-run-count]").textContent = runs.length + " run(s)";
      select.innerHTML = runs.map(function (rid, i) {
        return "<option value='" + esc(rid) + "'" + (i === 0 ? " selected" : "") + ">" + esc(rid) + "</option>";
      }).join("") || "<option value=''>latest</option>";
      select.addEventListener("change", function () { loadRun(select.value); });
      var initial = opts.runId || runs[0] || "";
      if (opts.runId) select.value = opts.runId;
      return loadRun(initial);
    }).catch(function () {
      return loadRun(opts.runId || "");
    });
  }

  function fetchAndMount(container, url, opts) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    }).then(function (data) {
      mount(container, data, opts);
      return data;
    });
  }

  global.EvidenceReviewer = {
    mount: mount,
    mountRunExplorer: mountRunExplorer,
    renderColumn: function (col, opts) { return renderColumnDrawer(withActions(col, {}, opts || {})); },
    fetchAndMount: fetchAndMount,
  };
})(typeof window !== "undefined" ? window : this);
