"""
Oracle verification: result.txt must match the expected checksum output.
Used to detect whether the agent found the correct configuration (oracle).
Expectation is derived from the same hash as runner (canonical correct config).
"""

import hashlib
import json
from pathlib import Path

# Canonical correct config (must match solution/solve.sh). Checksum derived, not hardcoded.
_CORRECT_CONFIG = {"algorithm": "bisect", "max_iter": 100, "output_mode": "checksum", "tolerance": 1e-5}
EXPECTED_LINE = "OK:" + hashlib.sha256(json.dumps(_CORRECT_CONFIG, sort_keys=True).encode()).hexdigest()[:8]


def test_result_file_exists():
    path = Path("/app/result.txt")
    assert path.exists(), "Result file /app/result.txt does not exist"


def test_result_matches_oracle():
    path = Path("/app/result.txt")
    content = path.read_text().strip()
    assert content == EXPECTED_LINE, (
        f"Oracle mismatch: expected '{EXPECTED_LINE}', got '{content}'"
    )
