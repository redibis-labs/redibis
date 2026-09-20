# `redibis eval from-contract` — contract → evaluation dataset

Converts an active PII contract into a
`redibis.table_column_eval_dataset` JSON file, where the contract supplies the
**expected** values: `is_pii`, `entity_type`, `privacy_classification`, the
agreed `tags`, and optionally a handful of sample records per column.

```bash
redibis eval from-contract telco.customers \
  --out telco-customers.eval.json \
  --data /srv/samples/telco/customers.csv \
  --samples 10 \
  --tags "pii,contact,identity,metric,id"
```

Then score the detection engine against it:

```bash
redibis eval run --dataset telco-customers.eval.json \
  --data /srv/samples/telco/customers.csv --target engine
```

> Do not score `--target contract` against a dataset built from that same
> contract. Expected and actual are then the same document and F1 is always
> 1.0. The useful targets are `engine` and `llm`.

## Options

| Flag | Meaning |
|---|---|
| `table` | Table whose active contract to convert |
| `--contract-file PATH` | Read a contract from YAML/JSON instead of the store |
| `-o, --out PATH` | Write the dataset (default: stdout) |
| `--data PATH` | CSV/Parquet/XLSX to draw sample records and dtypes from |
| `--samples N` | Attach N sample records per column. Default 0 — none. |
| `--sample-mode shape\|raw` | `shape` (default) or real values |
| `--require-consent` | Also gate shape masks on recorded steward consent |
| `--tags a,b,c` | Agreed tag vocabulary — only these are exported |
| `--tags-file PATH` | Same, one tag per line, `#` comments allowed |
| `--pii-only` | Export only columns the contract marks as PII |

Omit `--tags` entirely to export every tag the contract carries. Matching is
case-insensitive and the allowlist's spelling wins, so a contract written as
`PII` and a vocabulary written as `pii` agree on one form. Dropped tags are
listed under `notes.tags_dropped` so a reviewer can see what was filtered.

## Sample records and residency

The table evaluation dataset is value-free by design. Attaching samples
changes that, so the dataset says so in `residency` and carries a `warning`:

| Mode | `residency` | Consent required |
|---|---|---|
| no samples (default) | `portable` | — |
| `--sample-mode shape` | `contains_shapes` | none by default |
| `--sample-mode raw` | `contains_values` | yes, per column, always |

Shape masks come from `redibis.memory.redaction.redact_samples`: `n11:DDDDDDDDDDD`
for an MSISDN. They contain no characters or digits, but they do reveal
length and format, which is why they still flip `residency`.

Raw values are refused unless the steward has recorded sampling consent for
that exact column (`redibis contract` → sampling consent, or
`PUT /api/contracts/{table}/sampling-consent/{column}`). Columns without it
get `samples: []` and `samples_withheld: "no steward sampling consent"`, and
the CLI names them on stderr. `--contract-file` has no consent store behind
it, so `--sample-mode raw` is refused outright in that mode.

Passing `--data` without `--samples` attaches no samples — it is still useful,
because dtypes from the file fill in `logicalType` where the contract omits it.

## Schema

Datasets without samples stay at `schema_version` `1.0`. Samples introduce
`1.1`, which adds the optional per-column `samples` and `samples_withheld`
fields. Both versions load; `1.0` is never dropped. Scoring ignores `samples`
entirely — they are carried for the human reviewing the dataset.

## Failure you will actually hit

If the contract marks a column as PII without an entity type, the generated
dataset is invalid and the command refuses to write it, naming the columns:

```
eval from-contract: these columns are marked PII in the contract but carry no
entity type — set one with `redibis contract add-pii telco.customers
--column <name> --entity-type <TYPE>`: home_address, id_number
```
