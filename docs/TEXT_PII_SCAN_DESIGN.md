# Free-Text PII Scan — API + CLI + Playground UI + De-identification Policy

**Status:** Phase 1 + Phase 2 implemented (library, REST, CLI, playground).
See also `docs/FREE_TEXT_PII_IMPLEMENTATION_PLAN.md` and
`docs/TEXT_PII_API_CLI_TUTORIAL.md` (operator how-to).
**One line:** take an arbitrary string, return every PII span (`start`, `end`, `entity_type`,
`score`, `engine`), **then apply a de-identification policy** (mask / hash / FPE / fake /
redact — general rule + per-entity overrides) and return the de-identified text.
**Related:** `redibis/pii/detector.py`, `redibis/pii/presidio_nlp.py`,
`redibis/pii/regex_catalog.py`, `redibis/pii/phone_engine.py`, `redibis/pii/ner_backend.py`,
`redibis/masking/` (plan.py, engine.py, transforms.py), `redibis/webapp/backend.py`,
`CLAUDE.md` invariants (esp. #11 masking keys).

---

## 1. Why this is a new path (not a wrapper over `detect_pii`)

The existing PII engine is **column-oriented**:

- `detect_pii(df, …)` scans DataFrame *columns* and returns one `PIIDetection` per column
  with aggregate scores (`presidio_score`, `gliner_score`, `match_rate`) and `detected=False`;
- the equation engine then turns column evidence into a column-level verdict;
- `presidio_nlp.py` builds a **pattern-only** analyzer with a no-op NLP engine — Presidio
  returns matches but redibis consumes them as column stats, not character offsets;
- the GLiNER `NERReport` keeps only the best label + score + match rate — **no spans**.

Free-text scanning is the inverse: one string in, **many spans out**, each with exact
`start`/`end`. That is a different unit of work. The good news: the *evidence sources*
are all reusable — the regex catalog, the phone/telecom gate, and the NER model. What's
new is a **span-producing wrapper** around them and a **span merge/resolution** step.

This feature is **read-only and stateless** — it never writes a contract, never touches
the contracts bucket, never persists input by default. It sits beside the scan engine,
not inside the contract pipeline. (Invariants 1, 6 untouched: no contract writes; the
detector still produces evidence, and a small resolver decides the span verdict.)

## 2. Object model

```python
# redibis/pii/text_scan.py  (new module)

@dataclass(frozen=True)
class PIISpan:
    start: int                 # code-point offset, inclusive
    end: int                   # code-point offset, exclusive (text[start:end] == matched)
    entity_type: str           # canonical: PHONE_NUMBER, EMAIL_ADDRESS, PERSON, …
    text: str                  # the matched substring (redactable; see privacy note)
    score: float               # 0..1 confidence after resolution
    engine: str                # "regex" | "ner" | "phone" | "llm"
    recognizer: str = ""       # pattern name / NER label / model id
    context_boost: bool = False # nearby context word raised the score
    validator: str = ""        # e.g. "luhn:pass", "phonenumbers:valid"
    is_proposal: bool = False   # true for LLM-only findings — surfaced distinctly,
                                # never silently merged with deterministic spans

# Offsets are Unicode CODE-POINT indices, not UTF-16. Every response echoes
# offset_unit: "unicode_codepoint". Enforce text[start:end] server-side in Python;
# the browser renderer must walk code points, not JS string positions. This is a
# correctness contract (Arabic + emoji + combining chars break naive UTF-16 offsets).

@dataclass(frozen=True)
class TextScanResult:
    spans: tuple[PIISpan, ...]           # sorted by start
    entity_counts: Mapping[str, int]     # {"PHONE_NUMBER": 2, "EMAIL_ADDRESS": 1}
    engines_ran: tuple[str, ...]
    language: str
    char_count: int
    truncated: bool                      # input exceeded max length
    # NB: no raw input echoed at this level beyond span.text

@dataclass(frozen=True)
class TextScanConfig:
    engines: str = "both"        # "regex" | "ner" | "both"
    language: str = "en"         # drives NER phrases + phone regions (see LocalePack)
    min_score: float = 0.35
    return_text: bool = True     # include span.text; false = offsets only (max privacy)
    resolve: str = "priority"    # overlap policy: "priority" | "longest" | "all"
    use_llm: bool = False        # optional local-LLM refinement (off by default)
    max_chars: int = 50_000      # hard input cap
    entities: tuple[str, ...] = ()   # restrict to these entity types (empty = all)
```

## 3. Engine composition

```
text ──► RegexTextRecognizer   ─┐   (Presidio PatternRecognizers → RecognizerResult spans)
     ──► PhoneTextRecognizer    ─┤   (candidate spans → phonenumbers gate → validated spans)
     ──► NerTextRecognizer      ─┤   (GLiNER entities WITH offsets)
     ──► [LlmTextRefiner]       ─┘   (optional; local LLM confirms/adds ambiguous spans)
                                 │
                                 ▼
                        SpanResolver (overlap merge, context boost, score floor)
                                 ▼
                          TextScanResult (sorted spans + counts)
```

### 3.1 Regex — reuse the catalog, keep the offsets

`presidio_nlp.build_pattern_analyzer_engine()` already registers every catalog pattern
as a Presidio `PatternRecognizer`. `AnalyzerEngine.analyze(text, language)` returns
`RecognizerResult(entity_type, start, end, score)` — **the offsets we need are already
there**, redibis just never used them. The text path calls `analyze()` directly and maps
each result to a `PIISpan(engine="regex")`. Context boosting reuses the catalog's
`context_hints` against a window around each match.

### 3.2 Phone — reuse the telecom gate

The catalog's phone patterns produce candidate spans; each candidate string goes through
the existing `phone_engine` / `phonenumbers` validation with the config `language` →
region. Validated → `engine="phone"`, `validator="phonenumbers:valid"`; unvalidated
candidates below `phone_min` are dropped. This preserves the MSISDN gate logic that keeps
CGI/LAC/TAC out of telephony.

### 3.3 NER — needs a span-returning method (the one real gap)

GLiNER's `predict_entities()` **returns spans** (`{start, end, label, score}`) — but the
current `GLiNERBackend.analyze()` collapses them into a single best label + match rate for
column scanning. Add a sibling method:

```python
class NERBackend(Protocol):
    def analyze_text(self, text: str, *, labels: list[str] | None = None) -> list[NERSpan]:
        ...   # returns per-entity spans; default impl raises NotImplementedError
```

Implement `analyze_text` on `GLiNERBackend` (thin call to `predict_entities`, mapped to
`PIISpan(engine="ner")` via the existing label→canonical map, using the LocalePack phrase
map for the language). `RemoteNERBackend` gets the same method over HTTP. Backends that
can't do spans simply don't contribute to the text path — no breakage.

### 3.4 LLM — optional, local, off by default

When `use_llm: true`, a local provider (Ollama/vLLM via `get_provider`, routed through
`guarded_model_call`) is asked to confirm low-confidence spans or catch entities the
deterministic engines missed, returning strict JSON `{start, end, type}`. **Never a cloud
model on this path by default** (same rule as local codegen). LLM spans are marked
`engine="llm"` and, like all LLM output, are proposals — the UI flags them distinctly.

### 3.5 SpanResolver

- **Overlap policy** (`resolve`): `priority` keeps the highest-authority engine on a
  contested span (validated phone > regex-with-validator > NER > llm), `longest` keeps the
  widest match, `all` returns everything (useful for the playground's "show me the raw
  evidence" mode).
- **Context boost** raises score when a catalog context hint sits within N chars.
- **Score floor** drops anything below `min_score` after boosting.
- Deterministic ordering: sort by `start`, then by `-score`, then engine priority.

## 4. De-identification policy — the second half of the feature

Detection answers *where is the PII*. The policy answers *what to do with each one*.
**Do not build a new masking engine** — `redibis.masking` already has the whole
vocabulary and the security controls: `ColumnMaskRule.strategy` ∈
{`redact` (total mask), `hash`, `fpe`, `fake`, `passthrough`, …}, a `MaskingEngine` that
applies rules, per-run `RunKeys` (invariant 11: keys minted per run, never persisted into
output), position slicing (`start_index`/`end_index`), and — crucially — a
`suggest_rule(entity, ...)` that **already maps entities to sensible strategies**
(national-id/passport/card → FPE, email/phone/name/address → fake-with-format-preserved,
security tokens → redact, indirect identifiers → deterministic FPE).

The text-scan policy is a **thin span-level adapter over that engine**, plus a
declarative, overridable policy document.

### 4.1 The strategy palette (reused, not reinvented)

| Strategy | What it does to the span | Reversible? | Typical entities |
|---|---|---|---|
| `redact` | replace with a fixed token, e.g. `[PHONE_NUMBER]` or `██████` | no | secrets, free-text you never need back |
| `mask` | keep some chars, star the rest (`****1234`) via `start_index`/`end_index` | no | card/phone last-4 display |
| `hash` | HMAC-SHA256, truncatable — same input → same token within a run | no (pseudonymous) | join keys, indirect IDs |
| `fpe` | format-preserving encryption — output looks like the input, **reversible with the key** | yes | national ID, IBAN, card |
| `fake` | realistic synthetic value, optionally preserving format/domain/country/gender | no | name, email, address, DOB |
| `passthrough` | leave as-is (explicit "reviewed, keep it") | n/a | non-sensitive matches |

Reversibility and key handling come straight from the existing engine — no new crypto
in the text path. Keys are per-run and never appear in the response or any log
(invariant 11).

### 4.2 Policy object model

```python
# redibis/pii/text_policy.py  (new — a declarative doc; applies via redibis.masking)

@dataclass(frozen=True)
class EntityRule:
    entity_type: str                 # "PHONE_NUMBER"; "*" = the general/default rule
    strategy: str                    # redact | mask | hash | fpe | fake | passthrough
    params: Mapping[str, JSONValue] = {}   # engine params: {kind, preserve, algo, mode, key_ref, start_index, …}
    min_score: float = 0.0           # only apply above this confidence
    replacement: str = ""            # redact token override; default "[<ENTITY_TYPE>]"

@dataclass(frozen=True)
class DeidPolicy:
    id: str
    version: str
    default: EntityRule              # the GENERAL rule (entity_type="*")
    overrides: tuple[EntityRule, ...] = ()   # PER-ENTITY rules; most specific wins
    on_overlap: str = "priority"     # inherits the resolver's chosen span set
    unknown_entity: str = "redact"   # strategy for an entity with no rule + no default match
    locale: str = "en"               # drives fake-value locale
    checksum: str = ""

def resolve_rule(policy: DeidPolicy, entity_type: str, score: float) -> EntityRule:
    """Per-entity override (if score ≥ its min_score) else the default rule."""
```

YAML the user actually writes (Settings-authored or hand-written; ships as a
**pack `masking/` section**, so it's portable exactly like everything else):

```yaml
apiVersion: redibis.io/deid-policy/v1
id: telecom-support-notes
version: "1.0.0"
locale: en
default:                       # ← general rule for everything not overridden
  strategy: redact             # safest default: total mask
overrides:
  - entity_type: PERSON
    strategy: fake             # keep readable, non-real
    params: { kind: name, preserve: { gender: true } }
  - entity_type: EMAIL_ADDRESS
    strategy: fake
    params: { kind: email, preserve: { domain: true } }
  - entity_type: PHONE_NUMBER
    strategy: mask             # show last 4 for support agents
    params: { start_index: 0, end_index: -4 }
  - entity_type: NATIONAL_ID
    strategy: fpe              # reversible under key for re-identification
    params: { mode: ff3, alphabet: digits, key_ref: k1 }
    min_score: 0.6
  - entity_type: CREDIT_CARD
    strategy: redact
unknown_entity: redact
```

### 4.3 Precedence — one clear order

```
per-entity override (if span.score ≥ rule.min_score)
        ▼ else
default rule (entity_type "*")
        ▼ else (no default configured)
unknown_entity strategy
```

Two safe conventions, both defaulting to protection:

- **Fail-closed default.** If no policy is supplied at all, the endpoint returns detection
  only (no de-id) — it never silently passes PII through. If a policy *is* supplied but an
  entity matches no rule, `unknown_entity` (default `redact`) applies — a newly-detected
  entity type is masked, not leaked.
- **Score gate per rule.** `min_score` lets you say "only FPE the national ID when we're
  ≥0.6 sure" — low-confidence spans fall through to the safer default.

### 4.4 Applying the policy to spans (not columns)

`redibis.masking` transforms operate on a *value*. A span already isolates the value
(`text[start:end]`), so application is: for each span (right-to-left by offset so indices
stay valid) → `resolve_rule` → build a one-off `ColumnMaskRule(strategy, params)` →
`MaskingEngine._transform_series` on the single span string → splice the transformed value
back into the text. Right-to-left splicing is the same trick as `--redact`; it keeps every
earlier offset valid as later ones are replaced.

The result carries both views:

```python
@dataclass(frozen=True)
class DeidResult:
    original_spans: tuple[PIISpan, ...]        # what was detected
    deidentified_text: str                     # policy applied
    applied: tuple[SpanAction, ...]            # per span: entity, strategy, before_len, after, rule_id
    policy_id: str
    policy_version: str
    reversible_spans: int                      # count of fpe spans (re-identifiable with key)
    run_key_ref: str                           # which per-run key set was used (not the key)
```

`applied` is the audit trail: every span records which rule fired and which strategy —
so a reviewer can see "phone → mask (last-4), national_id → fpe(k1)" without seeing keys
or raw values beyond what the chosen strategy already reveals.

### 4.5 Where the policy lives (portable, governed)

- A `DeidPolicy` is a **`masking/` section of a Redibis Pack** (`REDIBIS_PACK_DESIGN.md`)
  — authored in Settings, exported, imported, versioned, checksummed like every other
  pack section. No new storage mechanism.
- Multiple named policies can coexist (`telecom-support-notes`, `analyst-export`,
  `full-redact`); the API/CLI selects one by id, or inlines an ad-hoc policy for the
  playground.
- Because it's masking-plan data, `auto_suggest_plan` / `suggest_rule` can **pre-fill a
  starter policy from a scan** — the user tweaks strategies rather than starting blank.

## 5. Library entry (facade)

```python
from redibis.pii import TextPIIScan, TextScanConfig

result = TextPIIScan(TextScanConfig(language="en")).scan(
    "Call John Doe on +20 100 123 4567 or john@acme.com"
)
for s in result.spans:
    print(s.entity_type, s.start, s.end, s.text, s.score, s.engine)
# PERSON 5 13 "John Doe" 0.86 ner
# PHONE_NUMBER 17 33 "+20 100 123 4567" 0.95 phone
# EMAIL_ADDRESS 37 50 "john@acme.com" 0.99 regex
```

Sits next to `PIIScan`/`QualityScan`/`ProfileScan`. Reuses the same `NERModelRegistry`,
regex catalog, and (once built) LocalePack selection, so text and column scanning stay
behaviorally consistent.

Scan **and de-identify** in one call:

```python
from redibis.pii import TextPIIScan, TextScanConfig, DeidPolicy

policy = DeidPolicy.from_yaml("policies/support-notes.yaml")   # or from a pack
out = TextPIIScan(TextScanConfig(language="en")).scan_and_deidentify(text, policy)

print(out.deidentified_text)
# "Call Michael Reed on +20 100 123 **** or m****@acme.com"
for a in out.applied:
    print(a.entity_type, "→", a.strategy)          # PERSON→fake, PHONE_NUMBER→mask, EMAIL_ADDRESS→fake
```

## 5. REST API

Mounted in `redibis/webapp/backend.py` (same FastAPI app as the existing `/api/*`):

```
POST /api/pii/text/scan            # detection only
POST /api/pii/text/scan-batch      # bounded list of texts
POST /api/pii/text/deidentify      # detection + policy application
POST /api/pii/text/suggest-policy  # pre-fill DeidPolicy via suggest_rule
POST /api/pii/text/policies        # save DeidPolicy under masking/policies/
GET  /api/pii/text/entities        # catalogue: entity types + which engine detects them (drives UI)
GET  /api/pii/text/policies        # named de-id policies (from active pack / disk)
GET  /api/pii/text/health          # engines loaded, NER model id, language support
```

Detection rules and NER labels come from the active pack's compiled `RuleSet`
(`PII_GUARD_AND_RULE_UNIFICATION_PLAN.md`), so text scanning stays in lockstep with column
scanning — a pack-added pattern/label appears in both, and in `/entities`, with no code
change. The recognizers here are the same `RegexRecognizer`/`PhoneRecognizer`/`NerRecognizer`
the column scanner uses, not a parallel stack.

### 5.1 `POST /api/pii/text/scan`

Request:

```json
{
  "text": "Call John Doe on +20 100 123 4567 or john@acme.com",
  "language": "en",
  "engines": "both",
  "min_score": 0.35,
  "return_text": true,
  "resolve": "priority",
  "use_llm": false,
  "entities": []
}
```

Response:

```json
{
  "spans": [
    {"start":5,"end":13,"entity_type":"PERSON","text":"John Doe","score":0.86,"engine":"ner","recognizer":"gliner-multi"},
    {"start":17,"end":33,"entity_type":"PHONE_NUMBER","text":"+20 100 123 4567","score":0.95,"engine":"phone","validator":"phonenumbers:valid"},
    {"start":37,"end":50,"entity_type":"EMAIL_ADDRESS","text":"john@acme.com","score":0.99,"engine":"regex","recognizer":"email"}
  ],
  "entity_counts": {"PERSON":1,"PHONE_NUMBER":1,"EMAIL_ADDRESS":1},
  "engines_ran": ["regex","phone","ner"],
  "language": "en",
  "offset_unit": "unicode_codepoint",
  "char_count": 50,
  "truncated": false
}
```

`offset_unit` is always present so a consumer never has to guess the index basis. LLM-only
spans carry `"is_proposal": true`.

### 5.1a `GET /api/pii/text/entities` — capability discovery

Returns the entity catalogue **and which engine detects each**, so the playground's entity
filter and the policy panel's per-entity rows populate from the server (and reflect
pack-added entities automatically) rather than a hardcoded client list:

```json
{
  "entities": [
    {"entity_type":"PHONE_NUMBER","engines":["regex","phone","ner"],"family":"telecom"},
    {"entity_type":"EMAIL_ADDRESS","engines":["regex","ner"],"family":"contact"},
    {"entity_type":"NATIONAL_ID","engines":["regex"],"family":"government_id"},
    {"entity_type":"PERSON","engines":["ner"],"family":"identity"}
  ],
  "language":"en","offset_unit":"unicode_codepoint"
}
```

`family` keys the UI color palette (§7). The list is derived from the active pack's
`RuleSet`, so it is always in sync with what the scanner can actually find.

### 5.2 API rules

- **Stateless, no persistence.** Input is processed in memory; nothing is logged as a
  body (metadata-only logs: char count, entity counts, latency, language). This is the
  same no-raw-value discipline the rest of redibis follows.
- **Input cap** `max_chars` (default 50k) → `413` with `truncated:true` if exceeded, or
  scan-and-flag depending on config.
- **Batch variant** `POST /api/pii/text/scan-batch` accepts `{"texts":[...]}` (bounded
  count) returning parallel results — handy for log/ticket triage.
- **Auth** follows the existing webapp auth; the endpoint is a candidate for the same
  token model as hosted codegen if exposed outside the deployment.
- **`return_text:false`** returns offsets only — for callers that must never receive the
  matched substrings back (redaction pipelines that already hold the source).

### 5.3 `POST /api/pii/text/deidentify`

Request adds a policy (by id from the active pack, or inline):

```json
{
  "text": "Call John Doe on +20 100 123 4567 or john@acme.com",
  "language": "en",
  "policy_id": "telecom-support-notes"
}
```

or inline for ad-hoc use:

```json
{
  "text": "…",
  "policy": {
    "default": {"strategy": "redact"},
    "overrides": [
      {"entity_type": "PHONE_NUMBER", "strategy": "mask", "params": {"end_index": -4}},
      {"entity_type": "PERSON", "strategy": "fake", "params": {"kind": "name"}}
    ]
  }
}
```

Response:

```json
{
  "deidentified_text": "Call Michael Reed on +20 100 123 **** or [EMAIL_ADDRESS]",
  "applied": [
    {"start":5,"end":13,"entity_type":"PERSON","strategy":"fake","rule":"PERSON"},
    {"start":17,"end":33,"entity_type":"PHONE_NUMBER","strategy":"mask","rule":"PHONE_NUMBER"},
    {"start":37,"end":50,"entity_type":"EMAIL_ADDRESS","strategy":"redact","rule":"*"}
  ],
  "policy_id":"telecom-support-notes","policy_version":"1.0.0",
  "reversible_spans":0,"run_key_ref":"run-7f3a"
}
```

Same statelessness/logging/cap rules as `/scan`. The de-id endpoint additionally: never
returns a masking key; marks `reversible_spans` so a caller knows re-identification is
possible under key custody; and applies the **fail-closed** rule (no policy ⇒ 400 "policy
required for deidentify", never silent passthrough).

## 6. CLI

```bash
# inline string
redibis pii text "Call John on +20 100 123 4567" --language en

# from a file or stdin
redibis pii text --file notes.txt
cat notes.txt | redibis pii text -

# output shapes
redibis pii text "…" --json                 # TextScanResult as JSON
redibis pii text "…" --format table         # aligned table (default for TTY)
redibis pii text "…" --redact               # print text with spans replaced by [ENTITY]
redibis pii text "…" --engines regex        # regex only (fast, no model load)
redibis pii text "…" --entities PHONE_NUMBER,EMAIL_ADDRESS
```

`--redact` reconstructs the string with each span replaced by `[<ENTITY_TYPE>]`
(right-to-left by offset so indices stay valid) — the most common ask after "where is it".

Apply a full policy (mask/hash/fpe/fake per entity):

```bash
redibis pii deid "Call John on +20 100 123 4567" --policy support-notes
redibis pii deid --file notes.txt --policy-file ./deid.yaml -o clean.txt
redibis pii deid "…" --default redact --set PHONE_NUMBER=mask --set PERSON=fake  # inline
```

`--policy` selects a named policy from the active pack; `--policy-file` reads a YAML doc;
`--default`/`--set entity=strategy` build an ad-hoc policy without a file. Fail-closed:
`redibis pii deid` with no policy errors rather than passing PII through.

Table output:

```
ENTITY         START  END   SCORE  ENGINE  TEXT
PERSON             5   13   0.86   ner     John Doe
PHONE_NUMBER      17   33   0.95   phone   +20 100 123 4567
EMAIL_ADDRESS     37   50   0.99   regex   john@acme.com
```

## 7. Playground UI

A new **Playground** page in the web app (`redibis/webapp/static/`), reachable from the
nav. Single screen, three zones:

1. **Input** — a large textarea, a "Scan" button, and a controls row: language select,
   engines (regex / NER / both), min-score slider, `use_llm` toggle (only if a local LLM
   is configured), entity-type multiselect.
2. **Highlighted output** — the input text re-rendered with each PII span wrapped in a
   colored `<mark>`; color keyed by entity type (stable legend). Hovering a mark shows a
   tooltip (`entity_type`, `score`, `engine`, `validator`). Overlaps rendered per the
   resolver's choice; an "show all evidence" switch flips `resolve:"all"` to reveal
   competing engine spans.
3. **Findings table** — one row per span: entity type (with its legend color chip),
   matched text, start, end, score, engine, validator. Sortable; a copy-JSON button; a
   "download result" button. Entity-count summary chips above the table.

Rendering the highlight safely: **build the marked HTML from offsets on the client by
splicing the original text**, escaping each slice — never trust server HTML. Sort spans,
walk the string, emit `escape(text[prev:start])` then `<mark class="e-PHONE_NUMBER">…`.
For overlapping spans in `priority` mode there is exactly one layer; in `all` mode,
render the outermost and list the rest in the tooltip/table to avoid nested-mark hell.

Colors: a fixed palette mapped to entity families (identity=blue, contact=green,
financial=amber, telecom=purple, government-id=red, other=grey), defined as CSS classes
so the legend and marks share one source.

Privacy note in the UI: a small banner — "Text is scanned in memory and not stored."
When `use_llm` is on, a second note — "LLM refinement sends this text to your configured
local model." Keep it honest; people paste real data into playgrounds.

### 7.1 Policy panel (the de-identification half)

A fourth zone / tab turns the playground into a **policy authoring surface**, not just a
viewer:

- **Policy selector** — pick a named policy from the active pack, or "ad-hoc".
- **General rule** — one dropdown for the default strategy (redact / mask / hash / fpe /
  fake / passthrough) applied to everything not overridden.
- **Per-entity table** — one row per detected entity type with a strategy dropdown and a
  params popover (mask keep-last-N, fake preserve-domain/format, fpe key-ref, hash
  truncate). Rows are **pre-filled by `suggest_rule`** from the scan, so the user tunes
  rather than starts blank.
- **Preview toggle** — flip the highlighted panel between *detected* (colored spans) and
  *de-identified* (the masked/faked output inline), so the effect of each strategy choice
  is visible immediately. A diff mode shows original vs de-identified side by side.
- **Apply / Export** — "Apply" calls `/deidentify` and shows the clean text + the
  `applied` audit table (entity → strategy → rule). "Save as policy" writes it into the
  pack's `masking/` section (versioned); "Download" gives the de-identified text.
- Honest labels: an FPE row shows a small "reversible with key" chip; the panel states
  that masking keys are per-run and never shown or exported.

This is the sellable demo: paste a support ticket → see the PII lit up → pick "mask phone
last-4, fake names, redact everything else" → get clean text back, with an audit trail.

## 8. Phases

**Phase 1 — Library core** (2–3 days). `redibis/pii/text_scan.py`: `PIISpan`,
`TextScanResult`, `TextScanConfig`, `RegexTextRecognizer` (map Presidio `analyze()`
offsets), `PhoneTextRecognizer` (reuse gate), `SpanResolver`, `TextPIIScan` facade.
NER excluded for now (regex+phone already covers most structured PII with exact spans).
Tests: golden spans for email/phone/IBAN/credit-card/national-id; overlap resolution;
context boost; offset correctness (`text[start:end]` equals match).

**Phase 2 — NER spans** (2 days). Add `analyze_text()` to `NERBackend` protocol +
`GLiNERBackend` + `RemoteNERBackend`; wire `NerTextRecognizer`; language→phrase map.
Tests: PERSON/ADDRESS spans; label→canonical mapping; graceful skip when no model.

**Phase 3 — REST API** (1–2 days). `/api/pii/text/scan`, `/entities`, `/health`,
`scan-batch`; input cap; metadata-only logging; auth wiring. Tests: contract test,
oversized input, `return_text:false`, batch.

**Phase 4 — CLI** (1 day). `redibis pii text` with `--file`/stdin, `--json`,
`--format table`, `--redact`, `--engines`, `--entities`. Tests: each output shape;
stdin; redaction offset correctness.

**Phase 5 — De-identification policy** (2–3 days). `redibis/pii/text_policy.py`
(`EntityRule`, `DeidPolicy`, `resolve_rule`, YAML load/validate); span-level adapter over
`redibis.masking` (`MaskingEngine` + per-run `RunKeys`); `scan_and_deidentify()` facade;
`/api/pii/text/deidentify` + `/policies`; `redibis pii deid` CLI. Policy lives in the pack
`masking/` section. Tests: each strategy on a span (redact/mask/hash/fpe/fake); per-entity
override beats default; `min_score` gate; fail-closed with no policy; keys never in output;
right-to-left splice keeps offsets valid; `auto_suggest_plan` pre-fill.

**Phase 6 — Playground UI** (3–4 days). Page, controls, client-side highlight splicer,
findings table, legend, privacy banners, copy/download, **and the policy panel** (general
rule + per-entity table + detected/de-identified preview + apply/export). Tests: highlight
offsets match API spans; XSS escaping; overlap rendering; preview matches `/deidentify`
output; "save as policy" round-trips through the pack.

**Phase 7 — LLM refinement** (optional, 1–2 days). `LlmTextRefiner` via local provider +
`guarded_model_call`, strict-JSON span output, `use_llm` plumbed through API/CLI/UI,
proposal marking. Tests: JSON parse hardening; fallback when model absent; never cloud
by default.

**Phase 8 — LocalePack alignment** ✅. Language selection drives regex catalog,
context tokens, NER phrases, phone regions **and fake-value locale** from the active
pack (`RuleSetCompiler.from_stack` + `TextPIIService`), so both detection and
de-identification are multilingual with zero new mechanism.

## 9. Acceptance criteria

1. `TextPIIScan().scan(s)` returns spans where `s[span.start:span.end] == span.text` for
   every span, for all engines.
2. Regex path reuses the existing catalog — a pattern added to the catalog appears in
   text scanning with no extra code.
3. Phone spans pass the same `phonenumbers` gate as column scanning (CGI/LAC/TAC excluded).
4. API is stateless: no request body appears in any log (asserted by test).
5. `return_text:false` yields offsets with empty `text`.
6. CLI `--redact` reproduces the input with spans replaced and all other characters intact.
7. Playground highlight offsets exactly match API spans; injected markup is escaped.
8. NER contributes spans when a model is loaded and is silently skipped when not.
9. LLM refinement is off by default and never calls a cloud model on this path.
10. Adding a language via a LocalePack changes text-scan behavior with no code change.
11. De-id applies the correct strategy per entity: general rule for unmatched entities,
    per-entity override otherwise; `min_score` respected.
12. `/deidentify` with no policy fails closed (400); an unmatched entity is masked via
    `unknown_entity`, never passed through.
13. Masking keys never appear in any response, log, or exported policy (invariant 11);
    FPE spans are counted as reversible.
14. De-identified output equals `MaskingEngine` applied to the same values — text and
    column de-id share one engine (a parity test proves it).
15. A de-id policy exports/imports as a pack `masking/` section unchanged.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Offset drift between engines / Unicode (emoji, combining chars) | operate on Python `str` code points consistently; test with multi-byte input; document that offsets are code-point based |
| Overlapping spans confuse redaction / de-id | deterministic resolver; `--redact` and de-id both walk right-to-left; `all` mode is opt-in and disabled for de-id (needs one non-overlapping span set) |
| People paste real PII into a hosted playground | in-memory only, metadata-only logs, explicit UI banner, optional auth/token gate before external exposure |
| NER latency on long text | input cap; NER is opt-in via `engines`; chunk long text with offset re-basing |
| XSS via highlighted output | client-side splice with per-slice escaping; never render server-built HTML |
| LLM hallucinated offsets | validate every returned span against the source (`text[start:end]` sanity check) before accepting |
| Policy passes PII through by mistake | fail-closed default; `unknown_entity: redact`; a test asserts no detected span survives verbatim under a redact-default policy |
| Length change from fake/redact breaks a caller expecting fixed offsets | de-id returns the new text + `applied` actions; callers needing stable positions use `mask` (length-preserving) or `fpe` (format-preserving); documented per strategy |
| FPE reversibility misunderstood as anonymization | UI "reversible with key" chip; `reversible_spans` in the response; docs state FPE is pseudonymization, not anonymization |
```

