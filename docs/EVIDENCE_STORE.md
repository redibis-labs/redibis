# Evidence store — scan once, decide many times

The evidence bundle is **not a report**. It is the durable detector output for
one table, one run: engine scores, validator rates, profile metrics, and the
pack stack that produced them. Verdicts are a *derived view*. Change the
equation or the thresholds and replay — **without reading the source table**.

## Unified run folder (foundation)

Every public scan that persists (`flush=True`, the default on `ProfileScan` /
`QualityScan` / `PIIScan`) writes a **run directory**:

```
{output_dir}/{run_id}/                  # or {session_id}/{run_id} for web sessions
  evidence_bundle.json                  # local raw samples — never uploaded, never served
  evidence_bundle.shareable.json        # sanitized; this is the shareable copy
  evidence_manifest.json                # coverage + artifact index (no samples)
  effective_config.yaml                 # sanitized operator snapshot
  effective_config.sha256
  result_variants.json                  # modes that actually ran
  result_comparison.json                # pairwise diffs among present modes
  pii_detections.json                   # verdicts; llm_reasoning omitted
  llm_calls/NNNN-<id>.json              # redacted shareable copy (uploadable)
  llm_calls/index.json                  # shareable index (no raw/spool paths)

{restricted_spool_dir}/{run_id}/        # default ./reports/_restricted_evidence
  llm_calls/NNNN-<id>.raw.json          # exact prompts (file 0600, dirs 0700)
  llm_calls/index.json                  # full index including restricted paths
  _audit/restricted_reads.jsonl         # steward --raw audit (0600)
```

Exact `*.raw.json` records are written **only** to the governed spool. Ordinary
run folders never receive a raw copy, even when a recorder has `run_dir` set.

`Scan` itself stays in-memory. Persistence is `ReportBundle.flush`. Failures
to write evidence are **surfaced**, not swallowed. Manifest `run_status` is
`success`, `failed`, `cancelled`, or `empty` (empty tables are not reported as
a successful full scan).

LLM dual-copy policy: the raw prompt/response never leaves the host. Only
redacted `*.json` and `evidence_bundle.shareable.json` may go through
`RunOutputWriter`. `redibis get llm-call-logs` and the web artifact API refuse
`*.raw.json` and `evidence_bundle.json`. Restricted CLI reads:

```bash
redibis scan evidence llm --table telecom.customers --call-id <id> \
  --raw --actor steward --reason debug --role data_steward
```

`--actor` and `--reason` are required when `evidence.access.require_actor` /
`require_reason` are true (the defaults). `--role` must match
`evidence.access.steward_role`. When `audit_enabled` is true, a failed audit
write is fail-closed (the read is refused).

Future engines plug in by emitting `engine_evidence[<id>]` with
`engine_id` / `ran` / `score` / `hits` / `reason`. Plugin-only scores are
stored for investigation and do **not** vote in built-in equations. No schema
bump is required.

Result comparison never triggers extra LLM calls. Missing modes are
`not_run`.

Reporting drill-down (list every table → zoom one table) is a later
add-on that **ingests** these artifacts; it is not part of this
foundation.

That only works if three things stay true:

1. Every engine's **raw, pre-verdict** number is stored (a GLiNER 0.31 that lost
   at 0.70 must still be there for investigation).
2. The **exact pack versions** that ran can be recovered (`uuid` + `sha256` +
   ordered `stack_uuid`).
3. The **shareable** copy has no literal cell values (`scan evidence store`).

Invariant 6 (detector → evidence, equation → verdict) is the reason this exists.
Invariant 7 (`RunOutputWriter` is the only runs-bucket writer) is how the stored
copy is persisted.

Related: [CLI cheat sheet](cli/scan.md) · [pack design](REDIBIS_PACK_DESIGN.md) ·
[PII tuning](PII_DETECTION_TUNING.md).

---

## 1. Two lifetimes, one schema

| Copy | Where | Samples / top values | What it is for |
|---|---|---|---|
| Run artifact | `{output-dir}/<run>/evidence_bundle.json` | **raw** by default | local analysis only; blocked from CLI/web retrieval |
| Shareable run copy | `{output-dir}/<run>/evidence_bundle.shareable.json` | **stripped** | hand to an LLM or another host from the run folder |
| Stored copy | `_meta/evidence/{table}/{run_id}.json` via `RunOutputWriter` | **stripped** (or masked) | archive, air-gap replay, `scan evidence store` |

The on-disk raw file keeps literals because that is the default you chose for
investigation. It is never the retrieval path. The stored copy and the
`.shareable.json` twin are what you hand to another host or an LLM.
Exact LLM prompts and responses live **only** in a steward-governed local spool
(`evidence.restricted_spool_dir`, default `./reports/_restricted_evidence`).
They are never mirrored into ordinary run folders and never uploaded to the
runs bucket. Restricted reads require `--actor` / `--reason` (and `--role`
when configured) and append an audit row.

Same schema (`kind: redibis.evidence_bundle`, `schema_version: "2.0"`).

```
scan once
   │
   ├─ evidence_bundle.json                 ← raw samples (local, not served)
   ├─ evidence_bundle.shareable.json       ← sanitized twin in the run folder
   │
   ├─ scan evidence store                  ← strip literals, persist under _meta/evidence/
   │
   ├─ scan decide --preset …        ← new verdicts, zero source reads
   │
   └─ context build --add …         ← LLM envelope (refuses raw PII unless flagged)
```

---

## 2. Emit the bundle

After a scan (or any run that wrote `evidence_bundle.json`):

```bash
# newest run for the table (default)
redibis scan evidence --table telecom.customers --latest --out -

# specific run
redibis scan evidence --table telecom.customers --run-id 20260809T120000Z --out bundle.json

# which runs even have evidence?
redibis scan evidence --table telecom.customers --list-runs
```

`--out -` writes JSON to stdout so it pipes. `--latest` is the default when
`--run-id` is omitted. Newest is chosen by the bundle/manifest **completion
timestamp** (mtime fallback), not by lexical run id — `--list-runs` uses the
same clock. Use the **same** `--output-dir` (default `./reports`, matching
`ScanConfig` / `ReportConfig`) as the scan so local `_dev_storage` lines up.

Web/session scans write runs-bucket keys as
`scan/{table}/{session_id}/{run_id}/`. Coverage, `scan evidence llm`, and
`get llm-call-logs` resolve both that nested prefix and the flat
`scan/{table}/{run_id}/` layout. `_meta/evidence` keys still use the **bare**
run id.

**Example — pipe a column list into `jq`:**

```bash
redibis scan evidence --table telecom.customers --latest --out - \
  | jq -r '.columns | keys[]'
```

**Example — why `--list-runs` first:** replay and store both need a run id.
If two scans landed today, `--latest` is the newest **completion time**, not
the lexicographically last run id.

```bash
redibis scan evidence --table telecom.customers --list-runs
# 20260809T091102Z
# 20260809T140033Z
redibis scan decide --table telecom.customers --run-id 20260809T091102Z --preset audit
```

---

## 3. Store without raw PII — `scan evidence store`

```bash
redibis scan evidence store --table telecom.customers --latest
redibis scan evidence store --table telecom.customers --all-tables
redibis scan evidence store --table telecom.customers --keep-masked-samples

# coverage index (no samples) and LLM dual-copy
redibis scan evidence coverage --table telecom.customers --latest
redibis scan evidence llm --table telecom.customers --latest
redibis scan evidence llm --table telecom.customers --call-id <id>
redibis scan evidence llm --table telecom.customers --call-id llm_prompt_context --raw \
  --actor steward --reason debug --role data_steward
redibis scan evidence llm --table telecom.customers --call-id <id> --raw \
  --actor steward --reason debug --role data_steward
```

Steps, in order (missing any one of them makes the command a lie):

1. Load the run's bundle.
2. Remove `columns.*.samples.values` — keep `mode`, `n`, `seed` so the shape
   stays stable.
3. Remove `columns.*.profile.frequency.top_values` **values** — those are
   literal data too. Keep counts and rates.
4. Omit free-text `llm_reasoning` / engine `reasoning`, prompt/response
   payloads, UUIDs, and other residual PII. Hashes and safe metadata stay.
5. Set `sensitivity.sample_mode = "none"`, `contains_raw_pii = false`,
   `egress = "allow"`.
6. Stamp `sensitivity.stripped_at` and `sensitivity.stripped_by`.
7. Write `_meta/evidence/{table}/{run_id}.json` via `RunOutputWriter`.
8. Print the storage key.

`--keep-masked-samples` runs `MaskingEngine` instead of dropping: a shareable
bundle that still shows value *shape* (format masks, length) without the
original strings.

**Example — prove the stored JSON has no source literals:**

```bash
# source cell you would never want in a shareable artifact
grep -F '01012345678' tests/data/golden_tutorial_customers.csv

redibis scan evidence store --table tutorial.customers --latest
# wrote _meta/evidence/tutorial.customers/<run_id>.json

# must print nothing
grep -F '01012345678' reports/_dev_storage/pii-reports/_meta/evidence/tutorial.customers/*.json
```

If `top_values` were left intact, that grep would hit even after samples were
dropped. That is why both paths are stripped.

**Example — why not "never write samples"?** Local debugging of a false
positive ("is this really an NID or a timestamp?") needs the raw run file.
Store is the *export* gate, not the scan gate. Two lifetimes, one schema.

---

## 4. Replay at new thresholds — `scan decide`

No rescan. Reconstruct `PIIDetection` from generic `engine_evidence` **first**
(entity, validator rates, LLM verdict/reasoning, hits, errors, timings,
versions). Legacy per-engine blocks (`presidio`, `gliner`, …) are a fallback
only when the generic block is absent. `ran: false` stays authoritative even
if leftover validator artifacts remain.

`decide_pii` is unchanged. Copy `stack_uuid` forward: re-deciding does not
change which rules were in effect. Plugin-only scores stay in
`engine_evidence` for investigation; they are **not** listed on
`pii_verdict.deciding_engines` (those are equation voters only:
regex / ner / llm / phone / learned / nid / imei / imsi / geo).

Replay must leave every evidence block **byte-identical**. Only derived
verdict fields (`pii_verdict`, table summary) change.

```bash
redibis scan decide --table telecom.customers --latest --preset investigation
redibis scan decide --table telecom.customers --latest --preset reporting
redibis scan decide --table telecom.customers --latest --preset audit --out audit.json

# override preset equation / floors
redibis scan decide --table telecom.customers --latest --preset investigation \
  --equation lenient --set presidio_min=0.40 --set gliner_min=0.25
```

| Preset | Equation | Thresholds | Optimises |
|---|---|---|---|
| `investigation` | `lenient` | presidio 0.45, gliner 0.30, phone 0.60 | recall |
| `reporting` | `balanced` | library defaults | balance |
| `audit` | `strict` | presidio 0.92, gliner 0.85, validators on | precision |

Stdout is a diff against the stored verdict:

```
telecom.customers — investigation vs balanced
  + notes        PERSON          0.52  (gliner 0.52 now votes; was below 0.70)
  + legacy_ref   EG_NATIONAL_ID  0.61  (regex 0.61 now votes)
  ~ msisdn       PHONE_NUMBER    0.94 → 0.94  unchanged
  7 → 11 columns detected
```

**Example — reading that diff.** `+ notes` was below the reporting floor
(GLiNER 0.70) and is now a vote at investigation (0.30). The evidence never
changed; only the equation did. If `notes` had no GLiNER score in
`pii_evidence`, investigation could not surface it — that would be a bundle
schema bug, not a threshold bug.

**Example — investigation is a superset of reporting.** Every column reporting
marks PII must still be PII under investigation. If a column *disappears*
when you lower thresholds, the replay reconstructed from the verdict instead
of the evidence.

**Example — audit for a regulator pack.** You already scanned last month. The
question now is "what would we still claim at precision floors?"

```bash
redibis scan decide --table telecom.customers --run-id 20260701T000000Z \
  --preset audit --out /tmp/audit-july.json
```

Zero reads against Hive/CSV/S3 source. If this command touches the table, an
engine score was dropped when the bundle was built.

`stack_uuid` in the replayed header **must equal** the original. A new
`equation_id` is appended to `header.rules.equations`; `pii_evidence` stays
byte-identical.

---

## 5. LLM context — `context build`

```bash
redibis context build \
  --add './reports/**/evidence_bundle.shareable.json' \
  --add ./notes/investigation.md \
  --out context.json \
  --max-bytes 400000
```

- `--add` is repeatable and glob-aware; any file type.
- JSON embeds as parsed structure; everything else as text with a detected `kind`.
- Paths are de-duplicated after glob expansion.
- `--max-bytes` drops **whole files** into `dropped` — never truncate silently.
- Refuses any input with `sensitivity.contains_raw_pii: true` **or** with
  literal `samples.values` / `top_values` payloads, even if the flag is false.
  Pass both `--allow-raw-pii` and `--reason TEXT` to override. The reason is
  recorded in the manifest.

Prefer `evidence_bundle.shareable.json` or the stored `_meta/evidence` copy.
Do not glob `evidence_bundle.json` into a shareable envelope.

**Example — preferred (already sanitized):**

```bash
redibis context build \
  --add './reports/**/evidence_bundle.shareable.json' \
  --out context.json
```

**Example — refuse by default (the safe path):**

```bash
# run artifact still has raw samples
redibis context build --add ./reports/20260809T140033Z/**/evidence_bundle.json
# exit non-zero: contains_raw_pii=true; pass --allow-raw-pii --reason …
```

**Example — store first, then build (preferred):**

```bash
redibis scan evidence store --table telecom.customers --latest
# then point --add at the stripped JSON, or export a shareable zip
redibis context build --add './reports/_dev_storage/**/_meta/evidence/telecom.customers/*.json' \
  --out context.json
```

**Example — explicit exception, reason on the record:**

```bash
redibis context build \
  --add ./reports/20260809T140033Z/**/evidence_bundle.json \
  --allow-raw-pii \
  --reason "incident INC-4421: confirm whether notes is PERSON or free text" \
  --out /tmp/incident-context.json
```

The envelope includes a `sources` manifest so the model can cite which table a
claim came from — and so you can verify afterwards what was actually sent.

---

## 6. Pack identity — why UUID is per version

The pack **is** the ruleset (regex catalogue, custom rules, NER entities,
validators, thresholds). Replay is only sound if the exact version that ran
can be recovered. A version string is not enough: a pack can be edited in
place without bumping `2.1.0`.

| Field | Job |
|---|---|
| `uuid` | this exact published version — the thing you download |
| `family_id` | stable across versions (`acme.policy.telecom`) — lineage |
| `version` | human-readable ordering (`2.1.0`) |
| `parent_uuid` | the version this was derived from |
| `sha256` | verifies the bytes you got are the bytes that ran |

UUID answers *which*; sha256 answers *whether it is intact*. Record both;
verify both on download.

### The stack, not the packs

Redibis applies packs in overlay order. The same two packs reversed are a
different ruleset. Hence `stack_uuid` (uuid5 over ordered `uuid:sha256` +
mode) and `stack_sha256` (SHA-256 of the **same** payload).

Both helpers **fail closed** if any layer is missing `uuid` or `sha256`.
Minting a stack identity from `None` rendered as the string `"None"` would
look confident and make every replay unverifiable.

`stack_sha256` includes each pack's content digest. Two blobs with the same
UUID and different bytes must not hash equal — otherwise tampering is
invisible at the stack fingerprint.

```bash
# what did this evidence run with?
redibis scan evidence packs --table telecom.customers --latest

  stack b7c1f0a2…  overlay  2 packs
    uuid                                  kind    id       version  local  contents
    3f2a91c4-6b0e-4c8a-9d17-52e8ab4f0c11  locale  eg       1.4.0    yes    —
    8d5e70bb-1c92-45af-b0d6-9f3c2e614a77  policy  telecom  2.1.0    yes    100 regex · 14 rules · 23 entities
```

---

## 7. Pack store CLI

```bash
redibis pack publish ./telecom-pack.zip --author acme-governance
redibis pack list --family acme.policy.telecom
redibis pack show 8d5e70bb-1c92-45af-b0d6-9f3c2e614a77
redibis pack history --family acme.policy.telecom
redibis pack get 8d5e70bb-1c92-45af-b0d6-9f3c2e614a77 --out ./packs/ --extract
redibis pack diff <uuid-a> <uuid-b>
```

`pack get` **verifies sha256 after download and fails on mismatch, writing
nothing.** A pack that does not hash to what the evidence recorded is not the
pack that ran; accepting it silently makes every replay wrong.

`pack diff` is what earns the UUID scheme. Given two evidence bundles with
different findings, it answers in one command whether the **rules** changed or
the **data** did. Report added / removed / changed for: regex patterns, custom
rules, NER entities, validators, thresholds.

**Example — findings changed between July and August. Rules or data?**

```bash
# UUIDs from each bundle's header.provenance.pack_stack.packs[]
JULY=8d5e70bb-1c92-45af-b0d6-9f3c2e614a77
AUG=c91e2a00-4b11-4f02-9aa1-0d3c8e7b6a22

redibis pack diff $JULY $AUG
# regex_patterns.added: eg_nid
# custom_rules.changed: cr.msisdn_by_name
# thresholds.added: presidio_min
```

If diff is empty, the ruleset is identical — look at the data (or sampling
seed). If diff lists new regex / lower floors, the extra August hits may be
rule changes, not new PII in the warehouse.

**Example — `pack get` fail-closed:**

```bash
redibis pack get 8d5e70bb-… --out /tmp/packs/
# sha256 mismatch: refusing to write /tmp/packs/8d5e70bb-….zip
# /tmp/packs/ stays empty
```

Do not "download anyway and warn". Replay on a tampered blob is worse than
failing.

---

## 8. Air-gap export — `--with-packs`

```bash
redibis scan evidence export --table telecom.customers --with-packs --out bundle.zip
redibis scan evidence packs --table telecom.customers --download --out ./packs/
```

The zip contains:

```
evidence_bundle.json
packs/{uuid}.zip          # one per stack layer — not base64 inside the JSON
MANIFEST.sha256
```

Do **not** inline the pack into the JSON. A policy pack is ~96 KB; inlining
would multiply the context cost of the artifact whose whole purpose is being
small enough to hand to an LLM.

**Example — replay on a host that has never seen the pack store:**

```bash
# on the air-gapped machine
rm -rf ./reports/_dev_storage/pii-reports/_meta/packs
unzip bundle.zip -d /tmp/replay
# evidence + packs/{uuid}.zip are enough
redibis scan decide --table telecom.customers --latest --preset reporting \
  --output-dir /tmp/replay
```

If decide fails after deleting the local pack store, export omitted a pack or
the manifest hash does not match. That is test 30 in the plan — the air-gap
proof.

---

## 9. Numeric profile: `parsed_count` vs `non_null`

Histogram bins are built from **successfully parsed** numbers, not from every
non-null cell. A column of 60 integers + 40 `"n/a"` strings has
`counts.non_null == 100` and `numeric.parsed_count == 60`. The histogram
counts **must sum to `parsed_count`**.

Without `parsed_count` that denominator is unassertable: you cannot tell
whether a missing bin is a profiler bug or an unparsed string. Rates such as
`outlier_rate` and `integer_valued_rate` are also over `parsed_count`.

```jsonc
"numeric": {
  "parsed_count": 60,
  "zero_count": 2,
  "negative_count": 0,
  "histogram": { "bins": 10, "counts": [/* sum == 60 */] }
}
```

---

## 10. Config

```yaml
report:
  evidence_bundle:
    enabled: true
    sample_mode: raw            # raw | masked | none
    sample_n: 10
    top_values_n: 20
    format_masks_n: 10
    numeric_histogram_bins: 20
    length_histogram_bins: 20

evidence:
  restricted_spool_dir: ./reports/_restricted_evidence
  access:
    steward_role: data_steward
    require_actor: true
    require_reason: true
    audit_enabled: true

pack:
  require_signature: false      # true → unsigned / untrusted packs fail load
  trust_store: ""               # key_id → public key (JSON/YAML)
```

`sample_mode: raw` is the local default. Share via `scan evidence store`, not
by flipping this to `none` globally unless you truly never want literals on
disk.

---

## 11. Library surface (tests / scripts)

```python
from redibis.scan.evidence_ops import (
    load_evidence_bundle, strip_raw_values, store_evidence, replay_preset,
    strip_bundle,  # deprecated alias of strip_raw_values (one release)
)
from redibis.pack.identity import compute_stack_uuid, compute_stack_sha256
from redibis.scan.profile_metrics import profile_numeric
from redibis.scan.context_build import build_context

bundle, hint = load_evidence_bundle(table="telecom.customers", latest=True, ...)
stripped = strip_raw_values(bundle)                    # no samples / llm_reasoning / residual PII
replayed = replay_preset(bundle, preset="investigation")
# compute_stack_uuid / compute_stack_sha256 raise ValueError on missing uuid/sha256
```

`replay` / `replay_preset` never open the source table. Assert that with a spy
if you extend the replay path.

---

## 12. Command / flag scope

| Flag | Commands |
|---|---|
| `--table` | evidence, store, decide, packs, export, coverage, llm |
| `--run-id` / `--latest` | evidence, store, decide, packs, export, coverage, llm |
| `--out` | evidence, store, decide, packs, export, context build, pack get |
| `--all-tables` / `--keep-masked-samples` | store |
| `--preset` / `--equation` / `--set` | decide |
| `--add` / `--max-bytes` / `--allow-raw-pii` / `--reason` | context build |
| `--with-packs` | export |
| `--download` | evidence packs |
| `--call-id` / `--raw` / `--actor` / `--role` | evidence llm (`--reason` also required with `--raw`) |
| `--extract` | pack get |
| `--family` | pack list, pack history |
| `--author` | pack publish |

One name per command, no aliases. `--skip-*` not `--no-*` on this surface.

Planner, provider-probe, and codegen each mint a unique `run_id` (`planner-<id>`,
`provider-probe-<id>`, `codegen-<table>-<id>`). Do not reuse shared ids such as
`planner` — they would merge unrelated calls into one spool folder.

Fetch shareable run artifacts (never raw bundles):

```bash
redibis get llm-call-logs <run_id> --zip evidence.zip --output-dir ./reports
```

That zip includes `evidence_bundle.shareable.json`, `evidence_manifest.json`,
shareable `llm_calls/*.json`, and enrich diffs. It omits `evidence_bundle.json`
and `*.raw.json`. Nested `{session_id}/{run_id}` prefixes are discovered the
same way as `scan evidence llm`.

---

## See also

- [`docs/EVIDENCE_REVIEW_LEDGER.md`](EVIDENCE_REVIEW_LEDGER.md) — the run explorer UI, steward decisions/history, drift, and verdict preview/import/replay built on top of this evidence store
- [`docs/cli/scan.md`](cli/scan.md) — scan / profile / quality cheat sheet (includes evidence/decide)
- [`docs/REDIBIS_PACK_DESIGN.md`](REDIBIS_PACK_DESIGN.md) — portable `.rdbpack` layout
- [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) — regex/NER floors that decide replays
