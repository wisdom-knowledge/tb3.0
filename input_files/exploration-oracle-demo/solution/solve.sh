#!/bin/bash
# Oracle solution: fix all four config keys and run runner to produce result.txt

cat > /app/config.json << 'EOF'
{
  "algorithm": "bisect",
  "tolerance": 1e-5,
  "max_iter": 100,
  "output_mode": "checksum"
}
EOF

python3 /app/runner.pyc
