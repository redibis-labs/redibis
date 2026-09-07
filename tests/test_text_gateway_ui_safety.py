"""Static contracts for the Text Gateway UI — no browser runtime required."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "redibis" / "webapp" / "static"
TEMPLATES = ROOT / "redibis" / "webapp" / "templates"


def test_no_web_storage_or_innerhtml_of_user_text():
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    mjs = (STATIC / "gateway_render.mjs").read_text(encoding="utf-8")
    blob = js + "\n" + mjs
    assert "localStorage" not in blob
    assert "sessionStorage" not in blob
    assert "indexedDB" not in blob
    # User text must be appended as text nodes, never assigned as HTML.
    assert "innerHTML" not in js
    assert "innerHTML" not in mjs
    assert "insertAdjacentHTML" not in blob
    assert "document.write" not in blob


def test_codepoints_use_array_from():
    mjs = (STATIC / "gateway_render.mjs").read_text(encoding="utf-8")
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "Array.from" in mjs
    assert "Array.from" in js
    assert "toChars" in mjs


def test_tooltip_and_marks_are_keyboard_accessible():
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    mjs = (STATIC / "gateway_render.mjs").read_text(encoding="utf-8")
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    assert 'id="gwTip"' in html
    assert "role=\"tooltip\"" in html or "role='tooltip'" in html
    assert "tabIndex" in mjs
    assert "focus" in js
    assert "blur" in js
    assert "mouseenter" in js


def test_no_raw_input_interpolation_into_html():
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    assert "${input.value}" not in js
    assert "input.value +" not in js.replace(" ", "")
    assert "{{ text }}" not in html
    assert "createTextNode" in js


def test_policy_review_is_required_before_deidentify():
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "/api/gateway/suggest-policy" in js
    assert "/api/gateway/deidentify" in js
    assert "collectPolicy" in js
    assert "reviewedPolicy" in js
    assert "deidBtn.disabled = true" in js


def test_mask_preview_toggle_uses_policy_and_keeps_highlights():
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    mjs = (STATIC / "gateway_render.mjs").read_text(encoding="utf-8")
    assert 'id="gwMaskToggle"' in html
    assert "toggleMaskPreview" in js
    assert "remapSpansAfterDeid" in js
    assert "remapSpansAfterDeid" in mjs
    assert "Show masked" in html or "Show masked" in js
    assert "aria-pressed" in html


def test_policy_set_all_to_redact_control():
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert 'id="gwPolicyAllRedact"' in html
    assert "setAllStrategies" in js
    assert 'setAllStrategies("redact")' in js or "setAllStrategies('redact')" in js


def test_nav_surfaces_include_gateway_except_share():
    templates = TEMPLATES
    for name in (
        "settings.html",
        "index.html",
        "v2.html",
        "agents.html",
        "reports_missing.html",
        "users.html",
        "gateway.html",
    ):
        text = (templates / name).read_text(encoding="utf-8")
        assert "/gateway" in text, name
    assert "/gateway" not in (templates / "share.html").read_text(encoding="utf-8")


def test_auth_js_is_the_csrf_wrapper():
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    assert "/static/auth.js" in html
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" not in js


def test_progress_bar_is_determinate_and_accessible():
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert 'role="progressbar"' in html
    assert "aria-valuemin" in html and "aria-valuemax" in html
    assert "aria-valuenow" in html
    assert "id=\"gwCancelScan\"" in html
    # Percent must move only from real backend stage callbacks, never a timer.
    assert "setInterval" not in js
    assert "STAGE_PERCENT" in js
    assert "/api/gateway/scan/stream" in js


def test_scan_can_be_cancelled_and_cleans_up_on_unload():
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "AbortController" in js
    assert "cancelScan" in js
    assert "scanAbort.abort()" in js
    assert "beforeunload" in js


def test_llm_provider_and_guard_controls_are_present():
    html = (TEMPLATES / "gateway.html").read_text(encoding="utf-8")
    assert 'id="gwLlmProvider"' in html
    assert 'id="gwCheckToxicity"' in html
    assert 'id="gwCheckInjection"' in html
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "/api/gateway/health" in js
    assert "check_toxicity" in js
    assert "check_prompt_injection" in js
    assert "llm_provider" in js and "llm_model" in js


def test_settings_has_text_gateway_models_tab():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'stab("gateway_models","Text Gateway")' in app
    assert "vSettingsGatewayModelsTab" in app
    assert "applySettingsGatewayModels" in app
    assert "testGatewayModelRole" in app
    assert "pii.text_refiner" in app
    assert "gateway.toxicity" in app
    assert "gateway.prompt_injection" in app
    assert "sglang" in app
    assert "GATEWAY_MODEL_ROLES" in app


def test_gateway_health_check_never_reveals_credentials():
    """The client-side provider list must only render safe metadata — never
    request an API key/endpoint field from the health payload."""
    js = (STATIC / "gateway.js").read_text(encoding="utf-8")
    assert "api_key" not in js.lower().replace("api_key_env_set", "")
    assert "endpoint_url" not in js
