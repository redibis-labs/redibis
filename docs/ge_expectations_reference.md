# Great Expectations Built-in Checks Reference

Complete reference for all 56 built-in expectations available through
`QualityGatekeeper.add_gx_expectation()`.

---

## How `add_gx_expectation()` Works

This method is a dynamic bridge to every built-in GE expectation. You pass
the expectation name as a string and the parameters as keyword arguments:

```python
qa.add_gx_expectation(
    "expect_column_values_to_not_be_null",
    column = "phone",
    mostly = 0.99,
)
```

The method looks up the expectation by name on the GE Validator and calls
it with your kwargs. If the name is misspelled, it raises `AttributeError`
with a helpful message.

### The `mostly` parameter

Most column-value expectations accept a `mostly` parameter (float 0–1)
that defines what fraction of values must satisfy the condition. This lets
you set tolerant rules for real-world data:

```python
# Strict: 100% of values must match
qa.add_gx_expectation("expect_column_values_to_not_be_null", column="id")

# Tolerant: 95% of values must match (5% nulls OK)
qa.add_gx_expectation("expect_column_values_to_not_be_null", column="email", mostly=0.95)
```

### The `meta` parameter

Every expectation accepts `meta` — a dict for attaching documentation
or custom metadata that appears in the GE Data Docs report:

```python
qa.add_gx_expectation(
    "expect_column_values_to_not_be_null",
    column = "phone",
    mostly = 0.99,
    meta   = {"notes": {"content": "Business rule BR-042: phone is mandatory for postpaid"}},
)
```

---

## Expectation Categories

All 56 expectations organized by category. Each category includes a real-world
example you can copy-paste.

---

## 1. Table Shape (5 expectations)

These validate the table structure: row counts, column presence, column order.
No `column` parameter — they apply to the whole table.

| Expectation | What it checks |
|-------------|---------------|
| `expect_table_row_count_to_equal` | Exact row count |
| `expect_table_row_count_to_be_between` | Row count within a range |
| `expect_table_row_count_to_equal_other_table` | Row count matches another table |
| `expect_table_column_count_to_equal` | Exact number of columns |
| `expect_table_column_count_to_be_between` | Column count within a range |

### Example: validate daily partition has reasonable volume

```python
qa.add_gx_expectation(
    "expect_table_row_count_to_be_between",
    min_value = 1000,
    max_value = 10_000_000,
)
```

---

## 2. Schema Validation (3 expectations)

Verify the column names and their ordering match expectations. Catches
silent schema drift — columns added, removed, or reordered.

| Expectation | What it checks |
|-------------|---------------|
| `expect_table_columns_to_match_set` | Expected columns exist (order irrelevant) |
| `expect_table_columns_to_match_ordered_list` | Columns in exact order |
| `expect_column_to_exist` | A single column exists |

### Example: ensure mandatory columns are present

```python
qa.add_gx_expectation(
    "expect_table_columns_to_match_set",
    column_set = ["customer_id", "phone", "email", "national_id", "city", "balance"],
)
```

### Example: verify a critical column wasn't dropped

```python
qa.add_gx_expectation(
    "expect_column_to_exist",
    column = "national_id",
)
```

---

## 3. Null Checks (3 expectations)

Control which columns allow nulls and how many.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_not_be_null` | Values are NOT null |
| `expect_column_values_to_be_null` | Values ARE null (rare — for deprecated columns) |
| `expect_column_proportion_of_nonnull_values_to_be_between` | Proportion of non-nulls in a range |

### Example: mandatory field with tolerance

```python
# phone must be non-null for at least 99% of rows
qa.add_gx_expectation(
    "expect_column_values_to_not_be_null",
    column = "phone",
    mostly = 0.99,
)
```

### Example: deprecated column should be fully null

```python
# old_fax_number was deprecated — should be null everywhere
qa.add_gx_expectation(
    "expect_column_values_to_be_null",
    column = "old_fax_number",
)
```

### Example: null rate monitoring

```python
# email should be 70-95% non-null (partially optional field)
qa.add_gx_expectation(
    "expect_column_proportion_of_nonnull_values_to_be_between",
    column    = "email",
    min_value = 0.70,
    max_value = 0.95,
)
```

---

## 4. Type Checks (2 expectations)

Verify column data types. Catches silent type coercion (e.g. an integer
column that became a string after an ETL change).

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_of_type` | All values are a single type |
| `expect_column_values_to_be_in_type_list` | Values are one of several types |

### Example: balance must be numeric

```python
qa.add_gx_expectation(
    "expect_column_values_to_be_in_type_list",
    column    = "balance",
    type_list = ["float", "float64", "double", "int", "int64"],
)
```

---

## 5. Set Membership (6 expectations)

Verify values belong to (or don't belong to) a defined set. Essential for
categorical columns, enum fields, and blacklists.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_in_set` | Values are in an allowed set |
| `expect_column_values_to_not_be_in_set` | Values are NOT in a forbidden set |
| `expect_column_distinct_values_to_be_in_set` | Distinct values are a subset of the allowed set |
| `expect_column_distinct_values_to_contain_set` | Distinct values include all required values |
| `expect_column_distinct_values_to_equal_set` | Distinct values exactly match the set |
| `expect_column_most_common_value_to_be_in_set` | The mode is in a set |

### Example: categorical validation with tolerance

```python
# 95% of account_type values must be in the allowed set
qa.add_gx_expectation(
    "expect_column_values_to_be_in_set",
    column    = "account_type",
    value_set = ["prepaid", "postpaid", "enterprise"],
    mostly    = 0.95,
)
```

### Example: blacklist check

```python
# status must NOT contain deprecated values
qa.add_gx_expectation(
    "expect_column_values_to_not_be_in_set",
    column    = "status",
    value_set = ["DELETED", "PURGED", "LEGACY"],
)
```

### Example: all expected categories must appear

```python
# Every region must be represented in the data
qa.add_gx_expectation(
    "expect_column_distinct_values_to_contain_set",
    column    = "region",
    value_set = ["CAIRO", "ALEX", "GIZA", "DELTA"],
)
```

---

## 6. Range and Bound Checks (6 expectations)

Verify numeric values fall within expected ranges. Catches data corruption,
unit conversion errors, and overflow.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_between` | Each value is within [min, max] |
| `expect_column_min_to_be_between` | Column minimum is within a range |
| `expect_column_max_to_be_between` | Column maximum is within a range |
| `expect_column_mean_to_be_between` | Column mean is within a range |
| `expect_column_median_to_be_between` | Column median is within a range |
| `expect_column_sum_to_be_between` | Column sum is within a range |

### Example: balance must be non-negative and bounded

```python
qa.add_gx_expectation(
    "expect_column_values_to_be_between",
    column    = "balance",
    min_value = 0,
    max_value = 1_000_000,
    mostly    = 0.99,
)
```

### Example: daily revenue sanity check

```python
qa.add_gx_expectation(
    "expect_column_sum_to_be_between",
    column    = "daily_revenue",
    min_value = 50_000,
    max_value = 5_000_000,
)
```

---

## 7. Distribution and Statistical Checks (5 expectations)

Verify statistical properties of numeric columns. Catches data drift,
sampling bias, and distribution shifts.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_stdev_to_be_between` | Standard deviation in a range |
| `expect_column_quantile_values_to_be_between` | Specific quantiles in ranges |
| `expect_column_kl_divergence_to_be_less_than` | KL divergence from a reference distribution |
| `expect_column_value_z_scores_to_be_less_than` | Z-scores below a threshold (outlier check) |
| `expect_column_proportion_of_unique_values_to_be_between` | Uniqueness ratio in a range |

### Example: detect outliers via z-score

```python
# Flag rows where balance is more than 3 standard deviations from mean
qa.add_gx_expectation(
    "expect_column_value_z_scores_to_be_less_than",
    column    = "balance",
    threshold = 3.0,
    mostly    = 0.99,
)
```

### Example: cardinality monitoring

```python
# customer_id should be highly unique (>95% distinct)
qa.add_gx_expectation(
    "expect_column_proportion_of_unique_values_to_be_between",
    column    = "customer_id",
    min_value = 0.95,
    max_value = 1.0,
)
```

---

## 8. Uniqueness (4 expectations)

Verify that values are unique — within a column, across columns, or
within records.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_unique` | No duplicate values in a column |
| `expect_column_unique_value_count_to_be_between` | Count of distinct values in a range |
| `expect_compound_columns_to_be_unique` | Composite key uniqueness across multiple columns |
| `expect_select_column_values_to_be_unique_within_record` | No repeated values within each row |

### Example: primary key uniqueness

```python
qa.add_gx_expectation(
    "expect_column_values_to_be_unique",
    column = "customer_id",
)
```

### Example: composite key uniqueness

```python
# (customer_id, product_id) should be unique together
qa.add_gx_expectation(
    "expect_compound_columns_to_be_unique",
    column_list = ["customer_id", "product_id"],
)
```

### Example: cardinality range

```python
# city should have between 10 and 500 distinct values
qa.add_gx_expectation(
    "expect_column_unique_value_count_to_be_between",
    column    = "city",
    min_value = 10,
    max_value = 500,
)
```

---

## 9. String Pattern Matching (10 expectations)

Verify string values match (or don't match) patterns. This is the most
PII-relevant category — used for phone numbers, IDs, emails, etc.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_match_regex` | Values match a regex |
| `expect_column_values_to_not_match_regex` | Values do NOT match a regex |
| `expect_column_values_to_match_regex_list` | Values match at least one regex in a list |
| `expect_column_values_to_not_match_regex_list` | Values match none of the regexes in a list |
| `expect_column_values_to_match_like_pattern` | Values match a SQL LIKE pattern |
| `expect_column_values_to_not_match_like_pattern` | Values do NOT match a SQL LIKE pattern |
| `expect_column_values_to_match_like_pattern_list` | Values match at least one LIKE pattern |
| `expect_column_values_to_not_match_like_pattern_list` | Values match none of the LIKE patterns |
| `expect_column_value_lengths_to_be_between` | String lengths within a range |
| `expect_column_value_lengths_to_equal` | All strings have exact length |

### Example: Egyptian phone number format

```python
qa.add_gx_expectation(
    "expect_column_values_to_match_regex",
    column = "phone",
    regex  = r"^(?:\+20|0020|0)?1[0125]\d{8}$",
    mostly = 0.95,
)
```

### Example: Egyptian national ID length

```python
# Egyptian national IDs are exactly 14 digits
qa.add_gx_expectation(
    "expect_column_value_lengths_to_equal",
    column = "national_id",
    value  = 14,
    mostly = 0.99,
)
```

### Example: email format validation

```python
qa.add_gx_expectation(
    "expect_column_values_to_match_regex",
    column = "email",
    regex  = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    mostly = 0.90,
)
```

### Example: reject values containing sensitive patterns

```python
# Notes column should NOT contain credit card patterns
qa.add_gx_expectation(
    "expect_column_values_to_not_match_regex",
    column = "notes",
    regex  = r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
    mostly = 0.99,
)
```

### Example: flexible string length range

```python
# Customer names should be 2–100 characters
qa.add_gx_expectation(
    "expect_column_value_lengths_to_be_between",
    column    = "full_name",
    min_value = 2,
    max_value = 100,
    mostly    = 0.99,
)
```

---

## 10. Date and Time (2 expectations)

Validate temporal data formats and monotonic ordering.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_match_strftime_format` | Values match a datetime format |
| `expect_column_values_to_be_dateutil_parseable` | Values are parseable as dates |

### Example: partition date format

```python
qa.add_gx_expectation(
    "expect_column_values_to_match_strftime_format",
    column          = "dt",
    strftime_format = "%Y-%m-%d",
)
```

### Example: freeform date parsing

```python
# created_at should be parseable as a date (any format)
qa.add_gx_expectation(
    "expect_column_values_to_be_dateutil_parseable",
    column = "created_at",
    mostly = 0.95,
)
```

---

## 11. Ordering (2 expectations)

Verify values are monotonically increasing or decreasing. Useful for
time series, sequence numbers, and sorted partitions.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_increasing` | Values are monotonically increasing |
| `expect_column_values_to_be_decreasing` | Values are monotonically decreasing |

### Example: sequence ID should be increasing

```python
qa.add_gx_expectation(
    "expect_column_values_to_be_increasing",
    column        = "sequence_id",
    strictly      = False,         # allows equal consecutive values
    parse_strings_as_datetimes = False,
)
```

---

## 12. JSON Validation (2 expectations)

Verify that string columns contain valid JSON, and optionally validate
against a JSON Schema.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_values_to_be_json_parseable` | Values are valid JSON |
| `expect_column_values_to_match_json_schema` | Values match a JSON Schema |

### Example: metadata column must be valid JSON

```python
qa.add_gx_expectation(
    "expect_column_values_to_be_json_parseable",
    column = "metadata_json",
    mostly = 0.99,
)
```

### Example: JSON structure validation

```python
qa.add_gx_expectation(
    "expect_column_values_to_match_json_schema",
    column      = "event_payload",
    json_schema = {
        "type": "object",
        "required": ["event_type", "timestamp"],
        "properties": {
            "event_type": {"type": "string"},
            "timestamp":  {"type": "string"},
        },
    },
    mostly = 0.95,
)
```

---

## 13. Multi-Column Checks (4 expectations)

Validate relationships between columns. Catches referential integrity
issues and business rule violations that span multiple fields.

| Expectation | What it checks |
|-------------|---------------|
| `expect_column_pair_values_to_be_equal` | Two columns have equal values |
| `expect_column_pair_values_a_to_be_greater_than_b` | Column A > Column B |
| `expect_column_pair_values_to_be_in_set` | (A, B) pairs are in an allowed set |
| `expect_multicolumn_sum_to_equal` | Sum of multiple columns equals a value |

### Example: start_date must be before end_date

```python
qa.add_gx_expectation(
    "expect_column_pair_values_a_to_be_greater_than_b",
    column_A     = "end_date",
    column_B     = "start_date",
    or_equal     = True,
    mostly       = 0.99,
)
```

### Example: redundant columns must agree

```python
# billing_phone and contact_phone should match
qa.add_gx_expectation(
    "expect_column_pair_values_to_be_equal",
    column_A = "billing_phone",
    column_B = "contact_phone",
    mostly   = 0.90,
)
```

---

## 14. SQL Query Checks (1 expectation)

Run arbitrary SQL against the data and verify the result. The most
flexible check — anything you can express as a SQL query, you can validate.

| Expectation | What it checks |
|-------------|---------------|
| `expect_query_results_to_match_comparison` | SQL query result matches a comparison |

### Example: custom business rule via SQL

For complex validations that don't fit any single expectation, use the
gatekeeper's `enforce_sql_rule()` method instead, which provides a
simpler interface:

```python
# No customer should have negative balance AND active status
qa.enforce_sql_rule(
    query = """
        SELECT * FROM pipeline_data
        WHERE balance < 0 AND status = 'active'
    """,
    expected_violation_count = 0,
)
```

---

## Quick Reference Card

### Most commonly used expectations (top 15)

```python
# ── Null checks ─────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_not_be_null",
    column="X", mostly=0.99)

# ── Uniqueness ──────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_unique",
    column="X")

# ── Set membership ──────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_in_set",
    column="X", value_set=["a","b","c"], mostly=0.95)

# ── Numeric range ───────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_between",
    column="X", min_value=0, max_value=100)

# ── String pattern ──────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_match_regex",
    column="X", regex=r"^\d{14}$", mostly=0.99)

# ── String length ───────────────────────────────────────
qa.add_gx_expectation("expect_column_value_lengths_to_be_between",
    column="X", min_value=2, max_value=100)

# ── Exact string length ─────────────────────────────────
qa.add_gx_expectation("expect_column_value_lengths_to_equal",
    column="X", value=14)

# ── Type check ──────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_in_type_list",
    column="X", type_list=["float64","int64"])

# ── Row count range ─────────────────────────────────────
qa.add_gx_expectation("expect_table_row_count_to_be_between",
    min_value=1000, max_value=10_000_000)

# ── Schema check ────────────────────────────────────────
qa.add_gx_expectation("expect_table_columns_to_match_set",
    column_set=["id","name","phone","email"])

# ── Column existence ────────────────────────────────────
qa.add_gx_expectation("expect_column_to_exist",
    column="X")

# ── Blacklist ───────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_not_be_in_set",
    column="X", value_set=["DELETED","PURGED"])

# ── Composite key ───────────────────────────────────────
qa.add_gx_expectation("expect_compound_columns_to_be_unique",
    column_list=["customer_id","product_id"])

# ── Date format ─────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_match_strftime_format",
    column="X", strftime_format="%Y-%m-%d")

# ── Cross-column comparison ─────────────────────────────
qa.add_gx_expectation("expect_column_pair_values_a_to_be_greater_than_b",
    column_A="end_date", column_B="start_date", or_equal=True)
```

---

## Real-World Telecom Data Quality Suite Example

A complete suite for a telecom customer table, combining multiple
categories:

```python
from redibis.quality.gatekeeper import QualityGatekeeper

qa = QualityGatekeeper(suite_name="telecom_customers_v1", in_memory=False)
qa.attach_dataframe(df, dataset_name="telecom_customers")

# ── Table-level ─────────────────────────────────────────────────
qa.add_gx_expectation("expect_table_row_count_to_be_between",
    min_value=10_000, max_value=50_000_000)
qa.add_gx_expectation("expect_table_columns_to_match_set",
    column_set=["customer_id", "full_name", "phone", "email",
                "national_id", "city", "account_type", "balance",
                "created_at", "status"])

# ── Primary key ─────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_unique",
    column="customer_id")
qa.add_gx_expectation("expect_column_values_to_not_be_null",
    column="customer_id")

# ── Phone number ────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_not_be_null",
    column="phone", mostly=0.99)
qa.add_gx_expectation("expect_column_values_to_match_regex",
    column="phone", regex=r"^(?:\+20|0020|0)?1[0125]\d{8}$", mostly=0.95)
qa.add_gx_expectation("expect_column_value_lengths_to_be_between",
    column="phone", min_value=10, max_value=15)

# ── National ID ─────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_value_lengths_to_equal",
    column="national_id", value=14, mostly=0.99)
qa.add_gx_expectation("expect_column_values_to_match_regex",
    column="national_id", regex=r"^\d{14}$", mostly=0.99)

# ── Email ───────────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_match_regex",
    column="email",
    regex=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    mostly=0.90)

# ── Account type (categorical) ──────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_in_set",
    column="account_type",
    value_set=["prepaid", "postpaid", "enterprise"],
    mostly=0.95)
qa.add_gx_expectation("expect_column_distinct_values_to_contain_set",
    column="account_type",
    value_set=["prepaid", "postpaid"])

# ── Balance (numeric) ──────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_be_between",
    column="balance", min_value=0, max_value=1_000_000, mostly=0.99)
qa.add_gx_expectation("expect_column_values_to_be_in_type_list",
    column="balance", type_list=["float64", "int64", "float", "int"])

# ── Status (no deprecated values) ──────────────────────────────
qa.add_gx_expectation("expect_column_values_to_not_be_in_set",
    column="status", value_set=["DELETED", "PURGED", "LEGACY"])

# ── Date format ─────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_values_to_match_strftime_format",
    column="created_at", strftime_format="%Y-%m-%d %H:%M:%S", mostly=0.95)

# ── Name length ─────────────────────────────────────────────────
qa.add_gx_expectation("expect_column_value_lengths_to_be_between",
    column="full_name", min_value=2, max_value=100, mostly=0.99)

# ── City cardinality ───────────────────────────────────────────
qa.add_gx_expectation("expect_column_unique_value_count_to_be_between",
    column="city", min_value=5, max_value=500)

# ── SQL business rule ──────────────────────────────────────────
qa.enforce_sql_rule(
    query="SELECT * FROM pipeline_data WHERE balance < 0 AND status = 'active'",
    expected_violation_count=0)

# ── Run everything ─────────────────────────────────────────────
results = qa.run_tests(stage="full_quality", generate_docs=True)
qa.copy_data_docs_to("./reports/ge_report")
contract = qa.export_quality_contract("telecom", "customers",
    output_path="./reports/quality_contract.yaml")
```

---

## Complete Expectation Index (all 56)

For quick lookup — every expectation name with its category and key parameters.

### Table-level
| # | Expectation | Key params |
|---|-------------|------------|
| 1 | `expect_table_row_count_to_equal` | `value` |
| 2 | `expect_table_row_count_to_be_between` | `min_value, max_value` |
| 3 | `expect_table_row_count_to_equal_other_table` | `other_table_name` |
| 4 | `expect_table_column_count_to_equal` | `value` |
| 5 | `expect_table_column_count_to_be_between` | `min_value, max_value` |
| 6 | `expect_table_columns_to_match_set` | `column_set` |
| 7 | `expect_table_columns_to_match_ordered_list` | `column_list` |

### Null and existence
| # | Expectation | Key params |
|---|-------------|------------|
| 8 | `expect_column_to_exist` | `column` |
| 9 | `expect_column_values_to_not_be_null` | `column, mostly` |
| 10 | `expect_column_values_to_be_null` | `column` |
| 11 | `expect_column_proportion_of_nonnull_values_to_be_between` | `column, min_value, max_value` |

### Type
| # | Expectation | Key params |
|---|-------------|------------|
| 12 | `expect_column_values_to_be_of_type` | `column, type_` |
| 13 | `expect_column_values_to_be_in_type_list` | `column, type_list` |

### Set membership
| # | Expectation | Key params |
|---|-------------|------------|
| 14 | `expect_column_values_to_be_in_set` | `column, value_set, mostly` |
| 15 | `expect_column_values_to_not_be_in_set` | `column, value_set` |
| 16 | `expect_column_distinct_values_to_be_in_set` | `column, value_set` |
| 17 | `expect_column_distinct_values_to_contain_set` | `column, value_set` |
| 18 | `expect_column_distinct_values_to_equal_set` | `column, value_set` |
| 19 | `expect_column_most_common_value_to_be_in_set` | `column, value_set` |

### Numeric range
| # | Expectation | Key params |
|---|-------------|------------|
| 20 | `expect_column_values_to_be_between` | `column, min_value, max_value, mostly` |
| 21 | `expect_column_min_to_be_between` | `column, min_value, max_value` |
| 22 | `expect_column_max_to_be_between` | `column, min_value, max_value` |
| 23 | `expect_column_mean_to_be_between` | `column, min_value, max_value` |
| 24 | `expect_column_median_to_be_between` | `column, min_value, max_value` |
| 25 | `expect_column_sum_to_be_between` | `column, min_value, max_value` |

### Distribution and statistics
| # | Expectation | Key params |
|---|-------------|------------|
| 26 | `expect_column_stdev_to_be_between` | `column, min_value, max_value` |
| 27 | `expect_column_quantile_values_to_be_between` | `column, quantile_ranges` |
| 28 | `expect_column_kl_divergence_to_be_less_than` | `column, partition_object, threshold` |
| 29 | `expect_column_value_z_scores_to_be_less_than` | `column, threshold, mostly` |
| 30 | `expect_column_proportion_of_unique_values_to_be_between` | `column, min_value, max_value` |

### Uniqueness
| # | Expectation | Key params |
|---|-------------|------------|
| 31 | `expect_column_values_to_be_unique` | `column, mostly` |
| 32 | `expect_column_unique_value_count_to_be_between` | `column, min_value, max_value` |
| 33 | `expect_compound_columns_to_be_unique` | `column_list` |
| 34 | `expect_select_column_values_to_be_unique_within_record` | `column_list` |
| 35 | `expect_multicolumn_values_to_be_unique` | `column_list` |

### String pattern
| # | Expectation | Key params |
|---|-------------|------------|
| 36 | `expect_column_values_to_match_regex` | `column, regex, mostly` |
| 37 | `expect_column_values_to_not_match_regex` | `column, regex` |
| 38 | `expect_column_values_to_match_regex_list` | `column, regex_list, match_on` |
| 39 | `expect_column_values_to_not_match_regex_list` | `column, regex_list` |
| 40 | `expect_column_values_to_match_like_pattern` | `column, like_pattern, mostly` |
| 41 | `expect_column_values_to_not_match_like_pattern` | `column, like_pattern` |
| 42 | `expect_column_values_to_match_like_pattern_list` | `column, like_pattern_list` |
| 43 | `expect_column_values_to_not_match_like_pattern_list` | `column, like_pattern_list` |

### String length
| # | Expectation | Key params |
|---|-------------|------------|
| 44 | `expect_column_value_lengths_to_be_between` | `column, min_value, max_value, mostly` |
| 45 | `expect_column_value_lengths_to_equal` | `column, value, mostly` |

### Date and time
| # | Expectation | Key params |
|---|-------------|------------|
| 46 | `expect_column_values_to_match_strftime_format` | `column, strftime_format` |
| 47 | `expect_column_values_to_be_dateutil_parseable` | `column, mostly` |

### Ordering
| # | Expectation | Key params |
|---|-------------|------------|
| 48 | `expect_column_values_to_be_increasing` | `column, strictly` |
| 49 | `expect_column_values_to_be_decreasing` | `column, strictly` |

### JSON
| # | Expectation | Key params |
|---|-------------|------------|
| 50 | `expect_column_values_to_be_json_parseable` | `column, mostly` |
| 51 | `expect_column_values_to_match_json_schema` | `column, json_schema, mostly` |

### Multi-column
| # | Expectation | Key params |
|---|-------------|------------|
| 52 | `expect_column_pair_values_to_be_equal` | `column_A, column_B, mostly` |
| 53 | `expect_column_pair_values_a_to_be_greater_than_b` | `column_A, column_B, or_equal` |
| 54 | `expect_column_pair_values_to_be_in_set` | `column_A, column_B, value_pairs_set` |
| 55 | `expect_multicolumn_sum_to_equal` | `column_list, sum_total` |

### SQL
| # | Expectation | Key params |
|---|-------------|------------|
| 56 | `expect_query_results_to_match_comparison` | (advanced — use `enforce_sql_rule()` instead) |
