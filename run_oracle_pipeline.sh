#!/usr/bin/env bash
# Oracle 校验（仅 TB1 / Terminal-Bench，与 oracle.py 一致）
# 已移除：harbor、uv tool install harbor、DAYTONA_API_KEY
set -euo pipefail

echo "==========【开始执行】Oracle 校验流程（TB1 / Terminal-Bench）=========="

echo "【步骤1】当前工作目录："
pwd

echo "【步骤1】当前目录文件列表："
ls -la || true

echo "【步骤2】检查 Python 环境（terminal-bench 要求 Python >= 3.12）..."
PYTHON_BIN=""
for _cand in python3.13 python3.12 python3; do
  if command -v "${_cand}" >/dev/null 2>&1; then
    if "${_cand}" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
      PYTHON_BIN="$(command -v "${_cand}")"
      break
    fi
  fi
done
if [ -z "${PYTHON_BIN}" ]; then
  echo "【错误】未找到 Python >= 3.12。PyPI 上 terminal-bench 不支持 3.11 及以下。"
  echo "请在镜像/Runner 中安装 python3.12+（例如 apt install python3.12 / 使用官方 python:3.12 镜像）。"
  command -v python3 >/dev/null 2>&1 && python3 --version || true
  exit 1
fi
echo "【信息】将使用: ${PYTHON_BIN}"
"${PYTHON_BIN}" --version

echo "【步骤3】安装 Oracle 校验脚本依赖（含 terminal-bench）..."
"${PYTHON_BIN}" -m ensurepip --upgrade 2>/dev/null || true
"${PYTHON_BIN}" -m pip install --upgrade pip --index-url=https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null || \
"${PYTHON_BIN}" -m pip install --upgrade pip --index-url=https://pypi.org/simple/
"${PYTHON_BIN}" -m pip install --user python-dotenv requests tos terminal-bench --index-url=https://mirrors.aliyun.com/pypi/simple/ || \
"${PYTHON_BIN}" -m pip install --user python-dotenv requests tos terminal-bench --index-url=https://pypi.org/simple/

export PATH="${HOME}/.local/bin:/workspace/.local/bin:${PATH}"

echo "【步骤3.1】校验 Python 依赖是否安装成功..."
"${PYTHON_BIN}" - <<'PY'
import sys
print("当前 Python 可执行文件:", sys.executable)
for m in ("dotenv", "requests", "tos"):
    __import__(m)
    print(f"[OK] {m}")
__import__("terminal_bench.cli.tb.main")
print("[OK] terminal_bench.cli.tb.main（与 oracle.py 默认 tb 调用一致）")
PY

echo "【步骤4】检查必要环境变量（TOS 下载 zip）..."
if [ -z "${VE_TOS_AK:-}" ]; then
  echo "【错误】环境变量 VE_TOS_AK 为空"
  exit 1
fi

if [ -z "${VE_TOS_SK:-}" ]; then
  echo "【错误】环境变量 VE_TOS_SK 为空"
  exit 1
fi

echo "【信息】VE_TOS_AK / VE_TOS_SK 已配置"
# 说明：oracle.py 仅 TB1，不再使用 Harbor/Daytona，故不要求 DAYTONA_API_KEY

echo "【步骤5】检查输入参数..."
RECORD_ID="$(parameters.record_id)"
TASK_ID="$(parameters.task_id)"
TOS_ENDPOINT="$(parameters.tos_endpoint)"
TOS_REGION="$(parameters.tos_region)"

if [ -z "${RECORD_ID}" ]; then
  echo "【错误】参数 record_id 为空"
  exit 1
fi

if [ -z "${TASK_ID}" ]; then
  echo "【错误】参数 task_id 为空"
  exit 1
fi

if [ -z "${TOS_ENDPOINT}" ]; then
  echo "【错误】参数 tos_endpoint 为空"
  exit 1
fi

if [ -z "${TOS_REGION}" ]; then
  echo "【错误】参数 tos_region 为空"
  exit 1
fi

ZIP_URL="tos://terminal-bench-internal/tb2_to_tb1/${TASK_ID}-tbench.zip"

echo "【信息】record_id    = ${RECORD_ID}"
echo "【信息】task_id      = ${TASK_ID}"
echo "【信息】tos_endpoint = ${TOS_ENDPOINT}"
echo "【信息】tos_region   = ${TOS_REGION}"
echo "【信息】拼接后的 zip_url = ${ZIP_URL}"

echo "【步骤6】定位 oracle.py 脚本..."
if [ -f "oracle.py" ]; then
  ORACLE_SCRIPT="oracle.py"
elif [ -f "tb/oracle.py" ]; then
  ORACLE_SCRIPT="tb/oracle.py"
else
  echo "【错误】未找到 oracle.py，当前目录和 tb/ 目录下都不存在"
  find . -maxdepth 2 -type f | sort || true
  exit 1
fi

echo "【信息】将使用脚本：${ORACLE_SCRIPT}"

echo "【步骤7】开始执行 Oracle 校验脚本..."
"${PYTHON_BIN}" "${ORACLE_SCRIPT}" \
  --record-id "${RECORD_ID}" \
  --zip-url "${ZIP_URL}" \
  --tos-endpoint "${TOS_ENDPOINT}" \
  --tos-region "${TOS_REGION}"

echo "【步骤8】Oracle 校验脚本执行完成"

echo "【步骤9】检查输出文件..."
ls -la || true
find . -maxdepth 3 -type f | sort || true

if [ ! -f return.json ]; then
  echo "【错误】未找到 return.json，说明 Oracle 校验流程未正常产出结果"
  exit 1
fi

echo "【步骤10】输出 return.json 内容："
cat return.json

echo "==========【执行结束】Oracle 校验流程完成 =========="
