#!/usr/bin/env python3
"""
批量转换 + Oracle 校验流水线。

从 downloads/ 读取所有 Harbor 格式的 zip，逐个执行：
1. 解压 → 找到 Harbor 任务目录（含 task.toml）
2. harbor2tbench 转换
3. 阿里云镜像替换
4. tb run --agent oracle 校验
5. 通过的任务收集到最终输出目录

最终输出:
    <output>/
      <task-id>/
        <task-id>.zip     转换后的任务包
        oracle.zip        oracle 运行日志

用法:
    python batch_pipeline.py
    python batch_pipeline.py --downloads-dir ./downloads --output-dir ./output_final
    python batch_pipeline.py --tb-command "uv run tb" --mirror-url http://mirrors.aliyun.com/pypi/simple/
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("batch_pipeline")

SCRIPT_DIR = Path(__file__).resolve().parent
CONVERTER = SCRIPT_DIR / "harbor2tbbench" / "harbor2tbench.py"
MIRROR_SCRIPT = SCRIPT_DIR / "replace_python_mirrors.py"
DEFAULT_MIRROR_URL = "http://mirrors.aliyun.com/pypi/simple/"


@dataclass
class TaskResult:
    zip_name: str
    task_id: str
    stage: str = "pending"
    solved: bool = False
    error: str = ""
    elapsed_sec: float = 0.0


@dataclass
class PipelineSummary:
    _results: list[TaskResult] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, result: TaskResult) -> None:
        with self._lock:
            self._results.append(result)

    @property
    def results(self) -> list[TaskResult]:
        with self._lock:
            return list(self._results)

    @property
    def total(self) -> int:
        with self._lock:
            return len(self._results)

    @property
    def solved_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._results if r.solved)

    @property
    def failed_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._results if not r.solved)

    def dump(self, path: Path) -> None:
        results = self.results
        solved = sum(1 for r in results if r.solved)
        data = {
            "total": len(results),
            "solved": solved,
            "failed": len(results) - solved,
            "tasks": [
                {
                    "zip": r.zip_name,
                    "task_id": r.task_id,
                    "stage": r.stage,
                    "solved": r.solved,
                    "error": r.error,
                    "elapsed_sec": round(r.elapsed_sec, 1),
                }
                for r in results
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find_harbor_dir(extract_root: Path) -> Path | None:
    """在解压目录中找到包含 task.toml 的 Harbor 任务目录。"""
    for pattern in ["task.toml", "*/task.toml", "*/*/task.toml"]:
        for toml_path in extract_root.glob(pattern):
            return toml_path.parent
    return None


def run_step(cmd: list[str], label: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    log.info("[%s] %s", label, " ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=True, cwd=cwd)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:]
        raise RuntimeError(f"{label} 失败 (exit {result.returncode}): {stderr_tail}")
    return result


def find_results_json(oracle_output: Path) -> Path | None:
    candidates = sorted(
        oracle_output.glob("*/results.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def check_solved(results_path: Path) -> bool:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    return data.get("n_resolved", 0) >= 1 and data.get("n_unresolved", 0) == 0


def make_zip(source_dir: Path, zip_path: Path) -> None:
    """将目录打包为 zip（保留相对路径）。"""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(source_dir.rglob("*")):
            if fp.is_file():
                zf.write(fp, fp.relative_to(source_dir.parent))


def process_one(
    zip_path: Path,
    work_root: Path,
    output_dir: Path,
    mirror_url: str,
    tb_command: str,
    n_attempts: int,
) -> TaskResult:
    """处理单个 zip 的完整流水线。"""
    task_base = zip_path.stem
    task_id = f"{task_base}-tbench"
    result = TaskResult(zip_name=zip_path.name, task_id=task_id)
    t0 = time.monotonic()

    task_work = work_root / task_base
    if task_work.exists():
        shutil.rmtree(task_work)

    unzip_dir = task_work / "unzip"
    dataset_dir = task_work / "dataset"
    oracle_output = task_work / "oracle"
    unzip_dir.mkdir(parents=True)
    dataset_dir.mkdir(parents=True)
    oracle_output.mkdir(parents=True)

    try:
        # 1. 解压
        result.stage = "unzip"
        log.info("── [%s] 解压 ──", task_base)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(unzip_dir)

        # 2. 定位 Harbor 目录
        result.stage = "find_harbor"
        harbor_dir = find_harbor_dir(unzip_dir)
        if harbor_dir is None:
            raise RuntimeError("未找到 task.toml，不是合法 Harbor 任务")

        # 3. 转换
        result.stage = "convert"
        tbench_dir = dataset_dir / task_id
        run_step(
            [sys.executable, str(CONVERTER), str(harbor_dir), str(tbench_dir)],
            f"convert({task_base})",
        )
        if not (tbench_dir / "task.yaml").exists():
            raise RuntimeError("转换失败，缺少 task.yaml")

        # 4. 镜像替换
        result.stage = "mirror"
        run_step(
            [sys.executable, str(MIRROR_SCRIPT), str(tbench_dir), "--mirror-url", mirror_url],
            f"mirror({task_base})",
        )

        # 5. Oracle 校验
        result.stage = "oracle"
        tb_parts = tb_command.split()
        cmd = [
            *tb_parts, "run",
            "--dataset-path", str(dataset_dir),
            "--agent", "oracle",
            "--n-attempts", str(n_attempts),
            "--output-path", str(oracle_output),
            "--task-id", task_id,
        ]
        log.info("[oracle(%s)] %s", task_base, " ".join(cmd))
        subprocess.run(cmd, text=True)

        # 6. 检查结果
        result.stage = "check"
        rj = find_results_json(oracle_output)
        if rj is None:
            raise RuntimeError("oracle 运行后未找到 results.json")

        solved = check_solved(rj)
        result.solved = solved

        if solved:
            result.stage = "package"
            dest = output_dir / task_id
            dest.mkdir(parents=True, exist_ok=True)

            task_zip = dest / f"{task_id}.zip"
            make_zip(tbench_dir, task_zip)
            log.info("任务包: %s", task_zip)

            oracle_zip = dest / "oracle.zip"
            oracle_run_dir = rj.parent
            make_zip(oracle_run_dir, oracle_zip)
            log.info("Oracle 日志: %s", oracle_zip)

            result.stage = "done"
            log.info("✓ [%s] SOLVED", task_id)
        else:
            result.stage = "not_solved"
            log.warning("✗ [%s] NOT SOLVED", task_id)

    except Exception as e:
        result.error = str(e)
        log.error("✗ [%s] 失败 @ %s: %s", task_id, result.stage, e)

    result.elapsed_sec = time.monotonic() - t0
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="批量 Harbor→tbench 转换 + Oracle 校验")
    parser.add_argument("--downloads-dir", default="downloads", help="原始 zip 所在目录")
    parser.add_argument("--output-dir", default="output_final", help="最终输出目录")
    parser.add_argument("--work-dir", default="work", help="中间工作目录")
    parser.add_argument("--tb-command", default="tb", help="tb CLI 命令（如 'uv run tb'）")
    parser.add_argument("--n-attempts", type=int, default=1, help="oracle 重试次数")
    parser.add_argument("--mirror-url", default=DEFAULT_MIRROR_URL, help="PyPI 镜像 URL")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4）")
    args = parser.parse_args()

    downloads = Path(args.downloads_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    work_root = Path(args.work_dir).resolve()

    if not downloads.is_dir():
        sys.exit(f"下载目录不存在: {downloads}")

    zips = sorted(downloads.glob("*.zip"))
    if not zips:
        sys.exit(f"未找到 zip 文件: {downloads}")

    for p in [CONVERTER, MIRROR_SCRIPT]:
        if not p.is_file():
            sys.exit(f"依赖脚本不存在: {p}")

    output_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    workers = max(1, args.workers)

    log.info("=" * 60)
    log.info("批量流水线启动")
    log.info("  zip 数量:  %d", len(zips))
    log.info("  并发数:    %d", workers)
    log.info("  输出目录:  %s", output_dir)
    log.info("  tb 命令:   %s", args.tb_command)
    log.info("=" * 60)

    summary = PipelineSummary()
    t_start = time.monotonic()
    finished = 0
    finished_lock = threading.Lock()

    def _run(idx: int, zp: Path) -> TaskResult:
        nonlocal finished
        task_id = f"{zp.stem}-tbench"
        existing = output_dir / task_id / "oracle.zip"
        if existing.exists():
            log.info("⏭ [%d/%d] 跳过（已完成）: %s", idx, len(zips), task_id)
            tr = TaskResult(zip_name=zp.name, task_id=task_id, stage="skipped", solved=True)
            summary.append(tr)
            with finished_lock:
                finished += 1
            return tr

        log.info("▶ [%d/%d] 开始: %s", idx, len(zips), zp.name)
        tr = process_one(
            zip_path=zp,
            work_root=work_root,
            output_dir=output_dir,
            mirror_url=args.mirror_url,
            tb_command=args.tb_command,
            n_attempts=args.n_attempts,
        )
        summary.append(tr)
        with finished_lock:
            finished += 1
            log.info(
                "进度: %d/%d | 通过: %d | 失败: %d",
                finished, len(zips), summary.solved_count, summary.failed_count,
            )
        return tr

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run, i, zp): zp
            for i, zp in enumerate(zips, 1)
        }
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                zp = futures[fut]
                log.error("未预期的异常 [%s]: %s", zp.name, e)

    elapsed = time.monotonic() - t_start
    summary_path = output_dir / "summary.json"
    summary.dump(summary_path)

    final_results = summary.results
    solved = sum(1 for r in final_results if r.solved)
    failed_list = [r for r in final_results if not r.solved]

    log.info("=" * 60)
    log.info("全部完成  耗时 %.0fs", elapsed)
    log.info("  总计:   %d", len(final_results))
    log.info("  通过:   %d", solved)
    log.info("  失败:   %d", len(failed_list))
    log.info("  汇总:   %s", summary_path)
    if failed_list:
        log.info("失败列表:")
        for r in failed_list:
            log.info("  - %s  stage=%s  err=%s", r.task_id, r.stage, r.error or "(not solved)")
    log.info("=" * 60)

    sys.exit(0 if solved == len(final_results) else 1)


if __name__ == "__main__":
    main()
