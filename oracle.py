#!/usr/bin/env python3
"""
Terminal-Bench（TB1）Oracle 校验：仅支持 TB1 布局（task.yaml、Dockerfile、solution.sh …）。

流程：下载/解压 zip → 导入含 task.yaml 的任务目录 → tb run --agent oracle → 解析 results.json
→ 打包 artifacts、写 return.json。

环境变量:
  VE_TOS_AK, VE_TOS_SK — TOS 下载时需要
  TB_ORACLE_CMD — 可选；默认「当前 Python -m terminal_bench.cli.tb.main」

若 zip 只有 Harbor（task.toml），请先在本机用 harbor2tbench 转成 TB1 再打包上传。
"""

import argparse
import json
import logging
import os
import shlex
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


def _default_tb_module_invoker() -> list[str]:
    return [sys.executable, "-m", "terminal_bench.cli.tb.main"]


def tb_oracle_invoker() -> list[str]:
    raw = os.environ.get("TB_ORACLE_CMD")
    if raw:
        parts = shlex.split(raw, posix=os.name != "nt")
        return parts if parts else _default_tb_module_invoker()
    return _default_tb_module_invoker()


def _validate_tb_invoker(invoker: list[str]) -> None:
    if not invoker:
        raise ValueError("TB_ORACLE_CMD 解析为空")
    exe = invoker[0]
    if not Path(exe).is_file() and shutil.which(exe) is None:
        raise FileNotFoundError(f"找不到解释器 {exe!r}")
    if (
        len(invoker) >= 3
        and invoker[1] == "-m"
        and str(invoker[2]).startswith("terminal_bench")
    ):
        try:
            __import__("terminal_bench.cli.tb.main")
        except ImportError as e:
            raise FileNotFoundError(
                "未安装 terminal-bench，请: pip install terminal-bench"
            ) from e


def upload_to_tos(
    local_path: str,
    tos_url: str,
    tos_endpoint: str,
    tos_region: str,
) -> str:
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
    zips = list(Path(".").glob("*.zip"))
    if len(zips) == 0:
        raise RuntimeError("当前目录未找到 zip 文件")
    if len(zips) > 1:
        raise RuntimeError(f"当前目录存在多个 zip 文件: {[str(z) for z in zips]}")
    log.info(f"找到任务 zip: {zips[0]}")
    return str(zips[0])


def extract_and_import_tb1_tasks(zip_path: str, tasks_dir: str) -> list[str]:
    """
    仅导入 Terminal-Bench（TB1）任务：每个任务根目录须有 task.yaml。
    若存在 task.toml（Harbor），直接报错，不自动转换。
    """
    extract_dir = tempfile.mkdtemp(prefix="extracted_tasks_")
    try:
        log.info(f"解压 {zip_path} ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        extract_path = Path(extract_dir)
        prefixes = ["", "*/", "*/*/"]

        harbor_any = False
        for pre in prefixes:
            if any(extract_path.glob(pre + "task.toml")):
                harbor_any = True
                break
        if harbor_any:
            raise RuntimeError(
                "zip 内含有 Harbor 任务(task.toml)。本脚本仅校验 TB1(task.yaml)；"
                "请先用 harbor2tbench 转为 Terminal-Bench 后再打包。"
            )

        roots: set[Path] = set()
        for pre in prefixes:
            for f in extract_path.glob(pre + "task.yaml"):
                roots.add(f.parent.resolve())

        if not roots:
            raise RuntimeError("zip 中未找到 task.yaml（Terminal-Bench / TB1 任务）")

        tasks_path = Path(tasks_dir)
        imported: list[str] = []

        for p in sorted(roots, key=lambda x: str(x)):
            name = p.name
            dest = tasks_path / name
            if dest.exists():
                log.warning(f"任务 '{name}' 已存在，将被覆盖")
                shutil.rmtree(dest)
            shutil.copytree(p, dest)
            log.info(f"已导入 (TB1): {name}")
            imported.append(name)

        log.info("共导入 %s 个任务: %s", len(imported), ", ".join(imported))
        return imported

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def run_tb_oracle(
    tasks_dir: str, task_ids: list[str], run_id: str, k_shots: int
) -> tuple[str, bool]:
    run_dir = f"runs/tb3-oracle-{run_id}"
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    dataset_path = str(Path(tasks_dir).resolve())
    output_path = str(Path(run_dir).resolve())
    invoker = tb_oracle_invoker()
    _validate_tb_invoker(invoker)

    all_ok = True
    for tid in task_ids:
        cmd = invoker + [
            "run",
            "--dataset-path",
            dataset_path,
            "--agent",
            "oracle",
            "--n-attempts",
            str(k_shots),
            "--output-path",
            output_path,
            "--task-id",
            tid,
        ]
        log.info("tb oracle: %s", " ".join(cmd))
        r = subprocess.run(cmd, text=True)
        if r.returncode != 0:
            log.error("tb run 失败 task_id=%s exit=%s", tid, r.returncode)
            all_ok = False
    return run_dir, all_ok


def check_tb_oracle_results(run_dir: str) -> tuple[bool, dict]:
    files = sorted(
        Path(run_dir).rglob("results.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return False, {"error": "未找到 results.json"}

    passed_all = True
    per_file: list[dict] = []
    n_res, n_unres = 0, 0

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            passed_all = False
            per_file.append({"file": str(f), "ok": False, "error": str(e)})
            continue

        nu = int(data.get("n_unresolved", 1))
        nr = int(data.get("n_resolved", 0))
        acc = float(data.get("accuracy", 0.0))
        n_res += nr
        n_unres += nu
        ok = nu == 0 and acc >= 1.0 - 1e-9
        if not ok:
            passed_all = False
        per_file.append(
            {
                "file": str(f),
                "ok": ok,
                "n_resolved": nr,
                "n_unresolved": nu,
                "accuracy": acc,
            }
        )

    summary = {
        "layout": "tb1",
        "n_resolved_total": n_res,
        "n_unresolved_total": n_unres,
        "n_errors": n_unres,
        "mean_score": 1.0 if passed_all else 0.0,
        "n_total_trials": n_res + n_unres,
        "result_files": [str(x) for x in files],
        "per_file": per_file,
    }
    return passed_all, summary


def pack_artifacts(run_dir: str, record_id: str) -> str:
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


def main():
    parser = argparse.ArgumentParser(
        description="Oracle：仅 Terminal-Bench（TB1，task.yaml）",
    )
    parser.add_argument("--record-id", required=True)
    parser.add_argument("--tasks-dir", default="tasks2")
    parser.add_argument(
        "--k-shots",
        "-k",
        type=int,
        default=1,
        help="tb run --n-attempts（默认 1）",
    )
    parser.add_argument("--zip-url")
    parser.add_argument("--tos-endpoint")
    parser.add_argument("--tos-region")
    parser.add_argument("--oracle-pass-field", default="oracle_pass_bool")
    parser.add_argument("--oracle-log-field", default="oracle_log_url")
    parser.add_argument("--upload-tos-url")
    args = parser.parse_args()

    run_id = f"{args.record_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    log.info("=== Oracle TB1 (run_id=%s) ===", run_id)

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

    log.info("── Step 1: 解压并导入（仅 task.yaml）──")
    try:
        if zip_path is None:
            zip_path = find_task_zip()
        task_ids = extract_and_import_tb1_tasks(zip_path, args.tasks_dir)
    except Exception as e:
        log.error("导入失败: %s", e)
        sys.exit(1)

    log.info("── Step 2: tb run --agent oracle ──")
    run_dir, ok = run_tb_oracle(
        args.tasks_dir, task_ids, run_id, args.k_shots
    )
    if not ok:
        log.error("tb run 存在失败")

    log.info("── Step 3: 解析 results.json ──")
    passed, summary = check_tb_oracle_results(run_dir)

    log.info("── Step 4: 打包 ──")
    artifacts_path = pack_artifacts(run_dir, args.record_id)

    tos_artifact_url = None
    if args.upload_tos_url and os.path.exists(artifacts_path):
        log.info("── Step 5: 上传 TOS ──")
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

    log.info("=" * 50)
    log.info("汇总: %s", summary)
    log.info("  任务: %s", ", ".join(task_ids))
    log.info("  Oracle: %s", "通过" if passed else "未通过")
    log.info("  产物: %s", artifacts_path)
    log.info("=" * 50)

    oracle_log_value = artifacts_path
    if tos_artifact_url and args.tos_endpoint:
        oracle_log_value = tos_url_to_http(tos_artifact_url, args.tos_endpoint)

    out = {
        args.oracle_pass_field: "通过" if passed else "未通过",
        args.oracle_log_field: oracle_log_value,
    }
    Path("return.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("return.json: %s", out)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
