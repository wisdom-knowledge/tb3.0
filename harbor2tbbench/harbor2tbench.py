#!/usr/bin/env python3
"""Convert a Harbor-format task directory to Terminal-Bench format.

Harbor layout:
    instruction.md
    task.toml
    environment/Dockerfile
    solution/solve.sh
    tests/test.sh
    tests/test_*.py

Terminal-Bench layout:
    task.yaml
    Dockerfile
    solution.sh          (or solution.yaml)
    run-tests.sh         (optional, omitted when defaults suffice)
    tests/test_outputs.py

Usage:
    python harbor2tbench.py <harbor_task_dir> [<output_dir>]

If <output_dir> is omitted, it defaults to <harbor_task_dir>-tbench.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import textwrap
from pathlib import Path

def _parse_toml(path: Path) -> dict:
    """Minimal TOML parser sufficient for Harbor task.toml files.

    Handles tables ([section], [section.sub]), basic key = value pairs
    (strings, numbers, booleans, arrays of scalars).  Does NOT cover the
    full TOML spec — only what Harbor task.toml files actually use.
    """
    data: dict = {}
    current: dict = data

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # Table header  [foo.bar]
            m = re.match(r"^\[([A-Za-z0-9_.\-]+)]$", line)
            if m:
                current = data
                for part in m.group(1).split("."):
                    current = current.setdefault(part, {})
                continue

            # Array of tables header  [[foo]] — not used in Harbor tasks, skip
            if line.startswith("[["):
                continue

            # key = value
            m = re.match(r'^([A-Za-z0-9_\-]+)\s*=\s*(.+)$', line)
            if not m:
                continue
            key, val_raw = m.group(1), m.group(2).strip()

            # Inline array  [ "a", "b" ]
            if val_raw.startswith("["):
                items = re.findall(r'"([^"]*)"', val_raw)
                current[key] = items
                continue

            # Quoted string
            if val_raw.startswith('"') and val_raw.endswith('"'):
                current[key] = val_raw[1:-1]
                continue

            # Boolean
            if val_raw in ("true", "false"):
                current[key] = val_raw == "true"
                continue

            # Number (int or float)
            try:
                current[key] = int(val_raw)
                continue
            except ValueError:
                pass
            try:
                current[key] = float(val_raw)
                continue
            except ValueError:
                pass

            current[key] = val_raw

    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yaml_block_scalar(text: str, indent: int = 6) -> str:
    """Return *text* formatted as a YAML block-scalar (``|``) with the given
    indentation for continuation lines."""
    pad = " " * indent
    return "\n".join(f"{pad}{line}" for line in text.splitlines())


def _yaml_scalar(value) -> str:
    """Format a Python value as a safe inline YAML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if re.search(r"[:\#{}\[\],&*?|>!%@`]", s) or s in ("true", "false", "null", ""):
        return f'"{s}"'
    return s


# ---------------------------------------------------------------------------
# task.toml  →  task.yaml
# ---------------------------------------------------------------------------

def build_task_yaml(config: dict, instruction: str) -> str:
    meta = config.get("metadata", {})
    agent_cfg = config.get("agent", {})
    verifier_cfg = config.get("verifier", {})

    lines: list[str] = []

    # instruction (block scalar)
    lines.append("instruction: |-")
    lines.append(_yaml_block_scalar(instruction, indent=2))
    lines.append("")

    # author
    if email := meta.get("author_email"):
        lines.append(f"author_email: {_yaml_scalar(email)}")

    # difficulty (required by Terminal-Bench)
    difficulty = meta.get("difficulty", "medium")
    lines.append(f"difficulty: {_yaml_scalar(difficulty)}")

    # tags
    tags = meta.get("tags", [])
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {_yaml_scalar(t)}")

    # timeouts
    lines.append(f"max_agent_timeout_sec: {int(agent_cfg.get('timeout_sec', 180))}")
    lines.append(f"max_test_timeout_sec: {int(verifier_cfg.get('timeout_sec', 30))}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Dockerfile cleanup
# ---------------------------------------------------------------------------

_RE_PIP_INSTALL_START = re.compile(r"^\s*RUN\s+pip\s+install\b", re.IGNORECASE)


_TBENCH_APT_DEPS = "RUN apt-get update && apt-get install -y tmux asciinema"


def clean_dockerfile(content: str) -> str:
    """Remove ``RUN pip install`` blocks that install test-only packages
    (pytest, etc.) — Terminal-Bench installs test deps via its own scripts.

    Also strips comment-only lines immediately preceding a removed block
    (e.g. ``# Install Python test dependencies in the image``).

    Ensures tmux and asciinema are installed (required by Terminal-Bench).
    """
    raw_lines = content.splitlines()
    keep = [True] * len(raw_lines)
    skip = False

    for i, line in enumerate(raw_lines):
        if skip:
            keep[i] = False
            if not line.rstrip().endswith("\\"):
                skip = False
            continue

        if _RE_PIP_INSTALL_START.match(line):
            keep[i] = False
            if line.rstrip().endswith("\\"):
                skip = True
            # Also drop comment lines directly above
            j = i - 1
            while j >= 0 and raw_lines[j].strip().startswith("#") and not raw_lines[j].strip().startswith("#!"):
                keep[j] = False
                j -= 1
            continue

    out = [l for l, k in zip(raw_lines, keep) if k]

    # Collapse multiple consecutive blank lines into one
    result: list[str] = []
    for line in out:
        if not line.strip() and result and not result[-1].strip():
            continue
        result.append(line)

    # Inject tmux/asciinema install if not already present
    joined = "\n".join(result)
    if "tmux" not in joined:
        # Insert after the FROM line
        for i, line in enumerate(result):
            if line.strip().upper().startswith("FROM "):
                result.insert(i + 1, _TBENCH_APT_DEPS)
                break
        else:
            result.insert(0, _TBENCH_APT_DEPS)

    return "\n".join(result).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Default file templates for Terminal-Bench
# ---------------------------------------------------------------------------

RUN_TESTS_SH = textwrap.dedent("""\
    #!/bin/bash
    set -euo pipefail

    bash /tests/test.sh

    reward=$(cat /logs/verifier/reward.txt 2>/dev/null || echo 0)
    if [ "$reward" = "1" ]; then
      exit 0
    else
      exit 1
    fi
""")


DEFAULT_DOCKER_COMPOSE_YAML = textwrap.dedent("""\
    services:
      client:
        build:
          context: .
          dockerfile: Dockerfile
        image: ${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
        container_name: ${T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}
        command: [ "sh", "-c", "sleep infinity" ]
        environment:
          - TEST_DIR=${T_BENCH_TEST_DIR}
        volumes:
          - ${T_BENCH_TASK_LOGS_PATH}:${T_BENCH_CONTAINER_LOGS_PATH}
""")


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def convert(src: Path, dst: Path) -> None:
    if not (src / "task.toml").exists():
        sys.exit(f"Error: {src / 'task.toml'} not found — is this a Harbor task?")

    dst.mkdir(parents=True, exist_ok=True)

    # 1. Parse Harbor config & instruction
    config = _parse_toml(src / "task.toml")
    instruction = (src / "instruction.md").read_text(encoding="utf-8").strip()

    # 2. Generate task.yaml
    (dst / "task.yaml").write_text(build_task_yaml(config, instruction), encoding="utf-8")

    # 3. Environment directory → copy everything, with special handling for
    #    Dockerfile (clean) and docker-compose.yaml (default if absent).
    env_dir = src / "environment"
    has_dockerfile = False
    has_compose = False

    if env_dir.is_dir():
        for item in env_dir.iterdir():
            if not item.is_file():
                continue
            if item.name == "Dockerfile":
                has_dockerfile = True
                cleaned = clean_dockerfile(item.read_text(encoding="utf-8"))
                (dst / "Dockerfile").write_text(cleaned, encoding="utf-8")
            elif item.name == "docker-compose.yaml":
                has_compose = True
                shutil.copy2(item, dst / "docker-compose.yaml")
            else:
                shutil.copy2(item, dst / item.name)

    if not has_dockerfile:
        print(f"Warning: {env_dir / 'Dockerfile'} not found, skipping Dockerfile.", file=sys.stderr)
    if not has_compose:
        (dst / "docker-compose.yaml").write_text(DEFAULT_DOCKER_COMPOSE_YAML, encoding="utf-8")

    # 5. Solution  (solution/solve.sh → solution.sh)
    solution_src = src / "solution" / "solve.sh"
    if solution_src.exists():
        shutil.copy2(solution_src, dst / "solution.sh")
    else:
        print(f"Warning: {solution_src} not found, skipping solution.", file=sys.stderr)

    # 6. Tests  →  copy entire tests/ directory; generate run-tests.sh wrapper
    tests_src = src / "tests"
    tests_dst = dst / "tests"
    if tests_src.is_dir():
        shutil.copytree(tests_src, tests_dst, dirs_exist_ok=True)
        (dst / "run-tests.sh").write_text(RUN_TESTS_SH, encoding="utf-8")
    else:
        print(f"Warning: {tests_src} not found, skipping tests.", file=sys.stderr)

    print(f"Converted: {src}  →  {dst}")
    print("Files written:")
    for p in sorted(dst.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(dst)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert a Harbor-format task to Terminal-Bench format.",
    )
    parser.add_argument("src", type=Path, help="Harbor task directory")
    parser.add_argument(
        "dst",
        type=Path,
        nargs="?",
        default=None,
        help="Output directory (default: <src>-tbench)",
    )
    args = parser.parse_args()

    src = args.src.resolve()
    dst = (args.dst or Path(f"{src}-tbench")).resolve()

    if dst.exists() and any(dst.iterdir()):
        answer = input(f"{dst} already exists and is non-empty. Overwrite? [y/N] ")
        if answer.lower() != "y":
            sys.exit("Aborted.")
        shutil.rmtree(dst)

    convert(src, dst)


if __name__ == "__main__":
    main()
