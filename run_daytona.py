#!/usr/bin/env python3
"""
run_daytona.py — 在 Daytona 沙箱内执行 Claude Code 机审

替代直接 `claude -p` 调用。沙箱位于海外，可正常访问 OpenRouter，
绕过国内防火墙限制。

用法:
    # 从远端仓库按规则名取 prompt/schema（需克隆仓库）
    python3 run_daytona.py <rule_name> <input_file> [output_file]

    # 使用本地 prompt/schema 文件（不克隆仓库，适合本地测试）
    python3 run_daytona.py --prompt-file <path> --schema-file <path> <input_file> [output_file]

    # Agent 模式：从任务目录自动组装输入（自动获取测试/任务文件）
    python3 run_daytona.py --task-dir <任务目录> [--prompt-file <path> --schema-file <path>] <rule_name 或留空> [output_file]

    rule_name   — 规则名称，如 template（仅第一种用法）
    input_file  — 构建好的输入消息文件路径（未使用 --task-dir 时必填）
    output_file — 输出 JSON 路径（默认 ./review_report_<rule_name>.json 或 ./review_report_local.json）。
                 最终文件格式为 structured_output（仅审核结果 JSON，不含 API 包装的 type/result/usage 等）。

Prompt / Schema 解析顺序：
    Prompt:
      1. rules/<rule_name>/prompt.runtime.md
      2. rules/<rule_name>/prompt.md
      3. rules/prompt.<rule_name>.md
    Schema:
      1. rules/<rule_name>/schema.self_check.json
      2. rules/<rule_name>/schema.json
      3. rules/schema.self_check.json
      4. rules/schema.json

环境变量（均可由流水线注入）:
    OPENROUTER_API_KEY   — OpenRouter 鉴权密钥
    OPENROUTER_BASE_URL  — OpenRouter 端点
    ANTHROPIC_MODEL      — 模型名称（也读取 ANTHROPIC_DEFAULT_SONNET_MODEL）
    DAYTONA_API_KEY      — Daytona API 密钥
    SNAPSHOT_NAME        — Daytona 快照名
    SANDBOX_NAME         — 沙箱名称（可选；未设置时用 SANDBOX_NAME_PREFIX+6位随机hex）
    SANDBOX_NAME_PREFIX  — 随机命名时的前缀（默认 code_review）
    GIT_REPO_URL         — 本仓库地址
    GIT_BRANCH           — 分支
    GIT_USERNAME         — Git 用户名
    GIT_TOKEN            — Git Token
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from daytona import (
    Daytona,
    DaytonaConfig,
    CreateSandboxFromSnapshotParams,
    DaytonaNotFoundError,
    DaytonaError,
    Resources,
    SandboxState,
    SessionExecuteRequest,
)

# ============================================================
# 配置（均可通过环境变量覆盖）
# ============================================================
DAYTONA_API_KEY = os.environ.get("DAYTONA_API_KEY", "")
SNAPSHOT_NAME = os.environ.get("SNAPSHOT_NAME", "claude-code-snapshot")
# 沙箱名：未设置 SANDBOX_NAME 时使用「前缀+6位随机hex」保证每次唯一
SANDBOX_NAME_PREFIX = os.environ.get("SANDBOX_NAME_PREFIX", "code_review")

OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CLAUDE_MODEL = os.environ.get(
    "ANTHROPIC_MODEL",
    os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "anthropic/claude-sonnet-4.6"),
)

GIT_REPO_URL = os.environ.get(
    "GIT_REPO_URL",
    "https://github.com/wisdom-knowledge/tb3.0",
)
GIT_BRANCH = os.environ.get("GIT_BRANCH", "master")
GIT_USERNAME = os.environ.get("GIT_USERNAME", "")
GIT_TOKEN = os.environ.get("GIT_TOKEN", "")

REMOTE_REPO_DIR = "/home/daytona/claude-code-review"
REMOTE_TMP_DIR = "/tmp/review"

CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
POLL_INTERVAL = 5

_SANDBOX_REPAIR_SCRIPT = r"""#!/usr/bin/env python3
import json, re, sys

def fix_unescaped_quotes(text):
    out = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == '\\' and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                k = i + 1
                while k < n and text[k] in ' \t\r\n':
                    k += 1
                if k >= n or text[k] in ':,}]':
                    in_string = False
                    out.append('"')
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append('"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)

def write_json(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

def try_extract_structured_output(obj):
    # 从 --output-format json 的包装中提取 structured_output 或 result
    if not isinstance(obj, dict):
        return None
    so = obj.get("structured_output")
    if isinstance(so, dict) and so:
        print("EXTRACT_METHOD=structured_output")
        return so
    result = obj.get("result")
    if isinstance(result, dict) and result:
        print("EXTRACT_METHOD=result_object")
        return result
    if isinstance(result, str) and result.strip():
        try:
            parsed = json.loads(result.strip())
            if isinstance(parsed, dict):
                print("EXTRACT_METHOD=result_string_parsed")
                return parsed
        except json.JSONDecodeError:
            pass
    return None

def try_repair(raw):
    # 多层修复：去掉 markdown 包装、提取大括号区块、状态机修复引号
    m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    candidate = m.group(1).strip() if m else raw
    if candidate != raw:
        try:
            obj = json.loads(candidate)
            print("REPAIR_METHOD=markdown_strip")
            return obj
        except json.JSONDecodeError:
            raw = candidate

    fb = raw.find("{")
    lb = raw.rfind("}")
    if fb != -1 and lb > fb:
        candidate = raw[fb:lb+1]
        try:
            obj = json.loads(candidate)
            print("REPAIR_METHOD=brace_extract")
            return obj
        except json.JSONDecodeError:
            raw = candidate

    try:
        fixed = fix_unescaped_quotes(raw)
        obj = json.loads(fixed)
        print("REPAIR_METHOD=state_machine_quote_fix")
        return obj
    except Exception as e:
        print(f"REPAIR_QUOTE_FIX_ERR={e}")

    return None

raw_file = sys.argv[1]
out_file = sys.argv[2]
with open(raw_file, "r", encoding="utf-8") as f:
    raw = f.read().strip()
if not raw:
    print("REPAIR_STATUS=empty")
    write_json({}, out_file)
    sys.exit(0)

# 1) 尝试按合法 JSON 解析
try:
    obj = json.loads(raw)
    extracted = try_extract_structured_output(obj)
    if extracted:
        print("JSON_VALID=true")
        write_json(extracted, out_file)
        sys.exit(0)
    if "query_check" in obj or "rubrics_review_result" in obj or ("criteria" in obj and isinstance(obj.get("criteria"), dict)):
        print("JSON_VALID=true")
        print("EXTRACT_METHOD=direct_review_json")
        write_json(obj, out_file)
        sys.exit(0)
    print("JSON_VALID=true")
    print("EXTRACT_METHOD=unknown_structure")
    write_json(obj, out_file)
    sys.exit(0)
except json.JSONDecodeError:
    pass

# 2) 多层修复
repaired = try_repair(raw)
if repaired:
    extracted = try_extract_structured_output(repaired)
    if extracted:
        print("JSON_VALID=true")
        write_json(extracted, out_file)
        sys.exit(0)
    print("JSON_VALID=true")
    write_json(repaired, out_file)
    sys.exit(0)

# 3) 全部失败
print("JSON_VALID=false")
print("REPAIR_STATUS=failed")
with open(out_file, "w", encoding="utf-8") as f:
    f.write(raw)
"""


def resolve_paths(rule_name: str):
    rules_dir = f"{REMOTE_REPO_DIR}/rules"
    rule_dir = f"{rules_dir}/{rule_name}"

    prompt_candidates = [
        f"{rule_dir}/prompt.runtime.md",
        f"{rule_dir}/prompt.md",
        f"{rules_dir}/prompt.{rule_name}.md",
    ]
    schema_candidates = [
        f"{rule_dir}/schema.self_check.json",
        f"{rule_dir}/schema.json",
        f"{rules_dir}/schema.self_check.json",
        f"{rules_dir}/schema.json",
    ]
    return prompt_candidates, schema_candidates


def _fix_unescaped_quotes(text: str) -> str:
    """用状态机遍历 JSON 文本，对字符串内非结构性双引号做转义。"""
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False

    while i < n:
        ch = text[i]
        if in_string:
            if ch == '\\' and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                k = i + 1
                while k < n and text[k] in ' \t\r\n':
                    k += 1
                if k >= n or text[k] in ':,}]':
                    in_string = False
                    out.append('"')
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append('"')
            i += 1
            continue
        out.append(ch)
        i += 1

    return ''.join(out)


def _try_extract_structured_output(obj: dict) -> dict | None:
    """从 --output-format json 的包装中提取审核结果 JSON。"""
    if not isinstance(obj, dict):
        return None
    so = obj.get("structured_output")
    if isinstance(so, dict) and so:
        print("  提取: 找到 structured_output 字段")
        return so
    result = obj.get("result")
    if isinstance(result, dict) and result:
        print("  提取: 找到 result 为 dict")
        return result
    if isinstance(result, str) and result.strip():
        try:
            parsed = json.loads(result.strip())
            if isinstance(parsed, dict):
                print("  提取: 将 result 字符串解析为 JSON")
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _try_repair_json(raw: str) -> str:
    """从模型输出中提取并修复为合法 JSON，支持原始审核 JSON 与 --output-format json 包装。"""
    import re

    # 先尝试按 JSON 解析并从包装中提取
    try:
        obj = json.loads(raw)
        extracted = _try_extract_structured_output(obj)
        if extracted:
            return json.dumps(extracted, ensure_ascii=False)
        if "query_check" in obj:
            print("  修复: 已是合法审核 JSON")
            return raw
        print("  修复: 合法 JSON 但结构未知，原样返回")
        return raw
    except json.JSONDecodeError:
        pass

    # 去掉 markdown ```json ... ``` 包装
    m = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            print("  修复: 已去掉 markdown 包装 → OK")
            return candidate
        except json.JSONDecodeError:
            raw = candidate

    # 提取从第一个 { 到最后一个 } 的区块
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            json.loads(candidate)
            print("  修复: 已提取 {..} 区块 → OK")
            return candidate
        except json.JSONDecodeError:
            raw = candidate

    # 状态机引号修复
    try:
        fixed = _fix_unescaped_quotes(raw)
        json.loads(fixed)
        print("  修复: 状态机引号修复 → OK")
        return fixed
    except json.JSONDecodeError as e:
        print(f"  修复: 状态机引号修复失败 — {e}")

    print("  修复: 所有尝试均失败，返回原始输出")
    return raw


_CLAUDE_ERROR_SUBTYPES = frozenset({
    "error_max_structured_output_retries",
    "error_max_turns",
    "error_model",
    "error_overloaded",
    "error_tool",
})

# 与 schema.json / prompt.txt 一致：schema 顶层为 rubrics_review_result，内含 criteria（19 条准则英文名）
_SCHEMA_CRITERIA_NAMES = frozenset({
    "verifiable", "well_specified", "solvable", "difficult", "interesting",
    "outcome_verified", "anti_cheat_robustness", "functional_verification",
    "deterministic_reproducible", "essential_difficulty", "test_instruction_alignment",
    "novel", "agentic", "reviewable", "instruction_clarity", "solution_quality",
    "environment_hygiene", "structured_data_schema", "typos",
})
_EXPECTED_REVIEW_KEYS = frozenset({
    "rubrics_review_result", "criteria", "review_suggestions", "summary", "task_path",
    "query_check", "rubric_format_check", "rubric_content_check", "rubric_adjustment_check", "additional_checks",
}) | _SCHEMA_CRITERIA_NAMES


def _is_claude_error_response(obj: dict) -> bool:
    """判断是否为 Claude Code CLI 的错误/元数据包装（非有效审核结果）。"""
    if not isinstance(obj, dict):
        return False
    
    # 获取 subtype
    subtype = obj.get("subtype", "")
    
    # 检查 subtype 是否为错误类型
    if isinstance(subtype, str) and subtype.startswith("error_"):
        return True
    
    # 处理 type 为 result，且 is_error 为 True 的情况
    if obj.get("type") == "result" and obj.get("is_error") is True:
        return True
    
    # 检查 subtype 是否为错误类型
    if obj.get("type") == "result" and isinstance(subtype, str) and subtype in _CLAUDE_ERROR_SUBTYPES:
        return True
    
    return False


def _abort_on_error_response(obj: dict, output_file: str):
    """将错误信息写入输出文件并以非零退出码结束。"""
    subtype = obj.get("subtype", "unknown")
    
    # 获取错误消息
    errors = obj.get("errors", [])
    error_msg = "; ".join(errors) if errors else subtype
    
    # 打印错误信息
    if subtype == "success":
        print(f"成功：Claude Code CLI 返回成功响应: subtype={subtype}")
    else:
        print(f"错误：Claude Code CLI 返回错误响应: subtype={subtype}")
        print(f"错误信息：{error_msg}")
        
        # 记录错误到文件
        error_result = {
            "解析失败": {
                "原因": f"Claude Code CLI 错误: {error_msg}",
                "subtype": subtype,
            }
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(error_result, f, ensure_ascii=False)
        
        sys.exit(4)  # 错误退出


def _validate_review_output(obj: dict, output_file: str, strict_keys: bool = True):
    """检查输出是否为非空 dict；可选是否要求包含预期审核键。"""
    if not isinstance(obj, dict) or not obj:
        print("错误：输出为空或不是 dict")
        sys.exit(4)
    if not strict_keys:
        return
    if _EXPECTED_REVIEW_KEYS & set(obj.keys()):
        return
    print(f"错误：输出缺少预期审核键。当前键: {list(obj.keys())[:10]}")
    sys.exit(4)


# 预设要纳入的文件名/目录名（Agent 自动获取测试与任务文件时使用）
_TASK_INSTRUCTION_NAMES = ("instruction.md", "README.md", "README.rst", "TASK.md")
_TASK_SPEC_FILES = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "build_docker.sh")
_TASK_SOLUTION_NAMES = ("solution", "solve.sh", "solve.py")
_TASK_TESTS_DIR = "tests"
_MAX_BYTES_PER_FILE = int(os.environ.get("REVIEW_MAX_BYTES_PER_FILE", "524288"))   # 512KB
_MAX_TOTAL_INPUT_BYTES = int(os.environ.get("REVIEW_MAX_TOTAL_INPUT_BYTES", "2097152"))  # 2MB


def build_input_from_task_dir(
    task_dir: str,
    max_bytes_per_file: int = _MAX_BYTES_PER_FILE,
    max_total_bytes: int = _MAX_TOTAL_INPUT_BYTES,
) -> str:
    """
    从任务目录自动收集 instruction、Dockerfile、solution、tests 等，
    组装成一份供 Claude 审核的输入文本。用于 Agent 模式「自动获取测试文件」。
    """
    root = Path(task_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"task_dir is not a directory: {task_dir}")

    parts: list[str] = []
    total_bytes = 0

    def add_file(path: Path, label: str) -> bool:
        nonlocal total_bytes
        if total_bytes >= max_total_bytes:
            return False
        try:
            raw = path.read_bytes()
        except OSError:
            return True
        if len(raw) > max_bytes_per_file:
            raw = raw[:max_bytes_per_file] + b"\n... (truncated)\n"
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return True
        parts.append(f"\n# -------- {label} --------\n{text}")
        total_bytes += len(raw)
        return True

    # 1) instruction / README 说明文件
    for name in _TASK_INSTRUCTION_NAMES:
        p = root / name
        if p.is_file():
            add_file(p, p.name)
            break

    # 2) Dockerfile / docker-compose / build_docker.sh
    for name in _TASK_SPEC_FILES:
        p = root / name
        if p.is_file() and add_file(p, p.name) is False:
            break

    # 3) solution 目录或 solve.sh / solve.py
    solution_dir = root / "solution"
    if solution_dir.is_dir():
        for f in sorted(solution_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                if not add_file(f, str(f.relative_to(root))):
                    break
    for name in ("solve.sh", "solve.py", "solution.py", "solution.sh"):
        p = root / name
        if p.is_file():
            add_file(p, p.name)

    # 4) 顶层 tests 目录
    tests_dir = root / _TASK_TESTS_DIR
    if tests_dir.is_dir():
        for f in sorted(tests_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                if not add_file(f, str(f.relative_to(root))):
                    break

    # 4b) Java/Maven 等：src/test 下的测试与资源（否则 step 5 会因含 test 被跳过）
    for test_pattern in ("**/src/test/**/*.java", "**/src/test/**/*.json", "**/src/test/**/*.xml", "**/src/test/**/*.py"):
        for f in sorted(root.glob(test_pattern)):
            if not f.is_file():
                continue
            if total_bytes >= max_total_bytes:
                break
            add_file(f, str(f.relative_to(root)))

    # 5) 常见源码（不含测试目录，避免重复）
    for pattern in ("**/*.py", "**/*.java", "**/*.ts", "**/*.js", "**/pom.xml", "**/requirements.txt"):
        for f in sorted(root.glob(pattern)):
            if not f.is_file():
                continue
            rel = str(f.relative_to(root))
            if "solution" in rel or rel.startswith("tests/") or "/src/test/" in rel.replace("\\", "/") or "test" in rel.lower():
                continue
            if total_bytes >= max_total_bytes:
                break
            add_file(f, rel)

    if not parts:
        raise ValueError(f"task_dir 下未找到可组装的文件: {task_dir}")

    return "\n".join(parts).strip()


def _parse_args():
    parser = argparse.ArgumentParser(
        description="在 Daytona 沙箱内执行 Claude Code 机审。支持从仓库规则或本地文件读取 prompt/schema；可选从任务目录自动组装输入（Agent 模式）。"
    )
    parser.add_argument("--prompt-file", help="本地 prompt 文件路径（与 --schema-file 同时指定则使用本地模式，不克隆仓库）")
    parser.add_argument("--schema-file", help="本地 schema JSON 文件路径")
    parser.add_argument(
        "--task-dir",
        help="任务目录：自动收集 instruction.md、Dockerfile、solution、tests 等组装为输入，无需单独提供 input_file（Agent 模式）",
    )
    parser.add_argument("positional", nargs="*", help="规则名 + 输入文件 [输出文件]，或（本地模式）输入文件 [输出文件]；使用 --task-dir 时为 [输出文件] 或 规则名 [输出文件]")
    args = parser.parse_args()
    use_local = bool(args.prompt_file and args.schema_file)
    task_dir = (args.task_dir or "").strip() or None

    if use_local:
        if task_dir:
            if len(args.positional) > 1:
                parser.error("本地模式且使用 --task-dir 时，最多一个位置参数: [output_file]")
            input_file = None
            output_file = args.positional[0] if args.positional else "./review_report_local.json"
        else:
            if len(args.positional) < 1:
                parser.error("本地模式需要至少一个位置参数: <input_file> [output_file]")
            input_file = args.positional[0]
            output_file = args.positional[1] if len(args.positional) > 1 else "./review_report_local.json"
        rule_name = "local"
    else:
        if task_dir:
            if len(args.positional) < 1:
                parser.error("仓库模式且使用 --task-dir 时，需要: <rule_name> [output_file]")
            rule_name = args.positional[0]
            input_file = None
            output_file = args.positional[1] if len(args.positional) > 1 else f"./review_report_{rule_name}.json"
        else:
            if len(args.positional) < 2:
                parser.error("仓库模式需要至少两个位置参数: <rule_name> <input_file> [output_file]")
            rule_name = args.positional[0]
            input_file = args.positional[1]
            output_file = args.positional[2] if len(args.positional) > 2 else f"./review_report_{rule_name}.json"
    return rule_name, input_file, output_file, use_local, args.prompt_file, args.schema_file, task_dir


def main():
    rule_name, input_file, output_file, use_local, local_prompt_file, local_schema_file, task_dir = _parse_args()

    if not DAYTONA_API_KEY:
        print("Error: DAYTONA_API_KEY 未设置，请设置环境变量")
        sys.exit(2)
    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY 未设置，请设置环境变量")
        sys.exit(2)
    if not use_local and (not GIT_USERNAME or not GIT_TOKEN):
        print("Warning: 仓库模式未设置 GIT_USERNAME/GIT_TOKEN，若为私有仓库将无法克隆")

    if task_dir:
        print(f"Agent 模式: 从任务目录组装输入: {task_dir}")
        try:
            input_content = build_input_from_task_dir(task_dir)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            sys.exit(2)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(2)
        print(f"  组装输入长度: {len(input_content)} 字符")
    else:
        if not os.path.isfile(input_file):
            print(f"Error: input file not found: {input_file}")
            sys.exit(2)
        with open(input_file, "r", encoding="utf-8") as f:
            input_content = f.read().strip()
        if not input_content:
            print(f"Error: input file is empty: {input_file}")
            sys.exit(2)

    if use_local:
        if not os.path.isfile(local_prompt_file):
            print(f"Error: prompt file not found: {local_prompt_file}")
            sys.exit(2)
        if not os.path.isfile(local_schema_file):
            print(f"Error: schema file not found: {local_schema_file}")
            sys.exit(2)
        with open(local_prompt_file, "r", encoding="utf-8") as f:
            prompt_content = f.read().strip()
        if not prompt_content:
            print(f"Error: prompt file is empty: {local_prompt_file}")
            sys.exit(2)
        try:
            with open(local_schema_file, "r", encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: schema file is not valid JSON: {local_schema_file} — {e}")
            sys.exit(2)

    print(f"规则: {rule_name}" + ("（本地文件）" if use_local else ""))
    print(f"输入: {task_dir or input_file}（{len(input_content)} 字节）")
    print(f"输出: {output_file}")
    print(f"模型: {CLAUDE_MODEL}")
    if not use_local:
        print(f"分支: {GIT_BRANCH}")

    daytona = Daytona(DaytonaConfig(api_key=DAYTONA_API_KEY))

    sandbox_name = os.environ.get("SANDBOX_NAME") or f"{SANDBOX_NAME_PREFIX}-{uuid.uuid4().hex[:6]}"
    print(f"沙箱名称: {sandbox_name}")

    # 若已有同名沙箱则先删除（例如显式指定了 SANDBOX_NAME 时）
    try:
        existing = daytona.get(sandbox_name)
        print(f"发现已存在沙箱: {existing.id}，正在删除...")
        daytona.delete(existing)
        time.sleep(2)
    except DaytonaNotFoundError:
        pass
    except Exception as e:
        print(f"清理已有沙箱时跳过: {e}")

    sandbox = None
    try:
        print(f"\n正在创建沙箱 (snapshot={SNAPSHOT_NAME})...")
        try:
            sandbox = daytona.create(
                CreateSandboxFromSnapshotParams(
                    name=sandbox_name,
                    snapshot=SNAPSHOT_NAME,
                    network_block_all=False,
                    auto_stop_interval=0,
                    auto_delete_interval=0,
                    resources=Resources(cpu=2, memory=4, disk=5),
                    env_vars={
                        "ANTHROPIC_BASE_URL": OPENROUTER_BASE_URL,
                        "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
                        "ANTHROPIC_API_KEY": "",
                        "ANTHROPIC_MODEL": CLAUDE_MODEL,
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": CLAUDE_MODEL,
                        "API_TIMEOUT_MS": "300000",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "CI": "1",
                    },
                ),
                timeout=0,
            )
        except DaytonaError as e:
            if "already exists" in str(e).lower():
                print(f"沙箱名已存在，正在删除后重试: {e}")
                try:
                    daytona.delete(daytona.get(sandbox_name))
                    time.sleep(2)
                except Exception:
                    pass
                sandbox = daytona.create(
                    CreateSandboxFromSnapshotParams(
                        name=sandbox_name,
                        snapshot=SNAPSHOT_NAME,
                        network_block_all=False,
                        auto_stop_interval=0,
                        auto_delete_interval=0,
                        resources=Resources(cpu=2, memory=4, disk=5),
                        env_vars={
                            "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
                            "ANTHROPIC_BASE_URL": OPENROUTER_BASE_URL,
                            "ANTHROPIC_AUTH_TOKEN": OPENROUTER_API_KEY,
                            "ANTHROPIC_API_KEY": "",
                            "ANTHROPIC_MODEL": CLAUDE_MODEL,
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": CLAUDE_MODEL,
                            "API_TIMEOUT_MS": "300000",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            "CI": "1",
                        },
                    ),
                    timeout=0,
                )
            else:
                raise
        print(f"沙箱已创建: {sandbox.id}")

        if use_local:
            # 本地模式：不克隆仓库，上传本地 prompt/schema 到沙箱
            sandbox.process.exec(f"mkdir -p {REMOTE_TMP_DIR}")
            prompt_basename = os.path.basename(local_prompt_file)
            prompt_remote = f"{REMOTE_TMP_DIR}/{prompt_basename}"
            schema_remote = f"{REMOTE_TMP_DIR}/schema.json"
            work_dir = REMOTE_TMP_DIR
            with open(local_prompt_file, "r", encoding="utf-8") as f:
                sandbox.fs.upload_file(f.read().encode("utf-8"), prompt_remote)
            with open(local_schema_file, "r", encoding="utf-8") as f:
                sandbox.fs.upload_file(f.read().encode("utf-8"), schema_remote)
            print(f"  Prompt（本地）: {local_prompt_file} -> {prompt_remote}")
            print(f"  Schema（本地）: {local_schema_file} -> {schema_remote}")
        else:
            print(f"\n正在克隆 {GIT_REPO_URL} (branch: {GIT_BRANCH})...")
            clone_kwargs = {
                "url": GIT_REPO_URL,
                "path": REMOTE_REPO_DIR,
                "branch": GIT_BRANCH,
            }
            if GIT_USERNAME and GIT_TOKEN:
                clone_kwargs["username"] = GIT_USERNAME
                clone_kwargs["password"] = GIT_TOKEN
            sandbox.git.clone(**clone_kwargs)
            print("仓库已克隆。")

            prompt_candidates, schema_candidates = resolve_paths(rule_name)

            prompt_remote = None
            for p in prompt_candidates:
                check = sandbox.process.exec(f"test -f {p} && echo ok || echo missing")
                if "ok" in check.result:
                    prompt_remote = p
                    break
            if not prompt_remote:
                print(f"错误：未找到 prompt 文件。已查找:\n" +
                      "\n".join(f"  - {p}" for p in prompt_candidates))
                sys.exit(3)

            schema_remote = None
            for s in schema_candidates:
                check = sandbox.process.exec(f"test -f {s} && echo ok || echo missing")
                if "ok" in check.result:
                    schema_remote = s
                    break
            if not schema_remote:
                print(f"错误：未找到 schema 文件。已查找:\n" +
                      "\n".join(f"  - {s}" for s in schema_candidates))
                sys.exit(3)

            work_dir = REMOTE_REPO_DIR
            print(f"  Prompt: {prompt_remote}")
            print(f"  Schema: {schema_remote}")

        sandbox.process.exec(f"mkdir -p {REMOTE_TMP_DIR}")
        input_remote = f"{REMOTE_TMP_DIR}/input_message.txt"
        sandbox.fs.upload_file(input_content.encode("utf-8"), input_remote)
        print(f"输入已上传到沙箱: {input_remote}")

        raw_remote = f"{REMOTE_TMP_DIR}/raw_output.txt"
        output_remote = f"{REMOTE_TMP_DIR}/output.json"
        repair_script = f"{REMOTE_TMP_DIR}/repair_json.py"

        sandbox.fs.upload_file(
            _SANDBOX_REPAIR_SCRIPT.encode("utf-8"), repair_script
        )

        claude_cmd = (
            f"cd {work_dir} && "
            f"cat {input_remote} | claude -p "
            f"--system-prompt-file {prompt_remote} "
            f"--output-format json "
            f"--json-schema \"$(cat {schema_remote})\" "
            f"> {raw_remote} 2>{REMOTE_TMP_DIR}/stderr.log; "
            f"CLAUDE_RC=$?; "
            f"echo \"CLAUDE_EXIT_CODE=$CLAUDE_RC\"; "
            f"echo \"RAW_BYTES=$(wc -c < {raw_remote})\"; "
            f"python3 {repair_script} {raw_remote} {output_remote}"
        )
        print(f"\nClaude command:\n  {claude_cmd}\n")

        session_id = "review-session"
        try:
            sandbox.process.create_session(session_id)
        except Exception:
            try:
                sandbox.process.delete_session(session_id)
            except Exception:
                pass
            sandbox.process.create_session(session_id)

        start_time = time.time()
        exec_resp = sandbox.process.execute_session_command(
            session_id,
            SessionExecuteRequest(command=claude_cmd, run_async=True),
        )
        cmd_id = exec_resp.cmd_id
        print(f"会话命令已提交 (cmd_id: {cmd_id})")

        stdout = ""
        stderr = ""
        exit_code = None
        last_stdout_len = 0
        heartbeat_counter = 0

        while (time.time() - start_time) < CLAUDE_TIMEOUT:
            time.sleep(POLL_INTERVAL)
            try:
                logs = sandbox.process.get_session_command_logs(session_id, cmd_id)
            except Exception:
                continue
            stdout = logs.stdout or ""
            stderr = logs.stderr or ""
            if len(stdout) > last_stdout_len:
                last_stdout_len = len(stdout)
                heartbeat_counter = 0
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
            heartbeat_counter += POLL_INTERVAL
            if heartbeat_counter > 0 and heartbeat_counter % 60 == 0:
                elapsed = time.time() - start_time
                print(f"  ... 运行中 {elapsed:.0f}s")
        else:
            print(f"Claude 超时（{CLAUDE_TIMEOUT}s），正在结束会话...")
            try:
                sandbox.process.delete_session(session_id)
            except Exception:
                pass
            exit_code = -1

        try:
            sandbox.process.delete_session(session_id)
        except Exception:
            pass

        elapsed = time.time() - start_time
        print(f"\nClaude 于 {elapsed:.1f}s 内完成, exit_code={exit_code}")

        if stdout.strip():
            print(f"会话标准输出:\n{stdout.strip()[:500]}")
        if stderr.strip():
            print(f"会话标准错误:\n{stderr.strip()[:500]}")

        sandbox_json_valid = "JSON_VALID=true" in (stdout or "")
        claude_rc_match = re.search(r"CLAUDE_EXIT_CODE=(\d+)", stdout or "")
        claude_rc = int(claude_rc_match.group(1)) if claude_rc_match else None
        raw_bytes_match = re.search(r"RAW_BYTES=(\d+)", stdout or "")
        raw_bytes = int(raw_bytes_match.group(1)) if raw_bytes_match else None

        print(f"\n[步骤 1] 沙箱端校验: JSON_VALID={sandbox_json_valid}, CLAUDE_RC={claude_rc}, RAW_BYTES={raw_bytes}")

        if claude_rc is not None and claude_rc != 0 and (raw_bytes is None or raw_bytes == 0):
            print(f"错误：Claude CLI 退出码 {claude_rc} 且无输出 (RAW_BYTES={raw_bytes})")
            sys.exit(4)

        print(f"[步骤 2] 正在从沙箱下载输出: {output_remote}")
        try:
            claude_output_bytes = sandbox.fs.download_file(output_remote)
            claude_output = claude_output_bytes.decode("utf-8").strip()
        except Exception as e:
            print(f"错误：下载输出文件失败: {e}")
            print(f"[步骤 2b] 尝试下载原始输出: {raw_remote}")
            try:
                claude_output_bytes = sandbox.fs.download_file(raw_remote)
                claude_output = claude_output_bytes.decode("utf-8").strip()
            except Exception as e2:
                print(f"错误：下载原始输出失败: {e2}")
                sys.exit(3)

        if not claude_output:
            print("错误：Claude 返回空输出")
            sys.exit(3)

        downloaded_bytes = len(claude_output.encode("utf-8"))
        print(f"[步骤 3] 已下载 {downloaded_bytes} 字节（{len(claude_output)} 字符）")

        # 最终输出格式统一为 structured_output（仅审核结果 JSON，不含 type/result/usage 等外层包装）
        try:
            parsed = json.loads(claude_output)
            extracted = _try_extract_structured_output(parsed)
            if extracted:
                if _is_claude_error_response(extracted):
                    _abort_on_error_response(extracted, output_file)
                claude_output = json.dumps(extracted, ensure_ascii=False)
                print("[步骤 4] 主机端：已从包装中提取 structured_output，将仅写入该部分")
            elif _is_claude_error_response(parsed):
                _abort_on_error_response(parsed, output_file)
            elif "query_check" in parsed or "rubrics_review_result" in parsed or ("criteria" in parsed and _SCHEMA_CRITERIA_NAMES & set(parsed.get("criteria") or {})):
                # 已是直接审核 JSON（旧版 query_check 或 schema 版 rubrics_review_result/criteria），无需再包一层
                print("[步骤 4] 主机端 JSON 校验：通过（直接审核 JSON），输出格式为 structured_output")
            else:
                # 可能是包装格式但 result 为字符串，尝试从 result 中抽出 JSON 作为 structured_output
                result_str = parsed.get("result") if isinstance(parsed, dict) else None
                if isinstance(result_str, str) and result_str.strip():
                    repaired = _try_repair_json(result_str)
                    try:
                        inner = json.loads(repaired)
                        if isinstance(inner, dict) and (_EXPECTED_REVIEW_KEYS & set(inner.keys()) or "query_check" in inner or "rubrics_review_result" in inner or ("criteria" in inner and _SCHEMA_CRITERIA_NAMES & set((inner.get("criteria") or {}).keys()))):
                            claude_output = repaired
                            print("[步骤 4] 主机端：从 result 字符串中解析出审核 JSON，输出格式为 structured_output")
                        else:
                            claude_output = _try_repair_json(claude_output)
                    except json.JSONDecodeError:
                        claude_output = _try_repair_json(claude_output)
                else:
                    print("[步骤 4] 主机端：合法 JSON 但结构不符，正在尝试修复")
                    claude_output = _try_repair_json(claude_output)
        except json.JSONDecodeError as e:
            print(f"[步骤 4] 主机端 JSON 校验：失败 — {e}")
            print(f"[步骤 5] 正在尝试 JSON 修复...")
            claude_output = _try_repair_json(claude_output)

        final_parsed = None
        write_content = claude_output
        try:
            final_parsed = json.loads(claude_output)
            # 若仍是 API 包装（含 type/result 等）而非审核结果，不写入包装，改为写入占位说明
            if isinstance(final_parsed, dict) and ("type" in final_parsed or "result" in final_parsed) and not (_EXPECTED_REVIEW_KEYS & set(final_parsed.keys()) or "query_check" in final_parsed or "rubrics_review_result" in final_parsed):
                raw_preview = (final_parsed.get("result") or claude_output)[:2000]
                if isinstance(raw_preview, dict):
                    raw_preview = json.dumps(raw_preview, ensure_ascii=False)[:2000]
                write_content = json.dumps(
                    {
                        "_output_format": "structured_output",
                        "_missing": "模型未返回符合 schema 的 structured_output，仅返回了文本或包装",
                        "raw_result_preview": raw_preview if isinstance(raw_preview, str) else str(raw_preview),
                    },
                    ensure_ascii=False,
                )
                final_parsed = json.loads(write_content)
            else:
                _validate_review_output(final_parsed, output_file, strict_keys=not use_local)
        except json.JSONDecodeError as e:
            print(f"WARNING: 最终 JSON 解析失败，写入带标记的结果 — {e}")
            write_content = json.dumps(
                {"_parse_error": True, "raw_preview": claude_output[:2000], "error": str(e)},
                ensure_ascii=False,
            )
        except SystemExit:
            raise

        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(write_content)

        print(f"输出已保存到: {output_file}（{len(write_content)} 字节）")
        print(f"输出内容:\n{write_content}")

        if exit_code is not None and exit_code != 0:
            print(f"警告：Claude 以退出码 {exit_code} 结束，但已捕获输出。")

        if final_parsed is not None:
            return {"output_file": output_file, "parsed": final_parsed, "success": True}
        return {"output_file": output_file, "parsed": None, "success": False}

    finally:
        print(f"\n正在清理沙箱...")
        if sandbox:
            try:
                sandbox.refresh_data()
                if sandbox.state == SandboxState.STARTED:
                    sandbox.stop()
                    print("沙箱已停止（因 auto_delete_interval=0 将自动删除）。")
                else:
                    print(f"沙箱当前状态：{sandbox.state}")
            except Exception as e:
                print(f"警告：沙箱清理失败：{e}")
                try:
                    daytona.delete(sandbox)
                    print("沙箱已强制删除。")
                except Exception:
                    pass


if __name__ == "__main__":
    main()