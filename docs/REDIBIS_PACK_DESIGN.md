# Redibis Pack — Portable Configuration & Behavior Bundle

**Status:** Phase 0–6 implemented; default pack export now ships the full portable
surface (regex, tokens, NER, classification, masking regex lib, prompts). Phase 7
pending (fat packs/signatures).
**CLI note:** enrichment packs keep ``redibis pack validate|inspect|evaluate``.
Portable packs use ``redibis rdbpack …``; ``redibis pack export-default`` is an alias.
**Supersedes:** `docs/LOCALE_PACK_DESIGN.md` (a LocalePack is now one *section* inside a Pack).
**Related:** `docs/BEHAVIOR_POLICY_ARCHITECTURE.md`, `docs/BEHAVIOR_POLICY_IMPLEMENTATION_PLAN.md`,
`NER_PLUGGABLE_MODELS_DESIGN.md`, `CLAUDE.md` invariants.

---

## 1. Requirement

One importable/exportable artifact — a zip or tar file — that carries **everything needed
to control how redibis behaves**: config, locale data, regex overrides, behavior policies,
quality rule sets, masking plans, NER model selection. Operators author it (Settings UI or
by hand), export it, ship it to another deployment, import it, and run a scan that behaves
identically.

Current shipped behavior becomes **the default pack**. Testing = export default → import →
scan → assert parity.

## 2. Concept

```
┌─────────────────────────────────────────────────────────────┐
│  redibis-telecom-fr-1.2.0.rdbpack   (zip, or .tar.gz)       │
│                                                             │
│  pack.yaml            manifest: id, version, requires, deps │
│  config/              RedibisConfig subset (allow-listed)   │
│  locale/              tokens, regex overrides, phone, faker │
│  behavior/            BehaviorPolicy documents (v1)         │
│  quality/             rule sets / suppressions              │
│  masking/             saved MaskingPlan templates           │
│  ner/                 model *reference* + labels + phrases  │
│  CHECKSUMS.json       sha256 per file + canonical pack SHA  │
│  README.md            human notes (not executed, not parsed)│
└─────────────────────────────────────────────────────────────┘
```

**Hard rule: a pack is data.** No `.py`, no pickles, no executables, no shell hooks.
Anything the pack *references* that is code (regex validators, behavior actions, NER
backends, profilers) must already be installed and registered in the target deployment.
Import validates every reference resolves; it never installs code. This is the same
boundary as `parse_ge_rules` and the behavior-policy plug-in tier.

## 3. Layout

```text
pack.yaml
config/redibis.yaml                 # allow-listed RedibisConfig subset
locale/tokens.yaml                  # ContextTokenSets (per entity, per locale)
locale/regex.yaml                   # RegexOverrides (add / remove / replace_all)
locale/phone.yaml                   # regions, msisdn prefixes, geofence name
behavior/<policy-id>@<version>.yaml # BehaviorPolicy documents
quality/rulesets/<name>.yaml        # generated/curated GE rule sets
masking/plans/<name>.yaml           # MaskingPlan templates (never keys)
ner/models.yaml                     # model refs: id, digest, labels, phrases, floors
classification/packs/<name>.yaml    # classification policy packs (+ edge rules)
assets/prompts/enrich/*.md           # enrichment system prompt parts (Settings-editable)
assets/prompts/enrichment_system.md  # composed convenience copy
assets/prompts/pii_*.md              # optional non-enrich prompt templates
assets/regex_patterns.json          # masking regex library override
assets/collisions.yaml              # collision-resolution table (audit / review)
assets/coverage_gaps.yaml           # known regex coverage gaps
CHECKSUMS.json
README.md
```

Enrichment prompts are **Settings-backed**: `configs/prompts/enrich/*.md` (or
`REDIBIS_PROMPTS_DIR`). Pack export reads the live store; pack import replaces it.
The composed system prompt is what LLM enrich uses.

### 3.1 `pack.yaml`

```yaml
apiVersion: redibis.io/pack/v1
kind: RedibisPack
metadata:
  id: telecom-fr
  version: "1.2.0"
  description: France telecom deployment — NIR, FR phones, CDR corrections.
  author: data-governance@example.com
  created: "2026-07-21T10:00:00Z"
  base: redibis-default@3.4.0        # pack this was derived from
requires:
  redibis: ">=3.4,<4"                # engine compatibility range
  registries:                        # must resolve at import, else fail closed
    validators: [nir_mod97, luhn]
    behavior_actions: [core.verdict.set_entity, core.review.add_reason]
    ner_backends: [gliner]
    profilers: [great_expectations]
contents:                            # declares which sections are present
  config: true
  locale: true
  behavior: [fr-corrections@1.0.0]
  quality: [cdr-baseline]
  masking: [fr-default]
  ner: true
mode: overlay                        # overlay | replace
checksum: sha256:…                   # canonical digest of all files
signature: null                      # optional detached signature (enterprise)
```

`mode: overlay` merges onto the active configuration; `mode: replace` discards
non-built-in state for the sections it declares. Overlay is the default and the
only mode offered in the UI without an explicit confirmation step.

### 3.1.1 Version identity (`uuid`, `family_id`, `parent_uuid`)

A UUID identifies an immutable pack **version**, not a family. Every publish
mints a new UUID; a UUID is never reused or re-pointed. `family_id` (e.g.
`acme.policy.telecom`) is the lineage key; `parent_uuid` walks the chain.

Evidence replay records the **ordered stack**, not just individual UUIDs:
`stack_uuid` / `stack_sha256` are derived from `uuid:sha256` per layer plus
overlay mode. Both helpers fail closed if any layer is missing identity.
`redibis pack get` verifies sha256 and writes nothing on mismatch.

See [`docs/EVIDENCE_STORE.md`](EVIDENCE_STORE.md) §6–8 for CLI examples
(`pack diff`, `--with-packs` export, air-gap replay).

### 3.2 What `config/redibis.yaml` may contain

An **allow-listed subset** of `RedibisConfig`. Portable behavior knobs only:

| Included | Excluded (never travels in a pack) |
|---|---|
| `scan_types`, `profiling.*`, `quality.*` thresholds | `storage.*` credentials, bucket names |
| `pii.*` (equation mode, engines, thresholds, tuning) | `source.*` JDBC URLs, hosts, secrets |
| `contract.*` (automerge, retention defaults) | `llm.*` API keys, provider tokens |
| `masking.*` strategy defaults | masking **keys/seeds** (per-run, invariant 11) |
| `classification.*`, `behavior.*` mode/limits | `catalog.*` endpoints & auth |
| `report.*` | `table`, run-specific identifiers |

The importer rejects excluded keys with a path-specific error rather than silently
dropping them — a pack that thinks it sets credentials must fail loudly. Environment
and secrets stay in the deployment, exactly as today.

### 3.3 NER models: reference, not weights

Default packs carry a **reference**:

```yaml
# ner/models.yaml
active: gliner-multi-v2.1
models:
  - id: gliner-multi-v2.1
    type: gliner
    digest: sha256:…            # verified against the installed model dir
    language: [en, fr, ar]
    labels: [PERSON, PHONE_NUMBER, EMAIL_ADDRESS, ADDRESS, NATIONAL_ID]
    phrases: { NATIONAL_ID: "numéro de sécurité sociale" }
    gliner_min: 0.45
```

Import verifies a model with that id/digest exists under the models dir; if absent it
warns and marks the pack **degraded** (NER disabled, regex path still runs) rather than
failing the whole import. A `--with-weights` export variant produces a fat pack (models
embedded, GB-scale) for air-gapped transfer — same format, `contents.ner_weights: true`.

## 4. Layering and precedence

```
built-in default pack  (shipped, redibis-default@<engine-version>)
        ▼ overlay
deployment pack        (config/pack.rdbpack, or Settings-imported)
        ▼ overlay
tenant / domain pack   (optional)
        ▼ overlay
run overlay            (ScanConfig / explicit RegexOverrides passed by a caller)
```

Merge rules, per section:

- **config:** deep merge, last layer wins per leaf key (existing `deep_merge`).
- **regex:** `RegexOverrides` semantics already defined — `add` unions, `remove`
  subtracts, `replace_all` truncates lower layers for that section.
- **context tokens:** union per entity; a layer may subtract with an explicit
  `remove:` list.
- **behavior policies:** compose into the `EffectivePolicySet` under the Behavior
  Policy Runtime's own rules (fully-qualified `(policy_id, rule_id)` replacement,
  priority sort). Packs **ship** policies; the runtime still owns activation,
  approval, simulation, and audit. Importing a pack does **not** auto-activate its
  policies unless the pack is imported with `--activate` and the caller has the role.
- **quality rule sets / masking plans:** named documents, replace by name.
- **ner:** last layer wins for `active`; model entries merge by id.

Every effective layer is recorded (id, version, checksum) in `RunMetadata` and in
`redibis.obs.decision` inputs, so any verdict traces back to the exact stack that shaped it.

## 5. Authoring, export, import

### 5.1 Settings UI (Packs page)

- **Active stack** — ordered list of applied layers with id/version/checksum, each
  expandable to show what it contributes; a computed "effective view" per section.
- **Author** — edit sections in place (locale tokens, regex, thresholds, behavior
  policies via the existing catalogue-driven editor, quality sets, masking plans).
  Edits accumulate in a **draft pack** rather than mutating the active config.
- **Diff vs base** — the draft always renders as a diff against its `metadata.base`,
  so an operator sees exactly what they are changing before export.
- **Export** — validate → canonicalize → checksum → download `.rdbpack`.
- **Import** — upload → validate → **dry-run report** (what changes, what conflicts,
  what references fail to resolve) → confirm → apply as a new layer.
- **Deactivate / rollback** — remove a layer; immutable history retained.

### 5.2 CLI

```bash
redibis pack export --out telecom-fr-1.2.0.rdbpack \
                    [--sections config,locale,behavior] [--with-weights]
redibis pack export-default --out redibis-default.rdbpack   # snapshot current defaults
redibis pack validate telecom-fr-1.2.0.rdbpack
redibis pack diff telecom-fr-1.2.0.rdbpack [--against active]
redibis pack import telecom-fr-1.2.0.rdbpack [--dry-run] [--activate] [--mode overlay]
redibis pack list            # active stack
redibis pack remove telecom-fr@1.2.0
```

### 5.3 Runtime load (no UI, no install step)

```yaml
# redibis.yaml
packs:
  - path: /etc/redibis/packs/telecom-fr-1.2.0.rdbpack
    mode: overlay
  - path: s3://cfg/packs/tenant-a-0.3.0.rdbpack
```

```python
from redibis import RedibisConfig
from redibis.pack import load_pack, apply_packs

cfg = apply_packs(RedibisConfig.load(), ["telecom-fr-1.2.0.rdbpack"])
```

Packs resolve once at config-build time into the same effective objects the engines
already consume (`RegexOverrides`, `Thresholds`, `NERModelSpec`, `EffectivePolicySet`,
token tables). **No engine learns about packs** — the resolver is the single merge point,
mirroring how `LocalePackResolver` was specified.

## 6. Import safety (fail closed)

1. Archive limits: max size, max entries, max uncompressed ratio (zip-bomb guard),
   path traversal rejected (`../`, absolute paths, symlinks).
2. File-type allow-list: `.yaml`, `.yml`, `.json`, `.md`. Anything else → reject.
   No `.py`, `.pkl`, `.so`, `.sh` — ever.
3. Every file's sha256 matches `CHECKSUMS.json`; canonical pack SHA recomputed.
4. `requires.redibis` version range satisfied, else reject with a clear message.
5. Every `requires.registries` entry resolves in the live registries (validators,
   behavior actions, NER backends, profilers). Unresolved → reject (or degrade, for
   NER weights only).
6. Each section validated by its **existing** validator — pack import adds no new
   permissive path: `RegexOverrides` rules, behavior-policy schema + registry
   validation, config allow-list + `RedibisConfig.validate()`, masking plan schema.
7. Behavior policies imported as **inactive drafts** by default; activation follows
   the runtime's approve/simulate lifecycle.
8. Optional signature verification (enterprise); allow-list of trusted authors.
9. Import is audited: actor, timestamp, pack id/version/checksum, sections applied,
   prior active stack. Stored with policy lifecycle records, outside ODCS contracts.

Contract invariants are untouched: packs never write contracts, never carry masking
keys, never contain raw data values, and `ContractStore.upsert()` remains the only
contract writer.

## 7. The default pack (this is also the test strategy)

Today's shipped behavior is exported as `redibis-default@<engine-version>` and becomes
the base layer — not a special case, just the first layer. That gives a strong,
self-checking test loop:

**T1 — Default snapshot.** `redibis pack export-default` produces a pack; a golden
copy is committed under `tests/data/packs/redibis-default.rdbpack`.

**T2 — Round-trip identity.** export → import → export produces a byte-identical
canonical form and the same pack SHA. Formatting/key-order changes must not alter the SHA.

**T3 — Scan parity (the core gate).** Run the regression corpus twice: once on stock
config, once after `pack import redibis-default.rdbpack`. Every verdict, entity,
confidence, quality rule, and contract output must be **identical**. This is the
proof that the pack mechanism is behavior-neutral.

**T4 — Drift detection.** CI regenerates the default pack from current code and diffs
it against the committed golden. A diff means someone changed shipped defaults —
the test fails until the golden is updated deliberately, with the change visible in review.

**T5 — Overlay semantics.** Apply a small test pack on top of default; assert only the
declared keys changed and everything else is byte-identical to T3 output.

**T6 — Locale pack end-to-end.** Import `fr-FR` pack → scan the synthetic French table
→ NIR detected as `NATIONAL_ID`, `06…` detected via FR region, EG plate **not** detected.

**T7 — Import security.** Negative tests: zip bomb, path traversal, `.py` entry,
checksum mismatch, version range violation, unresolved validator/action reference,
credential key in `config/`, oversized document, duplicate policy id.

**T8 — Degraded import.** Pack referencing an absent NER model imports with a warning,
NER disabled, regex path unaffected.

**T9 — Layer audit.** After a multi-layer import, `RunMetadata` and decision records
list every layer's id/version/checksum in order.

## 8. Phases

**Phase 0 — Parity corpus.** (unchanged from the locale plan) Snapshot current detector,
quality, and contract output on the test tables. Gate for everything below.

**Phase 1 — Pack format + reader/writer.** `redibis/pack/` — manifest models, canonical
serialization, checksums, archive read/write (zip primary, tar.gz accepted), archive
safety limits. No config wiring yet. Tests: T2, T7.

**Phase 2 — Default pack export.** `redibis pack export-default` + config allow-list +
golden fixture + drift test. Tests: T1, T4.

**Phase 3 — Resolver and import.** `apply_packs()` single merge point; layering and
precedence; `packs:` config key; CLI `import/validate/diff/list/remove` with dry-run.
Tests: T3 (the parity gate), T5, T9.

**Phase 4 — Locale sections.** Everything in `LOCALE_PACK_DESIGN.md` Phases 1–5
(context-token extraction, normalizers, NER phrase tiers, phone/geo parameterization,
EG catalog split) now delivered *as pack sections* rather than a separate mechanism.
Ship built-in `ar-EG` (auto-selected, preserving today's behavior) and `fr-FR` reference
pack. Tests: T6, plus locale parity.

**Phase 5 — Behavior + quality + masking sections.** Behavior policies ride the runtime's
lifecycle (import as drafts); quality rule sets and masking plan templates by name.
Requires Behavior Policy Runtime Phase 4 (PII adapter authoritative).

**Phase 6 — Settings UI.** ✅ Packs tab: active stack, export-default download,
import dry-run → confirm (optional behavior activate), remove layer. REST
`/api/rdbpack/*`. Full draft authoring / diff-vs-base UI deferred.

**Phase 7 — Distribution extras.** Fat packs (`--with-weights`), signatures + trusted
author allow-list, S3-hosted pack refs, pack registry listing.

## 9. Open decisions

1. **Archive format:** zip primary (browser-friendly for Settings upload/download),
   tar.gz accepted on import. Extension `.rdbpack` in both cases. *Default: yes.*
2. **Are packs mutable?** No — `(id, version)` is immutable and content-addressed;
   editing produces a new version. *Default: immutable, matching behavior policies.*
3. **Default import activates behavior policies?** No, drafts only. *Default: explicit
   `--activate` + role check.*
4. **Multi-tenancy:** optional `metadata.tenant`, not required in v1.
5. **Fat-pack weight storage:** embedded vs sidecar file with digest. *Default: sidecar
   referenced by digest, to keep the core format small and diffable.*

## 10. Risks

| Risk | Mitigation |
|---|---|
| Pack becomes a second config system that drifts from `RedibisConfig` | packs resolve *into* existing objects at one merge point; no engine reads a pack |
| Secrets leak into an exported pack | export allow-list + import rejection of excluded keys + T7 test |
| Import silently changes production behavior | mandatory dry-run diff in UI and CLI; layer audit; immediate rollback |
| Malicious pack | data-only file allow-list, archive limits, checksums, registry resolution, optional signing |
| Default pack and code defaults diverge | CI drift test (T4) fails the build |
| Pack sprawl per customer | immutable versions, `metadata.base` lineage, diff-vs-base in UI |
| NER weights bloat | reference-by-digest default; fat packs opt-in and clearly labelled |
