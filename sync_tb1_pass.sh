#!/usr/bin/env bash
set -euo pipefail

# 将当前目录同步到远端（排除 .gitignore 中的路径；需在仓库根目录执行）
REMOTE_USER_HOST="root@101.47.75.226"
REMOTE_PATH="~/tb1_pass"

RSYNC_OPTS=(
  -avz
  --progress
  --human-readable
)

if [[ -f .gitignore ]]; then
  RSYNC_OPTS+=(--exclude-from=.gitignore)
fi

rsync "${RSYNC_OPTS[@]}" ./ "${REMOTE_USER_HOST}:${REMOTE_PATH}/"
