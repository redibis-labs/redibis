#!/usr/bin/env bash
# ============================================================
# redibis — Local Development Environment Setup Script
#
# Sets up a clean environment with pinned dependency versions.
#
# Modes:
#   1. Conda/Micromamba Mode (Recommended — matches Docker)
#      Sets up a conda environment using docker/local/environment.yml.
#
#   2. Virtualenv/Pip Mode
#      Sets up a python venv and installs pre-resolved requirements.
#
# Usage:
#   ./scripts/setup_dev_env.sh [conda|pip]
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Color variables for nice output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
ok() { echo -e "${GREEN}[OK]${NC} $*"; }

usage() {
    echo "Usage: $0 [conda|pip]"
    echo ""
    echo "Options:"
    echo "  conda    Create a local Conda/Micromamba environment (matches Docker local stack)"
    echo "  pip      Create a Python virtualenv and install from requirements/requirements-all.txt"
    echo ""
}

setup_conda() {
    info "Setting up Conda/Micromamba environment..."
    
    local env_file="${REPO_ROOT}/docker/local/environment.yml"
    local constraints_file="${REPO_ROOT}/docker/local/constraints.txt"
    
    [ -f "${env_file}" ] || error "Environment file not found at ${env_file}"
    [ -f "${constraints_file}" ] || error "Constraints file not found at ${constraints_file}"
    
    # Detect manager
    local manager=""
    if command -v micromamba &>/dev/null; then
        manager="micromamba"
    elif command -v conda &>/dev/null; then
        manager="conda"
    else
        error "Neither 'conda' nor 'micromamba' found. Install one of them or use 'pip' mode."
    fi
    
    info "Using ${manager} to create environment 'redibis'..."
    if [ "${manager}" = "micromamba" ]; then
        PIP_CONSTRAINT="${constraints_file}" micromamba create -y -n redibis -f "${env_file}"
    else
        # Conda might not support PIP_CONSTRAINT directly in all shells, so we set it
        PIP_CONSTRAINT="${constraints_file}" conda env create -f "${env_file}" -n redibis -y
    fi
    
    # Install redibis in editable mode without deps (since all deps are solved)
    info "Installing redibis package in editable mode (--no-deps)..."
    if [ "${manager}" = "micromamba" ]; then
        micromamba run -n redibis pip install --no-deps -e "${REPO_ROOT}"
    else
        conda run -n redibis pip install --no-deps -e "${REPO_ROOT}"
    fi
    
    ok "Conda environment 'redibis' successfully created and configured!"
    echo "To activate:"
    echo "  ${manager} activate redibis"
}

setup_pip() {
    info "Setting up Python virtual environment..."
    
    local lock_file="${REPO_ROOT}/requirements/requirements-all.txt"
    [ -f "${lock_file}" ] || error "Requirements lockfile not found at ${lock_file}"
    
    # Detect python (prefer <=3.12 due to torch/gliner compatibility)
    local python_bin=""
    if command -v python3.11 &>/dev/null; then
        python_bin="python3.11"
    elif command -v python3.12 &>/dev/null; then
        python_bin="python3.12"
    elif command -v python3.10 &>/dev/null; then
        python_bin="python3.10"
    elif command -v python3 &>/dev/null; then
        python_bin="python3"
    elif command -v python &>/dev/null; then
        python_bin="python"
    else
        error "Python not found. Please install Python 3.10+."
    fi
    
    info "Creating virtual environment '.venv'..."
    "${python_bin}" -m venv "${REPO_ROOT}/.venv"
    
    local pip_bin="${REPO_ROOT}/.venv/bin/pip"
    local act_cmd="source .venv/bin/activate"
    
    info "Upgrading pip, setuptools, and wheel..."
    "${pip_bin}" install --upgrade pip setuptools wheel
    
    info "Installing pre-resolved dependencies from ${lock_file}..."
    "${pip_bin}" install -r "${lock_file}"
    
    info "Installing redibis package in editable mode (--no-deps)..."
    "${pip_bin}" install --no-deps -e "${REPO_ROOT}"
    
    ok "Virtual environment created and configured at ${REPO_ROOT}/.venv"
    echo "To activate:"
    echo "  ${act_cmd}"
}

# Main dispatch
MODE="${1:-}"
if [ -z "${MODE}" ]; then
    # Interactive prompt if no option given
    echo "No setup mode specified."
    echo "Choose setup mode:"
    echo "  1) Conda/Micromamba (Recommended - aligns with Docker)"
    echo "  2) Pip / Virtual environment"
    read -rp "Enter choice [1-2]: " choice
    case "${choice}" in
        1) MODE="conda" ;;
        2) MODE="pip" ;;
        *) error "Invalid choice" ;;
    esac
fi

case "${MODE}" in
    conda) setup_conda ;;
    pip)   setup_pip ;;
    -h|--help|help) usage ;;
    *)     usage; error "Unknown setup mode: ${MODE}" ;;
esac
