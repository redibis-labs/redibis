# 05 — Policy codegen (propose, don’t execute)

**Goal:** preview egress-safe requests and submit local or remote policy proposals.  
**Time:** 45 minutes.  
**Surface:** Codegen board view (also reachable from Composer when intent is codegen-like)

---

## 1. What codegen is (and is not)

Codegen **proposes** artifacts (e.g. Apache Ranger JSON). Open-core **never executes**
them. Masking enforcement stays outside redibis (Ranger/Trino).

Default path: **local** engine (`redibis.agents.codegen_local`) — template and optional
local LLM. Remote hosted GCP service is vendor-only; customers use URL + token.

Status: `GET /api/agents/codegen/status` (`method`: `local` | `remote` | …).

---

## 2. Board workflow

1. Open Codegen (or trigger from Composer on a code-shaped intent).
2. Set **table**, **intent**, **target** (`ranger`), **residency**.
3. **Preview egress request** — metadata only; shows redactions / allowed flag.
4. **Submit** — runs capability guard, then local/remote generation.
5. Inspect `code`, `vuln_report`, `judge_verdict`, provenance (remote).

If status is `native_capability_available`, redibis already has a pipeline for that ask —
prefer Composer nodes. Use **force external** only when teaching the override.

---

## 3. Capability guard (teach this explicitly)

```text
“Mask PII on telecom.customers”  →  native PII/mask/contract  →  do not codegen
“Emit a Ranger policy JSON for maskingPolicy columns”  →  codegen OK
```

Details: [`REDIBIS_CAPABILITY_MAP.md`](../REDIBIS_CAPABILITY_MAP.md).

---

## 4. Config switches

| Knob | Meaning |
|------|---------|
| `allow_external_codegen` | Permit egress / remote path |
| `codegen_service_url` + `REDIBIS_CODEGEN_TOKEN` | Remote method |
| `codegen_mode` | `local` vs `remote` |
| `rai.hard_block_external_pii` | Blocks unsafe egress when true |

Docs: [`CODEGEN_SERVICE.md`](../../CODEGEN_SERVICE.md),
[`enterprise/README.md`](../../CODEGEN_SERVICE.md).

---

## 5. Lab exercise

1. Preview codegen for a table that has an active contract with masking policies.
2. Submit local generation; confirm `status: proposed` and judge approved or explained.
3. Submit a native-sounding intent (“detect PII”); confirm guard steers away unless forced.

**Pass criteria:** learner distinguishes proposed artifact vs native pipeline and never
assumes code was applied to the warehouse.

---

## Next

→ [06 — Planner, chat & LLM roles](06-planner-chat-llm.md)
