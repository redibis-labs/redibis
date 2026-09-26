# HTTP API surface (open-core dashboard)

Generated from FastAPI route decorators in `redibis/webapp/`.
Auth is on by default — most `/api/*` routes require a session cookie.
Interactive docs: `/docs` (Swagger) after login.

**355 routes** listed below.

## Dashboard HTML pages

| Method | Path |
|--------|------|
| `GET` | `/` |
| `POST` | `/admin/reset` |
| `GET` | `/batch` |
| `GET` | `/gateway` |
| `GET` | `/gateway/evaluations` |
| `GET` | `/gateway/runs/{run_uuid}` |
| `GET` | `/gateway/usecases` |
| `GET` | `/gateway/usecases/{uc_id}` |
| `GET` | `/health` |
| `GET` | `/healthz` |
| `GET` | `/help` |
| `GET` | `/login` |
| `POST` | `/login` |
| `POST` | `/logout` |
| `GET` | `/ready` |
| `GET` | `/reports` |
| `GET` | `/review` |
| `GET` | `/settings` |
| `GET` | `/share/{token}` |
| `GET` | `/users` |
| `GET` | `/v2` |

## Behavior policies

| Method | Path |
|--------|------|
| `GET` | `/api/behavior/audit` |
| `GET` | `/api/behavior/catalog` |
| `GET` | `/api/behavior/metrics` |
| `GET` | `/api/behavior/policies` |
| `POST` | `/api/behavior/policies` |
| `POST` | `/api/behavior/policies/promote-correction` |
| `POST` | `/api/behavior/policies/simulate` |
| `POST` | `/api/behavior/policies/validate` |
| `GET` | `/api/behavior/policies/{policy_id}/active` |
| `GET` | `/api/behavior/policies/{policy_id}/history` |
| `POST` | `/api/behavior/policies/{policy_id}/rollback` |
| `GET` | `/api/behavior/policies/{policy_id}/{version}` |
| `POST` | `/api/behavior/policies/{policy_id}/{version}/activate` |
| `POST` | `/api/behavior/policies/{policy_id}/{version}/approve` |
| `POST` | `/api/behavior/policies/{policy_id}/{version}/deactivate` |

## Catalog push status

| Method | Path |
|--------|------|
| `GET` | `/api/catalog/backends` |
| `POST` | `/api/catalog/push/{table}` |
| `GET` | `/api/catalog/status/{table}` |

## Classification packs

| Method | Path |
|--------|------|
| `POST` | `/api/classification/classify` |
| `POST` | `/api/classification/edge-rules/promote` |
| `GET` | `/api/classification/packs` |
| `GET` | `/api/classification/packs/{name}` |
| `PUT` | `/api/classification/packs/{name}` |
| `POST` | `/api/classification/packs/{name}/test` |
| `POST` | `/api/classification/packs/{name}/validate` |

## Similar columns

| Method | Path |
|--------|------|
| `GET` | `/api/columns/{table}/{column}/similar` |

## RAI config

| Method | Path |
|--------|------|
| `GET` | `/api/config/rai` |

## Saved regex/quality configs

| Method | Path |
|--------|------|
| `GET` | `/api/configs/export` |
| `POST` | `/api/configs/import` |
| `GET` | `/api/configs/quality` |
| `POST` | `/api/configs/quality` |
| `DELETE` | `/api/configs/quality/{name}` |
| `GET` | `/api/configs/quality/{name}` |
| `GET` | `/api/configs/regex` |
| `POST` | `/api/configs/regex` |
| `DELETE` | `/api/configs/regex/{name}` |
| `GET` | `/api/configs/regex/{name}` |
| `POST` | `/api/configs/seed-defaults` |

## Active contracts & steward

| Method | Path |
|--------|------|
| `GET` | `/api/contracts` |
| `DELETE` | `/api/contracts/{table}` |
| `GET` | `/api/contracts/{table}` |
| `GET` | `/api/contracts/{table}/audit/{run_uuid}` |
| `POST` | `/api/contracts/{table}/columns/{column}/add-pii` |
| `DELETE` | `/api/contracts/{table}/columns/{column}/pii-decision` |
| `PATCH` | `/api/contracts/{table}/columns/{column}/privacy` |
| `POST` | `/api/contracts/{table}/columns/{column}/sampling-consent` |
| `POST` | `/api/contracts/{table}/columns/{column}/strip-pii` |
| `GET` | `/api/contracts/{table}/context-docs` |
| `POST` | `/api/contracts/{table}/context-docs` |
| `DELETE` | `/api/contracts/{table}/context-docs/{filename}` |
| `PATCH` | `/api/contracts/{table}/definitions` |
| `GET` | `/api/contracts/{table}/definitions-view` |
| `POST` | `/api/contracts/{table}/enrich` |
| `GET` | `/api/contracts/{table}/enrich/candidate` |
| `GET` | `/api/contracts/{table}/enrich/diff` |
| `POST` | `/api/contracts/{table}/enrich/merge` |
| `GET` | `/api/contracts/{table}/enrich/preflight` |
| `POST` | `/api/contracts/{table}/enrich/validate` |
| `GET` | `/api/contracts/{table}/example-docs` |
| `POST` | `/api/contracts/{table}/example-docs` |
| `DELETE` | `/api/contracts/{table}/example-docs/{filename}` |
| `GET` | `/api/contracts/{table}/export-package` |
| `GET` | `/api/contracts/{table}/history` |
| `GET` | `/api/contracts/{table}/html` |
| `GET` | `/api/contracts/{table}/memory/hints` |
| `GET` | `/api/contracts/{table}/metadata` |
| `GET` | `/api/contracts/{table}/pii-decisions` |
| `GET` | `/api/contracts/{table}/pii-view` |
| `POST` | `/api/contracts/{table}/quality-decisions/manual` |
| `POST` | `/api/contracts/{table}/quality-decisions/suppress-all` |
| `POST` | `/api/contracts/{table}/quality-decisions/{rule_id}/restore` |
| `POST` | `/api/contracts/{table}/quality-decisions/{rule_id}/suppress` |
| `GET` | `/api/contracts/{table}/quality-view` |
| `GET` | `/api/contracts/{table}/review` |
| `PUT` | `/api/contracts/{table}/review/columns/{column}` |
| `POST` | `/api/contracts/{table}/review/columns/{column}/approve` |
| `GET` | `/api/contracts/{table}/review/columns/{column}/history` |
| `POST` | `/api/contracts/{table}/review/columns/{column}/reject` |
| `POST` | `/api/contracts/{table}/review/columns/{column}/reset` |
| `POST` | `/api/contracts/{table}/review/finalize` |
| `GET` | `/api/contracts/{table}/review/status` |
| `GET` | `/api/contracts/{table}/rules` |
| `GET` | `/api/contracts/{table}/rules/export` |
| `GET` | `/api/contracts/{table}/runs` |
| `GET` | `/api/contracts/{table}/sample-data` |
| `POST` | `/api/contracts/{table}/sample-data` |
| `DELETE` | `/api/contracts/{table}/sample-data/{filename}` |
| `GET` | `/api/contracts/{table}/sampling-consent` |
| `POST` | `/api/contracts/{table}/share` |
| `GET` | `/api/contracts/{table}/shares` |
| `GET` | `/api/contracts/{table}/steward` |
| `GET` | `/api/contracts/{table}/steward/artifacts` |
| `GET` | `/api/contracts/{table}/steward/artifacts/{name}` |
| `GET` | `/api/contracts/{table}/steward/columns/{column}` |
| `POST` | `/api/contracts/{table}/steward/columns/{column}/verdict` |
| `POST` | `/api/contracts/{table}/steward/columns/{column}/verdicts` |
| `GET` | `/api/contracts/{table}/steward/export/artifacts` |
| `GET` | `/api/contracts/{table}/steward/export/verdicts` |
| `POST` | `/api/contracts/{table}/steward/finalize` |
| `POST` | `/api/contracts/{table}/steward/table/verdict` |
| `GET` | `/api/contracts/{table}/triage` |
| `GET` | `/api/contracts/{table}/verdicts/export` |

## LLM enrichment

| Method | Path |
|--------|------|
| `GET` | `/api/enrich/default-prompt` |
| `GET` | `/api/enrich/{table}/context` |
| `PUT` | `/api/enrich/{table}/context` |
| `POST` | `/api/enrich/{table}/context/preview` |
| `POST` | `/api/enrich/{table}/context/promote` |
| `POST` | `/api/enrich/{table}/run` |

## Evidence review

| Method | Path |
|--------|------|
| `POST` | `/api/evidence/review` |
| `POST` | `/api/evidence/{table}/restricted/llm` |
| `GET` | `/api/evidence/{table}/review` |
| `POST` | `/api/evidence/{table}/review/recompute-drift` |
| `GET` | `/api/evidence/{table}/runs` |
| `GET` | `/api/evidence/{table}/runs/compare` |
| `GET` | `/api/evidence/{table}/runs/{run_id}` |
| `GET` | `/api/evidence/{table}/runs/{run_id}/artifacts` |
| `GET` | `/api/evidence/{table}/runs/{run_id}/artifacts/{name:path}` |
| `GET` | `/api/evidence/{table}/runs/{run_id}/columns/{column}` |
| `POST` | `/api/evidence/{table}/runs/{run_id}/verdicts/replay` |
| `POST` | `/api/evidence/{table}/verdicts/import` |
| `GET` | `/api/evidence/{table}/verdicts/imports` |
| `POST` | `/api/evidence/{table}/verdicts/preview` |

## Text PII gateway / evaluations

| Method | Path |
|--------|------|
| `POST` | `/api/gateway/curation` |
| `GET` | `/api/gateway/curation/{run_uuid}` |
| `POST` | `/api/gateway/deidentify` |
| `GET` | `/api/gateway/evaluations/compare` |
| `POST` | `/api/gateway/evaluations/corpus-patch` |
| `POST` | `/api/gateway/evaluations/run` |
| `POST` | `/api/gateway/evaluations/run/stream` |
| `GET` | `/api/gateway/evaluations/runs` |
| `GET` | `/api/gateway/evaluations/runs/{run_uuid}` |
| `GET` | `/api/gateway/evaluations/runs/{run_uuid}/cases/{case_id}` |
| `POST` | `/api/gateway/guard` |
| `GET` | `/api/gateway/health` |
| `GET` | `/api/gateway/llm-log/{run_uuid}` |
| `POST` | `/api/gateway/llm-verdict` |
| `POST` | `/api/gateway/recommend` |
| `GET` | `/api/gateway/rules` |
| `PUT` | `/api/gateway/rules` |
| `POST` | `/api/gateway/rules/advise` |
| `POST` | `/api/gateway/rules/dry-run` |
| `POST` | `/api/gateway/scan` |
| `POST` | `/api/gateway/scan/stream` |
| `GET` | `/api/gateway/sessions` |
| `POST` | `/api/gateway/sessions` |
| `GET` | `/api/gateway/sessions/{slug}` |
| `POST` | `/api/gateway/sessions/{slug}/usecases` |
| `GET` | `/api/gateway/sessions/{slug}/{date}/export` |
| `GET` | `/api/gateway/sessions/{slug}/{date}/usecases` |
| `GET` | `/api/gateway/sessions/{slug}/{date}/usecases/{uc_id}` |
| `POST` | `/api/gateway/suggest-policy` |
| `POST` | `/api/gateway/trim` |
| `GET` | `/api/gateway/usecases` |
| `POST` | `/api/gateway/usecases` |
| `POST` | `/api/gateway/usecases/import` |
| `DELETE` | `/api/gateway/usecases/{uc_id}` |
| `GET` | `/api/gateway/usecases/{uc_id}` |
| `PUT` | `/api/gateway/usecases/{uc_id}` |
| `GET` | `/api/gateway/usecases/{uc_id}/download` |
| `POST` | `/api/gateway/usecases/{uc_id}/run` |

## Golden columns

| Method | Path |
|--------|------|
| `GET` | `/api/golden` |

## LLM calls & routes

| Method | Path |
|--------|------|
| `GET` | `/api/llm/calls` |
| `GET` | `/api/llm/calls/export` |
| `GET` | `/api/llm/providers` |
| `POST` | `/api/llm/providers` |
| `POST` | `/api/llm/providers/test` |
| `DELETE` | `/api/llm/providers/{name}` |
| `GET` | `/api/llm/providers/{name}/models` |
| `GET` | `/api/llm/routes` |
| `PUT` | `/api/llm/routes` |
| `GET` | `/api/llm/routes/runtime` |
| `POST` | `/api/llm/routes/validate` |
| `POST` | `/api/llm/routes/{role}/test` |

## LLM provider registry

| Method | Path |
|--------|------|
| `GET` | `/api/llm-providers` |
| `GET` | `/api/llm-providers/registry` |
| `PUT` | `/api/llm-providers/registry` |
| `GET` | `/api/llm-providers/{name}/env-status` |
| `POST` | `/api/llm-providers/{name}/test` |

## Masking helpers

| Method | Path |
|--------|------|
| `GET` | `/api/masking/capabilities` |
| `GET` | `/api/masking/regex-patterns` |
| `POST` | `/api/masking/regex-test` |

## Current user

| Method | Path |
|--------|------|
| `GET` | `/api/me` |

## Column memory

| Method | Path |
|--------|------|
| `GET` | `/api/memory/status` |

## BYOM NER models

| Method | Path |
|--------|------|
| `GET` | `/api/models` |
| `POST` | `/api/models/upload` |
| `DELETE` | `/api/models/{name}` |

## PII catalogue & free-text

| Method | Path |
|--------|------|
| `GET` | `/api/pii/ner/labels` |
| `PUT` | `/api/pii/ner/labels` |
| `GET` | `/api/pii/ner/models` |
| `GET` | `/api/pii/provenance/{provenance_uuid}` |
| `GET` | `/api/pii/regex` |
| `POST` | `/api/pii/regex/overrides` |
| `GET` | `/api/pii/regex/{name}` |
| `GET` | `/api/pii/runs` |
| `GET` | `/api/pii/runs/{run_uuid}` |
| `POST` | `/api/pii/text/deidentify` |
| `GET` | `/api/pii/text/entities` |
| `GET` | `/api/pii/text/health` |
| `GET` | `/api/pii/text/policies` |
| `POST` | `/api/pii/text/policies` |
| `GET` | `/api/pii/text/rules` |
| `PUT` | `/api/pii/text/rules` |
| `POST` | `/api/pii/text/rules/dry-run` |
| `POST` | `/api/pii/text/rules/promote` |
| `POST` | `/api/pii/text/rules/publish` |
| `POST` | `/api/pii/text/scan` |
| `POST` | `/api/pii/text/scan-batch` |
| `POST` | `/api/pii/text/suggest-policy` |

## Enrich prompts

| Method | Path |
|--------|------|
| `GET` | `/api/prompts/enrich` |
| `PUT` | `/api/prompts/enrich` |
| `POST` | `/api/prompts/enrich/reset` |
| `DELETE` | `/api/prompts/enrich/{name:path}` |
| `GET` | `/api/prompts/enrich/{name:path}` |
| `PUT` | `/api/prompts/enrich/{name:path}` |

## Quality helpers

| Method | Path |
|--------|------|
| `POST` | `/api/quality/parse-rules` |

## Portable packs

| Method | Path |
|--------|------|
| `POST` | `/api/rdbpack/activate` |
| `POST` | `/api/rdbpack/diff` |
| `GET` | `/api/rdbpack/export-default` |
| `POST` | `/api/rdbpack/import` |
| `POST` | `/api/rdbpack/remove` |
| `GET` | `/api/rdbpack/stack` |
| `DELETE` | `/api/rdbpack/stack/{identity}` |
| `POST` | `/api/rdbpack/validate` |
| `GET` | `/api/rdbpack/versions` |
| `GET` | `/api/rdbpack/versions/{uuid}` |
| `GET` | `/api/rdbpack/versions/{uuid}/diff` |
| `GET` | `/api/rdbpack/versions/{uuid}/runs` |

## Regex catalogue

| Method | Path |
|--------|------|
| `GET` | `/api/regex-catalog` |
| `GET` | `/api/regex-catalog/{name}` |

## Run artifacts

| Method | Path |
|--------|------|
| `GET` | `/api/runs/{kind}/{table}/{run_id}` |
| `PATCH` | `/api/runs/{kind}/{table}/{run_id}` |
| `POST` | `/api/runs/{kind}/{table}/{run_id}/discard` |
| `POST` | `/api/runs/{kind}/{table}/{run_id}/merge` |

## Sample CSV library

| Method | Path |
|--------|------|
| `GET` | `/api/sample-data` |
| `GET` | `/api/sample-data/preview` |

## Scan output browser

| Method | Path |
|--------|------|
| `GET` | `/api/scan_output/{session_id}` |

## Session sources

| Method | Path |
|--------|------|
| `GET` | `/api/session-sources` |

## Scan sessions

| Method | Path |
|--------|------|
| `GET` | `/api/sessions` |
| `POST` | `/api/sessions` |
| `GET` | `/api/sessions/available` |
| `POST` | `/api/sessions/flush_all` |
| `POST` | `/api/sessions/from-sample` |
| `POST` | `/api/sessions/load` |
| `DELETE` | `/api/sessions/{session_id}` |
| `GET` | `/api/sessions/{session_id}` |
| `DELETE` | `/api/sessions/{session_id}/approved` |
| `GET` | `/api/sessions/{session_id}/approved` |
| `POST` | `/api/sessions/{session_id}/approved/merge` |
| `POST` | `/api/sessions/{session_id}/approved/pii` |
| `GET` | `/api/sessions/{session_id}/approved/preview` |
| `POST` | `/api/sessions/{session_id}/approved/quality` |
| `DELETE` | `/api/sessions/{session_id}/approved/{prop_id}` |
| `GET` | `/api/sessions/{session_id}/approved/{prop_id}` |
| `PUT` | `/api/sessions/{session_id}/approved/{prop_id}` |
| `GET` | `/api/sessions/{session_id}/artifacts/{key}` |
| `GET` | `/api/sessions/{session_id}/config` |
| `PATCH` | `/api/sessions/{session_id}/config` |
| `PUT` | `/api/sessions/{session_id}/config/quality` |
| `POST` | `/api/sessions/{session_id}/config/quality/load/{name}` |
| `POST` | `/api/sessions/{session_id}/config/quality/rules` |
| `DELETE` | `/api/sessions/{session_id}/config/quality/rules/{index}` |
| `PUT` | `/api/sessions/{session_id}/config/regex` |
| `POST` | `/api/sessions/{session_id}/config/regex/load/{name}` |
| `POST` | `/api/sessions/{session_id}/config/regex/patterns` |
| `DELETE` | `/api/sessions/{session_id}/config/regex/patterns/{key}` |
| `GET` | `/api/sessions/{session_id}/data/columns` |
| `PATCH` | `/api/sessions/{session_id}/data/columns` |
| `GET` | `/api/sessions/{session_id}/data/preview` |
| `GET` | `/api/sessions/{session_id}/debug` |
| `GET` | `/api/sessions/{session_id}/discovery` |
| `POST` | `/api/sessions/{session_id}/discovery/pii` |
| `POST` | `/api/sessions/{session_id}/discovery/pii/test-regex` |
| `POST` | `/api/sessions/{session_id}/discovery/quality` |
| `GET` | `/api/sessions/{session_id}/discovery/runs/{run_id}` |
| `POST` | `/api/sessions/{session_id}/draft/quality/template` |
| `POST` | `/api/sessions/{session_id}/draft/quality/upload` |
| `POST` | `/api/sessions/{session_id}/eval/export` |
| `POST` | `/api/sessions/{session_id}/eval/run` |
| `POST` | `/api/sessions/{session_id}/eval/run/stream` |
| `GET` | `/api/sessions/{session_id}/eval/scaffold` |
| `POST` | `/api/sessions/{session_id}/eval/validate` |
| `POST` | `/api/sessions/{session_id}/flush` |
| `POST` | `/api/sessions/{session_id}/mask/apply` |
| `GET` | `/api/sessions/{session_id}/mask/audit` |
| `GET` | `/api/sessions/{session_id}/mask/audit/download` |
| `GET` | `/api/sessions/{session_id}/mask/export` |
| `GET` | `/api/sessions/{session_id}/mask/exports` |
| `GET` | `/api/sessions/{session_id}/mask/manifest` |
| `GET` | `/api/sessions/{session_id}/mask/plan` |
| `PUT` | `/api/sessions/{session_id}/mask/plan` |
| `POST` | `/api/sessions/{session_id}/mask/preview` |
| `GET` | `/api/sessions/{session_id}/mask/risk` |
| `POST` | `/api/sessions/{session_id}/models/activate` |
| `POST` | `/api/sessions/{session_id}/quality/evaluate` |
| `POST` | `/api/sessions/{session_id}/quality/export-package` |
| `POST` | `/api/sessions/{session_id}/quality/full-code` |
| `GET` | `/api/sessions/{session_id}/sample` |
| `POST` | `/api/sessions/{session_id}/scan` |
| `POST` | `/api/sessions/{session_id}/step/pii` |
| `POST` | `/api/sessions/{session_id}/step/profile` |
| `POST` | `/api/sessions/{session_id}/step/quality` |
| `GET` | `/api/sessions/{session_id}/stream` |
| `GET` | `/api/sessions/{session_id}/subcontracts` |
| `POST` | `/api/sessions/{session_id}/subcontracts` |
| `POST` | `/api/sessions/{session_id}/subcontracts/merge` |
| `DELETE` | `/api/sessions/{session_id}/subcontracts/{sub_id}` |
| `GET` | `/api/sessions/{session_id}/subcontracts/{sub_id}` |
| `PUT` | `/api/sessions/{session_id}/subcontracts/{sub_id}` |

## Settings

| Method | Path |
|--------|------|
| `GET` | `/api/settings` |
| `GET` | `/api/settings/global` |
| `PUT` | `/api/settings/global` |
| `GET` | `/api/settings/runtime` |

## Share links

| Method | Path |
|--------|------|
| `DELETE` | `/api/share/{token}` |
| `GET` | `/api/share/{token}` |
| `PATCH` | `/api/share/{token}` |

## Users / auth admin

| Method | Path |
|--------|------|
| `GET` | `/api/users` |
| `POST` | `/api/users` |
| `DELETE` | `/api/users/{username}` |
| `PATCH` | `/api/users/{username}` |

## Vector / similar

| Method | Path |
|--------|------|
| `POST` | `/api/vector/reload` |
| `GET` | `/api/vector/status` |

## Verdict export aliases

| Method | Path |
|--------|------|
| `GET` | `/api/verdicts/export` |

## Contract workspaces

| Method | Path |
|--------|------|
| `GET` | `/api/workspaces` |
| `POST` | `/api/workspaces` |
| `DELETE` | `/api/workspaces/{slug}` |
| `GET` | `/api/workspaces/{slug}/batches` |
| `POST` | `/api/workspaces/{slug}/batches` |
| `GET` | `/api/workspaces/{slug}/batches/{batch_id}` |
| `POST` | `/api/workspaces/{slug}/batches/{batch_id}/cancel` |
| `GET` | `/api/workspaces/{slug}/contracts` |
| `GET` | `/api/workspaces/{slug}/contracts/{table}` |
| `POST` | `/api/workspaces/{slug}/contracts/{table}/promote` |
| `GET` | `/api/workspaces/{slug}/export` |
| `POST` | `/api/workspaces/{slug}/reindex` |
