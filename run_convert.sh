#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONVERTER="$ROOT_DIR/harbor2tbbench/harbor2tbench.py"
MIRROR_SCRIPT="$ROOT_DIR/replace_python_mirrors.py"
DOWNLOAD_DIR="$ROOT_DIR/downloads"
PACKAGE_DIR="$ROOT_DIR/output_packages"
MIRROR_URL="${MIRROR_URL:-http://mirrors.aliyun.com/pypi/simple/}"

[[ -f "$CONVERTER" ]] || { echo "转换脚本不存在: $CONVERTER"; exit 1; }
[[ -f "$MIRROR_SCRIPT" ]] || { echo "源替换脚本不存在: $MIRROR_SCRIPT"; exit 1; }
[[ -d "$DOWNLOAD_DIR" ]] || { echo "下载目录不存在: $DOWNLOAD_DIR"; exit 1; }

ZIP_COUNT=$(find "$DOWNLOAD_DIR" -maxdepth 1 -type f -name '*.zip' | wc -l | tr -d ' ')
[[ "$ZIP_COUNT" == "1" ]] || { echo "downloads 目录下 zip 文件数量不为 1，当前为: $ZIP_COUNT"; exit 1; }

INPUT_ZIP="$(find "$DOWNLOAD_DIR" -maxdepth 1 -type f -name '*.zip' | head -n 1)"
TASK_ID="$(basename "$INPUT_ZIP" .zip)"

WORK_DIR="$ROOT_DIR/work/${TASK_ID}"
UNZIP_DIR="$WORK_DIR/unzip"

echo "INPUT_ZIP=$INPUT_ZIP"
echo "TASK_ID=$TASK_ID"

rm -rf "$WORK_DIR"
mkdir -p "$UNZIP_DIR" "$PACKAGE_DIR"

echo "[1/6] 解压输入文件..."
unzip -q "$INPUT_ZIP" -d "$UNZIP_DIR"

echo "[2/6] 定位 Harbor 任务目录..."
if [[ -f "$UNZIP_DIR/task.toml" ]]; then
  SRC_DIR="$UNZIP_DIR"
else
  TASK_TOML_PATH="$(find "$UNZIP_DIR" -type f -name task.toml | head -n 1 || true)"
  [[ -n "$TASK_TOML_PATH" ]] || { echo "未找到 task.toml，输入 zip 不是合法 Harbor 任务"; exit 1; }
  SRC_DIR="$(dirname "$TASK_TOML_PATH")"
fi
echo "SRC_DIR=$SRC_DIR"

echo "[3/6] 执行 harbor -> tbench 转换..."
python3 "$CONVERTER" "$SRC_DIR"

OUTPUT_DIR="${SRC_DIR}-tbench"
[[ -d "$OUTPUT_DIR" ]] || { echo "转换输出目录不存在: $OUTPUT_DIR"; exit 1; }
[[ -f "$OUTPUT_DIR/task.yaml" ]] || { echo "转换失败，缺少 task.yaml"; exit 1; }

echo "[4/6] 打包转换结果..."
OUTPUT_ZIP="$PACKAGE_DIR/${TASK_ID}-tbench.zip"
rm -f "$OUTPUT_ZIP"
python3 - "$OUTPUT_DIR" "$OUTPUT_ZIP" <<'PY'
import os, sys, zipfile
src_dir = sys.argv[1]
zip_path = sys.argv[2]
base_dir = os.path.dirname(src_dir)
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(src_dir):
        for f in files:
            full_path = os.path.join(root, f)
            zf.write(full_path, os.path.relpath(full_path, base_dir))
print(f"zip created: {zip_path}")
PY

echo "[5/6] 执行阿里云镜像替换..."
EXTRACT_DIR="$WORK_DIR/mirror_extracted"
TMP_ZIP="$PACKAGE_DIR/${TASK_ID}-tbench.tmp.zip"
CHANGE_LOG="$PACKAGE_DIR/${TASK_ID}-tbench.mirror-changes.log"
rm -rf "$EXTRACT_DIR"
mkdir -p "$EXTRACT_DIR"

python3 -c "
import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "$OUTPUT_ZIP" "$EXTRACT_DIR"

python3 "$MIRROR_SCRIPT" "$EXTRACT_DIR" --mirror-url "$MIRROR_URL" --verbose 2>&1 | tee "$CHANGE_LOG"

python3 - "$EXTRACT_DIR" "$TMP_ZIP" <<'PY'
import sys, zipfile
from pathlib import Path
src = Path(sys.argv[1]).resolve()
with zipfile.ZipFile(sys.argv[2], "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            zf.write(p, p.relative_to(src).as_posix())
print(f"repack done: {sys.argv[2]}")
PY

mv -f "$TMP_ZIP" "$OUTPUT_ZIP"

echo "[6/6] 校验输出压缩包..."
python3 -c "
import sys, zipfile
bad = zipfile.ZipFile(sys.argv[1]).testzip()
if bad: raise SystemExit(f'zip 损坏: {bad}')
print(f'校验通过: {sys.argv[1]}')
" "$OUTPUT_ZIP"

echo ""
echo "===== 转换完成 ====="
echo "输入文件:   $INPUT_ZIP"
echo "输出压缩包: $OUTPUT_ZIP"
echo "变更日志:   $CHANGE_LOG"
