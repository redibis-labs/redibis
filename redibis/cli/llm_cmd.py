"""CLI: ``redibis llm list|test|add|remove|roles`` — providers and capability routes."""

from __future__ import annotations

import json
import sys
from typing import Any, Optional


def run_llm(args) -> int:
    action = getattr(args, "llm_action", None)
    if action == "list":
        return _llm_list(args)
    if action == "test":
        return _llm_test(args)
    if action == "add":
        return _llm_add(args)
    if action == "remove":
        return _llm_remove(args)
    if action == "roles":
        return _llm_roles(args)
    print("llm: use `list`, `test`, `add`, `remove`, or `roles`", file=sys.stderr)
    return 2


def _llm_roles(args) -> int:
    from redibis.config import load_global_settings_optional
    from redibis.enrich.capability_routing import (
        RoutingError,
        build_bindings_from_global,
        known_roles,
        resolve_model_binding,
    )
    from redibis.enrich.probe import probe_provider

    gs = load_global_settings_optional()
    bindings = build_bindings_from_global(gs)
    roles_action = getattr(args, "roles_action", None) or "list"
    as_json = bool(getattr(args, "json", False))

    if roles_action == "list":
        rows = [bindings[r].to_dict() for r in known_roles()]
        if as_json:
            print(json.dumps({"roles": rows, "revision": int(gs.get("revision") or 0)}, indent=2))
            return 0
        print(f"revision={int(gs.get('revision') or 0)}")
        for row in rows:
            print(
                f"{row['role']:<28} enabled={row['enabled']!s:<5} "
                f"provider={row['provider'] or '-'} model={row['model'] or '-'} "
                f"src={row['source']}"
            )
        return 0

    role = (getattr(args, "role", None) or "").strip()
    if not role:
        print("llm roles: ROLE is required for show/test", file=sys.stderr)
        return 2
    if role not in known_roles():
        print(f"llm roles: unknown role {role!r}", file=sys.stderr)
        return 2

    try:
        binding = resolve_model_binding(role, bindings=bindings)
    except RoutingError as exc:
        print(f"llm roles: {exc}", file=sys.stderr)
        return 1

    if roles_action == "show":
        if as_json:
            print(json.dumps(binding.to_dict(), indent=2))
        else:
            for k, v in binding.to_dict().items():
                print(f"{k}: {v}")
        return 0

    if roles_action == "test":
        if not binding.provider:
            print(json.dumps({"ok": False, "error": "no provider bound", "binding": binding.to_dict()}, indent=2)
                  if as_json else "no provider bound for role")
            return 1
        result = probe_provider(binding.provider, model=binding.model or "")
        result["role"] = role
        result["binding"] = binding.to_dict()
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            status = "ok" if result.get("ok") else "FAIL"
            print(f"{status} role={role} provider={binding.provider} model={result.get('model')}")
            if result.get("error"):
                print(f"  error: {result['error']}")
        return 0 if result.get("ok") else 1

    print("llm roles: use list|show|test", file=sys.stderr)
    return 2


def _providers_file(args) -> Optional[str]:
    return getattr(args, "providers_file", None) or None


def _parse_params(raw: list[str] | None) -> dict[str, Any]:
    """Parse repeated ``--param k=v`` into a JSON-ish object (numbers/bools when obvious)."""
    out: dict[str, Any] = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--param expects k=v, got {item!r}")
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise SystemExit(f"empty key in --param {item!r}")
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
        else:
            try:
                if "." in v:
                    out[k] = float(v)
                else:
                    out[k] = int(v)
            except ValueError:
                out[k] = v
    return out


def _llm_list(args) -> int:
    from redibis.enrich.provider_profiles import list_profiles

    rows = list_profiles(_providers_file(args))
    if getattr(args, "json", False):
        print(json.dumps({"providers": rows}, indent=2))
        return 0
    for p in rows:
        key = " (needs key)" if p.get("needs_key") and not p.get("api_key_env_set") else ""
        if p.get("needs_key") and p.get("api_key_env_set"):
            key = f" (key via {p.get('api_key_env')})"
        base = f"  base={p['api_base']}" if p.get("api_base") else ""
        if p.get("offline"):
            cloud = " [offline]"
        elif p.get("cloud"):
            cloud = " [cloud]"
        elif p.get("editable"):
            cloud = " [custom]"
        else:
            cloud = " [local]"
        src = p.get("source") or ""
        src_bit = f" src={src}" if src and src != "packaged" else ""
        print(f"{p['name']:<16} {p['model']}{key}{base}{cloud}{src_bit}")
        if p.get("description"):
            print(f"                 {p['description']}")
    return 0


def _llm_test(args) -> int:
    from redibis.enrich.probe import normalize_provider_name, probe_provider
    from redibis.enrich.provider_profiles import list_profiles, test_profile

    providers_file = _providers_file(args)
    use_staged = bool(getattr(args, "staged", False))
    names: list[str] = []
    if getattr(args, "all", False):
        names = [p["name"] for p in list_profiles(providers_file) if p["name"] != "demo"]
        if getattr(args, "include_demo", False):
            names.insert(0, "demo")
    elif getattr(args, "provider", None):
        names = [normalize_provider_name(args.provider)]
    else:
        print(
            "llm test: pass a provider name (ollama|vllm|sglang|openai|claude|gemini|"
            "custom-profile…) or --all",
            file=sys.stderr,
        )
        return 2

    # Prefer staged test for custom profiles or when --staged is set
    profiles = {p["name"]: p for p in list_profiles(providers_file)}
    as_json = getattr(args, "json", False)
    results = []
    exit_code = 0
    for name in names:
        row = profiles.get(name) or {}
        staged = use_staged or bool(row.get("editable") or row.get("kind") == "custom")
        if staged:
            if getattr(args, "endpoint", None) or getattr(args, "model", None) or not row:
                target: object = {
                    "name": name,
                    "api_base": getattr(args, "endpoint", None) or row.get("api_base"),
                    "model": getattr(args, "model", None) or row.get("default_model_bare") or "",
                    "litellm_model": row.get("model") or "",
                    "model_prefix": row.get("model_prefix") or "openai",
                    "api_key_env": row.get("api_key_env") or "",
                    "supports_json": row.get("supports_json", True),
                    "params": row.get("params") or {},
                    "profile_type": "openai_compatible",
                }
            else:
                target = name
            result = test_profile(
                target,
                session_key=getattr(args, "api_key", None),
                config_path=providers_file,
                timeout=float(getattr(args, "timeout", 20.0) or 20.0),
            )
        else:
            result = probe_provider(
                name,
                model=getattr(args, "model", "") or "",
                api_key=getattr(args, "api_key", None),
                endpoint_url=getattr(args, "endpoint", None),
                prompt=getattr(args, "prompt", None),
                timeout=float(getattr(args, "timeout", 20.0) or 20.0),
                providers_file=providers_file,
            )
        results.append(result)
        if not result.get("ok"):
            exit_code = 1
        if not as_json:
            _print_result(result, verbose=getattr(args, "verbose", False))

    if as_json:
        payload = results[0] if len(results) == 1 else {"results": results}
        print(json.dumps(payload, indent=2))
    return exit_code


def _llm_add(args) -> int:
    from redibis.enrich.provider_profiles import SGLANG_QWEN_PRESET, save_profile
    from redibis.enrich.providers import EnrichmentError

    name = (getattr(args, "name", None) or "").strip()
    if not name:
        print("llm add: NAME is required", file=sys.stderr)
        return 2

    preset = SGLANG_QWEN_PRESET if getattr(args, "preset", None) == "sglang-qwen" else {}
    api_base = getattr(args, "api_base", None) or preset.get("api_base") or ""
    model = getattr(args, "model", None) or ""
    if not model and preset:
        model = "Qwen/Qwen2.5-14B-Instruct"
    profile_type = getattr(args, "profile_type", None) or "openai_compatible"
    if getattr(args, "raw", False):
        profile_type = "raw"

    params = dict(preset.get("params") or {"temperature": 0.2, "max_tokens": 4096, "timeout": 120})
    params.update(_parse_params(getattr(args, "param", None)))

    payload = {
        "name": name,
        "description": getattr(args, "description", None) or preset.get("description") or "",
        "profile_type": profile_type,
        "api_base": api_base or None,
        "model": model,
        "litellm_model": getattr(args, "litellm_model", None) or "",
        "model_prefix": getattr(args, "model_prefix", None) or (
            preset.get("model_prefix") if profile_type != "raw" else ""
        ) or "openai",
        "api_key_env": getattr(args, "api_key_env", None) or preset.get("api_key_env") or "SGLANG_API_KEY",
        "supports_json": not bool(getattr(args, "no_json", False)),
        "residency": getattr(args, "residency", None) or "local",
        "params": params,
    }
    if profile_type == "raw":
        payload["litellm_model"] = (
            getattr(args, "litellm_model", None)
            or getattr(args, "model", None)
            or ""
        )

    try:
        result = save_profile(
            payload,
            allow_override_packaged=bool(getattr(args, "force", False)),
            config_path=_providers_file(args),
        )
    except EnrichmentError as exc:
        print(f"llm add: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"saved custom profile {result['name']!r} → {result['path']}")
    return 0


def _llm_remove(args) -> int:
    from redibis.enrich.provider_profiles import delete_profile
    from redibis.enrich.providers import EnrichmentError

    name = (getattr(args, "name", None) or "").strip()
    if not name:
        print("llm remove: NAME is required", file=sys.stderr)
        return 2
    try:
        result = delete_profile(name, config_path=_providers_file(args))
    except EnrichmentError as exc:
        print(f"llm remove: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"removed custom profile {result['name']!r}")
    return 0


def _print_result(result: dict, *, verbose: bool = False) -> None:
    ok = result.get("ok")
    status = "OK" if ok else "FAIL"
    provider = result.get("provider", "?")
    model = result.get("model") or ""
    latency = result.get("latency_ms", 0)
    print(f"[{status}] {provider}  model={model}  latency_ms={latency}")
    stages = result.get("stages") or []
    if stages:
        for s in stages:
            st = "OK" if s.get("ok") else "FAIL"
            extra = ""
            if s.get("warning"):
                extra = f"  warning={s['warning']}"
            elif s.get("detail"):
                extra = f"  {s['detail']}"
            print(f"  [{st}] {s.get('stage')}: latency_ms={s.get('latency_ms', '—')}{extra}")
            if s.get("error"):
                print(f"       error: {s['error']}", file=sys.stderr)
            if s.get("models"):
                print(f"       models: {', '.join(s['models'][:12])}")
            snip = (s.get("response_snippet") or "").strip()
            if snip and verbose:
                print(f"       response: {snip[:200]}")
        if result.get("hint"):
            print(f"  hint: {result['hint']}", file=sys.stderr)
        return

    snippet = (result.get("response_snippet") or "").strip()
    if snippet:
        print(f"  response: {snippet[:300]}")
    err = (result.get("error") or "").strip()
    if err:
        print(f"  error: {err}", file=sys.stderr)
    if verbose and result.get("log"):
        print("  --- provider log (redacted) ---")
        print(result["log"])
