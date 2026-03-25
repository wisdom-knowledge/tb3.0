#!/usr/bin/env python3
"""
Import Tasks and Run Oracle - 本地/容器版

功能:
1. 从 TOS/HTTP URL 下载 zip (或使用当前目录已有 zip)
2. 解压并导入任务到 tasks-dir
3. 运行 harbor oracle agent
4. 检查 oracle 结果
5. 打包产物到 ./artifacts/
6. 输出 return.json (oracle_pass_bool, oracle_log_url)

环境变量: VE_TOS_AK, VE_TOS_SK (TOS 下载时需要)
后置步骤 (由流水线编排): TOS 上传 ./artifacts/ 下的产物

用法:
  python seta/oracle.py \
    --record-id "recXXX" \
    --zip-url "tos://bucket/path/tasks.zip" \
    --tos-endpoint "https://tos-cn-beijing.volces.com" \
    --tos-region "cn-beijing" \
    [--tasks-dir tasks2] \
    [--parallel 4] \
    [--k-shots 1]
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import requests
import tos

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_oracle")

ARTIFACTS_DIR = "./artifacts"


def upload_to_tos(
    local_path: str,
    tos_url: str,
    tos_endpoint: str,
    tos_region: str,
) -> str:
    """
    上传本地文件到 TOS

    TOS 凭证从环境变量 VE_TOS_AK / VE_TOS_SK 读取。

    Returns:
        TOS URL
    """
    from urllib.parse import urlparse

    parsed = urlparse(tos_url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")

    log.info(f"上传到 TOS: {local_path} -> {tos_url}")
    client = tos.TosClientV2(
        ak=os.environ["VE_TOS_AK"],
        sk=os.environ["VE_TOS_SK"],
        endpoint=tos_endpoint,
        region=tos_region,
    )
    client.put_object_from_file(bucket, key, local_path)
    log.info(f"上传完成: {tos_url}")
    return tos_url


def tos_url_to_http(tos_url: str, tos_endpoint: str) -> str:
    """将 tos://bucket/key 转为 https://bucket.endpoint/key"""
    from urllib.parse import urlparse

    parsed = urlparse(tos_url)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    host = tos_endpoint.replace("https://", "").replace("http://", "")
    return f"https://{bucket}.{host}/{key}"


def download_from_url(
    url: str,
    tos_endpoint: str | None = None,
    tos_region: str | None = None,
) -> str:
    """
    从 TOS URL 或 HTTP URL 下载 zip 到当前目录

    TOS 凭证从环境变量 VE_TOS_AK / VE_TOS_SK 读取，
    endpoint 和 region 通过参数传入。

    Returns:
        本地 zip 文件路径
    """
    if url.startswith("tos://"):
        from urllib.parse import urlparse

        if not tos_endpoint or not tos_region:
            raise ValueError("TOS URL 需要 --tos-endpoint 和 --tos-region 参数")

        parsed = urlparse(url)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        filename = os.path.basename(key)

        log.info(f"从 TOS 下载: {url}")
        client = tos.TosClientV2(
            ak=os.environ["VE_TOS_AK"],
            sk=os.environ["VE_TOS_SK"],
            endpoint=tos_endpoint,
            region=tos_region,
        )
        client.get_object_to_file(bucket, key, filename)
    else:
        filename = os.path.basename(url.split("?")[0])
        log.info(f"从 HTTP 下载: {url}")
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()
        with open(filename, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    log.info(f"下载完成: {filename}")
    return filename


def find_task_zip() -> str:
    """在当前目录找到唯一的 zip 文件"""
    zips = list(Path(".").glob("*.zip"))
    if len(zips) == 0:
        raise RuntimeError("当前目录未找到 zip 文件")
    if len(zips) > 1:
        raise RuntimeError(f"当前目录存在多个 zip 文件: {[str(z) for z in zips]}")
    log.info(f"找到任务 zip: {zips[0]}")
    return str(zips[0])


def extract_and_import_tasks(zip_path: str, tasks_dir: str) -> list[str]:
    """
    解压 zip 并将任务复制到 tasks_dir

    Returns:
        导入的任务名称列表
    """
    extract_dir = tempfile.mkdtemp(prefix="extracted_tasks_")
    try:
        log.info(f"解压 {zip_path} ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        task_tomls: list[Path] = []
        extract_path = Path(extract_dir)
        for depth_pattern in ["task.toml", "*/task.toml", "*/*/task.toml"]:
            task_tomls.extend(extract_path.glob(depth_pattern))

        if not task_tomls:
            raise RuntimeError("zip 文件中未找到 task.toml")

        log.info(f"找到 {len(task_tomls)} 个 task.toml")

        imported: list[str] = []
        tasks_path = Path(tasks_dir)

        for toml_file in task_tomls:
            task_src_dir = toml_file.parent
            task_name = task_src_dir.name

            dest_dir = tasks_path / task_name
            if dest_dir.exists():
                log.warning(f"任务 '{task_name}' 已存在，将被覆盖")
                shutil.rmtree(dest_dir)

            shutil.copytree(task_src_dir, dest_dir)
            log.info(f"已导入: {task_name}")
            imported.append(task_name)

        if not imported:
            raise RuntimeError("没有成功导入任何任务")

        log.info(f"共导入 {len(imported)} 个任务: {', '.join(imported)}")
        return imported

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


# ──────────────────────────── Harbor Oracle ────────────────────────────


def run_oracle(
    tasks_dir: str,
    task_names: list[str],
    run_id: str,
    parallel: int = 4,
    k_shots: int = 1,
) -> tuple[str, bool]:
    """
    运行 harbor oracle agent

    Returns:
        (run_dir, success)
    """
    run_dir = f"runs/tb3-oracle-{run_id}"

    cmd = [
        "harbor",
        "run",
        "-e",
        "daytona",
        "-p",
        tasks_dir,
        "-a",
        "oracle",
        "-n",
        str(parallel),
        "-k",
        str(k_shots),
        "--force-build",
        "-o",
        run_dir,
    ]
    for name in task_names:
        cmd.extend(["-t", name])

    log.info(f"运行 harbor oracle: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    success = result.returncode == 0

    if not success:
        log.error(f"harbor oracle 运行失败 (exit code: {result.returncode})")

    return run_dir, success


def check_oracle_results(run_dir: str) -> tuple[bool, dict]:
    """
    检查 oracle 运行结果

    Returns:
        (oracle_passed, summary_dict)
    """
    result_files = list(Path(run_dir).rglob("result.json"))

    job_result_file = None
    for f in result_files:
        try:
            data = json.loads(f.read_text())
            if "n_total_trials" in data:
                job_result_file = f
                break
        except (json.JSONDecodeError, OSError):
            continue

    if job_result_file is None:
        log.error("未找到 job-level result.json")
        return False, {"error": "未找到 result.json"}

    log.info(f"结果文件: {job_result_file}")
    data = json.loads(job_result_file.read_text())

    n_total_trials = data.get("n_total_trials", 0)
    n_errors = data.get("stats", {}).get("n_errors", 0)

    evals = data.get("stats", {}).get("evals", {})
    means = []
    for eval_entry in evals.values():
        metrics = eval_entry.get("metrics", [])
        if metrics:
            means.append(metrics[0].get("mean", 0))
    mean_score = sum(means) / len(means) if means else 0

    summary = {
        "n_total_trials": n_total_trials,
        "n_errors": n_errors,
        "mean_score": mean_score,
        "result_file": str(job_result_file),
    }

    log.info(f"结果: trials={n_total_trials}, errors={n_errors}, mean={mean_score}")

    passed = n_errors == 0 and mean_score == 1.0
    if passed:
        log.info("Oracle 通过: n_errors=0, mean=1.0")
    else:
        reasons = []
        if n_errors > 0:
            reasons.append(f"n_errors={n_errors}")
        if mean_score != 1.0:
            reasons.append(f"mean={mean_score} (期望 1.0)")
        log.error(f"Oracle 未通过: {', '.join(reasons)}")

    return passed, summary


def pack_artifacts(run_dir: str, record_id: str) -> str:
    """将运行产物打包为 zip，返回 zip 路径"""
    artifacts = Path(ARTIFACTS_DIR)
    artifacts.mkdir(parents=True, exist_ok=True)

    zip_stem = f"{record_id}-oracle"
    zip_path = str(artifacts / f"{zip_stem}.zip")

    if os.path.exists(zip_path):
        os.remove(zip_path)

    if os.path.isdir(run_dir):
        shutil.make_archive(str(artifacts / zip_stem), "zip", root_dir=run_dir)
        log.info(f"产物已打包: {zip_path}")
    else:
        log.warning(f"运行目录 {run_dir} 不存在，跳过打包")

    return zip_path


# ──────────────────────────── 主流程 ────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Import Tasks and Run Oracle",
    )
    parser.add_argument(
        "--record-id", required=True, help="记录 ID，用于 run_id 和产物文件名"
    )
    parser.add_argument("--tasks-dir", default="tasks2", help="任务目录 (默认 tasks2)")
    parser.add_argument(
        "--parallel", "-n", type=int, default=4, help="harbor 并行数 (默认 4)"
    )
    parser.add_argument(
        "--k-shots", "-k", type=int, default=1, help="harbor k-shots (默认 1)"
    )
    parser.add_argument(
        "--zip-url",
        help="TOS 或 HTTP URL，下载 zip 任务包 (如 tos://bucket/path/tasks.zip)",
    )
    parser.add_argument(
        "--tos-endpoint",
        help="TOS endpoint (如 https://tos-cn-beijing.volces.com)",
    )
    parser.add_argument(
        "--tos-region",
        help="TOS region (如 cn-beijing)",
    )
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
    log.info(f"=== 开始 Oracle 运行 (run_id={run_id}) ===")

    # ── Step 0: 下载任务包 (如果提供了 URL) ──
    if args.zip_url:
        log.info("── Step 0: 下载任务包 ──")
        try:
            zip_path = download_from_url(
                args.zip_url,
                tos_endpoint=args.tos_endpoint,
                tos_region=args.tos_region,
            )
        except Exception as e:
            log.error(f"下载失败: {e}")
            sys.exit(1)
    else:
        zip_path = None

    # ── Step 1: 解压并导入任务 ──
    log.info("── Step 1: 解压并导入任务 ──")
    try:
        if zip_path is None:
            zip_path = find_task_zip()
        task_names = extract_and_import_tasks(zip_path, args.tasks_dir)
    except Exception as e:
        log.error(f"任务导入失败: {e}")
        sys.exit(1)

    # ── Step 2: 运行 oracle ──
    log.info("── Step 2: 运行 harbor oracle ──")
    run_dir, harbor_success = run_oracle(
        tasks_dir=args.tasks_dir,
        task_names=task_names,
        run_id=run_id,
        parallel=args.parallel,
        k_shots=args.k_shots,
    )

    # ── Step 3: 检查结果 ──
    log.info("── Step 3: 检查 oracle 结果 ──")
    oracle_passed, summary = check_oracle_results(run_dir)

    # ── Step 4: 打包产物 ──
    log.info("── Step 4: 打包产物 ──")
    artifacts_path = pack_artifacts(run_dir, args.record_id)

    # ── Step 5: 上传产物到 TOS ──
    tos_artifact_url = None
    if args.upload_tos_url and os.path.exists(artifacts_path):
        log.info("── Step 5: 上传产物到 TOS ──")
        # 拼接目标 URL: base_url + 文件名
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
            log.error(f"上传失败: {e}")
    # ── 输出汇总 ──
    log.info("=" * 50)
    log.info("Oracle 运行汇总:")
    log.info(f"  导入任务: {', '.join(task_names)}")
    log.info(f"  总试验数: {summary.get('n_total_trials', 'N/A')}")
    log.info(f"  错误数:   {summary.get('n_errors', 'N/A')}")
    log.info(f"  平均分:   {summary.get('mean_score', 'N/A')}")
    log.info(f"  Oracle:   {'通过' if oracle_passed else '未通过'}")
    log.info(f"  产物:     {artifacts_path}")
    log.info("=" * 50)

    oracle_log_value = artifacts_path
    if tos_artifact_url and args.tos_endpoint:
        oracle_log_value = tos_url_to_http(tos_artifact_url, args.tos_endpoint)

    result_output = {
        args.oracle_pass_field: "通过" if oracle_passed else "未通过",
        args.oracle_log_field: oracle_log_value,
    }
    Path("return.json").write_text(
        json.dumps(result_output, ensure_ascii=False, indent=2)
    )
    log.info(f"return.json 已写入: {result_output}")

    sys.exit(0 if oracle_passed else 1)


if __name__ == "__main__":
    main()