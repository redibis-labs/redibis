# PII detection — logging, export, and tuning

Make PII scans transparent and tunable without editing source code: see **which regex fired which entity**, export the catalogue and NER inventory, and feed firing stats to an LLM or agent for proposed overrides.

Transport (structured logs, decision channel, per-scan log files) is documented in [`docs/OBSERVABILITY.md`](OBSERVABILITY.md). This doc covers the **PII-specific payload**.

## What you get per column

After a scan, each `PIIDetection` carries:

| Field | Content |
|---|---|
| `regex_hits` | Every Presidio pattern that fired: `pattern_name`, `regex` (string), `entity_type`, `score`, `match_rate`, `group`, `collision_group`, `validator` |
| `ner_hits` | Every NER pass (when `label_groups` used): `model`, `labels`, `label`, `score`, `match_rate` |
| `presidio_*` / `gliner_*` | Single-best scores (back-compat; equation still uses these) |

Decision lines land in the per-scan log and SSE stream:

```
DECISION pii detector._run_presidio column=phone verdict=PHONE_NUMBER …
```

The structured `decision.inputs` block includes `pattern_name`, `regex`, and `match_rate` — **never raw cell values**.

Grep persisted logs:

```bash
grep DECISION ./reports/<run_id>/telecom.customers.<run_id>.log
```

## Config (`pii` + `observability` blocks)

```yaml
observability:
  log_samples: false          # NEVER log raw cell values unless explicitly on

pii:
  ner:
    labels: []              # normal scan: single GLiNER entity set (empty = manifest/defaults)
    label_groups: []        # deep-scan / agentic only: [[PERSON,LOCATION],[PHONE_NUMBER]]
  logging:
    log_regex_hits: true      # emit per-pattern decision lines
    max_regex_hits_logged: 20
  tuning:
    bundle: false             # write LLM tuning bundle on scan (when wired to run dir)
  regex_overrides:          # optional inline overrides (same shape as RegexOverrides)
    add: {}
    remove: []

enrich:
  include_pii_evidence: false # attach catalogue + NER inventory summary to enrichment prompt
```

| Setting | Scope |
|---|---|
| `pii.ner.labels` | **Normal scan** — one label set for GLiNER |
| `pii.ner.label_groups` | **Deep scan / agentic only** — multiple NER passes per column |
| `observability.log_samples` | Off by default; when on, truncated samples may appear in progress logs |

## Export catalogue and NER inventory

### CLI

```bash
# Regex catalogue (effective = default + overrides)
redibis pii regex list
redibis pii regex export --format json --out catalog.json
redibis pii regex export --format yaml --active-only

# NER models under REDIBIS_MODELS_DIR or pii.models_dir
redibis pii ner list
redibis pii ner export --out ./ner_export/
redibis pii ner export --out ./ner_export/ --bundle my-gliner-model
```

### API

Dashboard session required ([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl)).
CLI export commands above do not.

| Route | Purpose |
|---|---|
| `GET /api/pii/regex?format=json\|yaml\|csv` | Export effective catalogue |
| `GET /api/pii/regex/{name}` | Single pattern (with regex string) |
| `POST /api/pii/regex/overrides` | Validate + persist `RegexOverrides` to global settings |
| `GET /api/pii/ner/models` | NER model inventory |
| `GET /api/pii/ner/labels` | Active label set + `source` (`config` \| `default`) |
| `PUT /api/pii/ner/labels` | Persist global NER labels |

Legacy read-only routes remain: `GET /api/regex-catalog`, `GET /api/models`.

### Settings UI

**Settings → PII** tab:

- **Regex catalogue** — preview patterns, export JSON/YAML/CSV links
- **NER entity labels** — comma/newline-separated labels, Save → `PUT /api/pii/ner/labels`
- **Active regex config** — named profiles from **Settings → Configs** (same as discovery)

**Settings → NER Models** — upload, activate, models directory (unchanged).

### Scan flag: NER labels

```bash
redibis scan data.csv demo.table \
  --ner-labels "person,phone number,iban,email"
```

Overrides `pii.ner.labels` for that run (empty/absent → manifest or defaults).

## Regex overrides (add / disable, never edit source)

The shipped catalogue in `redibis/pii/regex_catalog.py` is **read-only**. Tune via `RegexOverrides`:

```yaml
# saved profile or POST /api/pii/regex/overrides
add:
  my_custom_msisdn:
    pattern: "^\\+20\\d{10}$"
    entity_type: PHONE_NUMBER
    recognizer_group: structured
    presidio_score: 0.90
remove:
  - msisdn_egypt_vodafone   # disable an over-firing pattern
```

- **Add** — merge on top of default `CATALOG` (or replace key if it exists)
- **Remove** — drop pattern names from the effective catalog for that deployment
- **replace_all: true** — ignore defaults; only `add` entries are used

Named profiles: `POST /api/configs/regex` (same as discovery workflow).

## NER entities and governance

GLiNER is zero-shot: a new label phrase is detected immediately when added to `pii.ner.labels`.

To make a new entity **first-class** (sensitivity class, masking policy, contract tags), add a mapping in `CANONICAL_ENTITY` / `canonical_entity()` in `redibis/models.py`. Editing labels alone = detection; the mapping line = governed entity.

The Settings UI hints when a label is unknown to the canonical mapping.

## Multi-entity NER (deep scan / agentic)

Normal scans run **one** NER pass with `pii.ner.labels`. For columns that may contain several entity types, use **`NEREnsemble`** with `pii.ner.label_groups` (see [`docs/NER_BACKENDS.md`](NER_BACKENDS.md)):

```yaml
pii:
  ner:
    label_groups:
      - ["person", "address"]
      - ["phone number"]
      - ["national id"]
```

Each pass produces one `ner_hits` entry and one `DECISION pii detector._run_ner` line. Do **not** set `label_groups` on routine production scans — cost scales with passes.

## Tuning bundle (LLM / agent)

`build_pii_tuning_bundle()` writes `pii_tuning_bundle/` under a run directory:

| File | Purpose |
|---|---|
| `catalog.json` | Effective regex catalogue |
| `ner_models.json` | Discovered NER inventory |
| `firing_report.json` | Per-pattern stats: `never_fired`, `over_firing`, `missing_pattern_candidates` |
| `tuning_prompt.md` | Instructions for the LLM (no raw values) |
| `apply_template.json` | Empty `{add, remove, ner_label_groups}` for the model to fill |

### Agent board

Palette node **`pii_tune`** (`tool: pii_tune.propose`):

1. Runs PII scan on the table sample
2. Builds the tuning bundle
3. Returns a **proposed** `RegexOverrides` + `ner_label_groups` scaffold
4. Status `awaiting_approval` — **never auto-applied**

Apply proposals manually via `POST /api/pii/regex/overrides` and YAML `pii.ner.label_groups` after steward review.

### Enrichment

See [`docs/LLM_ENRICHMENT.md`](LLM_ENRICHMENT.md) for the full enrichment architecture (shared
`build_context`, per-column evidence, sample/output policy, output scrubbing).

```yaml
enrich:
  include_pii_evidence: true   # optional: regex catalog + NER inventory in user prompt
  similar_context:
    enabled: true
    k: 5
```

When `include_pii_evidence: true`, adds a **PII DETECTION CATALOG** section to the enrichment user
prompt (pattern names and models — no cell values). This is separate from per-column
`engine_evidence.regex_hits` / `ner_hits` / `phone` (`valid_rate`, `mobile_rate`, `regions` when
libphonenumber ran), which are always included in the column evidence block.

### Phone / MSISDN gating (Plan B)

When `pii.use_phonenumbers: true` (default), a `PHONE_NUMBER` verdict requires column-level
`phone_valid_rate ≥ pii.msisdn_valid_rate_min` (default 0.80) from `phonenumbers.parse` — not regex
alone. Network columns (`cell_global_identity`, `lac`, `tac_lte`) map to `NETWORK_ID` via telecom edge
rules. Per-run cancel: `redibis scan --no-phonenumbers` or `pii.use_phonenumbers: false`.

See [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) and
[`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md).

Cloud providers use `sample_policy: masked` (raw samples omitted from column context); output is
always scrubbed regardless of provider (`scrub_pii_enrichment_output` + `strip_pii_quality` on upsert).

## Evidence artifacts

`write_pii_signals_artifact()` (library) writes per-table evidence for Deep Scan / custom pipelines:

```
pii_signals.<table>.json   # regex_hits + ner_hits per column
pii_signals.<table>.md     # human-readable summary
```

Deep Scan producer `rule.regex_catalog` now includes `regex_hits` in column evidence when available.

## Invariants

1. **No raw PII in logs or exports** — regex strings, pattern names, entity types, scores, match-rates only.
2. **Source catalogue is never mutated** — changes go through `RegexOverrides` or named config profiles.
3. **LLM proposals are suggest-only** — human or approval gate before overrides affect scans.
4. **Contracts unchanged** — this feature is detection-time logging + export; `ContractStore.upsert()` is not involved.

## Related docs

- [`docs/LLM_ENRICHMENT.md`](LLM_ENRICHMENT.md) — shared enrich path, column evidence, output scrubbing
- [`docs/OBSERVABILITY.md`](OBSERVABILITY.md) — logging, decision channel, OTel
- [`docs/DEEP_SCAN.md`](DEEP_SCAN.md) — multi-producer evidence fan-out
- [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) — phonenumbers gating, IMEI, network IDs (Plan B)
- [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) — MSISDN / libphonenumber engine
