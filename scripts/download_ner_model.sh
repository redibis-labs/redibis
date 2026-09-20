#!/usr/bin/env bash
# ==============================================================================
# redibis — download_ner_model.sh
#
# Download a GLiNER (or other Hugging Face) NER checkpoint and optionally zip it
# for ``redibis models upload``.
#
# Usage:
#   ./scripts/download_ner_model.sh
#   ./scripts/download_ner_model.sh --model urchade/gliner_medium-v2.1
#   ./scripts/download_ner_model.sh --dest ./models --no-zip --slim
#   ./scripts/download_ner_model.sh --tokenizer-only --name gliner-multi-v2.1
#
# Outputs (default under ./models/, gitignored):
#   models/<name>/              — unpacked weights + encoder/ for offline Docker
#   models/<name>/encoder/      — base HF tokenizer (microsoft/mdeberta-v3-base, etc.)
#   models/<name>.zip           — upload-ready archive (unless --no-zip)
#
# After download:
#   redibis models list --models-dir ./models
#   redibis models activate <name> --config redibis.yaml
#   # or upload elsewhere:
#   redibis models upload ./models/<name>.zip --name <name>
#
# Requires: huggingface-cli or Python package ``huggingface_hub``
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_MODEL="urchade/gliner_multi-v2.1"
MODEL_ID="${DEFAULT_MODEL}"
DEST_DIR="${REPO_ROOT}/models"
FOLDER_NAME=""
DO_ZIP=1
DO_SLIM=0
WRITE_MANIFEST=1
WITH_BASE_TOKENIZER=1
TOKENIZER_ONLY=0
FORCE=0

GRN='\033[0;32m'
RED='\033[0;31m'
CYN='\033[0;36m'
BLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GRN}  ✓${NC}  $*"; }
fail() { echo -e "${RED}  ✗${NC}  $*"; exit 1; }
info() { echo -e "${CYN}  →${NC}  $*"; }
warn() { echo -e "${RED}  !${NC}  $*"; }

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  -m, --model ID           Hugging Face repo id (default: urchade/gliner_multi-v2.1)
  -d, --dest DIR           Models root (default: ./models)
  -n, --name NAME          Folder / zip base name (default: derived from repo id)
      --no-zip             Skip creating .zip archive
      --slim               Drop .cache and pytorch_model.bin when .safetensors exists
      --no-manifest        Do not write redibis-model.json
      --skip-base-tokenizer
                           Skip downloading encoder/ (offline Docker scans will fail)
      --with-base-tokenizer
                           Alias for default behaviour (bundle encoder/ subfolder)
      --tokenizer-only     Only download encoder/ into an existing model folder
      --force              Re-download even if destination folder exists
  -h, --help               Show this message

Environment:
  HF_TOKEN                 Hugging Face token for private / gated models
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -m|--model) MODEL_ID="$2"; shift 2 ;;
        -d|--dest) DEST_DIR="$2"; shift 2 ;;
        -n|--name) FOLDER_NAME="$2"; shift 2 ;;
        --no-zip) DO_ZIP=0; shift ;;
        --slim) DO_SLIM=1; shift ;;
        --no-manifest) WRITE_MANIFEST=0; shift ;;
        --skip-base-tokenizer) WITH_BASE_TOKENIZER=0; shift ;;
        --with-base-tokenizer) WITH_BASE_TOKENIZER=1; shift ;;
        --tokenizer-only) TOKENIZER_ONLY=1; WITH_BASE_TOKENIZER=1; DO_ZIP=0; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (try --help)" ;;
    esac
done

if [[ -z "${FOLDER_NAME}" ]]; then
    # urchade/gliner_multi-v2.1 → gliner-multi-v2.1
    FOLDER_NAME="$(basename "${MODEL_ID}")"
    FOLDER_NAME="${FOLDER_NAME//_/-}"
fi

DEST_ROOT="$(mkdir -p "${DEST_DIR}" && cd "${DEST_DIR}" && pwd)"
MODEL_DIR="${DEST_ROOT}/${FOLDER_NAME}"
ZIP_PATH="${DEST_ROOT}/${FOLDER_NAME}.zip"

if [[ "${TOKENIZER_ONLY}" -eq 1 ]]; then
    if [[ ! -d "${MODEL_DIR}" || ! -f "${MODEL_DIR}/gliner_config.json" ]]; then
        fail "Tokenizer-only mode requires an existing GLiNER folder: ${MODEL_DIR}"
    fi
else
    if [[ -d "${MODEL_DIR}" && "${FORCE}" -eq 0 ]]; then
        fail "Destination already exists: ${MODEL_DIR}
  Use --force to re-download, --tokenizer-only to add encoder/, or pick --name."
    fi

    if [[ "${FORCE}" -eq 1 && -d "${MODEL_DIR}" ]]; then
        info "Removing existing ${MODEL_DIR} (--force)"
        rm -rf "${MODEL_DIR}"
    fi
fi

download_hf() {
    local repo_id="$1"
    local local_dir="$2"
    if command -v huggingface-cli >/dev/null 2>&1; then
        info "Downloading ${repo_id} → ${local_dir} (huggingface-cli)"
        huggingface-cli download "${repo_id}" --local-dir "${local_dir}" --local-dir-use-symlinks False
        return 0
    fi
    if python3 -c "import huggingface_hub" 2>/dev/null; then
        info "Downloading ${repo_id} → ${local_dir} (huggingface_hub)"
        HF_REPO_ID="${repo_id}" HF_LOCAL_DIR="${local_dir}" python3 - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ["HF_REPO_ID"],
    local_dir=os.environ["HF_LOCAL_DIR"],
    local_dir_use_symlinks=False,
)
PY
        return 0
    fi
    fail "Need huggingface-cli or Python package huggingface_hub.
  pip install huggingface_hub
  # or: pip install 'huggingface_hub[cli]'"
}

bundle_base_tokenizer() {
    local model_dir="$1"
    local base_id=""
    if [[ -f "${model_dir}/gliner_config.json" ]]; then
        base_id="$(python3 - "${model_dir}/gliner_config.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print((data.get("model_name") or "").strip())
PY
)"
    fi
    if [[ -z "${base_id}" ]]; then
        warn "No model_name in gliner_config.json; skipping encoder download."
        return 1
    fi
    local encoder_dir="${model_dir}/encoder"
    info "Downloading base encoder ${base_id} → ${encoder_dir}"
    download_hf "${base_id}" "${encoder_dir}"
    rm -rf "${encoder_dir}/.cache"
    ok "Bundled offline tokenizer: ${encoder_dir}"
}

echo -e "${BLD}====================================================${NC}"
echo -e "${BLD}     redibis — NER model download                  ${NC}"
echo -e "${BLD}====================================================${NC}"
info "Model:  ${MODEL_ID}"
info "Folder: ${MODEL_DIR}"

mkdir -p "${DEST_ROOT}"

if [[ "${TOKENIZER_ONLY}" -eq 1 ]]; then
    bundle_base_tokenizer "${MODEL_DIR}" || fail "Could not bundle encoder for ${MODEL_DIR}"
    ok "Tokenizer bundle ready: ${MODEL_DIR}/encoder"
    exit 0
fi

download_hf "${MODEL_ID}" "${MODEL_DIR}"

if [[ ! -f "${MODEL_DIR}/gliner_config.json" ]]; then
    warn "No gliner_config.json in ${MODEL_DIR} — upload validation expects a GLiNER layout."
fi

if [[ "${DO_SLIM}" -eq 1 ]]; then
    rm -rf "${MODEL_DIR}/.cache"
    if [[ -f "${MODEL_DIR}/model.safetensors" && -f "${MODEL_DIR}/pytorch_model.bin" ]]; then
        info "Removing pytorch_model.bin (--slim; safetensors kept)"
        rm -f "${MODEL_DIR}/pytorch_model.bin"
    fi
else
    rm -rf "${MODEL_DIR}/.cache"
fi

if [[ "${WITH_BASE_TOKENIZER}" -eq 1 ]]; then
    bundle_base_tokenizer "${MODEL_DIR}" || true
fi

if [[ "${WRITE_MANIFEST}" -eq 1 && ! -f "${MODEL_DIR}/redibis-model.json" ]]; then
    info "Writing redibis-model.json"
    NAME="${FOLDER_NAME}" python3 - "${MODEL_DIR}/redibis-model.json" <<'PY'
import json, os, sys

path = sys.argv[1]
payload = {
    "type": "gliner",
    "name": os.environ["NAME"],
    "labels": [
        "person",
        "phone number",
        "email",
        "address",
        "national id",
        "passport",
        "credit card",
        "organization",
    ],
    "default_threshold": 0.35,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY
    ok "Wrote ${MODEL_DIR}/redibis-model.json"
fi

if [[ "${DO_ZIP}" -eq 1 ]]; then
    if [[ -f "${ZIP_PATH}" ]]; then
        info "Replacing existing ${ZIP_PATH}"
        rm -f "${ZIP_PATH}"
    fi
    info "Creating ${ZIP_PATH} (this may take several minutes for ~1–2 GB models)"
    (cd "${DEST_ROOT}" && zip -r -q "${FOLDER_NAME}.zip" "${FOLDER_NAME}/")
    ok "Archive: ${ZIP_PATH} ($(du -h "${ZIP_PATH}" | cut -f1))"
fi

ok "Model ready: ${MODEL_DIR}"
echo ""
echo "Next steps:"
echo "  export REDIBIS_MODELS_DIR=${DEST_ROOT}"
echo "  export REDIBIS_NER_MODEL=${MODEL_DIR}"
echo "  redibis models list --models-dir ${DEST_ROOT}"
if [[ "${DO_ZIP}" -eq 1 ]]; then
    echo "  redibis models upload ${ZIP_PATH} --name ${FOLDER_NAME} --models-dir ${DEST_ROOT}"
fi
echo "  redibis models activate ${FOLDER_NAME} --config redibis.yaml"
