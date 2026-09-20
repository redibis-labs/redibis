#!/usr/bin/env bash
# ============================================================
# redibis — batch LLM-enrich contracts under ./scan_output
#
# There is no `redibis enrich-batch`. This driver loops every ODCS YAML
# under OUTPUT_DIR/_dev_storage/active-contracts/active (or every table from
# `redibis list`) and calls `redibis enrich` once per contract.
#
# Only active/ is used — audit/{table}/{uuid}.yaml holds immutable historical
# snapshots of the SAME table; enriching/pushing those re-processes stale
# duplicates. If your session used a nested --output-dir (e.g.
# ./scan_output/<workflow>/...), pass --contracts-dir explicitly or use
# --output that nested path.
#
# Usage:
#   ./scripts/batch_enrich.sh
#   ./scripts/batch_enrich.sh --output ./scan_output --provider sglang
#   ./scripts/batch_enrich.sh --from-store --provider sglang-qwen
#   ./scripts/batch_enrich.sh --push
#
# Defaults match scripts/batch_scan.sh output + local SGLang.
# ============================================================
set -euo pipefail

OUTPUT_DIR="${REDIBIS_ENRICH_OUTPUT_DIR:-./scan_output}"
PROVIDER="${REDIBIS_ENRICH_PROVIDER:-sglang}"
ENDPOINT="${REDIBIS_ENRICH_ENDPOINT:-http://localhost:30000/v1}"
MODEL="${REDIBIS_ENRICH_MODEL:-Qwen/Qwen2.5-7B-Instruct}"
API_KEY="${REDIBIS_ENRICH_API_KEY:-${SGLANG_API_KEY:-sk-local}}"
PROVIDERS_FILE="${REDIBIS_LLM_PROVIDERS:-}"
CONFIG="${REDIBIS_CONFIG:-}"
CONTRACTS_DIR=""
FROM_STORE=0
PUSH=0
FAIL_FAST=0
EXTRA_ARGS=()

usage() {
    sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  -o, --output DIR         Scan / artifact root (default: ./scan_output)
      --contracts-dir DIR  ODCS YAML folder (default: OUTPUT/_dev_storage/active-contracts/active)
      --from-store         Enrich every table from `redibis list` instead of files
      --provider NAME      LLM provider (default: sglang)
      --endpoint URL       api_base override (default: http://localhost:30000/v1)
      --model NAME         model override (default: Qwen/Qwen2.5-7B-Instruct)
      --api-key KEY        provider key (default: $SGLANG_API_KEY or sk-local)
      --providers-file F   llm_providers.json (else $REDIBIS_LLM_PROVIDERS)
      --config FILE        redibis.yaml (else $REDIBIS_CONFIG)
      --push               After enrich, catalog push-batch the contracts dir
      --fail-fast          Stop on first enrich failure
  -h, --help               Show this message

Examples:
  ./scripts/batch_enrich.sh
  ./scripts/batch_enrich.sh -o ./scan_output --provider sglang
  ./scripts/batch_enrich.sh --from-store --provider sglang-qwen \
      --providers-file config/examples/llm-providers-sglang-qwen.json
  ./scripts/batch_enrich.sh --push --config /opt/redibis/app/redibis.yaml
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)         OUTPUT_DIR="$2"; shift 2 ;;
        --contracts-dir)     CONTRACTS_DIR="$2"; shift 2 ;;
        --from-store)        FROM_STORE=1; shift ;;
        --provider)          PROVIDER="$2"; shift 2 ;;
        --endpoint)          ENDPOINT="$2"; shift 2 ;;
        --model)             MODEL="$2"; shift 2 ;;
        --api-key)           API_KEY="$2"; shift 2 ;;
        --providers-file)    PROVIDERS_FILE="$2"; shift 2 ;;
        --config)            CONFIG="$2"; shift 2 ;;
        --push)              PUSH=1; shift ;;
        --fail-fast)         FAIL_FAST=1; shift ;;
        -h|--help)           usage; exit 0 ;;
        --)                  shift; EXTRA_ARGS+=("$@"); break ;;
        *)                   EXTRA_ARGS+=("$1"); shift ;;
    esac
done

if ! command -v redibis >/dev/null 2>&1; then
    echo "error: redibis not found on PATH (pip install -e .)" >&2
    exit 1
fi

if [[ -z "$CONTRACTS_DIR" ]]; then
    CONTRACTS_DIR="${OUTPUT_DIR}/_dev_storage/active-contracts/active"
fi

ENRICH_FLAGS=(
    --provider "$PROVIDER"
    --endpoint "$ENDPOINT"
    --model "$MODEL"
    --api-key "$API_KEY"
    --output-dir "$OUTPUT_DIR"
)
[[ -n "$PROVIDERS_FILE" ]] && ENRICH_FLAGS+=(--providers-file "$PROVIDERS_FILE")
[[ -n "$CONFIG" ]] && ENRICH_FLAGS+=(--config "$CONFIG")
[[ ${#EXTRA_ARGS[@]} -gt 0 ]] && ENRICH_FLAGS+=("${EXTRA_ARGS[@]}")

failed=0
succeeded=0

if [[ "$FROM_STORE" -eq 1 ]]; then
    mapfile -t tables < <(redibis list --output-dir "$OUTPUT_DIR" | sed 's/^[[:space:]]*//' | sed '/^$/d')
    if [[ ${#tables[@]} -eq 0 ]]; then
        echo "error: no active contracts in ${OUTPUT_DIR} (redibis list empty)" >&2
        exit 1
    fi
    for table in "${tables[@]}"; do
        echo "========================================"
        echo "Enrich (store): ${table}"
        echo "========================================"
        if redibis enrich "$table" "${ENRICH_FLAGS[@]}"; then
            succeeded=$((succeeded + 1))
        else
            echo "FAILED: ${table}" >&2
            failed=$((failed + 1))
            [[ "$FAIL_FAST" -eq 1 ]] && exit 1
        fi
    done
else
    if [[ ! -d "$CONTRACTS_DIR" ]]; then
        echo "error: contracts dir missing: ${CONTRACTS_DIR}" >&2
        echo "  scan with: ./scripts/batch_scan.sh --output ${OUTPUT_DIR} --auto-write" >&2
        if [[ -d "$OUTPUT_DIR" ]]; then
            mapfile -t found < <(find "$OUTPUT_DIR" -type d -path '*_dev_storage/active-contracts/active' 2>/dev/null)
            if [[ ${#found[@]} -gt 0 ]]; then
                echo "  found nested active-contracts under ${OUTPUT_DIR} — pass one explicitly:" >&2
                for d in "${found[@]}"; do
                    echo "    --contracts-dir ${d}" >&2
                done
            fi
        fi
        exit 1
    fi
    mapfile -t unique < <(find "$CONTRACTS_DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) | sort)
    if [[ ${#unique[@]} -eq 0 ]]; then
        echo "error: no *.yaml / *.yml under ${CONTRACTS_DIR}" >&2
        exit 1
    fi

    for f in "${unique[@]}"; do
        echo "========================================"
        echo "Enrich (file): ${f}"
        echo "========================================"
        if redibis enrich --contract "$f" "${ENRICH_FLAGS[@]}"; then
            succeeded=$((succeeded + 1))
        else
            echo "FAILED: ${f}" >&2
            failed=$((failed + 1))
            [[ "$FAIL_FAST" -eq 1 ]] && exit 1
        fi
    done
fi

echo ""
echo "Batch enrich complete: ${succeeded} succeeded, ${failed} failed"

if [[ "$PUSH" -eq 1 ]]; then
    echo "========================================"
    echo "catalog push-batch ${CONTRACTS_DIR}"
    echo "========================================"
    push_args=(catalog push-batch "$CONTRACTS_DIR" --glob '*.yaml' --backend openmetadata)
    [[ -n "$CONFIG" ]] && push_args+=(--config "$CONFIG")
    redibis "${push_args[@]}"
fi

if [[ "$succeeded" -eq 0 && "$failed" -eq 0 ]]; then
    echo "error: nothing was enriched" >&2
    exit 1
fi
if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
