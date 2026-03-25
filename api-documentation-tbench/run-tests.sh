#!/bin/bash
set -euo pipefail

bash /tests/test.sh

reward=$(cat /logs/verifier/reward.txt 2>/dev/null || echo 0)
if [ "$reward" = "1" ]; then
  exit 0
else
  exit 1
fi
