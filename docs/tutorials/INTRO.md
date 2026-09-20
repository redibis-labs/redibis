# Intro tutorial — e-shop customer account CSV

This is the **open-core** hands-on path. It uses one shipped fixture so you can
see scan → active contract → dashboard without extra config.

**Fixture:**
[`tests/data/realistic_eshop_customer_account.csv`](../../tests/data/realistic_eshop_customer_account.csv)
(~1000 synthetic rows). Columns include identifiers and contact fields such as
`eshop_customer_id`, `full_name`, `email_address`, `mobile_number`, plus
security-sensitive material (`password_hash`, `session_id`, `csrf_token`).

Flag overview for the whole product lives in the [root README](../../README.md).
Deeper tutorials are released to this repository only after they are verified.

---

## 0. Setup

```bash
cd /path/to/redibis   # this repo root after clone / install

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,web]" -c requirements/constraints.txt

export OUT=./reports/eshop_intro
export CSV=tests/data/realistic_eshop_customer_account.csv
export TABLE=eshop.customer_account
mkdir -p "$OUT"
```

CLI contracts live under `$OUT/_dev_storage`. The dashboard uses
`LOCAL_STORAGE_ROOT` (often `./_local_storage`) unless you point both at the
same root.

---

## 1. Scan the CSV (CLI)

```bash
redibis scan "$CSV" "$TABLE" \
  --mode pii \
  --automerge pii \
  --equation independent \
  --pii-engines both \
  --output-dir "$OUT"
```

Inspect:

```bash
redibis show "$TABLE" --output-dir "$OUT" | head -n 60
redibis list --output-dir "$OUT"
```

You should see an active ODCS contract for `eshop.customer_account` with PII
signals on contact-like columns. Exact engines and labels depend on whether
local NER weights are configured; regex-only still produces a useful first
contract.

Optional profile + quality (needs `[ge]` extras):

```bash
redibis scan "$CSV" "$TABLE" \
  --mode all \
  --automerge both \
  --output-dir "$OUT"
```

---

## 2. Dashboard start / stop

```bash
export USE_LOCAL_STORAGE=true
export LOCAL_STORAGE_ROOT=./_local_storage
export SCAN_OUTPUT_DIR=./scan_output

./scripts/webapp.sh --start
# open http://127.0.0.1:8000/  (login: admin / admin on empty users store)

./scripts/webapp.sh --status
./scripts/webapp.sh --stop
```

On the start page, upload or select
`tests/data/realistic_eshop_customer_account.csv` if your sample roots allow
`tests/data` (open-core installs include that fixture under `tests/data/`).

Then open **Contracts v2** (`/v2`) for the table: PII view, Steward Review,
enrich, and export actions. Nothing on Steward Review starts a new scan.

---

## 3. What to try next (same store)

Keep `--output-dir "$OUT"`:

| Goal | Command family |
|------|----------------|
| Human PII decisions | `redibis steward …` or Contracts v2 → Steward Review |
| LLM glossary / definitions | `redibis enrich …` (install `[enrich]`, configure a provider) |
| Safe CSV for sharing | `redibis mask auto …` |
| HTTP exploration | login → `/docs` (Swagger) · full route list in [API_SURFACE.md](../API_SURFACE.md) |

When you enable GLiNER, download weights once
(`./scripts/download_ner_model.sh`) and pass `--ner-model` or set
`REDIBIS_NER_MODEL`.

---

## 4. Storage reminder

| Surface | Default root |
|---------|----------------|
| CLI (`--output-dir`) | `./reports` → `./reports/_dev_storage` |
| Dashboard | `LOCAL_STORAGE_ROOT` → e.g. `./_local_storage` |

They do not share contracts unless you align those paths.

---

## See also

- [README](../../README.md) — product overview, CLI command index, API index
- [docs/API_SURFACE.md](../API_SURFACE.md) — full HTTP method/path list
