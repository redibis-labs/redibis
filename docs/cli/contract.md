# CLI help — contracts, runs, PII/quality views

Inspect and export the **active** contract after a scan (or enrich).

## Inspect

```bash
redibis list --output-dir ./reports
redibis show telecom.customers --output-dir ./reports
redibis history telecom.customers --output-dir ./reports
redibis merge telecom.customers --output-dir ./reports   # dump merged JSON
```

## PII / quality / definitions slices

```bash
# YAML (default)
redibis contract pii-view telecom.customers --output-dir ./reports > pii.yaml
redibis contract quality-view telecom.customers --output-dir ./reports > quality.yaml
redibis contract definitions-view telecom.customers --output-dir ./reports

# JSON
redibis contract pii-view telecom.customers --json --output-dir ./reports > pii.json

# Spec + telemetry package
redibis contract export-package telecom.customers --output-dir ./reports
redibis contract metadata telecom.customers --output-dir ./reports
```

## Decision overlays (no rescan)

```bash
redibis contract add-pii telecom.customers --column notes --entity-type PERSON \
  --output-dir ./reports
redibis contract strip-pii telecom.customers --column notes --output-dir ./reports

redibis contract quality-suppress telecom.customers --rule-id RULE_ID
redibis contract quality-restore telecom.customers --rule-id RULE_ID
redibis contract quality-suppress-all telecom.customers
```

## Run subcontracts

```bash
redibis runs list telecom.customers --output-dir ./reports
redibis runs merge telecom.customers <run_id> --output-dir ./reports
redibis runs discard telecom.customers <run_id> --output-dir ./reports
```

## Quality rules export

```bash
redibis rules list telecom.customers --output-dir ./reports
redibis rules export telecom.customers --target ge      # or soda / dbt
```

## Contract Synthesis (portable ODCS v3.1)

```bash
redibis contract synthesize -f base.odcs.yaml \
  --requirements ./reqs --source ./jobs -o ./synthesis_out
```

Does **not** upsert active Redibis contracts — see [synthesize.md](synthesize.md).

## See also

- [scan.md](scan.md) — how the active contract gets created
- [enrich.md](enrich.md) — LLM business definitions
- [synthesize.md](synthesize.md) — portable ODCS v3.1 Contract Synthesis
- [../CLI_SCAN_ENRICH_TOUR.md](../CLI_SCAN_ENRICH_TOUR.md) §4
