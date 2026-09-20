# How to add a NER backend type

The NER layer uses a **strategy pattern**: subclass `BaseNERBackend`, implement `analyze()`, register in `NERModelRegistry.BACKENDS`, ship a `redibis-model.json` manifest.

## Interface

```python
# redibis/pii/ner_backend.py
class BaseNERBackend(ABC):
    name: str
    labels: list[str]

    def analyze(self, values, column_name, *, labels=None) -> NERReport: ...  # implement
    def score_values(self, values, column_name, *, labels=None) -> NERResult: ...  # inherited
    def health_check(self) -> dict: ...  # implement
```

- `NERReport` carries **all** entity hits for the pass (`NERHit`: label, score, match_rate).
- `score_values()` defaults to `analyze(...).to_result()` — single best hit for back-compat.
- Pass `labels=` to run the same loaded model with a different entity set (multi-pass / deep scan).

## Steps to add a backend

1. **Create a class** under `redibis/pii/backends/` (or extend `ner_backend.py` for built-ins).
2. **Lazy-import** heavy deps inside methods (`_ensure_loaded`), never at module top level.
3. **Implement `analyze()`** — aggregate per-label hits across sampled values; never log raw cell values.
4. **Register** in `NERModelRegistry.BACKENDS`:
   ```python
   NERModelRegistry.BACKENDS["mytype"] = MyBackend
   ```
   Or use the `_backend_classes()` hook (see `http` adapter).
5. **Manifest** — `redibis-model.json` beside weights (or config URL for remote):
   ```json
   {
     "type": "mytype",
     "name": "my-ner-model",
     "labels": ["person", "phone number"],
     "language": ["en"],
     "default_threshold": 0.3
   }
   ```
6. **Tests** — stub the runtime; assert `analyze()` multi-hit + `score_values()` back-compat.

## Built-in types

| `type` | Class | Notes |
|---|---|---|
| `gliner` | `GLiNERBackend` | Local weights only (`local_files_only=True`) |
| `http` | `RemoteNERBackend` | POST `{values, labels, column}`; opt-in remote |

## Normal scan vs deep scan

| Mode | Entry | Models | Label passes |
|---|---|---|---|
| **Normal** `redibis scan` | `NERModelRegistry.try_load_pii()` | **One** | **One** (`pii.ner.labels`) |
| **Deep scan** | `NEREnsemble.from_config()` | Many (`pii.ner.models`) | Many (`pii.ner.label_groups`) |

Do not wire `NEREnsemble` or `label_groups` into the normal scan path.

## Example: HTTP adapter

See `redibis/pii/backends/http_ner.py`. Configure via manifest:

```json
{
  "type": "http",
  "name": "corp-ner",
  "path": "https://ner.example.com.example/score",
  "labels": ["person", "national id"]
}
```

Expected response shape:

```json
{
  "hits": [
    {"label": "person", "score": 0.91, "count": 3}
  ]
}
```

## Ensemble (deep scan)

```python
from redibis.pii.ner_ensemble import NEREnsemble

ensemble = NEREnsemble.from_config(redibis_config.pii)
passes = ensemble.run_column(values, "email", label_groups=[["person"], ["phone number"]])
```

Each `NERPass` has `model`, `labels`, and `report` (full `NERReport`).

## Related docs

- [`docs/NER_MODULAR_DESIGN.md`](NER_MODULAR_DESIGN.md) — design rationale
- [`docs/PII_DETECTION_TUNING.md`](PII_DETECTION_TUNING.md) — logging, export, tuning
- [`docs/DEEP_SCAN.md`](DEEP_SCAN.md) — `ner.gliner` producer + per-model evidence files
