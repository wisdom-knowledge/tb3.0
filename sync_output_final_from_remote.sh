#!/usr/bin/env bash
set -euo pipefail

# 从远端拉取 tb1_pass/output_final 到本仓库下的 output_final_remote/
# 在任意目录执行均可；目标目录固定在脚本所在目录旁。

REMOTE_USER_HOST="root@101.47.75.226"
REMOTE_SRC="~/tb1_pass/output_final"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${ROOT}/output_final_remote"

mkdir -p "${DEST}"

rsync -avz --progress --human-readable \
  "${REMOTE_USER_HOST}:${REMOTE_SRC}/" \
  "${DEST}/"
