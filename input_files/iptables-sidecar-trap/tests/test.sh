#!/bin/bash

# Install testing framework
curl -LsSf https://astral.sh/uv/0.9.7/install.sh | sh
source $HOME/.local/bin/env

# 1. Flush existing nat rules before running the agent's solution
sudo iptables -t nat -F

# 2. Execute the agent's solution
if [ -f "/workspace/setup_proxy.sh" ]; then
  echo "Found setup_proxy.sh, executing..."
  chmod +x /workspace/setup_proxy.sh
  sudo /workspace/setup_proxy.sh
  EXIT_CODE=$?
  if [ $EXIT_CODE -ne 0 ]; then
    echo "ERROR: setup_proxy.sh failed with exit code $EXIT_CODE"
  fi
else
  echo "WARNING: setup_proxy.sh not found!"
fi

# 3. Run validation
uvx \
  --with pytest==8.4.1 \
  --with pytest-json-ctrf==0.3.5 \
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA

# 4. Score
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
