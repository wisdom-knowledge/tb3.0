#!/bin/bash
set -euo pipefail

SOURCE_DIR="/solution"
TARGET_DIR="/app"
SPICEDB_URL="${SPICEDB_URL:-http://spicedb:8443}"
SPICEDB_TOKEN="${SPICEDB_TOKEN:-devtoken}"

copy_runtime_files() {
  install -m 0644 "${SOURCE_DIR}/package.json" "${TARGET_DIR}/package.json"
  install -m 0644 "${SOURCE_DIR}/server.js" "${TARGET_DIR}/server.js"
  install -m 0644 "${SOURCE_DIR}/rbac.js" "${TARGET_DIR}/rbac.js"
  install -m 0644 "${SOURCE_DIR}/rule-utils.js" "${TARGET_DIR}/rule-utils.js"
  install -m 0644 "${SOURCE_DIR}/validation.js" "${TARGET_DIR}/validation.js"
  install -m 0644 "${SOURCE_DIR}/spicedb-client.js" "${TARGET_DIR}/spicedb-client.js"
  install -m 0644 "${SOURCE_DIR}/state-store.js" "${TARGET_DIR}/state-store.js"
  install -m 0755 "${SOURCE_DIR}/run-service.sh" "${TARGET_DIR}/solve.sh"
}

request_restart() {
  curl -fsS \
    -X POST \
    -H "X-Harbor-Op: restart" \
    "http://127.0.0.1:8081/internal/ops/restart" >/dev/null
}

kill_existing_node() {
  local proc_path
  local pid
  local cmdline

  for proc_path in /proc/[0-9]*; do
    pid="${proc_path##*/}"
    if [[ ! -r "${proc_path}/cmdline" ]]; then
      continue
    fi

    cmdline="$(tr '\0' ' ' < "${proc_path}/cmdline" 2>/dev/null || true)"
    if [[ "$cmdline" == *"node server.js"* ]]; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
}

wait_for_http_200() {
  local url="$1"
  local label="$2"
  local extra_args=("${@:3}")

  for _ in $(seq 1 90); do
    if curl -fsS "${extra_args[@]}" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "${label} did not become ready: ${url}" >&2
  return 1
}

wait_for_gateway_payload() {
  for _ in $(seq 1 90); do
    local payload
    payload="$(curl -fsS "http://gateway:8080/api/users/admin/permissions" 2>/dev/null || true)"
    if [[ "$payload" == *'"permissions"'* ]]; then
      return 0
    fi
    sleep 1
  done

  echo "gateway did not return permissions payload" >&2
  return 1
}

wait_for_spicedb_schema() {
  for _ in $(seq 1 90); do
    local payload
    payload="$(
      curl -fsS \
        -X POST \
        -H "Authorization: Bearer ${SPICEDB_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{}' \
        "${SPICEDB_URL}/v1/schema/read" 2>/dev/null || true
    )"
    if [[ "$payload" == *"definition policy"* ]]; then
      return 0
    fi
    sleep 1
  done

  echo "spicedb schema was not ready" >&2
  return 1
}

copy_runtime_files

if ! request_restart; then
  kill_existing_node
fi

wait_for_http_200 "http://127.0.0.1:8081/health" "main service health"
wait_for_gateway_payload
wait_for_spicedb_schema

echo "oracle patch applied successfully"
