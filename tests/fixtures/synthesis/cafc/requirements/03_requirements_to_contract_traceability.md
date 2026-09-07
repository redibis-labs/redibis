# Traceability Matrix — REQ-CAFC-001 → ODCS contract

| Req | Contract key | Mode |
|---|---|---|
| BR-01 | `description.purpose` | Native |
| BR-02 | `customProperties.upstreamSources` | Custom |
| BR-03 | `description.limitations` | Native |
| BR-04 | `description.usage` | Native |
| TGT-F01 | `schema[0].properties[customer_id,business_date].primaryKey` | Native |
| TGT-F02 | `properties[customer_id]` | Native |
| TGT-F03 | `properties[business_date]` | Native |
| TGT-F04 | `properties[arpu_30d]` | Native |
| TGT-F05 | `properties[region_code]` | Native |
| TGT-F06 | `properties[msisdn_hash]` | Native |
| SRC-01 | `transformSourceObjects` / lineage | Custom |
| SRC-02 | `transformSourceObjects` / lineage | Custom |
| LKP-01 | lineage `lookup` | Custom |
| DQ-01 | `properties[customer_id].quality` | Native |
| DQ-02 | `schema[0].quality` | Native |
| DQ-03 | uniqueness | Native |
| SLA-01 | `slaProperties.freshness` | Native |
| SLA-02 | `slaProperties.frequency` | Native |
| SEC-01 | `roles` | Native |
| SEC-02 | prohibited field | Doc + lineage |
| OPS-01 | `support` | Native |
| CM-01 | version policy | Doc-only |
| AC-01 | acceptance | Doc-only |
