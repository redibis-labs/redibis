# Modular / Swappable NER — design

> **Status: implemented** (Phases 1–5). See [`docs/NER_BACKENDS.md`](NER_BACKENDS.md) for the operator/developer runbook and [`docs/NER_MODULAR_DESIGN.md`](NER_MODULAR_DESIGN.md) for the build sheet.

**Question being answered.** How do we make the NER layer modular so we can (1) plug in *any* NER
model in future, (2) **swap** the model but keep the **same calling API** (strategy pattern), (3) in
**deep scan** run **many different models**, (4) call the **same model multiple times, each pass for
different entities**, and (5) emit **md/json artifacts** as context for an LLM call.

**Key finding: the strategy pattern already exists — we extend it, we don't rebuild it.**
`redibis/pii/ner_backend.py` already defines a `Protocol` interface and `redibis/pii/ner_registry.py`
already defines a type→class factory. Four small, additive changes deliver everything above without
breaking the current single-model path.

---

## 1. What already exists (reuse)

```python
# redibis/pii/ner_backend.py  — the STRATEGY interface (already a Protocol)
@runtime_checkable
class NERBackend(Protocol):
    name: str
    labels: list[str]
    def score_values(self, values: list[str], column_name: str) -> NERResult: ...  # best-of only
    def health_check(self) -> dict: ...

class GLiNERBackend:  # one concrete strategy (local-only GLiNER weights)
    @property
    def name(self) -> str: return f"gliner:{self._model_path}"
    def score_values(...): ...   # uses self.labels (fixed at construction)
```
```python
# redibis/pii/ner_registry.py  — the FACTORY / REGISTRY
class NERModelRegistry:
    BACKENDS = {"gliner": GLiNERBackend}          # type string → strategy class
    def discover(models_dir) -> list[NERModelSpec] # find models on disk (manifest redibis-model.json)
    def spec_from_path(path) -> NERModelSpec        # {type, path, name, labels, language, default_threshold}
    def load(spec, ...) -> NERBackend               # instantiate + cache
    def try_load_pii(pii_config) -> NERBackend | None  # ← returns ONE backend today
```

**Design patterns already in play:** Strategy (`NERBackend`), Abstract-Factory/Registry
(`NERModelRegistry.BACKENDS` + `discover`/`load`), Value Objects (`NERModelSpec`, `NERResult`).

### Gaps vs the five goals
| Goal | Gap today |
|---|---|
| Swap model, same API | ✅ already (Protocol) — just formalise the method name |
| Many different models at once (deep scan) | `try_load_pii` returns **one** backend; no ensemble |
| Same model, different entities per call | `score_values` uses **fixed** `self.labels`; no per-call labels |
| Multi-entity result | `NERResult` returns only the **single best** `{score,label,match_rate}` |
| md/json artifacts for LLM | none emitted from the NER layer |

---

## 2. Target design (all additive)

### 2.1 Richer result DTO (keep the old one for back-compat)
```python
# ner_backend.py
@dataclass
class NERHit:
    label: str          # canonical entity, e.g. PHONE_NUMBER
    score: float        # best confidence for this label on the column
    match_rate: float   # fraction of sampled values that fired this label

@dataclass
class NERReport:
    model: str          # backend.name  (e.g. "gliner:/models/eg-pii")
    labels_requested: list[str]
    hits: list[NERHit]  # ALL entities discovered (multi-entity), sorted by score
    def best(self) -> NERHit | None: ...
    def to_result(self) -> "NERResult":   # back-compat: best-of as the old {score,label,match_rate}
        b = self.best()
        return {"score": b.score if b else None, "label": b.label if b else None,
                "match_rate": b.match_rate if b else None}
```

### 2.2 Extend the strategy interface with one label-aware method
Add `analyze(...)` to the Protocol; keep `score_values` as a thin best-of wrapper so **nothing that
calls the old method breaks**.
```python
@runtime_checkable
class NERBackend(Protocol):
    name: str
    labels: list[str]
    def analyze(self, values: list[str], column_name: str, *,
                labels: list[str] | None = None) -> NERReport: ...   # NEW — labels override per call
    def score_values(self, values, column_name) -> NERResult:        # keep (default: analyze().to_result())
        ...
    def health_check(self) -> dict: ...
```
- `labels=None` → use `self.labels` (today's behaviour).
- Passing `labels=[...]` → run this model **for that entity set only** → satisfies "same model, called
  multiple times for different entities". In `GLiNERBackend` this is a one-line change:
  `self._model.batch_predict_entities(texts, labels or self.labels, threshold=...)`, and collect
  **all** entities into `NERReport.hits` instead of keeping only the top score.

### 2.3 Registry stays the only place a new model type is added
Adding *any* future model = **one line** + a class implementing the Protocol:
```python
NERModelRegistry.BACKENDS = {
    "gliner":  GLiNERBackend,
    "spacy":   SpaCyNERBackend,        # future
    "hf_token": HFTokenClassBackend,   # future (HuggingFace token-classification)
    "presidio_ner": PresidioNERBackend,# future (wrap Presidio's own NER)
    "http":    RemoteNERBackend,       # future (Adapter over an HTTP NER microservice)
}
```
`spec.type` (from the model's `redibis-model.json` manifest, or `infer_type`) selects the class.
External/remote models are just an **Adapter** that implements `analyze()` — the rest of the system
never knows the difference. This is the "swap the model, same class call" you asked for.

### 2.4 Orchestrator for "many models" and "model × entity-group matrix"
A new **Composite** that holds N backends and runs a matrix — this is what deep scan calls.
```python
# redibis/pii/ner_ensemble.py  (new)
@dataclass
class NERPass:
    model: str
    labels: list[str]
    report: NERReport

class NEREnsemble:
    def __init__(self, backends: list[NERBackend]): self.backends = backends

    @classmethod
    def from_config(cls, pii_config, *, models_dir=None) -> "NEREnsemble":
        # load every model listed in pii.ner.models (or the single resolved model) via the registry
        ...

    def run_column(self, values, column_name, *,
                   label_groups: list[list[str]] | None = None) -> list[NERPass]:
        groups = label_groups or [None]           # None = each model's own labels
        passes = []
        for backend in self.backends:             # many DIFFERENT models
            for labels in groups:                 # same model, many ENTITY sets
                passes.append(NERPass(backend.name, labels or backend.labels,
                                      backend.analyze(values, column_name, labels=labels)))
        return passes
```
- Deep scan mode → pass several backends (different models).
- Multi-entity mode → pass several `label_groups` for one (or each) model.
- The matrix (`models × label_groups`) is exactly the flexibility requested; each cell is one
  `NERPass` with a full `NERReport`.

### 2.5 Artifacts for the LLM (md + json)
The ensemble (or the deep-scan producer wrapping it) emits one **`EvidenceArtifact`** (the shape from
`docs/OBSERVABILITY_AND_DEEPSCAN_PLAN.md`) per model per run + a merged view:
```
evidence/ner.{model}.{table}.json     # per-column NERReport hits (label, score, match_rate)
evidence/ner.{model}.{table}.md       # human/LLM-readable: "model X found PHONE_NUMBER on col Y (0.88, 74%)"
evidence/ner.summary.{table}.json     # agreement across models (which models agree per column/entity)
```
The **json** is the machine input; the **md** is the LLM context. No raw cell values — only
`label/score/match_rate/model/labels` (honours the no-PII-in-logs invariant).

---

## 3. How the four asks map onto the design

| Ask | Mechanism |
|---|---|
| Add any NER model in future | Implement `analyze()` in a new class + register in `BACKENDS` (Strategy + Registry). Remote model = Adapter (`http`). |
| Swap model, same calling API | Everyone calls `NERBackend.analyze()` / `NEREnsemble.run_column()`; the concrete class is chosen by `spec.type`. Callers never change. |
| Deep scan: many different models | `NEREnsemble([m1, m2, …]).run_column(...)` iterates backends. |
| Same model, different entities each call | `analyze(..., labels=[...])` per call; ensemble loops `label_groups`. |
| md/json as LLM context | Ensemble/producer writes `EvidenceArtifact` (md+json) per model + a merged agreement view. |

---

## 4. Config surface (additive, reflective dataclasses)
```yaml
pii:
  ner:
    model_path: /models/eg-pii        # existing single-model path (unchanged default)
    labels: []                        # NORMAL scan: single GLiNER entity set — editable via CLI --ner-labels / config / Settings UI (see PII_SCAN_LOGGING_AND_EXPORT_PLAN.md §B5). Empty = manifest/defaults.
    models:                           # NEW — deep-scan ensemble (optional; empty = single model)
      - { type: gliner, path: /models/eg-pii }
      - { type: gliner, path: /models/multilingual-pii }
      - { type: http,   path: "http://ner-svc:8080/analyze" }   # future adapter
    label_groups:                     # NEW — run each model once per group (multi-entity)
      - [PERSON, LOCATION]
      - [PHONE_NUMBER, EMAIL_ADDRESS]
      - [EG_NATIONAL_ID, PASSPORT]
```
Mirror the existing dataclass style in `config.py` (`NERConfig` gains `models: list` +
`label_groups: list`). Reflective load/save wires it automatically. **Defaults preserve today's
behaviour**: no `models` → single resolved model; no `label_groups` → one pass with the model's own
labels.

---

## 5. Call-site changes (small, when we implement)
- `pii/detector.py` — `detect_pii` accepts either a single `ner_backend` (today) **or** a
  `NEREnsemble`; `_run_ner` becomes `_run_ner_ensemble` returning `list[NERPass]`; populate the new
  `PIIDetection.ner_hits` (from the PII-logging plan) from the passes.
- `pii/ner_registry.py` — add `load_many(specs)` / `NEREnsemble.from_config`.
- `agents/deep_scan.py` — the `ner.*` producers call the ensemble and write the artifacts (§2.5).
- `equations.py` — unchanged in shape; it now sees multiple engine signals per column and can fuse
  them (a column is stronger evidence when several independent models agree).

---

## 6. Back-compat & invariants
- `NERResult`, `score_values`, `try_load_pii`, and the single-`ner_backend` `detect_pii` path all
  keep working — `analyze()` is additive and `score_values` delegates to `analyze().to_result()`.
- New model types are **local-first / air-gap-safe**; the `http` adapter is optional and only used if
  configured (no egress by default), consistent with the deployment posture.
- Artifacts/logs carry labels + scores only — never raw values.
- Heavy deps (gliner/torch, and any future spaCy/HF/torch) stay **lazy-imported inside the backend
  class** (as GLiNER already does) and behind extras — the base install stays lean.

---

## 7. One-paragraph summary
Keep `NERBackend` (Protocol = Strategy) and `NERModelRegistry` (Factory) as-is; add a label-aware
`analyze() -> NERReport` (multi-entity result) with `score_values` delegating for back-compat; add a
small `NEREnsemble` Composite that runs a **models × label-groups** matrix; register any future model
(local or remote-via-Adapter) with one line in `BACKENDS`; and have the ensemble emit md+json
`EvidenceArtifact`s for the LLM. This gives swappable models behind one calling API, many models in
deep scan, the same model re-invoked per entity set, and LLM-ready artifacts — all additive, all
honouring the air-gap and no-raw-PII invariants.
