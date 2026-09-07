import json
import logging
import os
import webbrowser
from typing import Optional

from redibis.pii.regex_catalog import build_effective_catalog
from redibis.pii.regex_overrides import RegexOverrides

logger = logging.getLogger("pii.regex_review")

def export_pii_regex_review(
    overrides: Optional[RegexOverrides] = None,
    output_filename: str = "pii_regex_review.html",
    open_browser: bool = False,
) -> str:
    """
    Generates a custom interactive HTML dashboard for PII regex pattern review.
    """
    logger.info("Generating interactive PII regex dashboard...")

    # Use the effective catalog (defaults + overrides)
    catalog = build_effective_catalog(overrides)

    rules_data = []
    for name, entry in catalog.items():
        rules_data.append({
            "name": name,
            "entity_type": entry.entity_type,
            "recognizer_group": entry.recognizer_group,
            "script": entry.script,
            "presidio_score": entry.presidio_score,
            "active": entry.active,
            "pattern": entry.pattern,
            "context_hints": list(entry.context_hints),
            "requires_validator": entry.requires_validator,
            "requires_normalizer": entry.requires_normalizer,
            "collision_group": entry.collision_group,
        })

    rules_json = json.dumps(rules_data)
    
    # We will use string formatting to insert the JSON into the template.
    # The template itself will be loaded or defined below.
    final_html = _INTERACTIVE_HTML_TEMPLATE.replace("DATA_PLACEHOLDER", rules_json)

    output_path = os.path.abspath(output_filename)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_html)
        
    logger.info("Interactive PII regex dashboard saved: %s", output_path)
    if open_browser:
        webbrowser.open(f"file://{output_path}")
        
    return output_path

_INTERACTIVE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>PII Regex Rules Review</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root { --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0; --text: #1e293b; --muted: #64748b; --accent: #4f46e5; --red: #ef4444; --green: #22c55e; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); padding-bottom: 150px; }
  .header { background: #0f172a; color: #fff; padding: 1.5rem 2rem; }
  .header h1 { font-size: 1.4rem; font-weight: 800; }
  .header p { color: #94a3b8; font-size: .85rem; margin-top: .35rem; }
  .action-bar { padding: 1rem 2rem; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; gap: 1rem; align-items: center; }
  .btn { padding: 0.5rem 1rem; border-radius: 6px; font-weight: 600; cursor: pointer; border: 1px solid var(--border); background: var(--surface); }
  .btn.primary { background: var(--accent); color: white; border-color: var(--accent); }
  .rule-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin: 1rem 2rem; padding: 1rem; }
  .rule-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
  .rule-title { font-weight: 700; font-size: 1.1rem; }
  .badge { font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 9999px; background: #e0e7ff; color: #4338ca; font-weight: 600; }
  .code-block { background: #1e293b; color: #a7f3d0; padding: 0.75rem; border-radius: 6px; font-family: monospace; font-size: 0.85rem; overflow-x: auto; margin-top: 0.5rem; }
  .toggle-btn { background: #f1f5f9; border: 1px solid #cbd5e1; padding: 0.25rem 0.75rem; border-radius: 4px; font-size: 0.8rem; cursor: pointer; }
  .toggle-btn.active { background: #dcfce7; border-color: #86efac; color: #166534; }
  .toggle-btn.inactive { background: #fee2e2; border-color: #fca5a5; color: #991b1b; }
  .footer { position: fixed; bottom: 0; left: 0; right: 0; background: var(--surface); border-top: 1px solid var(--border); padding: 1rem 2rem; display: flex; justify-content: space-between; z-index: 50; }
  .footer-output { flex: 1; margin-right: 2rem; }
  .footer-output textarea { width: 100%; height: 80px; font-family: monospace; font-size: 0.8rem; padding: 0.5rem; border: 1px solid var(--border); border-radius: 4px; }
  .controls { display: grid; grid-template-columns: 120px 1fr; gap: 0.5rem; align-items: center; font-size: 0.85rem; margin-top: 0.5rem; }
  .controls input { padding: 0.25rem; border: 1px solid var(--border); border-radius: 4px; width: 100%; }
</style>
</head>
<body>
<div class="header">
  <h1>PII Regex Rules Review</h1>
  <p>Review, edit, and toggle PII detection regex patterns. Generate configuration code or save directly to local storage.</p>
</div>
<div class="action-bar">
  <button class="btn" onclick="renderRules('all')">All</button>
  <button class="btn" onclick="renderRules('structured')">Structured</button>
  <button class="btn" onclick="renderRules('free_text')">Free Text</button>
  <button class="btn primary" onclick="addNewRule()">+ Add Custom Pattern</button>
</div>
<div id="rules-container"></div>
<div class="footer">
  <div class="footer-output">
    <div style="font-size: 0.75rem; font-weight: 700; color: var(--muted); margin-bottom: 0.25rem;">GENERATED CONFIG (Python / YAML)</div>
    <textarea id="output-code" readonly>Click 'Generate Config' to view changes.</textarea>
  </div>
  <div style="display: flex; gap: 0.5rem; flex-direction: column; justify-content: center;">
    <button class="btn primary" onclick="generateConfig()">1. Generate Config</button>
    <button class="btn" onclick="saveToStore()">2. Save to Local Store</button>
  </div>
</div>

<script>
let rules = DATA_PLACEHOLDER;
// Keep track of original rules to compute diffs
const originalRulesMap = new Map(rules.map(r => [r.name, JSON.stringify(r)]));

// Custom rules added in this session
let customRules = [];

let currentFilter = 'all';

function renderRules(filter = currentFilter) {
    currentFilter = filter;
    const container = document.getElementById('rules-container');
    container.innerHTML = '';
    
    const allRulesToRender = [...rules, ...customRules].filter(r => 
        filter === 'all' ? true : r.recognizer_group === filter
    );

    allRulesToRender.forEach((rule, index) => {
        const isCustom = !originalRulesMap.has(rule.name);
        const card = document.createElement('div');
        card.className = 'rule-card';
        card.innerHTML = `
            <div class="rule-header">
                <div>
                    <span class="rule-title">${rule.name}</span>
                    <span class="badge">${rule.entity_type}</span>
                    <span class="badge" style="background: #f1f5f9; color: #475569;">${rule.recognizer_group}</span>
                    ${isCustom ? '<span class="badge" style="background: #fef08a; color: #854d0e;">Custom</span>' : ''}
                </div>
                <button class="toggle-btn ${rule.active ? 'active' : 'inactive'}" onclick="toggleActive('${rule.name}')">
                    ${rule.active ? 'Active' : 'Inactive'}
                </button>
            </div>
            <div class="code-block" contenteditable="true" onblur="updatePattern('${rule.name}', this.innerText)">${rule.pattern}</div>
            <div class="controls">
                <label>Regex Score:</label>
                <input type="number" step="0.05" min="0" max="1" value="${rule.presidio_score}" onchange="updateScore('${rule.name}', this.value)">
                <label>Context Hints:</label>
                <input type="text" value="${rule.context_hints.join(', ')}" onchange="updateHints('${rule.name}', this.value)">
            </div>
        `;
        container.appendChild(card);
    });
}

function findRule(name) {
    return rules.find(r => r.name === name) || customRules.find(r => r.name === name);
}

function toggleActive(name) {
    const rule = findRule(name);
    if(rule) {
        rule.active = !rule.active;
        renderRules();
    }
}

function updatePattern(name, newPattern) {
    const rule = findRule(name);
    if(rule) rule.pattern = newPattern.trim();
}

function updateScore(name, newScore) {
    const rule = findRule(name);
    if(rule) rule.presidio_score = parseFloat(newScore);
}

function updateHints(name, newHints) {
    const rule = findRule(name);
    if(rule) rule.context_hints = newHints.split(',').map(s => s.trim()).filter(s => s);
}

function addNewRule() {
    const name = prompt("Enter a unique name for the new pattern (e.g., custom_auth_token):");
    if (!name || findRule(name)) {
        alert("Invalid or duplicate name.");
        return;
    }
    const newRule = {
        name: name,
        entity_type: "CUSTOM_ENTITY",
        recognizer_group: "structured",
        script: "latin",
        presidio_score: 0.85,
        active: true,
        pattern: "^[A-Za-z0-9_-]+$",
        context_hints: [],
        requires_validator: null,
        requires_normalizer: null,
        collision_group: null
    };
    customRules.unshift(newRule);
    renderRules();
}

function computeOverrides() {
    const addObj = {};
    
    // Check original rules for modifications
    rules.forEach(rule => {
        const originalStr = originalRulesMap.get(rule.name);
        const currentStr = JSON.stringify(rule);
        if (originalStr !== currentStr) {
            // Include modified original rules
            const { name, ...rest } = rule;
            addObj[name] = rest;
        }
    });
    
    // Add all custom rules
    customRules.forEach(rule => {
        const { name, ...rest } = rule;
        addObj[name] = rest;
    });

    return addObj;
}

function generateConfig() {
    const addObj = computeOverrides();
    const overrides = {
        replace_all: false,
        add: addObj
    };
    
    const yamlStr = "replace_all: false\\nadd:\\n" + Object.entries(addObj).map(([name, props]) => {
        let pStr = `  ${name}:\\n`;
        pStr += `    pattern: '${props.pattern.replace(/'/g, "''")}'\\n`;
        pStr += `    entity_type: ${props.entity_type}\\n`;
        pStr += `    recognizer_group: ${props.recognizer_group}\\n`;
        pStr += `    presidio_score: ${props.presidio_score}\\n`;
        pStr += `    active: ${props.active}\\n`;
        if (props.context_hints.length > 0) {
            pStr += `    context_hints:\\n${props.context_hints.map(h => `    - ${h}`).join('\\n')}\\n`;
        }
        return pStr;
    }).join('');

    const output = document.getElementById('output-code');
    if (Object.keys(addObj).length === 0) {
        output.value = "# No changes made to the default catalog.";
    } else {
        output.value = yamlStr;
    }
    return overrides;
}

async function saveToStore() {
    const addObj = computeOverrides();
    if (Object.keys(addObj).length === 0) {
        alert("No changes to save.");
        return;
    }
    
    const configName = prompt("Enter a name for this config (e.g., custom-telecom-v2):");
    if (!configName) return;

    try {
        const payload = {
            name: configName,
            description: "Saved from Interactive Review Dashboard",
            add: addObj,
            replace_all: false
        };
        
        // This expects the backend to be running
        const res = await fetch("http://localhost:8080/api/configs/regex", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            const data = await res.json();
            alert(`Config saved successfully to ${data.path}`);
        } else {
            alert(`Failed to save: ${res.statusText}`);
        }
    } catch (e) {
        alert("Error saving config. Ensure backend is running. " + e.message);
    }
}

renderRules();
</script>
</body>
</html>
"""
