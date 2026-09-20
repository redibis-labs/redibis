# Design: pluggable / uploadable NER models + slim Docker image

Status: implemented · Phase 1–5 complete (2026-06-12) · Targets `redibis.pii`

## Goals

1. **No NER weights baked into the Docker image.** GLiNER weights (~500 MB–1.2 GB) and spaCy models are removed from the image; the image ships code only.
2. **Bring-your-own model.** The operator mounts or uploads one or more NER models and points redibis at the active one by path.
3. **One config knob.** `pii.ner.model_path` in `redibis.yaml` (the existing config spine) selects the active model. Switching models = editing one line, no rebuild.
4. **Presidio keeps working independently.** The regex/Presidio path must not silently require spaCy when only pattern recognizers are used.
5. **Extensible beyond GLiNER.** GLiNER-from-local-path is the first backend, but the interface must allow HF token-classification and spaCy backends later ("could it be another model?" — yes, see §4).

## Non-goals (this iteration)

Model fine-tuning, multi-model ensembles per scan, GPU scheduling, model versioning/rollback UI.

---

## 1. Current state (why this is needed)

| Problem | Where |
|---|---|
| Model ID hardcoded as module constant `GLINER_MODEL = "urchade/gliner_multi-v2.1"` | `pii/detector.py:24` |
| `GlinerConfig(model_id, device, batch_size)` exists in `config.py` but **detector ignores it** — config and runtime are disconnected | `config.py:53`, `detector.py` |
| `GLiNER.from_pretrained()` downloads from HF hub at first use → image must either pre-bake weights or have network at runtime | `detector.py:52` |
| Module-level singletons `_gliner_model`, `_presidio_nlp_engine` make the model unswappable within a process | `detector.py:30–31` |
| `AnalyzerEngine(supported_languages=["en"])` boots a full spaCy pipeline even though redibis only registers **pattern recognizers** (spaCy NER results are unused) | `detector.py:40` |
| GLiNER labels hardcoded (`GLINER_LABELS`) — a custom model with different labels can't express them | `detector.py:25` |

## 2. Architecture

New module: **`redibis/pii/ner_backend.py`** (pure interface, no heavy imports) and **`redibis/pii/ner_registry.py`**.

```
RedibisConfig.pii.ner ──► NERModelRegistry.load(spec) ──► NERBackend (cached)
                                                              │
detector.detect_pii(df, ner_backend=...)  ◄───────────────────┘
        │
        ├── _run_presidio(values, …)        # regex path, NO spaCy needed
        └── backend.score_values(values, column_name)   # replaces _run_gliner
```

### 2.1 `NERBackend` protocol

```python
class NERResult(TypedDict):
    score: float | None        # best entity score across values
    label: str | None          # best entity label
    match_rate: float | None   # fraction of values with ≥1 entity

class NERBackend(Protocol):
    name: str                  # e.g. "gliner:/models/gliner-ar-v1"
    labels: list[str]
    def score_values(self, values: list[str], column_name: str) -> NERResult: ...
    def health_check(self) -> dict: ...   # loadable? device? param count?
```

`PIIDetection.gliner_score/gliner_label/gliner_match_rate` keep their field names
(contract compatibility) but are documented as "NER engine" scores; add
`ner_engine: str` to `PIIDetection` so contracts record *which* model produced
the evidence — important for audit once models are operator-supplied.

### 2.2 `GLiNERBackend` (first implementation)

```python
class GLiNERBackend:
    def __init__(self, model_path: str, labels: list[str], device="cpu",
                 threshold=0.3, batch_size=8):
        from gliner import GLiNER                      # lazy import
        self.model = GLiNER.from_pretrained(
            model_path, local_files_only=True          # ← never hits HF hub
        )
```

- `local_files_only=True` is the key change: a path under `/models/...` loads offline; a hub ID without local cache **fails fast** with a clear error instead of downloading 1 GB mid-scan.
- Labels come from config / model manifest, not the hardcoded list.
- The `column_name: value` context-prefix trick from the current `_run_gliner` is kept inside the backend (it's GLiNER-specific).

### 2.3 Registry

```python
class NERModelRegistry:
    BACKENDS = {"gliner": GLiNERBackend}     # "hf_token_cls", "spacy" later
    def load(self, spec: NERModelSpec) -> NERBackend   # cached by (type, path)
    def discover(self, models_dir: Path) -> list[NERModelSpec]  # scan /models
    def validate(self, path: Path) -> ManifestReport   # used by web upload
```

`discover()` reads an optional **`redibis-model.json` manifest** next to the
weights:

```json
{
  "type": "gliner",
  "name": "gliner-arabic-pii-v2",
  "labels": ["person", "phone number", "national id", "address", "iban"],
  "language": ["ar", "en"],
  "default_threshold": 0.35
}
```

If the manifest is absent, type is inferred (`gliner_config.json` present → gliner) and the default label set is used.

### 2.4 Decouple Presidio from spaCy

redibis only registers **PatternRecognizers**, so spaCy adds nothing but ~4 s
startup and an image dependency. Replace `_get_presidio_nlp_engine()` with a
no-op engine:

```python
class _NoOpNlpEngine(NlpEngine):           # returns empty NlpArtifacts
    def process_text(self, text, language): return NlpArtifacts.empty(...)
    def is_loaded(self): return True
```

`AnalyzerEngine(registry=registry, nlp_engine=_NoOpNlpEngine(), ...)`.
Result: `pip install presidio-analyzer` **without** `spacy`/model wheels is
sufficient for the regex path → smaller image, faster cold start.
(If a future spaCy NER backend is added, it can hand its pipeline to Presidio —
but that becomes an explicit choice, not a hidden dependency.)

## 3. Configuration

Extend the config spine (replaces `GlinerConfig` usage; keep the old block as a
deprecated alias for one release):

```yaml
pii:
  engines: both           # regex | ner | both   (rename "gliner" → "ner", alias kept)
  ner:
    type: gliner          # backend key in the registry
    model_path: /models/gliner-arabic-pii-v2    # REQUIRED in offline mode
    labels: [person, phone number, email, national id]   # optional, manifest wins
    device: cpu
    threshold: 0.3
    batch_size: 8
    always_run: false     # replaces gliner_always_run
  models_dir: /models     # where discover()/uploads look
```

Resolution order for `model_path` (first hit wins):
1. `pii.ner.model_path` in YAML
2. `REDIBIS_NER_MODEL` env var (the Docker-friendly path — one `-e` flag, no config edit)
3. legacy `pii.gliner.model_id` (deprecation warning; only works if locally cached)
4. none → NER engine disabled, scan proceeds regex-only with a logged notice
   (mirrors today's graceful degradation when `gliner` isn't installed)

## 4. "Could it be another model?" — yes, by design

The registry is a string-keyed adapter table. Planned backends, in order of
likely value:

| Backend key | Wraps | Use case |
|---|---|---|
| `gliner` | `GLiNER.from_pretrained(path)` | now — zero-shot, custom label sets |
| `hf_token_cls` | `transformers` `AutoModelForTokenClassification` + aggregation pipeline | classic NER checkpoints: CAMeL-BERT/AraBERT Arabic NER, bert-base-NER, your own fine-tunes |
| `spacy` | `spacy.load(path)` | existing spaCy pipelines; optionally doubles as Presidio NLP engine |
| `remote` | HTTP call to a model server (vLLM/TEI-style) | share one GPU model across many scan workers |

Adding one = implement `score_values()` + register in `BACKENDS`. The equation
engine (`equations.py`) is untouched: it already consumes only
`PIIDetection` fields (invariant #5), so any backend that fills
`gliner_score/label/match_rate` flows through `strict/balanced/lenient/independent`
unchanged. `Thresholds.gliner_min` is reused as the generic NER floor
(alias `ner_min`).

## 5. Docker layout

```dockerfile
# image: code only — no model weights, no spacy models
RUN pip install "redibis[ner-runtime]"     # presidio-analyzer + gliner lib (code, ~50 MB)
ENV REDIBIS_NER_MODEL=""
VOLUME /models
```

```bash
docker run -v /srv/ner-models:/models:ro \
  -e REDIBIS_NER_MODEL=/models/gliner-arabic-pii-v2 \
  redibis scan data.csv --table telecom.customers
```

- New extra `ner-runtime` = `presidio-analyzer`, `gliner` (library code only) —
  distinct from today's `ner` extra which implies hub downloads.
- Set `HF_HUB_OFFLINE=1` in the image so nothing can phone home; with
  `local_files_only=True` this is belt-and-braces.
- Image size estimate: current ≈ base + torch + weights; new ≈ base + torch
  (torch stays — GLiNER needs it; if even torch must go, only the `remote`
  backend or regex-only mode achieves that).

### Web upload flow (optional, webapp)

```
POST /api/models/upload   (tar.gz/zip)  → extract to {models_dir}/{name}/
                                        → registry.validate() (manifest, loadable, smoke inference on 3 strings)
GET  /api/models                        → discover() listing + active marker
POST /api/models/activate {name}        → updates session GlobalConfig (NOT redibis.yaml on disk)
```

Security requirements for upload (these are not optional):
- archive extraction must guard against **zip-slip** (reject `..`/absolute paths),
- size cap (e.g. 5 GB) and disk-quota check before extraction,
- `trust_remote_code=False` always; reject models whose load would execute code
  (no pickled `*.bin` with custom classes — prefer `safetensors`; warn otherwise),
- uploads land in a quarantine dir, only moved into `models_dir` after
  `validate()` passes,
- the activate endpoint changes the **session** config only; persisting to
  `redibis.yaml` stays a deliberate operator action.

## 6. Detector changes (summary of diff)

- Delete `GLINER_MODEL`, `GLINER_LABELS`, `_gliner_model`, `_get_gliner`.
- `detect_pii(..., ner_backend: NERBackend | None = None)`; `services/pipeline.py`
  builds the backend once per scan from `RedibisConfig` and passes it down
  (no more module-global state → two scans with different models in one
  process work).
- `_run_gliner` body moves into `GLiNERBackend.score_values`.
- `_get_presidio_nlp_engine` → `_NoOpNlpEngine` (§2.4).
- Skip-logic (`presidio_score >= 0.80` short-circuit) stays in `detect_pii`,
  backend-agnostic.

## 7. Sampling note (related question, documented here for the team)

Detection is **sample-based, per column** — not one record, not all records:
Spark input is capped at 5,000 rows, then each column samples up to **100
random non-null values** (`random_state=42`), and regex + NER examine each of
those values individually. `match_rate` = matched/sampled. Good for column
classification; it will miss rare PII (≪1% incidence) in otherwise clean
columns. If exhaustive row-level detection is ever needed, that's a separate
"deep scan" mode — out of scope here, but the `NERBackend` interface supports
it unchanged (just feed it more values; consider exposing
`pii.sample_size` in config rather than the hardcoded 100).

## 8. Rollout plan

1. `ner_backend.py` + `GLiNERBackend` + registry; detector refactor behind the new `pii.ner` config (legacy `pii.gliner` aliased). Tests: offline-load from tmp path, missing-path fast-fail, regex-only fallback.
2. `_NoOpNlpEngine`; drop spaCy from the runtime image; verify regex parity on the golden fixtures.
3. Docker: new `ner-runtime` extra, `HF_HUB_OFFLINE=1`, `/models` volume convention; update docker/{local,azure,gcp} constraint files.
4. (Optional) webapp upload/activate endpoints with the §5 security checklist.
5. Docs: README "bring your own NER model" section + manifest spec.

## 9. Open questions

- Should `engines: "both"` be renamed (`regex,ner`) as part of the CLI grammar cleanup from the pre-release review, or aliased now and renamed later? (Suggest: alias now.)
- `PIIDetection` field rename (`gliner_*` → `ner_*`) — breaking for stored contracts; suggest keeping field names, adding `ner_engine`, and renaming only at a 2.0.
- Does the Cloudera deployment need multi-tenant model isolation (per-team models_dir), or is one shared `/models` enough? **Partially addressed:** `pii.models_dir` in YAML, session `pii_models_dir`, CLI/API `--models-dir`, and `REDIBIS_MODELS_DIR` env (precedence: explicit override → session → env → `/models`).
