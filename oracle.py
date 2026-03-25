#!/usr/bin/env python3
"""
Import Tasks and Run Oracle - 本地/容器版

功能:
1. 从 TOS/HTTP URL 下载 zip (或使用当前目录已有 zip)
2. 解压并导入任务到 tasks-dir
3. 运行 Oracle：
   - --runner tb（默认）: 对 Harbor 任务先 harbor2tbench 转为 TB 目录 (*-tbench)，再 tb run --agent oracle
   - --runner harbor: 沿用 harbor run -e daytona -a oracle（旧逻辑）
4. 检查 oracle 结果（TB: results.json；Harbor: result.json）
5. 打包产物到 ./artifacts/
6. 输出 return.json (oracle_pass_bool, oracle_log_url)

环境变量:
  VE_TOS_AK, VE_TOS_SK (TOS 下载时需要)
  TB_ORACLE_CMD: 调用 Terminal-Bench CLI 的前缀（默认 tb）。容器里若未安装 tb，请 pip install
    terminal-bench，或设为 uv run tb（需已安装 uv 且项目能解析 tb）。

用法:
  python oracle.py --record-id "recXXX" --zip-url "https://..." ...
  python oracle.py --record-id "recXXX" --runner harbor ...   # 仅 Harbor
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def default_harbor2tbench_script() -> Path:
    return _repo_root() / "harbor2tbbench" / "harbor2tbench.py"


def tb_oracle_invoker() -> list[str]:
    """tb 可执行前缀，如 ['tb'] 或 ['uv','run','tb']。"""
    raw = os.environ.get("TB_ORACLE_CMD", "tb")
    parts = shlex.split(raw, posix=os.name != "nt")
    return parts if parts else ["tb"]


def _validate_tb_invoker(invoker: list[str]) -> None:
    """确保 invoker 第一个 token 在 PATH 中可执行（避免 subprocess FileNotFoundError: tb）。"""
    if not invoker:
        raise ValueError("TB_ORACLE_CMD 解析为空")
    exe = invoker[0]
    if shutil.which(exe) is None:
        raise FileNotFoundError(
            f"找不到命令 {exe!r}（不在 PATH 中）。--runner tb 需要 Terminal-Bench CLI。\n"
            "  • 安装: pip install terminal-bench（会提供 tb 命令）\n"
            "  • 或使用 uv: export TB_ORACLE_CMD='uv run tb'\n"
            f"  • 当前 TB_ORACLE_CMD={os.environ.get('TB_ORACLE_CMD', '(未设置，默认 tb)')!r}"
        )


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


def extract_and_import_tasks(zip_path: str, tasks_dir: str) -> list[tuple[str, str]]:
    """
    解压 zip 并将任务复制到 tasks_dir。

    Returns:
        [(任务目录名, 类型), ...]，类型为 \"harbor\"（含 task.toml）或 \"tb\"（仅 task.yaml，已为 TB 布局）
    """
    extract_dir = tempfile.mkdtemp(prefix="extracted_tasks_")
    try:
        log.info(f"解压 {zip_path} ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        extract_path = Path(extract_dir)
        depth_patterns = ["", "*/", "*/*/"]

        harbor_roots: set[Path] = set()
        for prefix in depth_patterns:
            for f in extract_path.glob(prefix + "task.toml"):
                harbor_roots.add(f.parent.resolve())

        tb_only_roots: set[Path] = set()
        for prefix in depth_patterns:
            for f in extract_path.glob(prefix + "task.yaml"):
                parent = f.parent.resolve()
                if parent in harbor_roots:
                    continue
                if (parent / "task.toml").exists():
                    continue
                tb_only_roots.add(parent)

        if not harbor_roots and not tb_only_roots:
            raise RuntimeError("zip 中未找到 task.toml 或 task.yaml")

        tasks_path = Path(tasks_dir)
        imported: list[tuple[str, str]] = []

        def copy_one(task_src_dir: Path, kind: str) -> None:
            task_name = task_src_dir.name
            dest_dir = tasks_path / task_name
            if dest_dir.exists():
                log.warning(f"任务 '{task_name}' 已存在，将被覆盖")
                shutil.rmtree(dest_dir)
            shutil.copytree(task_src_dir, dest_dir)
            log.info(f"已导入 ({kind}): {task_name}")
            imported.append((task_name, kind))

        for p in sorted(harbor_roots, key=lambda x: str(x)):
            copy_one(p, "harbor")

        for p in sorted(tb_only_roots, key=lambda x: str(x)):
            if p in harbor_roots:
                continue
            copy_one(p, "tb")

        if not imported:
            raise RuntimeError("没有成功导入任何任务")

        log.info(
            f"共导入 {len(imported)} 个任务: "
            + ", ".join(f"{n}({k})" for n, k in imported)
        )
        return imported

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def run_harbor2tbench(
    harbor_task_dir: Path,
    tb_out_dir: Path,
    script_path: Path,
) -> None:
    """调用 harbor2tbench.py：Harbor 目录 -> TB 目录（与 CLI 默认一致：dst 为独立目录）。"""
    if not script_path.is_file():
        raise FileNotFoundError(f"未找到 harbor2tbench: {script_path}")

    if tb_out_dir.exists():
        shutil.rmtree(tb_out_dir)

    cmd = [sys.executable, str(script_path), str(harbor_task_dir), str(tb_out_dir)]
    log.info(f"harbor2tbench: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


# ──────────────────────────── Harbor Oracle ────────────────────────────


def run_harbor_oracle(
    tasks_dir: str,
    task_names: list[str],
    run_id: str,
    parallel: int = 4,
    k_shots: int = 1,
) -> tuple[str, bool]:
    """
    运行 harbor oracle agent（Harbor + daytona）。

    Returns:
        (run_dir, subprocess_success)
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


def run_tb_oracle(
    tasks_dir: str,
    tb_task_ids: list[str],
    run_id: str,
    k_shots: int = 1,
) -> tuple[str, bool]:
    """
    Terminal-Bench: tb run --agent oracle，数据集目录为 tasks_dir（内含 *-tbench 等任务子目录）。

    Returns:
        (run_dir, subprocess_success 全部为 0)
    """
    run_dir = f"runs/tb3-oracle-{run_id}"
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    dataset_path = str(Path(tasks_dir).resolve())
    output_path = str(Path(run_dir).resolve())
    invoker = tb_oracle_invoker()
    _validate_tb_invoker(invoker)

    all_ok = True
    for task_id in tb_task_ids:
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
            task_id,
        ]
        log.info(f"运行 tb oracle: {' '.join(cmd)}")
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            log.error(
                f"tb run 失败 task_id={task_id} (exit code: {result.returncode})"
            )
            all_ok = False

    return run_dir, all_ok


def check_harbor_oracle_results(run_dir: str) -> tuple[bool, dict]:
    """
    检查 Harbor oracle 运行结果（result.json，含 n_total_trials）。

    Returns:
        (oracle_passed, summary_dict)
    """
    result_files = list(Path(run_dir).rglob("result.json"))

    job_result_file = None
    for f in result_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if "n_total_trials" in data:
                job_result_file = f
                break
        except (json.JSONDecodeError, OSError):
            continue

    if job_result_file is None:
        log.error("未找到 job-level result.json")
        return False, {"error": "未找到 result.json"}

    log.info(f"结果文件: {job_result_file}")
    data = json.loads(job_result_file.read_text(encoding="utf-8"))

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
        "runner": "harbor",
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


def check_tb_oracle_results(run_dir: str) -> tuple[bool, dict]:
    """
    检查 Terminal-Bench oracle 结果：run_dir 下各时间戳目录中的 results.json。
    通过条件：每个 results.json 均 n_unresolved==0 且 accuracy==1.0。
    """
    result_files = sorted(
        Path(run_dir).rglob("results.json"),
        key=lambda p: p.stat().st_mtime,
    )

    if not result_files:
        log.error("未找到 results.json（Terminal-Bench 输出）")
        return False, {"error": "未找到 results.json", "runner": "tb"}

    passed_all = True
    per_file: list[dict] = []
    n_resolved_total = 0
    n_unresolved_total = 0

    for f in result_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"无法解析 {f}: {e}")
            passed_all = False
            per_file.append({"file": str(f), "ok": False, "error": str(e)})
            continue

        n_unresolved = int(data.get("n_unresolved", 1))
        n_resolved = int(data.get("n_resolved", 0))
        accuracy = float(data.get("accuracy", 0.0))

        n_resolved_total += n_resolved
        n_unresolved_total += n_unresolved

        ok = n_unresolved == 0 and accuracy >= 1.0 - 1e-9
        if not ok:
            passed_all = False
            for trial in data.get("results", []):
                if trial.get("is_resolved") is not True:
                    log.error(
                        f"  未解决 trial: task_id={trial.get('task_id')}, "
                        f"failure_mode={trial.get('failure_mode')}"
                    )

        per_file.append(
            {
                "file": str(f),
                "ok": ok,
                "n_resolved": n_resolved,
                "n_unresolved": n_unresolved,
                "accuracy": accuracy,
            }
        )
        log.info(
            f"TB 结果 {f.name}: resolved={n_resolved}, unresolved={n_unresolved}, "
            f"accuracy={accuracy}"
        )

    summary = {
        "runner": "tb",
        "n_resolved_total": n_resolved_total,
        "n_unresolved_total": n_unresolved_total,
        "result_files": [str(f) for f in result_files],
        "per_file": per_file,
        "mean_score": 1.0 if passed_all else 0.0,
        "n_total_trials": n_resolved_total + n_unresolved_total,
        "n_errors": n_unresolved_total,
    }

    if passed_all:
        log.info("Terminal-Bench Oracle 通过: 全部 results.json 满足 accuracy=1 且 n_unresolved=0")
    else:
        log.error("Terminal-Bench Oracle 未通过: 存在失败的 results.json 或未解决 trial")

    return passed_all, summary


def build_tb_task_ids(
    imported: list[tuple[str, str]],
    tasks_dir: Path,
    no_convert: bool,
    harbor2tbench_script: Path,
) -> list[str]:
    """
    Harbor 任务 -> harbor2tbench -> 目录名 {name}-tbench；
    已为 TB 的任务（仅 task.yaml）-> task_id 为导入目录名。
    """
    tb_ids: list[str] = []
    for name, kind in imported:
        src = tasks_dir / name
        if kind == "harbor":
            if no_convert:
                raise RuntimeError(
                    f"任务 {name} 为 Harbor(task.toml)，不能使用 --no-convert。"
                    "请去掉 --no-convert 以自动执行 harbor2tbench，或改为上传已转换的 TB 任务 zip（仅 task.yaml）。"
                )
            dst = tasks_dir / f"{name}-tbench"
            run_harbor2tbench(src, dst, harbor2tbench_script)
            tb_ids.append(dst.name)
        else:
            tb_ids.append(name)
    return tb_ids


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
        description="Import Tasks and Run Oracle（支持 Harbor 与 Terminal-Bench）",
    )
    parser.add_argument(
        "--record-id", required=True, help="记录 ID，用于 run_id 和产物文件名"
    )
    parser.add_argument("--tasks-dir", default="tasks2", help="任务目录 (默认 tasks2)")
    parser.add_argument(
        "--runner",
        choices=("tb", "harbor"),
        default="tb",
        help="tb: harbor2tbench 转换后执行 tb run --agent oracle（默认）；"
        "harbor: 仍用 harbor run -e daytona",
    )
    parser.add_argument(
        "--no-convert",
        action="store_true",
        help="仅 --runner tb：zip 内已是 TB 任务（task.yaml），跳过 harbor2tbench",
    )
    parser.add_argument(
        "--harbor2tbench-script",
        type=Path,
        default=None,
        help="harbor2tbench.py 路径（默认同仓库 harbor2tbbench/harbor2tbench.py）",
    )
    parser.add_argument(
        "--parallel", "-n", type=int, default=4, help="harbor 并行数 (默认 4)"
    )
    parser.add_argument(
        "--k-shots",
        "-k",
        type=int,
        default=1,
        help="harbor 的 k-shots；tb 模式下对应 --n-attempts (默认 1)",
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

    h2t_script = args.harbor2tbench_script or default_harbor2tbench_script()

    run_id = f"{args.record_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    log.info(f"=== 开始 Oracle 运行 (run_id={run_id}, runner={args.runner}) ===")

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
        imported = extract_and_import_tasks(zip_path, args.tasks_dir)
    except Exception as e:
        log.error(f"任务导入失败: {e}")
        sys.exit(1)

    tasks_path = Path(args.tasks_dir)

    # ── Step 2: 运行 oracle ──
    if args.runner == "harbor":
        log.info("── Step 2: 运行 harbor oracle ──")
        task_names_only = [n for n, _ in imported]
        run_dir, run_ok = run_harbor_oracle(
            tasks_dir=args.tasks_dir,
            task_names=task_names_only,
            run_id=run_id,
            parallel=args.parallel,
            k_shots=args.k_shots,
        )
        if not run_ok:
            log.error("harbor 进程返回非 0")
    else:
        log.info("── Step 2a: Harbor -> Terminal-Bench（harbor2tbench）──")
        try:
            tb_task_ids = build_tb_task_ids(
                imported,
                tasks_path,
                no_convert=args.no_convert,
                harbor2tbench_script=h2t_script,
            )
        except Exception as e:
            log.error(f"转换/解析 TB task_id 失败: {e}")
            sys.exit(1)

        log.info(f"TB task_id 列表: {tb_task_ids}")
        log.info("── Step 2b: 运行 tb oracle ──")
        run_dir, run_ok = run_tb_oracle(
            tasks_dir=args.tasks_dir,
            tb_task_ids=tb_task_ids,
            run_id=run_id,
            k_shots=args.k_shots,
        )
        if not run_ok:
            log.error("tb run 存在失败（非 0 退出码）")

    # ── Step 3: 检查结果 ──
    log.info("── Step 3: 检查 oracle 结果 ──")
    if args.runner == "harbor":
        oracle_passed, summary = check_harbor_oracle_results(run_dir)
    else:
        oracle_passed, summary = check_tb_oracle_results(run_dir)

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
    log.info(f"  runner:   {args.runner}")
    log.info(
        f"  导入任务: {', '.join(f'{n}({k})' for n, k in imported)}"
    )
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
        json.dumps(result_output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"return.json 已写入: {result_output}")

    sys.exit(0 if oracle_passed else 1)


if __name__ == "__main__":
    main()