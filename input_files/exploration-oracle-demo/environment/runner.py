#!/usr/bin/env python3
"""
Numerical solver runner. Reads config from config.json and writes result to result.txt.
Fails or produces wrong result if config is invalid. Error messages do not name the bad key.
Correct output format: one line OK:<checksum> where checksum is 8-char lowercase hex (derived from config).
"""
import hashlib
import json
import sys
from pathlib import Path

CONFIG_PATH = Path("/app/config.json")
RESULT_PATH = Path("/app/result.txt")

# Initial (wrong) config shipped with the task; runner must see config was actually changed.
_INITIAL_CONFIG = {"algorithm": "linear", "max_iter": 5, "output_mode": "verbose", "tolerance": 0.00001}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def main() -> int:
    if not CONFIG_PATH.exists():
        log("ERROR: config.json not found")
        return 1

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        log(f"ERROR: invalid JSON: {e}")
        return 1

    # Required keys (high exploration: any of these can be wrong type or value)
    algorithm = config.get("algorithm")
    tolerance = config.get("tolerance")
    max_iter = config.get("max_iter")
    output_mode = config.get("output_mode")

    # Decision point 1: algorithm must be "bisect" (not "linear" or missing)
    if algorithm != "bisect":
        log("ERROR: solver did not converge (check algorithm choice)")
        return 1

    # Decision point 2: tolerance must be number in (0, 1]
    try:
        t = float(tolerance)
        if not (0 < t <= 1):
            raise ValueError("tolerance out of range")
    except (TypeError, ValueError):
        log("ERROR: invalid or out-of-range parameter")
        return 1

    # Decision point 3: max_iter must be integer >= 10
    try:
        n = int(max_iter)
        if n < 10:
            raise ValueError("max_iter too small")
    except (TypeError, ValueError):
        log("ERROR: invalid or out-of-range parameter")
        return 1

    # Decision point 4: output_mode must be "checksum"
    if output_mode != "checksum":
        log("ERROR: unsupported output mode")
        return 1

    # All config correct: compute checksum from canonical config (no hardcoded answer)
    canonical = {"algorithm": algorithm, "max_iter": n, "output_mode": output_mode, "tolerance": t}
    # Require that config was actually changed (prevent trivial hack: writing result.txt only)
    if canonical == _INITIAL_CONFIG:
        log("ERROR: config was not modified from initial state")
        return 1
    checksum = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:8]
    RESULT_PATH.write_text(f"OK:{checksum}\n")
    log("Success")
    return 0


if __name__ == "__main__":
    sys.exit(main())
