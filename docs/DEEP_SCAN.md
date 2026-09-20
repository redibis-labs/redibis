# Deep Scan

Deep Scan fans out **multiple producers** (regex, NER, equation, profilers, quality candidates) over one table sample. Each producer writes a standard **evidence artifact** (JSON + Markdown). A **synthesis bundle** helps an LLM (or steward) assemble a contract with fewer false positives.

Deep Scan **never writes contracts** — evidence and bundles only.

## CLI

```bash
redibis deep-scan data/customers.csv telecom.customers \
  --producers rule.regex_catalog,equation.decide,profile.ge,quality.candidates \
  --bundle \
  --output-dir ./reports
```

| Flag | Meaning |
|---|---|
| `--producers` | Comma-separated producer ids (default: catalogue `default_producers`) |
| `--bundle` | Build `deep_scan_bundle/` (on by default; use `--no-bundle` to skip) |
| `--output-dir` | Run root (artifacts under `<dir>/<run_id>/`) |
| `--config` | `RedibisConfig` YAML |

## Agent board

Palette node **`deep_scan`** (`tool: deep_scan.run`) runs the same orchestrator. Params:

- `producers` — comma-separated ids (empty = defaults)
- `build_bundle` — boolean (default true)

Spans appear as `deep_scan.producer` in the audit DAG.

## Producer catalogue

Defined in `redibis/config/deep_scan.yaml`:

| id | wraps |
|---|---|
| `rule.regex_catalog` | Presidio regex / catalogue path |
| `ner.presidio` | Regex engine alias |
| `ner.gliner` | NER backend (when model configured) |
| `equation.decide` | `decide_pii` fusion |
| `profile.ge` | Great Expectations profiler |
| `profile.om` | OpenMetadata-style metrics |
| `profile.duckdb` | DuckDB SUMMARIZE |
| `profile.relationships` | Cross-column relationships |
| `profile.ydata` | YData (optional extra) |
| `quality.candidates` | Value-set expectation candidates |

Deny patterns and param validation mirror `agents/deep_profile` capabilities.

## Artifacts

```
<run_dir>/
  evidence/
    <producer>.<table>.json
    <producer>.<table>.md
  deep_scan_manifest.json
  deep_scan_bundle/          # when --bundle
    manifest.json            # refs + agreement matrix + reconciled verdicts
    contract_synthesis.md    # LLM-ready brief
    columns.jsonl            # one row per column, all signals
  <table>.<run_id>.log       # when persist_run_log enabled
```

No raw PII cell values in PII producer artifacts — match rates, pattern ids, scores only.

## Agreement matrix & false-positive risk

`reconcile_signals()` (pure function, unit-tested) assigns per-column `false_positive_risk`:

- **low** — ≥2 independent PII engines agree
- **high** — single engine fires but profiler suggests surrogate key (e.g. unique sequential int)
- **medium** — single engine, no profiler contradiction

Use `manifest.json` → `reconciled` and `contract_synthesis.md` for human or LLM review.

## Feeding an LLM

1. Run with `--bundle`.
2. Attach `deep_scan_bundle/contract_synthesis.md` + `columns.jsonl` to your prompt.
3. Ask for ODCS v3 property entries; escalate `false_positive_risk: high` columns.

**Default consumer (open question):** bundle is designed for enrich, an agent synthesis node, or an external LLM — no single default is enforced in open-core yet.

## Library

```python
from redibis.agents.deep_scan import run_deep_scan

result = run_deep_scan(
    "telecom.customers",
    run_id,
    df,
    run_dir=Path("./reports") / run_id,
    build_bundle=True,
)
```

Returns `{table, run_id, evidence, errors, manifest, bundle, telemetry}`.
