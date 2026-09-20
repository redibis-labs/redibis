
# Redibis — Complete User Guide

**Version:** 1.0 | **Last updated:** May 2026

## Table of Contents

1. [Overview](#overview)
2. [Installation](#installation)
3. [Web UI — Interactive Scanning](#web-ui--interactive-scanning)
4. [CLI — Batch Scanning](#cli--batch-scanning)
5. [PII Detection Pipeline](#pii-detection-pipeline)
6. [Contract Management](#contract-management)
7. [Quality Scanning](#quality-scanning)
8. [Batch Operations & Automation](#batch-operations--automation)
9. [Configuration & Customization](#configuration--customization)
10. [Troubleshooting](#troubleshooting)

---

## Overview

**Redibis** is an enterprise data quality and governance platform for scanning CSV/Parquet files to detect:

- **PII (Personally Identifiable Information)** — using three independent engines: Presidio (regex), GLiNER (NER), and optional LLM refinement
- **Data Quality Rules** — 57 Great Expectations rules across nullness, uniqueness, ranges, patterns, types, and distribution
- **Automatic Contract Generation** — produces ODCS v3 data contracts with classification, quality rules, and audit history

**Use cases:**
- One-time CSV scanning via web UI (interactive)
- Batch scanning of 100+ files via CLI (scripts/Airflow)
- Automated contract creation for governance pipelines
- PII discovery and remediation workflows
- Data catalog integration

**Key differentiators:**
- Per-engine confidence thresholds (regex vs GLiNER) — not a single global confidence
- Arabic-aware PII detection with language-specific patterns
- Decision path explanation for every PII detection
- Signal detection for non-confirmed columns with PII indicators
- ODCS-native contract storage with merge logic for multi-workflow scans

---

## Installation

### Prerequisites

- Python 3.10+
- Pip or Conda
- PostgreSQL (for ContractStore) — or in-memory mode for testing
- MinIO (for artifact storage) — or local filesystem mode

### Option A: Docker (Recommended for teams)

```bash
git clone https://github.com/yourusername/redibis.git
cd redibis

docker compose up -d
```

This starts:
- **Jupyter** on `localhost:8888` (development/exploration)
- **FastAPI** on `localhost:8000` (web UI + API)
- **MinIO** on `localhost:9000` (file artifacts)

### Option B: Local installation (for CLI scripting)

```bash
pip install -e ".[all]"
# or specific extras:
pip install -e ".[pii,quality,cli]"
```

Verify installation:
```bash
redibis --version
redibis --help
```

---

## Web UI — Interactive Scanning

### 1. Access the Dashboard

Open `http://localhost:8000` in your browser. Auth is **on by default** — you
land on `/login`. With no `users.json` yet, the default is **`admin` / `admin`**.
See [`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md).

### 2. Upload a CSV

**Step 1: File Upload**
- Click **Scan** tab
- Drag & drop a CSV file (or click to browse)
- The table preview appears instantly (first 100 rows)
- Check "Select all columns" or individually tick columns to scan

**Step 2: Configure Settings**

Before scanning, adjust settings in the **Settings** panel:

| Setting | Default | Purpose |
|---------|---------|---------|
| **PII Engines** | Both (Presidio + GLiNER) | Which PII detection engines to run |
| **Regex (Presidio) confidence** | 0.80 | Minimum score for regex pattern matches |
| **GLiNER confidence** | 0.40 | Minimum score for NER model (lower = more sensitive) |
| **Equation used** | independent | How to combine three engines (see [Equations](#equations)) |
| **Quality Rules** | Profiler (50 auto-generated) | Which quality rules to check |
| **LLM refiner** | disabled | Optional: use Claude/GPT to confirm ambiguous PII |

**Tip:** For unstructured text columns, lower GLiNER confidence (e.g., 0.25). For phone numbers/emails, increase regex confidence (e.g., 0.90) to reduce false positives.

**Step 3: Run Scan**

Click **Run Scan** (or **PII Only** / **Quality Only** / **Both**).

Watch the progress bar in the **Scan Progress** log:
- 5% — validating input
- 20% — profiling columns
- 35% — running quality rules
- 50% — detecting PII (Presidio)
- 70% — detecting PII (GLiNER)
- 85% — merging results + generating contract
- 100% — done

### 3. Review Results

#### **Results Tab**

Shows three cards:

| Card | What it shows |
|------|---------------|
| **Overview** | File name, rows/columns, scan time, which steps ran |
| **Data Profiling** | Min/max/mean per column, nullness, cardinality |
| **PII Summary** | Confirmed PII count, signal count, CSV with detections |

Click on each card to see the full table.

#### **PII Tab** (if PII was scanned)

Shows all columns with detection results:

| Column | Detected | Entity | Presidio | GLiNER | Confidence |
|--------|----------|--------|----------|--------|------------|
| `email` | ✓ | EMAIL | 0.95 | 0.87 | 0.95 |
| `phone` | ✓ | PHONE | 0.92 | — | 0.92 |
| `name` | signal | PERSON | 0.61 | 0.72 | — |
| `age` | — | — | — | — | — |

- **Detected** = column has confirmed PII
- **Signal** = PII indicators but not confirmed (e.g., GLiNER found "PERSON" but confidence < threshold)
- **Presidio/GLiNER** = engine-specific scores
- **Confidence** = final merged score (if detected)

Click **PII Regex Review** to see all regex patterns that matched and sample values.

#### **Quality Tab** (if quality rules ran)

Shows a donut chart (passed vs failed) and list of failed expectations.

Click **interactive review** to see which specific rows failed and why.

#### **Contracts Tab**

Shows both **PII Contract** and **Quality Contract** in formatted HTML:

- **Columns tab** — schema with PII/quality status
- **PII Detail tab** — full detection scores, decision path, GLiNER label, Arabic fraction, sample match rate
- **Markdown/YAML/JSON tabs** — raw exports for CI/CD pipelines

**Save contracts** — Click **export** buttons to download as YAML/JSON/ODCS for version control.

---

## CLI — Batch Scanning

### Core Command

```bash
redibis scan <CSV_PATH> [OPTIONS]
```

### Example 1: Simple PII scan

```bash
redibis scan customers.csv   --pii   --pii-engines both   --pii-regex-confidence 0.80   --pii-gliner-confidence 0.40   --output /tmp/results
```

**Output files:**
- `customers_pii_detections.json` — per-column PII scores
- `customers_pii_contract.yaml` — ODCS contract (PII only)
- `customers_pii_regex_review.html` — interactive pattern review

### Example 2: Quality + PII scan with custom rules

```bash
redibis scan transactions.csv   --quality   --pii   --quality-rules "not_null,unique,in_set"   --quality-columns "user_id,amount,timestamp"   --output /tmp/transactions_scan
```

### Example 3: Batch scanning 100 CSVs

**Create a bash script:**

```bash
#!/bin/bash
# batch_scan.sh

CSV_DIR="/data/raw_csvs"
OUTPUT_DIR="/data/contracts"
LOG_FILE="/tmp/batch_scan.log"

for csv in $CSV_DIR/*.csv; do
  table_name=$(basename "$csv" .csv)
  echo "[$(date)] Scanning $table_name..." | tee -a "$LOG_FILE"
  
  redibis scan "$csv"     --pii     --quality     --pii-engines both     --quality-columns "all"     --output "$OUTPUT_DIR/$table_name"     2>&1 | tee -a "$LOG_FILE"
  
  if [ $? -eq 0 ]; then
    echo "[$(date)] ✓ $table_name completed" | tee -a "$LOG_FILE"
  else
    echo "[$(date)] ✗ $table_name FAILED" | tee -a "$LOG_FILE"
  fi
done

echo "[$(date)] Batch scan complete" | tee -a "$LOG_FILE"
```

Run it:
```bash
chmod +x batch_scan.sh
./batch_scan.sh
```

Monitor with:
```bash
tail -f /tmp/batch_scan.log
```

### Example 4: Integration with Airflow (continuous quality monitoring)

Generate DAGs from active contracts, then deploy to Airflow:

```bash
redibis quality-monitor airflow generate --all-contracts \
  -o /opt/airflow/dags/redibis \
  --schedule "0 2 * * *"
```

Each DAG calls `redibis quality-monitor run` (validate-only — no contract overwrite). Example generated DAG:

```python
# dags/redibis_quality_telecom_customers.py (generated)
from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta

with DAG(
    dag_id="redibis_quality_telecom_customers",
    schedule="0 6 * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["redibis", "quality", "monitor"],
) as dag:
    BashOperator(
        task_id="monitor_run",
        bash_command=(
            "redibis quality-monitor run telecom.customers "
            '--sample "${REDIBIS_SAMPLE_ROOT}/telecom_customers.csv" '
            '--config "${REDIBIS_CONFIG}" --output-dir /var/redibis/reports --json'
        ),
    )
```

For **discovery scans** (profile + PII + quality on new CSV files), use `redibis scan` in a separate DAG — see `scripts/batch_scan.sh`.

Reference: [`docs/cli/monitor.md`](cli/monitor.md).

### CLI Options Reference

```bash
redibis scan --help

Options:
  --pii                           Run PII detection
  --quality                       Run quality rules
  --pii-engines [regex|gliner|both]  Default: both
  --pii-regex-confidence FLOAT    Default: 0.80 (0.0-1.0)
  --pii-gliner-confidence FLOAT   Default: 0.40 (0.0-1.0)
  --pii-equation [independent|balanced|strict]  Default: independent
  --quality-rules LIST            Comma-separated GE rule names or "profiler"
  --quality-columns LIST          Columns to scan or "all"
  --output DIR                    Write results here
  --database NAME                 Database name (for contract)
  --table NAME                    Table name (inferred from filename if not set)
  --physical-table NAME           Physical table name (e.g., "raw.customers_v2")
  --sample-size INT               Profile sample size (default: 10000)
  --sample-seed INT               Random seed for reproducibility
  --format [csv|parquet]          Input format (auto-detected by extension)
  --encoding utf-8|latin-1|...    Default: utf-8
  --delimiter ,|;|\t              Default: ,
  --log-level [DEBUG|INFO|WARN]   Default: INFO
  --no-profile                    Skip data profiling
  --skip-artifacts                Don't write HTML/JSON artifacts
  --contract-version X.Y.Z        Version for ODCS contract
  --run-id STR                    Scan run identifier
```

---

## PII Detection Pipeline

### Three-Engine Architecture

Redibis runs **three independent detection engines** on each column:

#### **1. Presidio (Regex)**

**What it detects:** Patterns with known structure (phone, email, ID, credit card, dates).

**Patterns included:**
- US/UK phone numbers
- Email addresses
- Social Security numbers (SSN)
- Credit card numbers (Visa, Mastercard, Amex)
- Bank account numbers
- IP addresses
- URLs
- Dates (MM/DD/YYYY, DD/MM/YYYY)
- Custom patterns (configurable)

**Score interpretation:**
- `0.95+` — very confident match (e.g., 16-digit Visa card)
- `0.70-0.94` — likely match (e.g., email, phone)
- `0.40-0.69` — fuzzy match (e.g., partial SSN, date-like string)
- `< 0.40` — weak signal

**Confidence threshold:** Default 0.80 (only scores ≥ 0.80 confirm PII)

#### **2. GLiNER (Named Entity Recognition)**

**What it detects:** Named entities in free text — PERSON, ORG, LOCATION, EMAIL, PHONE, etc.

**How it works:**
- Receives input as `{column_name}: {value}` — context matters
- Example: `full_name: John Smith` scores higher for PERSON than `john smith` alone
- Vocabulary-independent — finds names even if not in training data

**Score interpretation:**
- `0.90+` — very confident (e.g., "John Smith" in a `full_name` column)
- `0.60-0.89` — likely (e.g., "John" alone, or "Alice Cooper" in description)
- `0.30-0.59` — possible (e.g., capitalized word in comment field)
- `< 0.30` — weak signal

**Confidence threshold:** Default 0.40 (intentionally low for high recall)

#### **3. LLM Refiner (Optional)**

**What it does:** Takes uncertain cases (Presidio + GLiNER disagree, or one says "maybe") and asks Claude/GPT:

> *Column: "contact_info"*
> *Value: "john.doe@example.com"*
> *Presidio says: EMAIL (0.92)*
> *GLiNER says: EMAIL (0.87)*
> *Question: Is this PII?*
> *LLM response: YES, EMAIL (confidence 0.95)*

**When to enable:**
- Lots of free-text fields (comments, descriptions, narrative)
- High false-positive rate from Presidio alone
- You can afford the ~500ms delay per column

**When to disable (default):**
- Structured columns (already Presidio-dominated)
- Real-time pipelines (too slow)
- Cost-sensitive environments

### Decision Path & Equations

**Decision path** — explains *why* a detection was made:

```
Presidio regex: EMAIL pattern matched (0.92)
GLiNER: EMAIL entity detected (0.87)
Equation: independent — both agree → CONFIRMED
Final confidence: 0.92 (max of scores)
```

**Equations** — how to combine three engines:

| Equation | Logic | Use case |
|----------|-------|----------|
| **independent** | Any engine detects = PII confirmed | Default; high recall; tolerates false positives |
| **balanced** | 2 of 3 engines agree = PII confirmed | Medium recall/precision; best for most data |
| **strict** | All 3 engines agree = PII confirmed | High precision; only very clear PII flagged |

**Set via CLI:**
```bash
redibis scan data.csv --pii-equation balanced
```

### Handling Signals (Non-Confirmed PII)

A column is marked as **signal** when:
- Presidio scored 0.61 (below 0.80 threshold) + GLiNER scored 0.72 (below 0.40)
- *OR* GLiNER scored 0.35 (below 0.40) but Presidio scored 0.0

**Action:** Review these columns in the "PII Detail" tab. Decide to:
1. **Lower confidence thresholds** — (e.g., `--pii-regex-confidence 0.60`)
2. **Enable LLM refiner** — let Claude confirm
3. **Mark as false positive** — exclude from PII contract
4. **Add custom pattern** — if it's a domain-specific format

### Custom PII Patterns (Advanced)

Create a `custom_patterns.yaml`:

```yaml
patterns:
  - name: "employee_id"
    regex: "^EMP-\d{6}$"
    description: "Internal employee ID format"
    confidence: 0.95
    entity_type: "EMPLOYEE_ID"

  - name: "internal_reference"
    regex: "^REF[0-9A-Z]{8}$"
    description: "Internal reference number"
    confidence: 0.80
    entity_type: "REFERENCE_NUM"

  - name: "bank_account_arabic"
    regex: "^[0-9]{10,16}$"
    description: "Arabic bank account number"
    confidence: 0.85
    entity_type: "BANK_ACCOUNT"
    arabic_aware: true
```

Use in CLI:
```bash
redibis scan data.csv   --pii   --custom-patterns custom_patterns.yaml
```

Or via web UI — **Settings → PII → Custom Patterns** (paste YAML).

---

## Contract Management

### What is a Data Contract?

A **Data Contract** is an ODCS (Open Data Contract Standard) v3.0.1 document that describes:

```yaml
apiVersion: v3.0.1
kind: DataContract
name: customers_contract
database_name: analytics
table_name: customers
version: 1.0.0
status: active

description:
  purpose: "Customer data warehouse table"
  owner: "data-eng@company.com"

schema:
  - name: analytics_customers
    physicalName: "analytics.public.customers"
    properties:
      - name: customer_id
        logicalType: integer
        required: true
        unique: true
        pii:
          detected: false
          
      - name: email
        logicalType: string
        classification: pii_personal
        pii:
          detected: true
          entity_type: EMAIL
          confidence: 0.95
          presidio_score: 0.95
          gliner_score: 0.87
          
      - name: full_name
        logicalType: string
        classification: pii_personal
        pii:
          detected: true
          entity_type: PERSON
          confidence: 0.92
```

### Creating Contracts from Scans

**Step 1: Run scan (creates partial contracts)**

```bash
redibis scan customers.csv   --pii --quality   --output /tmp/scan_results
```

Creates:
- `customers_pii_contract.yaml` — PII metadata only
- `customers_quality_contract.yaml` — Quality rules only
- `customers_merged_contract.yaml` — Both combined

**Step 2: Merge into main contract**

```bash
redibis contracts merge   --database analytics   --table customers   --pii-contract /tmp/scan_results/customers_pii_contract.yaml   --quality-contract /tmp/scan_results/customers_quality_contract.yaml   --output-file /tmp/contracts/analytics.customers.yaml
```

**Step 3: Store in version control**

```bash
git add /tmp/contracts/analytics.customers.yaml
git commit -m "Update analytics.customers contract — PII + quality v1.0.2"
git push
```

### Batch Contract Creation

**Process 100 CSVs → 100 contracts:**

```bash
#!/bin/bash
# batch_create_contracts.sh

CSV_DIR="/mnt/raw_data"
CONTRACT_DIR="/mnt/contracts"

mkdir -p "$CONTRACT_DIR"

for csv in "$CSV_DIR"/*.csv; do
  table=$(basename "$csv" .csv)
  db=$(echo "$table" | cut -d_ -f1)
  
  echo "Processing $db.$table..."
  
  # Step 1: Scan
  redibis scan "$csv"     --pii --quality     --database "$db"     --table "$table"     --output "/tmp/$table"
  
  # Step 2: Merge contracts
  redibis contracts merge     --database "$db"     --table "$table"     --pii-contract "/tmp/$table/${table}_pii_contract.yaml"     --quality-contract "/tmp/$table/${table}_quality_contract.yaml"     --output-file "$CONTRACT_DIR/$db.$table.v1.0.0.yaml"
  
  echo "✓ Created $db.$table contract"
done

# Step 3: Validate all contracts
redibis contracts validate   --directory "$CONTRACT_DIR"   --report contracts_validation_report.html

# Step 4: Generate data catalog entry
redibis contracts export   --directory "$CONTRACT_DIR"   --format openmetadata   --output /tmp/data_catalog.json
```

Run:
```bash
chmod +x batch_create_contracts.sh
time ./batch_create_contracts.sh
```

### Contract Format Conversions

Export contracts to other tools:

```bash
# Export to Soda CL
redibis contracts export   --input analytics.customers.yaml   --format sodacl   --output checks.yml

# Export to dbt sources
redibis contracts export   --input analytics.customers.yaml   --format dbt-sources   --output models/staging/_sources.yml

# Export to Great Expectations
redibis contracts export   --input analytics.customers.yaml   --format great-expectations   --output expectations/customers_expectations.json

# Export to BigQuery schema
redibis contracts export   --input analytics.customers.yaml   --format bigquery   --output customers_schema.json

# Export to Markdown (for docs)
redibis contracts export   --input analytics.customers.yaml   --format markdown   --output CUSTOMERS_CONTRACT.md
```

---

## Quality Scanning

### 57 Built-in Great Expectations Rules

Redibis includes all 57 GE 1.x expectations organized by category:

#### Column-level rules (40 rules)

```
Column - Nulls (3):
  • expect_column_values_to_not_be_null
  • expect_column_values_to_be_null
  • expect_column_proportion_of_non_null_values_to_be_between

Column - Uniqueness (3):
  • expect_column_values_to_be_unique
  • expect_column_unique_value_count_to_be_between
  • expect_column_proportion_of_unique_values_to_be_between

Column - Range / Numeric (8):
  • expect_column_values_to_be_between
  • expect_column_max_to_be_between
  • expect_column_min_to_be_between
  • expect_column_mean_to_be_between
  • expect_column_median_to_be_between
  • expect_column_stdev_to_be_between
  • expect_column_sum_to_be_between
  • expect_column_value_z_scores_to_be_less_than

Column - Set Membership (6):
  • expect_column_values_to_be_in_set
  • expect_column_values_to_not_be_in_set
  • expect_column_distinct_values_to_be_in_set
  • expect_column_distinct_values_to_contain_set
  • expect_column_distinct_values_to_equal_set
  • expect_column_most_common_value_to_be_in_set

Column - String / Regex (8):
  • expect_column_values_to_match_regex
  • expect_column_values_to_not_match_regex
  • expect_column_value_lengths_to_be_between
  • expect_column_value_lengths_to_equal
  • [... and 4 more pattern matching rules]

Column - Type / Format (6):
  • expect_column_values_to_be_of_type
  • expect_column_values_to_be_in_type_list
  • expect_column_values_to_be_dateutil_parseable
  • expect_column_values_to_match_strftime_format
  • expect_column_values_to_be_json_parseable
  • expect_column_values_to_match_json_schema

Column - Monotonicity (2):
  • expect_column_values_to_be_increasing
  • expect_column_values_to_be_decreasing

Column - Distribution (2):
  • expect_column_kl_divergence_to_be_less_than
  • expect_column_quantile_values_to_be_between
```

#### Table-level rules (7 rules)

```
Table-Level:
  • expect_table_row_count_to_be_between
  • expect_table_row_count_to_equal
  • expect_table_column_count_to_equal
  • expect_table_column_count_to_be_between
  • expect_table_columns_to_match_ordered_list
  • expect_table_columns_to_match_set
  • expect_table_row_count_to_equal_other_table
```

### Using Custom Quality Rules

**CLI:** Select specific rules

```bash
redibis scan data.csv   --quality   --quality-rules "not_null,unique,in_set,regex"   --quality-columns "email,age,status"
```

**Profiler mode (default):** Auto-detect 50 rules

```bash
redibis scan data.csv   --quality   --quality-rules profiler
```

This runs rules based on column type:
- Integer columns → `between`, `mean_to_be_between`
- String columns → `length_to_be_between`, `match_regex`
- Date columns → `match_strftime_format`, `increasing/decreasing`

### Quality Rule Parameters

Define rules with parameters:

```bash
redibis scan data.csv   --quality   --quality-config quality_rules.yaml
```

**quality_rules.yaml:**

```yaml
rules:
  - column: email
    expectation: expect_column_values_to_match_regex
    params:
      regex: '^[\w\.-]+@[\w\.-]+\.\w+$'
      mostly: 0.95  # 95% of rows must match

  - column: age
    expectation: expect_column_values_to_be_between
    params:
      min_value: 0
      max_value: 150

  - column: signup_date
    expectation: expect_column_values_to_be_increasing

  - column: amount
    expectation: expect_column_values_to_be_between
    params:
      min_value: 0.01
      max_value: 999999.99

  - column: status
    expectation: expect_column_values_to_be_in_set
    params:
      value_set: ["active", "inactive", "pending", "deleted"]
```

---

## Batch Operations & Automation

### Pattern: Multi-file Scanning with Contract Merge

**Scan 200 files daily, merge into master contract:**

```bash
#!/bin/bash
# daily_batch_scan_and_merge.sh
set -e

DATE=$(date +%Y-%m-%d)
BASE_DIR="/data/governance"
SCAN_DIR="$BASE_DIR/scans/$DATE"
CONTRACT_DIR="$BASE_DIR/contracts"
LOG_FILE="$BASE_DIR/logs/$DATE.log"

mkdir -p "$SCAN_DIR" "$CONTRACT_DIR" "$BASE_DIR/logs"

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Batch scan starting ==="
TOTAL=0
SUCCESS=0
FAILED=0

# Scan all CSVs
for csv_file in /mnt/raw_data/*.csv; do
  TOTAL=$((TOTAL + 1))
  table_name=$(basename "$csv_file" .csv)
  
  log "[$TOTAL] Scanning $table_name..."
  
  if redibis scan "$csv_file"     --pii --quality     --database raw     --table "$table_name"     --output "$SCAN_DIR/$table_name"     >> "$LOG_FILE" 2>&1; then
    
    SUCCESS=$((SUCCESS + 1))
    log "  ✓ $table_name complete"
  else
    FAILED=$((FAILED + 1))
    log "  ✗ $table_name FAILED"
  fi
done

log "=== Merge phase: combining all contracts ==="

# Collect all PII + Quality contracts
for scan_result in "$SCAN_DIR"/*; do
  if [ -d "$scan_result" ]; then
    table=$(basename "$scan_result")
    pii_contract="$scan_result/${table}_pii_contract.yaml"
    quality_contract="$scan_result/${table}_quality_contract.yaml"
    
    if [ -f "$pii_contract" ] && [ -f "$quality_contract" ]; then
      redibis contracts merge         --database raw         --table "$table"         --pii-contract "$pii_contract"         --quality-contract "$quality_contract"         --output-file "$CONTRACT_DIR/raw.$table.v1.0.0.yaml"
    fi
  fi
done

# Generate summary report
log "=== Generating reports ==="
redibis contracts validate   --directory "$CONTRACT_DIR"   --report "$BASE_DIR/reports/$DATE/validation.html"

redibis contracts analyze   --directory "$CONTRACT_DIR"   --format json   --output "$BASE_DIR/reports/$DATE/analysis.json"

log "=== Batch complete: $SUCCESS/$TOTAL succeeded, $FAILED failed ==="
```

**Cron job — run every day at 2 AM:**

```
0 2 * * * /opt/redibis/daily_batch_scan_and_merge.sh
```

### Pattern: Scan → Validate → Publish to Data Catalog

```bash
#!/bin/bash
# scan_and_publish.sh

CSV_FILE="$1"
CATALOG_URL="http://openmetadata:8585/api/v1"
DB_NAME="analytics"
TABLE_NAME=$(basename "$CSV_FILE" .csv)

# Step 1: Scan
echo "Scanning $TABLE_NAME..."
redibis scan "$CSV_FILE"   --pii --quality   --database "$DB_NAME"   --table "$TABLE_NAME"   --output "/tmp/$TABLE_NAME"

# Step 2: Validate contract
echo "Validating contract..."
redibis contracts validate   --input "/tmp/$TABLE_NAME/${TABLE_NAME}_merged_contract.yaml"   --strict

# Step 3: Export to OpenMetadata
echo "Publishing to catalog..."
CONTRACT_JSON=$(redibis contracts export   --input "/tmp/$TABLE_NAME/${TABLE_NAME}_merged_contract.yaml"   --format openmetadata)

curl -X POST "$CATALOG_URL/tables"   -H "Content-Type: application/json"   -d "$CONTRACT_JSON"

echo "✓ $TABLE_NAME published to catalog"
```

Use in pipeline:
```bash
for csv in /mnt/raw_data/telecom_*.csv; do
  ./scan_and_publish.sh "$csv"
done
```

### Pattern: Continuous quality monitoring (daily / event-driven)

After rules are approved and merged into the active contract, use **`redibis quality-monitor`** to
re-validate on a schedule. This does **not** re-profile or overwrite rules.

```bash
# Daily batch (all tables with active contracts)
redibis quality-monitor batch --all-contracts \
  --config redibis.yaml \
  --output-dir /var/redibis/reports \
  --json

# Per-table with CSV sample
redibis quality-monitor run telecom.customers \
  --sample /data/samples/telecom_customers.csv \
  --config redibis.yaml

# Export deployment package (full executable program + canonical rule set + Airflow DAGs)
redibis quality-monitor export telecom.customers -o ./packages

# Generate Airflow DAGs
redibis quality-monitor airflow generate --all-contracts -o ./dags
```

**Config** (`redibis.yaml`):

```yaml
catalog:
  push:
    quality: true   # publish test results to OpenMetadata (default: true)

source:
  engine: jdbc      # or hive / postgres / oracle — omit --sample when configured
  jdbc:
    host: warehouse.example.com
    database: analytics
```

**Artifacts:** `monitor/{table}/{run_id}/quality_results.json` and the canonical
`quality_run.json` (`redibis.io/quality/v1alpha1`) in the runs bucket (MinIO).

**Dashboard & alerts:** OpenMetadata table → **Quality** tab. Configure OM Observability alerts
(Slack, email, webhook) on test-suite failures.

**Web UI:** Quality review step or Data quality page → **Copy Jupyter Code** (one
self-contained Spark-first program, rules inline) or **Download monitor package**
(zip containing that same program). Edit the program in Jupyter and bring it back
with `redibis quality-monitor export --python-file …`, or through the paste panel
for approve → merge; edited Python is parsed, never executed.

Full reference: [`docs/cli/monitor.md`](cli/monitor.md).

---

## Configuration & Customization

### Environment Variables

```bash
# Storage
REDIBIS_MINIO_URL=http://localhost:9000
REDIBIS_MINIO_ACCESS_KEY=minioadmin
REDIBIS_MINIO_SECRET_KEY=minioadmin
REDIBIS_MINIO_BUCKET=redibis

# Database (contracts)
REDIBIS_DATABASE_URL=postgresql://user:password@localhost:5432/redibis
REDIBIS_DATABASE_MODE=postgresql  # or "in-memory" for testing

# PII defaults
REDIBIS_PII_ENGINES=both
REDIBIS_PII_REGEX_CONFIDENCE=0.80
REDIBIS_PII_GLINER_CONFIDENCE=0.40

# LLM (optional)
REDIBIS_LLM_PROVIDER=anthropic
REDIBIS_LLM_API_KEY=sk-ant-...
REDIBIS_LLM_MODEL=claude-3-sonnet

# Logging
REDIBIS_LOG_LEVEL=INFO
REDIBIS_LOG_FILE=/var/log/redibis/scan.log
```

Set in `.env`:
```
REDIBIS_LOG_LEVEL=DEBUG
REDIBIS_MINIO_URL=http://minio:9000
```

Load with:
```bash
export $(cat .env | grep -v '^#' | xargs)
redibis scan data.csv --pii
```

### Custom Regex Patterns

Create `patterns.yaml`:

```yaml
catalog:
  structured:
    - name: "customer_id"
      regex: "^CUST-[0-9]{8}$"
      confidence: 0.95
      entity_type: "CUSTOMER_ID"
      
    - name: "internal_reference"
      regex: "^REF[A-Z0-9]{10}$"
      confidence: 0.90
      entity_type: "INTERNAL_REF"

  financial:
    - name: "account_number_uk"
      regex: "^[0-9]{8}[A-Z]{2}[0-9A-Z]{1,30}$"
      confidence: 0.92
      entity_type: "BANK_ACCOUNT"

  healthcare:
    - name: "patient_mrn"
      regex: "^MRN[0-9]{7}$"
      confidence: 0.95
      entity_type: "MEDICAL_RECORD_NUMBER"

  arabic:
    - name: "saudi_id"
      regex: "^[0-9]{10}$"
      confidence: 0.88
      entity_type: "SAUDI_ID"
      arabic_aware: true
      
    - name: "egyptian_id"
      regex: "^[0-9]{14}$"
      confidence: 0.90
      entity_type: "EGYPTIAN_ID"
      arabic_aware: true
```

Use:
```bash
redibis scan data.csv   --pii   --custom-patterns patterns.yaml
```

Or upload in web UI — Settings → PII → Custom Patterns.

---

## Troubleshooting

### "PII detections are empty"

**Symptom:** Run PII scan, but `pii_detections` table is empty.

**Causes:**
1. **Confidence thresholds too high** — all detections filtered out
2. **Column data type wrong** — e.g., scanning integers as strings
3. **PII engines not installed** — missing Presidio or GLiNER

**Fix:**
```bash
# Lower confidence thresholds
redibis scan data.csv   --pii   --pii-regex-confidence 0.60   --pii-gliner-confidence 0.25

# Check installed engines
python -c "import presidio_analyzer; import flair; print('✓ Engines OK')"

# Enable debug logging
redibis scan data.csv --pii --log-level DEBUG 2>&1 | grep -i "presidio\|gliner\|score"
```

### "PII twice = all clean" (already fixed in current version)

**Symptom:** Run PII scan twice on same file, second scan shows no detections.

**Cause:** Session config not persisted between scans.

**Fix:** Update to latest `app.js` — includes `S.sid = null` to force fresh session per scan.

### "Quality rules fail with 'unknown expectation'"

**Symptom:**
```
ValueError: Unknown expectation: 'expect_column_values_to_match_json_schema'
```

**Cause:** Rule name not in GE registry for your version.

**Fix:**
```bash
# List available rules
redibis quality list-rules

# Use only 57 built-in rules
redibis scan data.csv --quality --quality-rules profiler
```

### "Memory error on large CSV"

**Symptom:** `MemoryError` scanning 500MB+ file.

**Cause:** Loading entire file into RAM at once.

**Fix:**
```bash
# Sample the data first
redibis scan data.csv   --pii --quality   --sample-size 10000   --sample-seed 42

# Or process in chunks (external)
split -l 100000 large.csv chunk_
for chunk in chunk_*; do
  redibis scan "$chunk" --pii --quality --output "/tmp/$(basename "$chunk")"
done
```

### "Contract merge fails: version conflict"

**Symptom:**
```
ValueError: Cannot merge v1.0.0 (PII) with v1.0.1 (Quality) — versions must match
```

**Fix:**
```bash
# Explicitly set version for both contracts
redibis contracts merge   --pii-contract pii.yaml   --quality-contract quality.yaml   --contract-version 1.0.2
```

---

## API Reference

### Python API

```python
from redibis import Redibis, ScanConfig

# Initialize
rb = Redibis(
    minio_url="http://localhost:9000",
    minio_key="minioadmin",
    minio_secret="minioadmin",
    database_url="postgresql://localhost/redibis"
)

# Configure scan
config = ScanConfig(
    pii_engines="both",
    pii_regex_confidence=0.80,
    pii_gliner_confidence=0.40,
    pii_equation="balanced",
    quality_rules="profiler",
)

# Run scan
result = rb.scan_csv(
    csv_path="data.csv",
    database_name="analytics",
    table_name="customers",
    config=config,
)

# Access results
print(f"PII detected: {len(result.pii_detections)}")
print(f"Quality passed: {result.quality_passed}/{result.quality_total}")

# Export contract
contract_yaml = result.export_contract(format="yaml")
contract_html = result.export_contract(format="html")

# Save
with open("contract.yaml", "w") as f:
    f.write(contract_yaml)
```

### REST API

Dashboard auth is on by default. Sign in first
([`DASHBOARD_AUTH.md`](DASHBOARD_AUTH.md#calling-the-api-curl)), then:

```bash
# Scan via API
curl -s -b "$RB_COOKIES" -X POST http://localhost:8000/api/sessions \
  -H "X-CSRF-Token: $CSRF" \
  -F "csv=@data.csv" \
  -F "database_name=analytics" \
  -F "table_name=customers" | jq '.session_id' > sid.txt

SESSION_ID=$(cat sid.txt)

# Configure and run PII
curl -s -b "$RB_COOKIES" -X POST http://localhost:8000/api/sessions/$SESSION_ID/step/pii \
  -H "X-CSRF-Token: $CSRF" \
  -d "pii_engines=both" \
  -d "pii_regex_confidence=0.80" \
  -d "pii_gliner_confidence=0.40"

# Poll for completion
curl -s -b "$RB_COOKIES" http://localhost:8000/api/sessions/$SESSION_ID

# Get results
curl -s -b "$RB_COOKIES" http://localhost:8000/api/sessions/$SESSION_ID/result | jq '.pii_detections'

# Export contract
curl -s -b "$RB_COOKIES" http://localhost:8000/api/contracts/analytics.customers/html > contract.html
curl -s -b "$RB_COOKIES" http://localhost:8000/api/contracts/analytics.customers/yaml > contract.yaml
```

---

## Advanced Examples

### Example 1: PII Discovery with Human Review Loop

```python
#!/usr/bin/env python3
# pii_discovery_with_review.py

import json
from redibis import Redibis
from pathlib import Path

rb = Redibis()

# Scan
result = rb.scan_csv(
    csv_path="sensitive_data.csv",
    database_name="prod",
    table_name="users",
)

# Extract signals (non-confirmed but flagged)
signals = [d for d in result.pii_detections if d['triage_score'] and not d['detected']]

if signals:
    print(f"Found {len(signals)} PII signals requiring review:")
    
    for col, det in signals:
        print(f"\n  Column: {col}")
        print(f"  Entity: {det['entity_type']}")
        print(f"  Presidio: {det.get('presidio_score', 0):.2f}")
        print(f"  GLiNER: {det.get('gliner_score', 0):.2f}")
        print(f"  Triage: {det.get('triage_score', 0):.2f}")
        
        # Ask human
        response = input("  Mark as PII? (y/n): ")
        if response.lower() == 'y':
            det['detected'] = True
            det['decision_path'] = "Human-approved from triage signal"

# Save reviewed contract
reviewed_contract = result.export_contract(format="yaml")
Path("reviewed_contract.yaml").write_text(reviewed_contract)
print("✓ Contract saved with human review")
```

### Example 2: Batch Scan → Auto-quarantine PII Tables

```bash
#!/bin/bash
# auto_quarantine.sh

CSV_DIR="/mnt/landing_zone"
QUARANTINE_DIR="/mnt/quarantine"
SAFE_DIR="/mnt/safe_zone"

for csv in "$CSV_DIR"/*.csv; do
  table=$(basename "$csv" .csv)
  echo "Scanning $table..."
  
  # Scan
  redibis scan "$csv" --pii --output "/tmp/$table"
  
  # Check for PII
  pii_count=$(jq '.pii_summary.pii_confirmed' "/tmp/$table/pii_detections.json")
  
  if [ "$pii_count" -gt 0 ]; then
    echo "  ⚠ Found $pii_count PII columns — quarantining"
    mv "$csv" "$QUARANTINE_DIR/$table.PII.csv"
    
    # Notify security
    curl -X POST https://slack.company.com/hooks       -d "{"text": "🚨 PII found in $table — quarantined"}"
  else
    echo "  ✓ Safe — moving to production"
    mv "$csv" "$SAFE_DIR/$table.csv"
  fi
done
```

---

## Support & Community

- **Documentation:** `docs/` directory
- **Issues:** GitHub Issues (with label `cli` or `batch`)
- **Slack:** `#redibis-users` on company Slack
- **Email:** redibis-support@company.com

---

**Version:** 1.0 | **Updated:** May 2026 | **Maintainers:** data-eng@company.com
