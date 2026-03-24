#!/bin/bash
set -euo pipefail
task_id=config-schema-migrator
python3 harbor2tbench.py $task_id
uv run tb run \
    --dataset-path . \
    --agent oracle \
    --n-attempts 1 \
    --output-path . \
    --task-id $task_id-tbench

