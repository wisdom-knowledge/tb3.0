#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
流水线：读取本地机审结果 -> 回填飞书多维表格 -> 写入 ByteHouse

功能：
1. 从本地 result.json 读取机审结果
2. 从本地 record.json / 环境变量读取 record_id
3. 更新飞书多维表格当前记录的 code_review_result 字段
4. 读取该飞书记录的补充字段（如 千识TalentID、版本）
5. 将结果写入 ByteHouse task_events

运行示例：
python3 /workspace/pipeline_feishu_bytehouse.py \
  --result-file /workspace/result.json \
  --record-file /workspace/record.json
"""

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime

import requests

try:
    from clickhouse_driver import Client
except ImportError:
    print("请先安装: pip install clickhouse-driver", file=sys.stderr)
    sys.exit(1)


def _env(key: str, *alt: str) -> str:
    v = os.environ.get(key)
    if v:
        return v
    for k in alt:
        v = os.environ.get(k)
        if v:
            return v
    return ""


# ---------- 环境变量 ----------
FEISHU_APP_ID = _env("FEISHU_APP_ID", "APP_ID")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET", "APP_SECRET")
BITABLE_APP_TOKEN = _env("BITABLE_APP_TOKEN", "APP_TOKEN")
BITABLE_TABLE_ID = _env("BITABLE_TABLE_ID", "COMMIT_TABLE_ID")

BH_HOST = _env("BH_HOST")
BH_PORT = _env("BH_PORT")
BH_USER = _env("BH_USER")
BH_PASSWORD = _env("BH_PASSWORD")
BH_DATABASE = _env("BH_DATABASE")
BH_VW_ID = _env("BH_VW_ID")

RECORD_ID_ENV = _env("RECORD_ID")

# ---------- 飞书字段名 ----------
FEISHU_FIELD_TALENT_ID = _env("FEISHU_FIELD_TALENT_ID") or "千识TalentID"
FEISHU_FIELD_SOURCE_TABLE = _env("FEISHU_FIELD_SOURCE_TABLE") or "版本"
FEISHU_FIELD_CONTENT = _env("FEISHU_FIELD_CONTENT") or "code_review_result"

DEFAULT_TALENT_ID = _env("DEFAULT_TALENT_ID") or "default_talent"
DEFAULT_SOURCE_TABLE = _env("DEFAULT_SOURCE_TABLE") or "feishu_bitable"


def parse_args():
    parser = argparse.ArgumentParser(description="回填飞书 + 写入 ByteHouse")
    parser.add_argument(
        "--result-file",
        default="/workspace/result.json",
        help="机审结果文件路径，默认 /workspace/result.json",
    )
    parser.add_argument(
        "--record-file",
        default="/workspace/record.json",
        help="回填飞书记录文件路径，默认 /workspace/record.json",
    )
    parser.add_argument(
        "--skip-feishu-update",
        action="store_true",
        help="跳过飞书更新，仅写 ByteHouse",
    )
    return parser.parse_args()


def check_required_env():
    required = [
        ("FEISHU_APP_ID", FEISHU_APP_ID),
        ("FEISHU_APP_SECRET", FEISHU_APP_SECRET),
        ("BITABLE_APP_TOKEN", BITABLE_APP_TOKEN),
        ("BITABLE_TABLE_ID", BITABLE_TABLE_ID),
        ("BH_HOST", BH_HOST),
        ("BH_PORT", BH_PORT),
        ("BH_USER", BH_USER),
        ("BH_PASSWORD", BH_PASSWORD),
        ("BH_DATABASE", BH_DATABASE),
        ("BH_VW_ID", BH_VW_ID),
    ]
    missing = [name for name, val in required if not val]
    if missing:
        print(f"错误: 缺少环境变量: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def read_json_file(path: str) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def json_stringify(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def resolve_record_id(record_data: dict) -> str:
    """
    record_id 优先级：
    1. 环境变量 RECORD_ID
    2. record.json 顶层 record_id
    """
    rid = (RECORD_ID_ENV or "").strip()
    if rid:
        return rid

    rid = str(record_data.get("record_id", "")).strip()
    if rid:
        return rid

    return ""


def normalize_field_value(value) -> str:
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return str(value)

    if isinstance(value, dict):
        if "text" in value and value["text"] is not None:
            return str(value["text"])
        return json.dumps(value, ensure_ascii=False)

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, (str, int, float, bool)):
                parts.append(str(item))
            elif isinstance(item, dict):
                if "text" in item and item["text"] is not None:
                    parts.append(str(item["text"]))
                elif "name" in item and item["name"] is not None:
                    parts.append(str(item["name"]))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return ", ".join([p for p in parts if p])

    return str(value)


def get_feishu_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(
        url,
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def feishu_get_record(token: str, record_id: str) -> dict:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}"
        f"/tables/{BITABLE_TABLE_ID}/records/{record_id}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书记录失败: {data}")
    return data.get("data", {}).get("record", {})


def feishu_update_record(token: str, record_id: str, fields: dict):
    """
    更新飞书多维表格指定记录。
    """
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}"
        f"/tables/{BITABLE_TABLE_ID}/records/{record_id}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"fields": fields}

    resp = requests.put(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"更新飞书记录失败: {data}")
    return data


def safe_database_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValueError(f"非法数据库名: {name}")
    return name


def build_bytehouse_row(record: dict, review_content: str) -> tuple:
    """
    将飞书记录 + 本地机审结果 映射成 task_events 一行。
    """
    record_id = str(record.get("record_id", "")).strip()
    fields = record.get("fields", {}) or {}

    talent_id = normalize_field_value(fields.get(FEISHU_FIELD_TALENT_ID)).strip() or DEFAULT_TALENT_ID
    source_table = normalize_field_value(fields.get(FEISHU_FIELD_SOURCE_TABLE)).strip() or DEFAULT_SOURCE_TABLE

    event_time = datetime.now()
    event_type = "task_submit"
    row_id = str(uuid.uuid4())

    return (
        row_id,
        record_id,
        talent_id,
        event_time,
        source_table,
        event_type,
        review_content,
    )


def write_to_bytehouse(rows):
    if not rows:
        return

    db_name = safe_database_name(BH_DATABASE)
    port = int(BH_PORT) if BH_PORT else 19000

    client = Client(
        host=BH_HOST,
        port=port,
        user=BH_USER,
        password=BH_PASSWORD,
        database=db_name,
        secure=True,
        verify=False,
        settings={"virtual_warehouse": BH_VW_ID},
    )

    sql = f"""
    INSERT INTO {db_name}.task_events
    (id, record_id, talent_id, event_time, source_table, event_type, content)
    VALUES
    """
    client.execute(sql, rows)
    client.disconnect()


def main():
    args = parse_args()
    check_required_env()

    print("===== 1. 读取本地结果文件 =====")
    result_obj = read_json_file(args.result_file)
    review_content = json_stringify(result_obj)
    print(f"result 文件: {args.result_file}")
    print(f"result 长度: {len(review_content)}")

    print("===== 2. 读取 record 文件 =====")
    record_data = read_json_file(args.record_file)
    print(f"record 文件: {args.record_file}")

    record_id = resolve_record_id(record_data)
    print(f"解析得到 RECORD_ID: {record_id or '<空>'}")

    if not record_id:
        print("错误: 未获取到 RECORD_ID，无法精确回填飞书记录", file=sys.stderr)
        sys.exit(1)

    print("===== 3. 获取飞书 token =====")
    token = get_feishu_token()

    if not args.skip_feishu_update:
        print("===== 4. 回填飞书 code_review_result =====")
        update_fields = {
            FEISHU_FIELD_CONTENT: review_content
        }
        feishu_update_record(token, record_id, update_fields)
        print("飞书记录更新成功")
    else:
        print("===== 4. 跳过飞书更新 =====")

    print("===== 5. 拉取飞书记录，补齐入库字段 =====")
    record = feishu_get_record(token, record_id)
    print("飞书记录拉取成功")

    print("===== 6. 组装 ByteHouse 数据 =====")
    row = build_bytehouse_row(record, review_content)
    print(f"row record_id={row[1]}")
    print(f"row talent_id={row[2]}")
    print(f"row source_table={row[4]}")
    print(f"content length={len(row[6])}")

    print("===== 7. 写入 ByteHouse =====")
    write_to_bytehouse([row])
    print(f"已写入 1 条数据，id={row[0]}")

    print("===== 8. 完成 =====")


if __name__ == "__main__":
    main()