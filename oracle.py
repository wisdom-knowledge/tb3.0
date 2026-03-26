#!/usr/bin/env python3
"""
Import TB1 Tasks and Run Oracle - 本地/容器版

面向 Terminal-Bench 1.x（Terminal-Bench layout）任务的 Oracle 校验脚本。

功能:
1. 从 TOS/HTTP URL 下载 zip（或使用当前目录已有 zip）
2. 解压并导入 TB1 任务到 tasks-dir
3. 通过 `tb run` 执行 Oracle 校验
4. 汇总每个任务是否通过
5. 打包产物到 ./artifacts/
6. 输出 return.json (oracle_pass_bool, oracle_log_url)

TB1 任务布局（最小要求）:
- task.yaml
- Dockerfile
- solution.sh 或 solution.yaml
- tests/test_outputs.py
- run-tests.sh 可选

环境变量:
- VE_TOS_AK, VE_TOS_SK: 访问 TOS 时需要

说明:
- 本脚本不再依赖 Harbor / task.toml / instruction.md / solution/solve.sh / tests/test.sh
- 依赖 pip 包 terminal-bench（要求 Python >= 3.12；3.10/3.11 无可用 wheel）
- 默认通过当前解释器 `-m terminal_bench.cli.tb.main` 调用 CLI（见 TB_ORACLE_CMD）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

import requests
import tos

if load_dotenv is not None:
    load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_tb1_oracle")

ARTIFACTS_DIR = "./artifacts"
DEFAULT_TOS_REGION = "cn-beijing"


@dataclass
class TaskRunResult:
    task_name: str
    run_dir: str
    returncode: int
    passed: bool
    stdout_path: str
    stderr_path: str


def upload_to_tos(
    local_path: str,
    tos_url: str,
    tos_endpoint: str,
    tos_region: str = DEFAULT_TOS_REGION,
) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(tos_url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")

    log.info("上传到 TOS: %s -> %s", local_path, tos_url)
    client = tos.TosClientV2(
        ak=os.environ["VE_TOS_AK"],
        sk=os.environ["VE_TOS_SK"],
        endpoint=tos_endpoint,
        region=tos_region,
    )
    client.put_object_from_file(bucket, key, local_path)
    log.info("上传完成: %s", tos_url)
    return tos_url


def tos_url_to_http(tos_url: str, tos_endpoint: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(tos_url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    host = tos_endpoint.replace("https://", "").replace("http://", "")
    return f"https://{bucket}.{host}/{key}"


def download_from_url(
    url: str,
    tos_endpoint: str | None = None,
    tos_region: str = DEFAULT_TOS_REGION,
) -> str:
    """从 TOS 或 HTTP 下载 zip 到当前目录。"""
    if url.startswith("tos://"):
        from urllib.parse import urlparse

        if not tos_endpoint:
            raise ValueError("TOS URL 需要 --tos-endpoint 参数")

        parsed = urlparse(url)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        filename = os.path.basename(key)

        log.info("从 TOS 下载: %s", url)
        client = tos.TosClientV2(
            ak=os.environ["VE_TOS_AK"],
            sk=os.environ["VE_TOS_SK"],
            endpoint=tos_endpoint,
            region=tos_region,
        )
        client.get_object_to_file(bucket, key, filename)
    else:
        filename = os.path.basename(url.split("?")[0])
        log.info("从 HTTP 下载: %s", url)
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    log.info("下载完成: %s", filename)
    return filename


def find_task_zip() -> str:
    zips = list(Path(".").glob("*.zip"))
    if len(zips) == 0:
        raise RuntimeError("当前目录未找到 zip 文件")
    if len(zips) > 1:
        raise RuntimeError(f"当前目录存在多个 zip 文件: {[str(z) for z in zips]}")
    log.info("找到任务 zip: %s", zips[0])
    return str(zips[0])


def _is_tb1_task_dir(task_dir: Path) -> bool:
    """判断目录是否符合 TB1 / Terminal-Bench layout。"""
    if not (task_dir / "task.yaml").is_file():
        return False
    if not (task_dir / "Dockerfile").is_file():
        return False
    if not ((task_dir / "solution.sh").is_file() or (task_dir / "solution.yaml").is_file()):
        return False
    if not (task_dir / "run-tests.sh").is_file():
        return False
    # has_test_outputs = (task_dir / "tests" / "test_outputs.py").is_file()
    # has_verifier = (task_dir / "tests" / "verifier.py").is_file()

    # if not (has_test_outputs or has_verifier):
    #     return False
    return True


def _iter_candidate_task_dirs(extract_root: Path) -> Iterable[Path]:
    # 兼容 zip 多包裹一层目录的情况
    yielded: set[Path] = set()
    for depth_pattern in ["task.yaml", "*/task.yaml", "*/*/task.yaml", "*/*/*/task.yaml"]:
        for yaml_path in extract_root.glob(depth_pattern):
            task_dir = yaml_path.parent
            if task_dir not in yielded and _is_tb1_task_dir(task_dir):
                yielded.add(task_dir)
                yield task_dir


def extract_and_import_tasks(zip_path: str, tasks_dir: str) -> list[str]:
    """解压 zip 并导入 TB1 任务。"""
    extract_dir = tempfile.mkdtemp(prefix="extracted_tb1_tasks_")
    try:
        log.info("解压 %s ...", zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        extract_path = Path(extract_dir)
        task_dirs = list(_iter_candidate_task_dirs(extract_path))
        if not task_dirs:
            raise RuntimeError(
                "zip 文件中未找到符合 TB1 格式的任务目录；需要至少包含 task.yaml、Dockerfile、solution.sh/solution.yaml、run-tests.sh"
            )

        log.info("找到 %d 个 TB1 任务目录", len(task_dirs))
        tasks_path = Path(tasks_dir)
        tasks_path.mkdir(parents=True, exist_ok=True)

        imported: list[str] = []
        for task_src_dir in task_dirs:
            task_name = task_src_dir.name
            dest_dir = tasks_path / task_name

            if dest_dir.exists():
                log.warning("任务 '%s' 已存在，将被覆盖", task_name)
                shutil.rmtree(dest_dir)

            shutil.copytree(task_src_dir, dest_dir)
            log.info("已导入 TB1 任务: %s", task_name)
            imported.append(task_name)

        if not imported:
            raise RuntimeError("没有成功导入任何任务")

        log.info("共导入 %d 个 TB1 任务: %s", len(imported), ", ".join(imported))
        return imported
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _ensure_tb_cli(tb_command: str) -> None:
    parts = tb_command.split()
    if not parts:
        raise RuntimeError("tb_command 不能为空")
    tb_exe = shutil.which(parts[0])
    if tb_exe is None:
        raise FileNotFoundError(
            f"未找到 TB1 CLI 命令 '{parts[0]}'。请先安装与 TB1 任务兼容的 terminal-bench / tb。"
        )
    log.info("使用 TB CLI: %s", tb_exe)


def run_oracle_tb1(
    tasks_dir: str,
    task_names: list[str],
    run_id: str,
    tb_command: str = "tb",
    n_attempts: int = 1,
) -> tuple[str, list[TaskRunResult]]:
    """
    使用 TB1 CLI 执行 Oracle。

    这里按任务逐个执行，而不是一次把所有 task_id 打包跑完，这样更容易定位失败任务，
    同时不依赖 TB CLI 的聚合输出 JSON 格式。
    """
    _ensure_tb_cli(tb_command)
    run_root = f"runs/tb1-oracle-{run_id}"
    Path(run_root).mkdir(parents=True, exist_ok=True)

    results: list[TaskRunResult] = []
    tb_parts = tb_command.split()

    for task_name in task_names:
        task_run_dir = str(Path(run_root) / task_name)
        Path(task_run_dir).mkdir(parents=True, exist_ok=True)

        cmd = [
            *tb_parts,
            "run",
            "--dataset-path",
            tasks_dir,
            "--agent",
            "oracle",
            "--n-attempts",
            str(n_attempts),
            "--output-path",
            task_run_dir,
            "--task-id",
            task_name,
        ]
        log.info("运行 TB1 oracle: %s", " ".join(cmd))
        completed = subprocess.run(cmd, text=True, capture_output=True)

        stdout_path = str(Path(task_run_dir) / "tb_stdout.log")
        stderr_path = str(Path(task_run_dir) / "tb_stderr.log")
        Path(stdout_path).write_text(completed.stdout or "", encoding="utf-8")
        Path(stderr_path).write_text(completed.stderr or "", encoding="utf-8")

        passed = completed.returncode == 0
        if passed:
            log.info("任务 %s Oracle 通过", task_name)
        else:
            log.error("任务 %s Oracle 失败，exit code=%s", task_name, completed.returncode)

        results.append(
            TaskRunResult(
                task_name=task_name,
                run_dir=task_run_dir,
                returncode=completed.returncode,
                passed=passed,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        )

    return run_root, results


def check_tb1_results(task_results: list[TaskRunResult]) -> tuple[bool, dict]:
    total = len(task_results)
    passed_count = sum(1 for r in task_results if r.passed)
    failed = [r.task_name for r in task_results if not r.passed]
    mean_score = (passed_count / total) if total else 0.0

    summary = {
        "n_total_trials": total,
        "n_passed": passed_count,
        "n_failed": total - passed_count,
        "mean_score": mean_score,
        "failed_tasks": failed,
        "tasks": [
            {
                "task_name": r.task_name,
                "passed": r.passed,
                "returncode": r.returncode,
                "run_dir": r.run_dir,
                "stdout_path": r.stdout_path,
                "stderr_path": r.stderr_path,
            }
            for r in task_results
        ],
    }

    oracle_passed = passed_count == total and total > 0
    if oracle_passed:
        log.info("TB1 Oracle 通过: %d/%d", passed_count, total)
    else:
        log.error("TB1 Oracle 未通过: %d/%d，通过失败任务: %s", passed_count, total, failed)

    return oracle_passed, summary


def pack_artifacts(run_dir: str, record_id: str) -> str:
    artifacts = Path(ARTIFACTS_DIR)
    artifacts.mkdir(parents=True, exist_ok=True)

    zip_stem = f"{record_id}-oracle"
    zip_path = str(artifacts / f"{zip_stem}.zip")

    if os.path.exists(zip_path):
        os.remove(zip_path)

    if os.path.isdir(run_dir):
        shutil.make_archive(str(artifacts / zip_stem), "zip", root_dir=run_dir)
        log.info("产物已打包: %s", zip_path)
    else:
        log.warning("运行目录 %s 不存在，跳过打包", run_dir)

    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Import TB1 Tasks and Run Oracle")
    parser.add_argument("--record-id", required=True, help="记录 ID，用于 run_id 和产物文件名")
    parser.add_argument("--tasks-dir", default="tasks", help="TB1 任务导入目录 (默认 tasks)")
    parser.add_argument("--tb-command", default="tb", help="TB1 CLI 命令，默认 tb")
    parser.add_argument("--n-attempts", type=int, default=1, help="TB1 Oracle 重试次数，默认 1")
    parser.add_argument("--zip-url", help="TOS 或 HTTP URL，下载 zip 任务包")
    parser.add_argument("--tos-endpoint", help="TOS endpoint，如 https://tos-cn-beijing.volces.com")
    parser.add_argument("--tos-region", default=DEFAULT_TOS_REGION, help=f"TOS region，默认 {DEFAULT_TOS_REGION}")
    parser.add_argument(
        "--oracle-pass-field",
        default="oracle_pass_bool",
        help="return.json 中 oracle 通过状态的 key 名",
    )
    parser.add_argument(
        "--oracle-log-field",
        default="oracle_log_url",
        help="return.json 中 oracle 产物路径的 key 名",
    )
    parser.add_argument(
        "--upload-tos-url",
        help="上传产物到 TOS 的目标 URL (如 tos://bucket/path/prefix/)",
    )
    args = parser.parse_args()

    run_id = f"{args.record_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    log.info("=== 开始 TB1 Oracle 运行 (run_id=%s) ===", run_id)

    # Step 0: 下载任务包
    if args.zip_url:
        log.info("── Step 0: 下载任务包 ──")
        try:
            zip_path = download_from_url(
                args.zip_url,
                tos_endpoint=args.tos_endpoint,
                tos_region=args.tos_region,
            )
        except Exception as e:
            log.error("下载失败: %s", e)
            sys.exit(1)
    else:
        zip_path = None

    # Step 1: 解压并导入 TB1 任务
    log.info("── Step 1: 解压并导入 TB1 任务 ──")
    try:
        if zip_path is None:
            zip_path = find_task_zip()
        task_names = extract_and_import_tasks(zip_path, args.tasks_dir)
    except Exception as e:
        log.error("任务导入失败: %s", e)
        sys.exit(1)

    # Step 2: 运行 TB1 Oracle
    log.info("── Step 2: 运行 TB1 Oracle ──")
    run_dir, task_results = run_oracle_tb1(
        tasks_dir=args.tasks_dir,
        task_names=task_names,
        run_id=run_id,
        tb_command=args.tb_command,
        n_attempts=args.n_attempts,
    )

    # Step 3: 检查结果
    log.info("── Step 3: 检查 TB1 Oracle 结果 ──")
    oracle_passed, summary = check_tb1_results(task_results)
    Path(run_dir, "oracle_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Step 4: 打包产物
    log.info("── Step 4: 打包产物 ──")
    artifacts_path = pack_artifacts(run_dir, args.record_id)

    # Step 5: 上传产物到 TOS（可选）
    tos_artifact_url = None
    if args.upload_tos_url and os.path.exists(artifacts_path):
        log.info("── Step 5: 上传产物到 TOS ──")
        artifact_filename = os.path.basename(artifacts_path)
        upload_url = args.upload_tos_url.rstrip("/") + "/" + artifact_filename
        try:
            tos_artifact_url = upload_to_tos(
                artifacts_path,
                upload_url,
                tos_endpoint=args.tos_endpoint,
                tos_region=args.tos_region,
            )
        except Exception as e:
            log.error("上传失败: %s", e)

    # 汇总
    log.info("=" * 50)
    log.info("TB1 Oracle 运行汇总:")
    log.info("  导入任务: %s", ", ".join(task_names))
    log.info("  总任务数: %s", summary.get("n_total_trials", "N/A"))
    log.info("  通过数:   %s", summary.get("n_passed", "N/A"))
    log.info("  失败数:   %s", summary.get("n_failed", "N/A"))
    log.info("  平均分:   %s", summary.get("mean_score", "N/A"))
    log.info("  Oracle:   %s", "通过" if oracle_passed else "未通过")
    log.info("  产物:     %s", artifacts_path)
    log.info("=" * 50)

    oracle_log_value = artifacts_path
    if tos_artifact_url and args.tos_endpoint:
        oracle_log_value = tos_url_to_http(tos_artifact_url, args.tos_endpoint)

    result_output = {
        args.oracle_pass_field: "通过" if oracle_passed else "未通过",
        args.oracle_log_field: oracle_log_value,
    }
    Path("return.json").write_text(
        json.dumps(result_output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("return.json 已写入: %s", result_output)


if __name__ == "__main__":
    main()
