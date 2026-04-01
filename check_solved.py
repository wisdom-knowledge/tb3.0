#!/usr/bin/env python3
"""
通用任务 solved 校验脚本。

运行 `tb run --agent oracle` 并解析 results.json 中的 n_resolved 字段，
判断任务是否被正确求解。

用法:
    python check_solved.py --dataset-path . --task-id <task-id>
    python check_solved.py --dataset-path ./tasks --task-id foo-tbench --output-path /tmp/runs
    python check_solved.py --results-json 2026-04-01__11-40-12/results.json  # 仅解析已有结果
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("check_solved")


def find_latest_results_json(output_path: Path) -> Path | None:
    """在 output_path 下找到最新的 results.json（按目录修改时间）。"""
    candidates = sorted(
        output_path.glob("*/results.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_results(results_path: Path) -> dict:
    """解析 results.json，返回关键字段。"""
    data = json.loads(results_path.read_text(encoding="utf-8"))
    n_resolved = data.get("n_resolved", 0)
    n_unresolved = data.get("n_unresolved", 0)
    accuracy = data.get("accuracy", 0.0)
    resolved_ids = data.get("resolved_ids", [])
    unresolved_ids = data.get("unresolved_ids", [])

    trials = data.get("results", [])
    failure_modes = {
        t["task_id"]: t.get("failure_mode", "unset")
        for t in trials
        if not t.get("is_resolved")
    }

    return {
        "n_resolved": n_resolved,
        "n_unresolved": n_unresolved,
        "accuracy": accuracy,
        "resolved_ids": resolved_ids,
        "unresolved_ids": unresolved_ids,
        "failure_modes": failure_modes,
        "solved": n_resolved >= 1 and n_unresolved == 0,
    }


def run_tb_oracle(
    dataset_path: str,
    task_id: str,
    output_path: str,
    n_attempts: int = 1,
    tb_command: str = "tb",
) -> Path | None:
    """执行 tb run --agent oracle，返回生成的 results.json 路径。"""
    cmd = [
        tb_command, "run",
        "--dataset-path", dataset_path,
        "--agent", "oracle",
        "--n-attempts", str(n_attempts),
        "--output-path", output_path,
        "--task-id", task_id,
    ]
    log.info("执行: %s", " ".join(cmd))

    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        log.warning("tb run 退出码 %d（不一定代表任务失败，以 results.json 为准）", result.returncode)

    results_json = find_latest_results_json(Path(output_path))
    if results_json is None:
        log.error("未找到 results.json，运行可能异常")
    return results_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="通用任务 solved 校验：运行 oracle 或解析已有 results.json",
    )
    parser.add_argument("--dataset-path", help="tb 任务数据集目录")
    parser.add_argument("--task-id", help="任务 ID")
    parser.add_argument("--output-path", default=".", help="tb run 的输出目录（默认当前目录）")
    parser.add_argument("--n-attempts", type=int, default=1, help="oracle 重试次数")
    parser.add_argument("--tb-command", default="tb", help="tb CLI 命令")
    parser.add_argument(
        "--results-json",
        help="直接指定 results.json 路径（跳过 tb run，仅解析）",
    )
    args = parser.parse_args()

    if args.results_json:
        results_path = Path(args.results_json)
    elif args.dataset_path and args.task_id:
        results_path = run_tb_oracle(
            dataset_path=args.dataset_path,
            task_id=args.task_id,
            output_path=args.output_path,
            n_attempts=args.n_attempts,
            tb_command=args.tb_command,
        )
    else:
        parser.error("需要指定 --results-json，或同时指定 --dataset-path 和 --task-id")

    if results_path is None or not results_path.exists():
        log.error("results.json 不存在: %s", results_path)
        sys.exit(2)

    log.info("解析: %s", results_path)
    summary = parse_results(results_path)

    log.info("─" * 40)
    log.info("n_resolved:   %d", summary["n_resolved"])
    log.info("n_unresolved: %d", summary["n_unresolved"])
    log.info("accuracy:     %.2f%%", summary["accuracy"] * 100)
    if summary["resolved_ids"]:
        log.info("resolved:     %s", ", ".join(summary["resolved_ids"]))
    if summary["unresolved_ids"]:
        log.info("unresolved:   %s", ", ".join(summary["unresolved_ids"]))
    if summary["failure_modes"]:
        for tid, mode in summary["failure_modes"].items():
            log.info("  failure [%s]: %s", tid, mode)
    log.info("─" * 40)

    if summary["solved"]:
        log.info("SOLVED ✓")
        sys.exit(0)
    else:
        log.error("NOT SOLVED ✗")
        sys.exit(1)


if __name__ == "__main__":
    main()
