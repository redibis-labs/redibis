#!/usr/bin/env bash
# Local FastAPI webapp dev helper (python -m redibis.webapp.backend).
#
# Usage:
#   ./scripts/webapp.sh --start                         # foreground (+ reload)
#   ./scripts/webapp.sh --start --background            # detached → .run/webapp.log
#   ./scripts/webapp.sh --start --background --monitor
#   ./scripts/webapp.sh --restart [--background] [--monitor]  # stop + start
#   ./scripts/webapp.sh --stop
#   ./scripts/webapp.sh --status
#   ./scripts/webapp.sh --start --venv                  # use .venv/bin/python
#   ./scripts/webapp.sh --start --ner                   # set REDIBIS_MODELS_DIR (+ default model if present)
#   ./scripts/webapp.sh --start --clean-users           # wipe users, then start (login admin/admin)
#   ./scripts/webapp.sh --restart --clean-users --background
#
# Environment (optional overrides):
#   REDIBIS_WEBAPP_PORT=8000
#   REDIBIS_WEBAPP_START_TIMEOUT=180  # seconds to wait for bind (conda imports can be slow on WSL)
#   REDIBIS_WEBAPP_USE_VENV=1         # same as --venv (prefer .venv over .conda)
#   REDIBIS_WEBAPP_USE_NER=1          # same as --ner (export REDIBIS_MODELS_DIR / REDIBIS_NER_MODEL)
#   PYTHON=/path/to/python            # explicit interpreter (wins over --venv)
#   USE_LOCAL_STORAGE=true
#   LOCAL_STORAGE_ROOT=./_local_storage
#   SCAN_OUTPUT_DIR=./scan_output
#   REDIBIS_CONFIG=/path/to/redibis.yaml
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${REDIBIS_WEBAPP_PORT:-8000}"
START_TIMEOUT="${REDIBIS_WEBAPP_START_TIMEOUT:-180}"
RUN_DIR="${ROOT}/.run"
LOG_FILE="${RUN_DIR}/webapp.log"
PID_FILE="${RUN_DIR}/webapp.pid"
SCRIPT_NAME="$(basename "$0")"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

resolve_python() {
  if [ -n "${PYTHON:-}" ] && [ -x "${PYTHON}" ]; then
    echo "${PYTHON}"
    return 0
  fi
  local candidate
  local -a candidates=()
  if [ "${USE_VENV}" = "1" ]; then
    candidates=("${ROOT}/.venv/bin/python")
  else
    candidates=(
      "${ROOT}/.conda/bin/python"
      "${ROOT}/.venv/bin/python"
      "${ROOT}/venv/bin/python"
    )
  fi
  for candidate in "${candidates[@]}"; do
    if [ -x "${candidate}" ]; then
      echo "${candidate}"
      return 0
    fi
  done
  if [ "${USE_VENV}" = "1" ]; then
    echo "No .venv at ${ROOT}/.venv — create one:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -e \".[dev,web]\"" >&2
    exit 1
  fi
  command -v python3 2>/dev/null || command -v python
}

# Conda wheels (sqlite3, ICU, GE) need the env libstdc++ ahead of /lib64.
prefer_interpreter_libs() {
  local python_bin="$1"
  local prefix libdir
  prefix="$(cd "$(dirname "${python_bin}")/.." && pwd 2>/dev/null || true)"
  libdir="${prefix}/lib"
  if [ -z "${prefix}" ] || [ ! -d "${libdir}" ]; then
    return 0
  fi
  case ":${LD_LIBRARY_PATH:-}:" in
    *":${libdir}:"*) ;;
    *) export LD_LIBRARY_PATH="${libdir}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" ;;
  esac
}

port_pids() {
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  fi
  if [ -z "${pids}" ] && command -v ss >/dev/null 2>&1; then
    pids="$(ss -ltnp "sport = :${PORT}" 2>/dev/null \
      | awk -F'pid=' 'NF>1 {split($2,a,","); print a[1]}' \
      | sort -u \
      | tr '\n' ' ')"
  fi
  if [ -z "${pids}" ] && command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "${PORT}/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)"
  fi
  echo "${pids}" | xargs echo 2>/dev/null || true
}

port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE ":${PORT}[[:space:]]" && return 0
    ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN && return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | grep -q . && return 0
  fi
  return 1
}

http_ready() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sf --max-time 2 "http://127.0.0.1:${PORT}/docs" >/dev/null 2>&1
}

log_has_uvicorn_running() {
  [ -f "${LOG_FILE}" ] && grep -q "Uvicorn running on" "${LOG_FILE}" 2>/dev/null
}

strip_ansi() {
  sed -E 's/\x1b\[[0-9;]*m//g'
}

log_since_marker() {
  local n="${1:-5}"
  [ -f "${LOG_FILE}" ] || return 0
  awk '
    /────.*(webapp\.sh|restart_webapp\.sh) ────/ { buf=""; inblock=1; next }
    inblock { buf = buf $0 "\n" }
    END { printf "%s", buf }
  ' "${LOG_FILE}" \
    | strip_ansi \
    | grep -vE '^(python:|port:)[[:space:]]' \
    | grep -E '.+' \
    | tail -n "${n}"
}

recent_log_lines() {
  local n="${1:-3}"
  local lines
  lines="$(log_since_marker "${n}")"
  if [ -n "${lines}" ]; then
    printf '%s\n' "${lines}"
    return 0
  fi
  [ -f "${LOG_FILE}" ] || return 0
  tail -n "${n}" "${LOG_FILE}" 2>/dev/null \
    | strip_ansi \
    | grep -E '^(INFO:|ERROR:|WARNING:)' \
    | tail -n "${n}" || true
}

sync_pid_from_log() {
  local uv_pid
  uv_pid="$(grep "Started server process" "${LOG_FILE}" 2>/dev/null | tail -1 | sed -n 's/.*\[\([0-9]*\)\].*/\1/p' || true)"
  if [ -n "${uv_pid}" ]; then
    echo "${uv_pid}" >"${PID_FILE}"
  fi
}

wait_for_port_free() {
  local i=0
  local timeout=20
  while port_listening && [ "${i}" -lt "${timeout}" ]; do
    sleep 0.5
    i=$((i + 1))
  done
  if port_listening; then
    echo "Port ${PORT} still in use after stop" >&2
    return 1
  fi
  return 0
}

stop_webapp() {
  local pids
  pids="$(port_pids)"
  if [ -n "${pids}" ]; then
    echo "Stopping process(es) on port ${PORT}: ${pids}"
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
  fi
  pkill -f "[p]ython -m redibis.webapp.backend" 2>/dev/null || true
  pkill -f "[u]vicorn redibis.webapp.backend:app" 2>/dev/null || true
  rm -f "${PID_FILE}"
  wait_for_port_free || true
}

status_webapp() {
  if port_listening; then
    echo "Webapp listening on http://127.0.0.1:${PORT}"
    if command -v ss >/dev/null 2>&1; then
      ss -ltnp "sport = :${PORT}" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
      lsof -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
    fi
    if [ -f "${PID_FILE}" ]; then
      echo "  pid file: $(cat "${PID_FILE}")"
    fi
    echo "  log: ${LOG_FILE}"
    return 0
  fi
  echo "No listener on port ${PORT}"
  return 1
}

webapp_ready() {
  if port_listening; then
    http_ready && return 0
    log_has_uvicorn_running && return 0
    return 0
  fi
  return 1
}

wait_for_ready() {
  local i=0
  local line
  while [ "${i}" -lt "${START_TIMEOUT}" ]; do
    if webapp_ready; then
      sync_pid_from_log
      echo "  ready (${i}s)"
      return 0
    fi
    if [ -f "${PID_FILE}" ]; then
      local pid
      pid="$(cat "${PID_FILE}")"
      if ! kill -0 "${pid}" 2>/dev/null; then
        echo "Process ${pid} exited before binding port ${PORT}" >&2
        echo "Recent log:" >&2
        recent_log_lines 15 | sed 's/^/  /' >&2 || true
        return 1
      fi
    fi
    sleep 1
    i=$((i + 1))
    if [ $((i % 5)) -eq 0 ]; then
      echo "  waiting for port ${PORT}… (${i}s)"
      if recent_log_lines 3 | grep -q .; then
        while IFS= read -r line; do
          [ -n "${line}" ] && echo "  log: ${line}"
        done < <(recent_log_lines 3)
      else
        echo "  log: (loading Python modules — first startup can take ~90s on WSL)"
      fi
    fi
  done
  echo "Timed out after ${START_TIMEOUT}s waiting for port ${PORT}" >&2
  echo "Tip: conda/WSL imports can be slow — try REDIBIS_WEBAPP_START_TIMEOUT=300" >&2
  return 1
}

monitor_log() {
  mkdir -p "${RUN_DIR}"
  touch "${LOG_FILE}"
  echo "─── tail -f ${LOG_FILE} (Ctrl+C stops tail only) ───"
  tail -n "${TAIL_LINES}" -f "${LOG_FILE}"
}

apply_ner_env() {
  if [ "${USE_NER}" != "1" ]; then
    return 0
  fi
  export REDIBIS_MODELS_DIR="${REDIBIS_MODELS_DIR:-${ROOT}/models}"
  local default_model="${ROOT}/models/gliner-multi-v2.1"
  if [ -z "${REDIBIS_NER_MODEL:-}" ] && [ -d "${default_model}" ]; then
    export REDIBIS_NER_MODEL="${default_model}"
  fi
}

# Validate the *exact* interpreter that will serve requests can import
# gliner/torch and see the model weights, before uvicorn ever binds the
# port. This is diagnostic only (never blocks --start): a broken NER
# runtime should still let regex/phone scanning work, but the operator
# should see the real reason ("torch._C missing" vs "no model at path")
# instead of a silent, misleading "engines_ran" without ner.
validate_ner_runtime() {
  local python_bin="$1"
  if [ "${USE_NER}" != "1" ]; then
    return 0
  fi
  echo "  ner: validating runtime with ${python_bin} …"
  local report
  report="$("${python_bin}" - "${REDIBIS_NER_MODEL:-}" <<'PYEOF' 2>&1
import sys

model_path = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    import gliner  # noqa: F401
except ImportError as exc:
    print(f"FAIL gliner-import: {exc}")
    sys.exit(0)
print(f"OK gliner-import: {getattr(gliner, '__version__', 'unknown')}")

try:
    import torch  # noqa: F401
    print(f"OK torch-import: {torch.__version__}")
except ImportError as exc:
    print(f"FAIL torch-import: {exc}")
    sys.exit(0)

if model_path:
    from pathlib import Path

    p = Path(model_path)
    if p.is_dir():
        print(f"OK model-path: {p}")
    else:
        print(f"FAIL model-path: {p} does not exist")
else:
    print("WARN model-path: REDIBIS_NER_MODEL not set — relying on Settings/config default")
PYEOF
  )"
  local line
  while IFS= read -r line; do
    [ -n "${line}" ] && echo "  ner: ${line}"
  done <<<"${report}"
  {
    echo "ner-runtime-check (python: ${python_bin}):"
    echo "${report}" | sed 's/^/  /'
  } >>"${LOG_FILE}"
  if echo "${report}" | grep -q '^FAIL gliner-import'; then
    echo "  ner: WARNING — gliner failed to import in ${python_bin}." >&2
    echo "  ner:   If gliner is installed but this still fails, it is almost" >&2
    echo "  ner:   always a broken transitive dependency (torch/onnxruntime)" >&2
    echo "  ner:   in *this* interpreter, not a missing package. Try --venv," >&2
    echo "  ner:   or reinstall torch in this interpreter." >&2
  elif echo "${report}" | grep -q '^FAIL torch-import'; then
    echo "  ner: WARNING — torch failed to import in ${python_bin}; NER will be unavailable." >&2
  elif echo "${report}" | grep -q '^FAIL model-path'; then
    echo "  ner: WARNING — NER model path not found; set REDIBIS_NER_MODEL or REDIBIS_MODELS_DIR." >&2
  else
    echo "  ner: runtime looks OK"
  fi
}

_rm_if_exists() {
  local f="$1"
  if [ -e "${f}" ]; then
    rm -f "${f}"
    echo "  removed ${f}"
  fi
}

clean_users() {
  local auth_json="${REDIBIS_AUTH_PATH:-${ROOT}/configs/auth/users.json}"
  local auth_dir
  auth_dir="$(dirname "${auth_json}")"
  local storage="${LOCAL_STORAGE_ROOT:-${ROOT}/_local_storage}"
  echo "Cleaning dashboard users — next start logs in as admin/admin"
  _rm_if_exists "${auth_json}"
  _rm_if_exists "${auth_dir}/sessions.json"
  _rm_if_exists "${auth_dir}/shares.json"
  _rm_if_exists "${auth_json}.lock"
  _rm_if_exists "${auth_dir}/users.json.lock"
  _rm_if_exists "${storage}/active-contracts/_meta/users.json"
  _rm_if_exists "${storage}/active-contracts/_meta/sessions.json"
  _rm_if_exists "${storage}/active-contracts/_meta/shares.json"
}

start_webapp() {
  local python_bin
  python_bin="$(resolve_python)"
  if [ -z "${python_bin}" ]; then
    echo "No python found — activate your venv/conda or set PYTHON=" >&2
    exit 1
  fi
  prefer_interpreter_libs "${python_bin}"

  if port_listening; then
    echo "Port ${PORT} already in use — run ./scripts/webapp.sh --stop or --restart" >&2
    exit 1
  fi

  if [ "${CLEAN_USERS}" = "1" ]; then
    clean_users
  fi

  mkdir -p "${RUN_DIR}"
  export USE_LOCAL_STORAGE="${USE_LOCAL_STORAGE:-true}"
  export LOCAL_STORAGE_ROOT="${LOCAL_STORAGE_ROOT:-${ROOT}/_local_storage}"
  export SCAN_OUTPUT_DIR="${SCAN_OUTPUT_DIR:-${ROOT}/scan_output}"
  export PYTHONUNBUFFERED=1
  mkdir -p "${LOCAL_STORAGE_ROOT}" "${SCAN_OUTPUT_DIR}"
  apply_ner_env

  cd "${ROOT}"
  validate_ner_runtime "${python_bin}"

  if [ "${BACKGROUND}" = "1" ]; then
    echo "Starting webapp in background on port ${PORT}…"
    echo "  python: ${python_bin}"
    echo "  log: ${LOG_FILE}"
    echo "  timeout: ${START_TIMEOUT}s (set REDIBIS_WEBAPP_START_TIMEOUT to override)"
    if [ "${USE_NER}" = "1" ]; then
      echo "  ner: REDIBIS_MODELS_DIR=${REDIBIS_MODELS_DIR:-}"
      if [ -n "${REDIBIS_NER_MODEL:-}" ]; then
        echo "  ner: REDIBIS_NER_MODEL=${REDIBIS_NER_MODEL}"
      fi
    fi
    {
      echo "──── $(date -Iseconds) ${SCRIPT_NAME} ────"
      echo "python: ${python_bin}"
      echo "port: ${PORT}"
    } >>"${LOG_FILE}"
    nohup "${python_bin}" -m uvicorn redibis.webapp.backend:app \
      --host 0.0.0.0 \
      --port "${PORT}" \
      >>"${LOG_FILE}" 2>&1 &
    echo $! >"${PID_FILE}"
    if ! wait_for_ready; then
      echo "Failed to start — recent log:" >&2
      recent_log_lines 30 | sed 's/^/  /' >&2 || true
      exit 1
    fi
    echo "  pid: $(cat "${PID_FILE}")"
    echo "  scan: http://127.0.0.1:${PORT}/"
    echo "  agents: http://127.0.0.1:${PORT}/agents  (type URL; not linked in nav)"
    echo "  settings: http://127.0.0.1:${PORT}/settings"
    echo "  local-full preset: REDIBIS_CONFIG=config/examples/agents-local-full.yaml"
    echo "  monitor: ./scripts/webapp.sh --logs"
    if [ "${MONITOR}" = "1" ]; then
      echo ""
      monitor_log
    fi
    return 0
  fi

  echo "Starting webapp on http://127.0.0.1:${PORT}/ (Ctrl+C to stop)…"
  echo "  agents: http://127.0.0.1:${PORT}/agents  (type URL; not linked in nav)"
  echo "  settings: http://127.0.0.1:${PORT}/settings"
  echo "  local-full preset: REDIBIS_CONFIG=config/examples/agents-local-full.yaml"
  echo "  python: ${python_bin}"
  if [ "${USE_NER}" = "1" ]; then
    echo "  ner: REDIBIS_MODELS_DIR=${REDIBIS_MODELS_DIR:-}"
    if [ -n "${REDIBIS_NER_MODEL:-}" ]; then
      echo "  ner: REDIBIS_NER_MODEL=${REDIBIS_NER_MODEL}"
    fi
  fi
  exec "${python_bin}" -m redibis.webapp.backend
}

BACKGROUND=0
MONITOR=0
TAIL_LINES=80
ACTION=""
CLEAN_USERS=0
USE_VENV="${REDIBIS_WEBAPP_USE_VENV:-0}"
case "${USE_VENV}" in
  1|true|yes|on) USE_VENV=1 ;;
  *) USE_VENV=0 ;;
esac
USE_NER="${REDIBIS_WEBAPP_USE_NER:-0}"
case "${USE_NER}" in
  1|true|yes|on) USE_NER=1 ;;
  *) USE_NER=0 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    ---*)
      # ---background → --background (extra-dash typo)
      set -- "--${1#---}" "${@:2}"
      continue
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --start)
      ACTION=start
      shift
      ;;
    --restart)
      ACTION=restart
      shift
      ;;
    --stop)
      ACTION=stop
      shift
      ;;
    --status)
      ACTION=status
      shift
      ;;
    --logs|--log)
      ACTION=logs
      shift
      ;;
    -d|--background|--detach)
      BACKGROUND=1
      shift
      ;;
    -m|--monitor)
      MONITOR=1
      shift
      ;;
    --venv)
      USE_VENV=1
      shift
      ;;
    --ner)
      USE_NER=1
      shift
      ;;
    --clean-users|--reset-users)
      CLEAN_USERS=1
      shift
      ;;
    --port)
      PORT="${2:?missing port}"
      shift 2
      ;;
    --tail-lines)
      TAIL_LINES="${2:?missing line count}"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "${ACTION}" ]; then
  if [ "${CLEAN_USERS}" = "1" ]; then
    clean_users
    echo "Start the dashboard with ./scripts/webapp.sh --start (or --restart) to recreate admin/admin."
    exit 0
  fi
  echo "Missing action — use --start, --restart, --stop, --status, or --logs" >&2
  usage >&2
  exit 1
fi

case "${ACTION}" in
  stop)
    stop_webapp
    echo "Stopped (port ${PORT})"
    ;;
  status)
    status_webapp
    ;;
  logs)
    monitor_log
    ;;
  start)
    start_webapp
    ;;
  restart)
    stop_webapp
    start_webapp
    ;;
esac
