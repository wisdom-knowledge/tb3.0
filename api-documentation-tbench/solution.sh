#!/bin/bash
set -euo pipefail

cd /solution
nohup node src/server.js >/tmp/api-documentation-oracle.log 2>&1 &

for _ in $(seq 1 60); do
  if python3 - <<'PY'
import sys
from urllib.request import urlopen

try:
    with urlopen("http://127.0.0.1:8080/api/docs", timeout=1) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  then
    echo "oracle patch applied successfully"
    exit 0
  fi
  sleep 1
done

echo "service did not become ready" >&2
exit 1
