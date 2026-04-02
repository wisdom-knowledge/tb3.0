#!/usr/bin/env bash
set -euo pipefail

# 将本仓库 downloads/ 整目录 scp 到远端 ~/tb1_pass/downloads/

REMOTE_USER_HOST="root@101.47.75.226"
REMOTE_PATH="~/tb1_pass"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${ROOT}/downloads"

if [[ ! -d "${SRC}" ]]; then
  echo "本地目录不存在: ${SRC}" >&2
  exit 1
fi

scp -r "${SRC}" "${REMOTE_USER_HOST}:${REMOTE_PATH}/"
