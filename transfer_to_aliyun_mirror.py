#!/usr/bin/env python3
"""
transfer_to_aliyun_mirror.py — 解压 ZIP，在 Daytona 沙箱内用 Claude Code 仅对白名单文件做「非阿里云资源 → 阿里云」的字符串级替换，再打包为 ZIP。

不递归扫描；只处理下列路径（相对任务根目录，存在才处理）：
  根: run-tests.sh, solution.sh, task.yaml, task.yml, Dockerfile, docker-compose.yaml, docker-compose.yml
  tests 目录下仅一层: tests/*.sh（不含子目录中的 .sh）

用法:
    python3 transfer_to_aliyun_mirror.py --input-zip task.zip --output-zip task_aliyun.zip
    python3 transfer_to_aliyun_mirror.py -i task.zip -o out.zip --change-log
    python3 transfer_to_aliyun_mirror.py -i task.zip -o out.zip --dry-run
    python3 transfer_to_aliyun_mirror.py -i a.zip -o b.zip --snapshot-name 你在Daytona里的快照名
    # 输入与输出可为同一 ZIP 路径（先打临时包再原子替换，文件名保持一致）
    python3 transfer_to_aliyun_mirror.py -i task.zip -o task.zip --change-log

环境变量（与 run_daytona 对齐）:
    DAYTONA_API_KEY, SNAPSHOT_NAME, SANDBOX_NAME, SANDBOX_NAME_PREFIX
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL
    ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_SONNET_MODEL
    CLAUDE_TIMEOUT, MIRROR_MAX_FILE_BYTES
    MIRROR_CHANGE_LOG_MAX_DIFF_LINES — 单文件 unified diff 最多行数（默认 800）
    MIRROR_CHANGE_LOG_FULL_BODY_BYTES — 单文件「替换前/后全文」附在日志中的上限（总字节，默认 65536）；超出则仅 diff
    MIRROR_CLAUDE_PERMISSION_MODE — 传给 claude -p 的 --permission-mode（默认 acceptEdits）；设为空则不加
"""

from __future__ import annotations

import argparse
import difflib
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from daytona import (
    Daytona,
    DaytonaConfig,
    CreateSandboxFromSnapshotParams,
    DaytonaError,
    DaytonaNotFoundError,
    Resources,
    SandboxState,
    SessionExecuteRequest,
)

logger = logging.getLogger(__name__)

# ============================================================
# 配置（环境变量覆盖）
# ============================================================
DAYTONA_API_KEY = os.environ.get("DAYTONA_API_KEY", "")
SNAPSHOT_NAME = os.environ.get("SNAPSHOT_NAME", "claude-code-snapshot")
SANDBOX_NAME_PREFIX = os.environ.get("SANDBOX_NAME_PREFIX", "aliyun_mirror")

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLAUDE_MODEL = os.environ.get(
    "ANTHROPIC_MODEL",
    os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "anthropic/claude-sonnet-4.6"),
)


REMOTE_WORK = "/tmp/aliyun_mirror_transfer"
POLL_INTERVAL = 5
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
MIRROR_MAX_FILE_BYTES = int(os.environ.get("MIRROR_MAX_FILE_BYTES", "524288"))
MIRROR_CHANGE_LOG_MAX_DIFF_LINES = int(os.environ.get("MIRROR_CHANGE_LOG_MAX_DIFF_LINES", "800"))
MIRROR_CHANGE_LOG_FULL_BODY_BYTES = int(os.environ.get("MIRROR_CHANGE_LOG_FULL_BODY_BYTES", "65536"))
MIRROR_CLAUDE_PERMISSION_MODE = os.environ.get("MIRROR_CLAUDE_PERMISSION_MODE", "acceptEdits").strip()

# 相对「任务根目录」的固定清单（不递归）
_ROOT_FILE_NAMES = (
    "run-tests.sh",
    "solution.sh",
    "task.yaml",
    "task.yml",
    "Dockerfile",
    "docker-compose.yaml",
    "docker-compose.yml",
)

DEFAULT_SYSTEM_PROMPT = """【角色与任务】
你是一个严格的代码与配置文件定点修改助手。请对提供的单个项目文件进行静态分析和最小化修改。你的目标不是重写文件，而是在尽可能保持原文件内容逐字不变的前提下，只将其中的非阿里云资源引用替换为对应的阿里云资源引用。

【处理范围】
当前输入只会是以下文件之一：
（1）根目录下的 run-tests.sh
（2）根目录下的 solution.sh
（3）根目录下的 task.yaml
（4）根目录下的 task.yml
（5）根目录下的 Dockerfile
（6）根目录下的 docker-compose.yaml
（7）根目录下的 docker-compose.yml
（8）tests 目录下的某个 .sh 文件，即 tests/*.sh

【修改规则】
一、只做最小必要修改
只允许修改命中的资源引用字符串本身。
除命中的资源引用外，其他所有内容必须保持不变，包括注释、空格、换行、缩进、字段顺序、命令顺序、逻辑结构和原有文本内容。

二、需要识别并替换的“非阿里云资源引用”
需要重点检查以下内容：
（1）容器镜像地址
（2）apt 软件源地址
（3）pip 安装源地址
（4）uv、npm 等依赖下载源
（5）wget 或 curl 下载的外部资源地址
（6）配置文件或脚本中写死的外部仓库、镜像、源、下载链接

三、阿里云替换要求
除 Dockerfile 中的 FROM 指令外，其余所有涉及网络下载、依赖安装、镜像拉取、仓库源配置的内容，如果当前不是阿里云资源，且可以明确替换为阿里云资源，则应替换为对应的阿里云资源。
如果某项资源已经是阿里云地址，则保持不变。
如果某项非阿里云资源无法确定其明确的阿里云等价地址，则保持原样，不要猜测，不要臆造。

四、特殊处理要求
（1）如果文件中包含 apt-get update、apt-get install、apt install 等命令，但在此之前没有明确切换到阿里云 apt 源，则应补全或改写为阿里云 apt 源配置。
（2）如果文件中包含 pip install、python -m pip install、uv pip install 等命令，但没有明确使用阿里云 PyPI 源，则应改为阿里云 PyPI 源，或补充等价的阿里云源配置。
（3）如果文件中包含 curl 或 wget 下载外部资源，且目标地址不是阿里云域名，则应优先替换为明确存在的阿里云资源地址；如果无法确定，则保持原样。
（4）如果文件中包含 docker pull、docker-compose 的 image、变量中的镜像地址、task.yaml 中的镜像字段等，只要不是阿里云镜像地址且可以明确替换，就替换为对应的阿里云镜像地址。
（5）Dockerfile 中的 FROM 指令允许保留原基础镜像，不强制替换。

【禁止事项】
不要改注释。
不要改空格。
不要改换行。
不要改缩进。
不要改字段顺序。
不要改命令顺序。
不要改逻辑结构。
不要删除内容。
不要新增无关内容。
不要补充解释。
不要输出分析过程。
不要输出 diff。
不要输出 markdown 代码块。
不要对文件做格式化、润色或重排。

【输出要求】
只输出修改后的完整文件正文。
如果无需修改，则原样输出完整文件内容。
不要输出任何额外说明、前言、后记或解释文字。"""

def _strip_optional_markdown_fence(text: str) -> str:
    m = re.fullmatch(r"```[^\n]*\n([\s\S]*?)\n?```\s*", text)
    if not m:
        return text
    return m.group(1)

def _is_probably_binary(sample: bytes) -> bool:
    if b"\x00" in sample[:8192]:
        return True
    return False


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for m in zf.infolist():
            name = (m.filename or "").replace("\\", "/")
            if not name or name.endswith("/"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"ZIP 包含非法路径: {name!r}")
            out_path = (dest / name).resolve()
            try:
                out_path.relative_to(dest)
            except ValueError as e:
                raise ValueError(f"ZIP 路径越界: {name!r}") from e
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m, "r") as src, open(out_path, "wb") as out_f:
                shutil.copyfileobj(src, out_f)


def _zip_tree(src_dir: Path, out_zip: Path) -> None:
    src_dir = src_dir.resolve()
    out_zip = out_zip.resolve()
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file():
                arc = path.relative_to(src_dir).as_posix()
                zf.write(path, arcname=arc)


def _resolve_task_root(extract_root: Path) -> Path:
    """
    任务根目录：若解压根下已有白名单中的任一路径，则用解压根；
    否则若解压根下仅有单个子目录且根下无普通文件，则用该子目录（常见「单文件夹打包」）。
    """
    extract_root = extract_root.resolve()

    def any_root_marker(root: Path) -> bool:
        for n in _ROOT_FILE_NAMES:
            if (root / n).is_file():
                return True
        t = root / "tests"
        if t.is_dir():
            for p in t.iterdir():
                if p.is_file() and p.suffix.lower() == ".sh":
                    return True
        return False

    if any_root_marker(extract_root):
        return extract_root

    subs = [p for p in extract_root.iterdir() if p.is_dir()]
    files_here = [p for p in extract_root.iterdir() if p.is_file()]
    if len(subs) == 1 and not files_here:
        sole = subs[0]
        if any_root_marker(sole):
            return sole.resolve()

    return extract_root


def _allowed_files(task_root: Path) -> list[Path]:
    """返回任务根下按规则存在的文件绝对路径（有序、去重）。"""
    task_root = task_root.resolve()
    out: list[Path] = []
    seen: set[Path] = set()

    for name in _ROOT_FILE_NAMES:
        p = task_root / name
        if p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(p)

    tests = task_root / "tests"
    if tests.is_dir():
        for p in sorted(tests.iterdir()):
            if p.is_file() and p.suffix.lower() == ".sh":
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    out.append(p)

    return out


def _build_sandbox_env() -> dict[str, str]:
    return {
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        "ANTHROPIC_BASE_URL": OPENROUTER_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": CLAUDE_MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": CLAUDE_MODEL,
        "API_TIMEOUT_MS": "300000",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CI": "1",
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="解压输入 ZIP，仅对白名单文件在 Daytona 沙箱内用 Claude Code 做阿里云镜像/资源替换，再打包输出 ZIP。"
    )
    p.add_argument("--input-zip", "-i", required=True, type=Path, help="输入 ZIP 路径")
    p.add_argument("--output-zip", "-o", required=True, type=Path, help="输出 ZIP 路径")
    p.add_argument(
        "--snapshot-name",
        metavar="NAME",
        default=None,
        help="Daytona 快照名，覆盖环境变量 SNAPSHOT_NAME（默认仍为 claude-code-snapshot）",
    )
    p.add_argument("--prompt-file", type=Path, help="自定义系统 prompt 文件（默认内置）")
    log_g = p.add_mutually_exclusive_group()
    log_g.add_argument(
        "--no-change-log",
        action="store_true",
        help="不写变更日志文件",
    )
    log_g.add_argument(
        "--change-log",
        nargs="?",
        const="__AUTO__",
        default="__OFF__",
        metavar="FILE",
        help="写入变更日志：含每个白名单文件的状态、unified diff（替换前后对比）。"
        "若只写 --change-log 不带路径，则默认为与输出 ZIP 同目录的 <stem>.mirror-changes.log",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅解压并列出将送交 Claude 的文件（相对任务根），不创建沙箱、不写输出 ZIP",
    )
    return p.parse_args()


def _ensure_session(sandbox, session_id: str) -> None:
    try:
        sandbox.process.create_session(session_id)
    except Exception:
        try:
            sandbox.process.delete_session(session_id)
        except Exception:
            pass
        sandbox.process.create_session(session_id)


def _wait_claude_command(
    sandbox,
    session_id: str,
    cmd_id: str,
    timeout_sec: int,
    label: str,
) -> tuple[int | None, str, str]:
    start = time.time()
    stdout = ""
    stderr = ""
    last_len = 0
    hb = 0
    exit_code: int | None = None

    while (time.time() - start) < timeout_sec:
        time.sleep(POLL_INTERVAL)
        try:
            logs = sandbox.process.get_session_command_logs(session_id, cmd_id)
        except Exception:
            continue
        stdout = logs.stdout or ""
        stderr = logs.stderr or ""
        if len(stdout) > last_len:
            last_len = len(stdout)
            hb = 0
        try:
            cmd_info = sandbox.process.get_session_command(session_id, cmd_id)
            if cmd_info.exit_code is not None:
                exit_code = cmd_info.exit_code
                logs = sandbox.process.get_session_command_logs(session_id, cmd_id)
                stdout = logs.stdout or ""
                stderr = logs.stderr or ""
                break
        except Exception:
            pass
        hb += POLL_INTERVAL
        if hb > 0 and hb % 60 == 0:
            print(f"    ... [{label}] 已运行 {time.time() - start:.0f}s")
    else:
        print(f"    警告: [{label}] 超时 {timeout_sec}s")
        try:
            sandbox.process.delete_session(session_id)
        except Exception:
            pass
        exit_code = -1

    return exit_code, stdout, stderr


def _sandbox_fetch_remote_text(sandbox, remote_path: str, max_chars: int) -> str:
    try:
        raw = sandbox.fs.download_file(remote_path).decode("utf-8", errors="replace")
    except Exception as e:
        return f"（读取 {remote_path} 失败: {e}）"
    if len(raw) > max_chars:
        return raw[:max_chars] + f"\n...（共 {len(raw)} 字符，已截断）"
    return raw


def _write_text_exact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def _resolve_change_log_path(
    change_log_raw: str,
    no_change_log: bool,
    output_zip: Path,
) -> Path | None:
    if no_change_log or change_log_raw == "__OFF__":
        return None
    if change_log_raw == "__AUTO__":
        return (output_zip.parent / f"{output_zip.stem}.mirror-changes.log").resolve()
    return Path(change_log_raw).resolve()


def _unified_diff_section(before: str, after: str, rel_path: str) -> str:
    if before == after:
        return "（全文与替换前逐字一致，无内容差异。）\n"
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=False),
            after.splitlines(keepends=False),
            fromfile=f"替换前/{rel_path}",
            tofile=f"替换后/{rel_path}",
            lineterm="",
        )
    )
    if not lines:
        return "（行内容相同，差异可能仅在换行符等不可见字符。）\n"
    if len(lines) > MIRROR_CHANGE_LOG_MAX_DIFF_LINES:
        head = lines[:MIRROR_CHANGE_LOG_MAX_DIFF_LINES]
        return (
            "\n".join(head)
            + f"\n\n... 已截断：本 diff 共 {len(lines)} 行，仅显示前 {MIRROR_CHANGE_LOG_MAX_DIFF_LINES} 行；"
            + "可调高环境变量 MIRROR_CHANGE_LOG_MAX_DIFF_LINES。\n"
        )
    return "\n".join(lines) + "\n"


def _full_body_appendix(before: str, after: str) -> str:
    total = len(before.encode("utf-8")) + len(after.encode("utf-8"))
    if total > MIRROR_CHANGE_LOG_FULL_BODY_BYTES:
        return (
            f"（未附「替换前/后全文」：两者 UTF-8 合计 {total} 字节，超过 MIRROR_CHANGE_LOG_FULL_BODY_BYTES={MIRROR_CHANGE_LOG_FULL_BODY_BYTES}，请仅依据上方 diff 排查。）\n"
        )
    return (
        "### 替换前全文\n"
        + before
        + "\n\n### 替换后全文\n"
        + after
        + "\n"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    args = _parse_args()
    input_zip = args.input_zip.resolve()
    output_zip = args.output_zip.resolve()

    if not input_zip.is_file():
        print(f"Error: 输入 ZIP 不存在: {input_zip}", file=sys.stderr)
        return 2

    same_inout = not args.dry_run and output_zip.resolve() == input_zip.resolve()
    if same_inout:
        print("注意: 输入与输出为同一路径，将先写入临时 ZIP 再替换（文件名不变）")

    log_path: Path | None = None
    if not args.dry_run:
        log_path = _resolve_change_log_path(
            args.change_log,
            args.no_change_log,
            output_zip,
        )

    if args.prompt_file:
        if not args.prompt_file.is_file():
            print(f"Error: prompt 文件不存在: {args.prompt_file}", file=sys.stderr)
            return 2
        system_prompt = args.prompt_file.read_text(encoding="utf-8")
    else:
        system_prompt = DEFAULT_SYSTEM_PROMPT

    with tempfile.TemporaryDirectory(prefix="aliyun_mirror_") as td:
        td_path = Path(td)
        extracted = td_path / "extracted"
        staging = td_path / "staging"

        try:
            _safe_extract_zip(input_zip, extracted)
        except (ValueError, zipfile.BadZipFile, OSError) as e:
            print(f"Error: 解压失败: {e}", file=sys.stderr)
            return 2

        task_root = _resolve_task_root(extracted)
        candidates = _allowed_files(task_root)
        if not candidates:
            print(
                "Error: ZIP 中未找到可处理的白名单文件（根目录固定文件或 tests/*.sh）",
                file=sys.stderr,
            )
            return 2

        if args.dry_run:
            print(f"任务根目录（解析为）: {task_root}")
            print("Dry-run：将送交 Claude 的文件（相对任务根）:")
            for p in candidates:
                print(f"  {p.relative_to(task_root).as_posix()}")
            print(f"共 {len(candidates)} 个文件")
            return 0

        shutil.copytree(extracted, staging, symlinks=True)

        log_blocks: list[str] = []
        if log_path:
            log_blocks.append(
                "# aliyun_mirror 变更日志\n"
                f"# 生成时间: {datetime.now(timezone.utc).isoformat()}\n"
                f"# 输入 ZIP: {input_zip}\n"
                f"# 输出 ZIP: {output_zip}\n"
                f"# 任务根目录(解压后解析): {task_root}\n"
            )

        staging_candidates = []
        for p in candidates:
            rel_to_extract = p.relative_to(extracted)
            staging_candidates.append(staging / rel_to_extract)

        if not DAYTONA_API_KEY:
            print("Error: 未设置 DAYTONA_API_KEY", file=sys.stderr)
            return 2
        if not OPENROUTER_API_KEY:
            print("Error: 未设置 OPENROUTER_API_KEY", file=sys.stderr)
            return 2

        snapshot_name = (args.snapshot_name or "").strip() or SNAPSHOT_NAME

        daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))
        sandbox_name = os.environ.get("SANDBOX_NAME") or f"{SANDBOX_NAME_PREFIX}-{uuid.uuid4().hex[:6]}"
        print(f"沙箱名称: {sandbox_name}")

        try:
            existing = daytona.get(sandbox_name)
            print(f"删除已存在沙箱: {existing.id}")
            daytona.delete(existing)
            time.sleep(2)
        except DaytonaNotFoundError:
            pass
        except Exception as e:
            print(f"清理已有沙箱时跳过: {e}")

        sandbox = None
        session_id = "mirror-session"

        try:
            print(f"创建沙箱 snapshot={snapshot_name} ...")
            try:
                sandbox = daytona.create(
                    CreateSandboxFromSnapshotParams(
                        name=sandbox_name,
                        snapshot=snapshot_name,
                        network_block_all=False,
                        auto_stop_interval=0,
                        auto_delete_interval=0,
                        resources=Resources(cpu=2, memory=4, disk=5),
                        env_vars=_build_sandbox_env(),
                    ),
                    timeout=0,
                )
            except DaytonaError as e:
                if "already exists" in str(e).lower():
                    try:
                        daytona.delete(daytona.get(sandbox_name))
                        time.sleep(2)
                    except Exception:
                        pass
                    sandbox = daytona.create(
                        CreateSandboxFromSnapshotParams(
                            name=sandbox_name,
                            snapshot=snapshot_name,
                            network_block_all=False,
                            auto_stop_interval=0,
                            auto_delete_interval=0,
                            resources=Resources(cpu=2, memory=4, disk=5),
                            env_vars=_build_sandbox_env(),
                        ),
                        timeout=0,
                    )
                else:
                    raise

            assert sandbox is not None
            print(f"沙箱已创建: {sandbox.id}")

            sandbox.process.exec(f"mkdir -p {REMOTE_WORK}")
            prompt_remote = f"{REMOTE_WORK}/system_prompt.md"
            sandbox.fs.upload_file(system_prompt.encode("utf-8"), prompt_remote)
            print(f"已上传系统 prompt -> {prompt_remote}")

            _ensure_session(sandbox, session_id)

            processed = 0
            failed = 0

            for dst in staging_candidates:
                rel_label = dst.relative_to(staging).as_posix()
                try:
                    raw = dst.read_bytes()
                except OSError as e:
                    print(f"  读失败 {rel_label}: {e}")
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 跳过\n原因: 读取失败 — {e}\n"
                        )
                    continue

                if _is_probably_binary(raw[: min(len(raw), 8192)]):
                    print(f"  跳过（疑似二进制）: {rel_label}")
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 跳过\n原因: 疑似二进制\n"
                        )
                    continue

                if len(raw) > MIRROR_MAX_FILE_BYTES:
                    print(f"  跳过（超过 MIRROR_MAX_FILE_BYTES）: {rel_label}")
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 跳过\n"
                            f"原因: 超过 MIRROR_MAX_FILE_BYTES ({len(raw)} > {MIRROR_MAX_FILE_BYTES})\n"
                        )
                    continue

                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    print(f"  跳过（非 UTF-8）: {rel_label}")
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 跳过\n原因: 非 UTF-8 文本\n"
                        )
                    continue

                remote_token = f"{uuid.uuid4().hex}_{Path(rel_label).name}"
                in_remote = f"{REMOTE_WORK}/{remote_token}.stdin.txt"
                out_remote = f"{REMOTE_WORK}/{remote_token}.stdout.txt"
                err_remote = f"{REMOTE_WORK}/{remote_token}.stderr.log"

                sandbox.fs.upload_file(text.encode("utf-8"), in_remote)

                perm_arg = (
                    f" --permission-mode {MIRROR_CLAUDE_PERMISSION_MODE}"
                    if MIRROR_CLAUDE_PERMISSION_MODE
                    else ""
                )
                claude_cmd = (
                    f"cd {REMOTE_WORK} && "
                    f"cat {in_remote} | claude -p{perm_arg} "
                    f"--system-prompt-file {prompt_remote} "
                    f"> {out_remote} 2>{err_remote}; "
                    f"CLAUDE_RC=$?; "
                    f'echo "CLAUDE_EXIT_CODE=$CLAUDE_RC"'
                )

                print(f"  Claude 处理: {rel_label} ({len(text)} 字符)")
                exec_resp = sandbox.process.execute_session_command(
                    session_id,
                    SessionExecuteRequest(command=claude_cmd, run_async=True),
                )
                cmd_id = exec_resp.cmd_id
                exit_code, out_log, sess_err = _wait_claude_command(
                    sandbox, session_id, cmd_id, CLAUDE_TIMEOUT, rel_label
                )

                if exit_code == -1:
                    logger.warning("[%s] 超时，保留原文件", rel_label)
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 失败（未写回）\n原因: Claude 调用超时\n"
                        )
                    _ensure_session(sandbox, session_id)
                    continue

                rc_match = re.search(r"CLAUDE_EXIT_CODE=(\d+)", out_log or "")
                claude_rc = int(rc_match.group(1)) if rc_match else None

                if claude_rc not in (0, None):
                    err_snip = _sandbox_fetch_remote_text(sandbox, err_remote, 12000)
                    out_snip = _sandbox_fetch_remote_text(sandbox, out_remote, 8000)
                    se = (sess_err or "").strip()
                    ol = (out_log or "").strip()
                    logger.warning("[%s] claude 退出码 %s，保留原文件", rel_label, claude_rc)
                    if se:
                        logger.warning("[%s] 会话 stderr（节选）:\n%s", rel_label, se[:4000])
                    if err_snip.strip():
                        logger.warning("[%s] stderr.log（节选）:\n%s", rel_label, err_snip[:4000])
                    elif not se:
                        logger.debug("[%s] 会话 stderr 与 stderr.log 均为空或不可读", rel_label)
                    if ol:
                        logger.debug("[%s] 会话 stdout（节选）:\n%s", rel_label, ol[:3500])
                    if out_snip.strip():
                        logger.debug("[%s] stdout.txt（节选）:\n%s", rel_label, out_snip[:2500])
                    diag = f"{se}\n{ol}\n{out_snip}\n{err_snip}"
                    hint = ""
                    if "Author anthropic is banned" in diag or "anthropic is banned" in diag.lower():
                        hint = (
                            "\n### 定位说明\n"
                            "上游 API（常见为 OpenRouter）返回 403：当前 Key/组织禁止使用 Anthropic 来源的模型，"
                            "与脚本或 ZIP 无关。请在供应商控制台更换策略/模型 ID，或改用 Anthropic 官方 API；"
                            "「Please run /login」在无人值守沙箱中通常无法解决此 403。\n"
                        )
                        logger.warning(
                            "[%s] 403「Author anthropic is banned」— 当前 API 线路禁止 Anthropic 模型，"
                            "请换模型/换 Key 或直连 Anthropic；非本脚本缺陷。",
                            rel_label,
                        )
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 失败（未写回）\n"
                            f"原因: Claude CLI 退出码 {claude_rc}\n"
                            f"### 会话 stderr\n{se or '（空）'}\n"
                            f"### 会话 stdout\n{ol or '（空）'}\n"
                            f"### stderr.log\n{err_snip}\n"
                            f"### stdout.txt\n{out_snip}\n"
                            f"{hint}"
                        )
                    continue

                try:
                    out_bytes = sandbox.fs.download_file(out_remote)
                    out_text = out_bytes.decode("utf-8")
                except Exception as e:
                    logger.warning("[%s] 下载输出失败 %s，保留原文件", rel_label, e)
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 失败（未写回）\n原因: 下载沙箱输出失败 — {e}\n"
                        )
                    continue

                out_text = _strip_optional_markdown_fence(out_text)
                if not out_text.strip() and text.strip():
                    logger.warning("[%s] 模型返回空，保留原文件", rel_label)
                    failed += 1
                    if log_path:
                        log_blocks.append(
                            f"\n## 文件: {rel_label}\n### 状态: 失败（未写回）\n原因: 模型返回空\n"
                        )
                    continue

                _write_text_exact(dst, out_text)
                processed += 1
                if log_path:
                    b_before = len(text.encode("utf-8"))
                    b_after = len(out_text.encode("utf-8"))
                    changed = text != out_text
                    log_blocks.append(
                        f"\n## 文件: {rel_label}\n"
                        f"### 状态: {'已写回（与替换前有差异）' if changed else '已写回（与替换前全文一致）'}\n"
                        f"### 大小: 替换前 {b_before} B（UTF-8） → 替换后 {b_after} B（UTF-8）\n"
                        "### Unified diff（行首 - 为替换前，+ 为替换后）\n"
                        + _unified_diff_section(text, out_text, rel_label)
                        + _full_body_appendix(text, out_text)
                    )

            try:
                sandbox.process.delete_session(session_id)
            except Exception:
                pass

            zip_target = (
                output_zip.parent / f".{output_zip.name}.mirror-tmp.{uuid.uuid4().hex}.zip"
                if same_inout
                else output_zip
            )
            try:
                _zip_tree(staging, zip_target)
            except OSError as e:
                print(f"Error: 打包失败: {e}", file=sys.stderr)
                if same_inout and zip_target.exists():
                    try:
                        zip_target.unlink()
                    except OSError:
                        pass
                if log_path and log_blocks:
                    try:
                        log_path.parent.mkdir(parents=True, exist_ok=True)
                        log_path.write_text("\n".join(log_blocks), encoding="utf-8")
                        print(f"（已尽力写入变更日志）{log_path}", file=sys.stderr)
                    except OSError:
                        pass
                return 3

            if same_inout:
                try:
                    os.replace(zip_target, output_zip)
                except OSError as e:
                    print(f"Error: 无法将临时 ZIP 替换为输出路径: {e}", file=sys.stderr)
                    try:
                        zip_target.unlink()
                    except OSError:
                        pass
                    return 3

            if log_path and log_blocks:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("\n".join(log_blocks), encoding="utf-8")
                print(f"变更日志: {log_path}")

            print(f"\n完成: Claude 已写回 {processed} 个文件; 失败/跳过 {failed}")
            print(f"输出 ZIP: {output_zip}")
            return 0 if failed == 0 else 1

        finally:
            if sandbox:
                print("\n清理沙箱...")
                try:
                    sandbox.refresh_data()
                    if sandbox.state == SandboxState.STARTED:
                        sandbox.stop()
                        print("沙箱已停止。")
                except Exception as e:
                    print(f"警告: 停止沙箱失败: {e}")
                try:
                    daytona.delete(sandbox)
                    print("沙箱已删除。")
                except Exception as e:
                    print(f"警告: 删除沙箱失败: {e}")


if __name__ == "__main__":
    sys.exit(main() or 0)
