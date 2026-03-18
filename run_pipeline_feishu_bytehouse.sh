#!/bin/bash
# 流水线「自定义环境命令执行」用：传入飞书/ByteHouse 参数后执行写库脚本
# 注意：敏感信息建议用流水线「变量/密钥」配置，不要提交到 Git

set -e

echo "===== 1. 检查环境 ====="
python3 --version
pwd
ls -la

echo "===== 2. 安装依赖 ====="
python3 -m pip install --user -U pip
python3 -m pip install --user requests clickhouse-driver

echo "===== 3. 传入参数（飞书 + ByteHouse）====="
export FEISHU_APP_ID="${FEISHU_APP_ID:-cli_a98e38e7233cd00b}"
export FEISHU_APP_SECRET="${FEISHU_APP_SECRET:-Nk1tvrA5EmsGzAMhDYUYsflsRJ4iSNqH}"
export BITABLE_APP_TOKEN="${BITABLE_APP_TOKEN:-SsFHbRCHHa2FO0scv7QcJdNZnQb}"
export BITABLE_TABLE_ID="${BITABLE_TABLE_ID:-tblRAkMZWuarcT73}"

export BH_HOST="${BH_HOST:-tenant-2102482408-cn-beijing-public.bytehouse.volces.com}"
export BH_PORT="${BH_PORT:-19000}"
export BH_USER="${BH_USER:-bytehouse}"
export BH_PASSWORD="${BH_PASSWORD:-7mATMPiSVN:ufHPPacoNG}"
export BH_DATABASE="${BH_DATABASE:-task_db}"
export BH_VW_ID="${BH_VW_ID:-vw-2102482408-default}"

# 若流水线已注入 RECORD_ID 等，会优先使用
# export RECORD_ID="${RECORD_ID:-}"

echo "===== 4. 检查脚本 ====="
[ -f /workspace/pipeline_feishu_bytehouse.py ] || { echo "/workspace/pipeline_feishu_bytehouse.py 不存在"; exit 1; }

echo "===== 5. 执行写库 ====="
python3 /workspace/pipeline_feishu_bytehouse.py
