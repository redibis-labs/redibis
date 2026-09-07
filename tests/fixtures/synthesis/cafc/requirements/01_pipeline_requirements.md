# Data Pipeline Requirements — Customer Analytical Feature Composite (CAFC)

| Item | Value |
|---|---|
| Document ID | REQ-CAFC-001 |
| Version | 1.0.0 |
| Status | Approved |
| Owner | Customer Analytics Data Product Team |
| Target object | `analytics.customer_feature_composite_daily` |

## 1. Purpose and Scope

**BR-01 Purpose.** Produce one daily, customer-level table of analytical features that consolidates behaviour, value, service, marketing and billing signals from batch source systems.

**BR-02 Upstream.** Read from `crm.customers` and `billing.usage_daily`, enriched with `reference.dim_region`.

**BR-03 Limitations.** Batch T+1 only; no streaming.

**BR-04 Usage.** Downstream ML feature stores and CRM scoring only.

## 2. Target schema

**TGT-F01** Primary key is `(customer_id, business_date)`.

**TGT-F02** `customer_id` is required string.

**TGT-F03** `business_date` is required date and the partition key.

**TGT-F04** `arpu_30d` is optional number derived from billing usage.

**TGT-F05** `region_code` is optional string from region lookup.

**TGT-F06** Never persist raw MSISDN; only `msisdn_hash`.

## 3. Sources

**SRC-01** Source `crm.customers` provides customer identity.

**SRC-02** Source `billing.usage_daily` provides revenue and usage measures.

**LKP-01** Lookup `reference.dim_region` maps region keys to `region_code`.

## 4. Data quality

**DQ-01** `customer_id` null rate must be 0.

**DQ-02** Daily row count must be greater than 0.

**DQ-03** Primary key uniqueness on `(customer_id, business_date)`.

## 5. SLA and security

**SLA-01** Freshness ≤ 24 hours after business day close.

**SLA-02** Frequency = daily.

**SEC-01** Role `analytics_reader` has read access only.

**SEC-02** Prohibited field propagation: raw MSISDN must not appear in the target.

## 6. Operations

**OPS-01** Support channel is email to cafc-ops@example.com.

**CM-01** Contract version bumps on schema-breaking changes.

**AC-01** Steward sign-off required before production cutover (doc-only).
