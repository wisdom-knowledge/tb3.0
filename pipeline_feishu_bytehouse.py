#!/usr/bin/env python3
"""
流水线「写入数据库」：从飞书多维表格读取字段，写入 ByteHouse task_events。

流程：连接飞书 + ByteHouse → 从飞书表格获取对应字段 → 映射后写入数据库。

字段映射：
  - id           = maidian_<随机 UUID>
  - record_id    = 飞书表格中的记录 ID
  - talent_id    = 飞书字段「提交人ID」（列名可配置）
  - event_time   = 当前时间
  - event_type   = task_submit
  - source_table = feishu_bitable
  - content      = 飞书字段「code_review_result」的内容

运行（流水线自定义命令）:
  pip install requests clickhouse-driver -q && python pipeline_feishu_bytehouse.py

环境变量（由流水线注入）:
  飞书: APP_ID/FEISHU_APP_ID, APP_SECRET/FEISHU_APP_SECRET,
        APP_TOKEN/BITABLE_APP_TOKEN, COMMIT_TABLE_ID/BITABLE_TABLE_ID
  ByteHouse: BH_HOST, BH_PORT, BH_USER, BH_PASSWORD, BH_DATABASE, BH_VW_ID
  可选: RECORD_ID — 指定飞书记录 ID，不传则取第一页第一条
        FEISHU_FIELD_TALENT_ID — 飞书表中「提交人ID」的列名，默认 提交人ID 或 talent_id
        FEISHU_FIELD_CONTENT   — 飞书表中「机审结果」的列名，默认 code_review_result
"""

import json
import os
import sys
import uuid
import requests
from datetime import datetime

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


# ---------- 配置 ----------
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

RECORD_ID = _env("RECORD_ID")
# 飞书列名：提交人ID、机审结果（若你表里列名不同，用环境变量覆盖）
FEISHU_FIELD_TALENT_ID = _env("FEISHU_FIELD_TALENT_ID") or "提交人ID"
FEISHU_FIELD_CONTENT = _env("FEISHU_FIELD_CONTENT") or "code_review_result"
DEFAULT_TALENT_ID = _env("DEFAULT_TALENT_ID") or "default_talent"


def get_feishu_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    r = requests.post(
        url,
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 token 失败: {data}")
    return data["tenant_access_token"]


def feishu_get_record(token: str, record_id: str) -> dict:
    """从飞书多维表格拉取一条记录。"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}"
        f"/tables/{BITABLE_TABLE_ID}/records/{record_id}"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书获取记录失败: {data}")
    return data.get("data", {}).get("record", {})


def feishu_get_first_record(token: str) -> dict | None:
    """拉取飞书表格第一页第一条记录。"""
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}"
        f"/tables/{BITABLE_TABLE_ID}/records?page_size=1"
    )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书查询失败: {data}")
    items = data.get("data", {}).get("items", [])
    return items[0] if items else None


def record_to_row(record: dict) -> tuple:
    """
    将飞书一条记录转为 task_events 表一行。
    返回 (id, record_id, talent_id, event_time, source_table, event_type, content)。
    """
    record_id = record.get("record_id", "")
    fields = record.get("fields", {})
    # 提交人ID：支持多种列名
    talent_id = (
        fields.get(FEISHU_FIELD_TALENT_ID)
        or fields.get("talent_id")
        or fields.get("提交人ID")
        or DEFAULT_TALENT_ID
    )
    if isinstance(talent_id, dict):
        talent_id = talent_id.get("text") or str(talent_id)
    talent_id = str(talent_id).strip() if talent_id else DEFAULT_TALENT_ID
    # content = code_review_result 字段内容
    content = fields.get(FEISHU_FIELD_CONTENT) or fields.get("code_review_result") or ""
    if isinstance(content, dict):
        content = content.get("text") or json.dumps(content, ensure_ascii=False)
    content = str(content) if content else ""

    event_time = datetime.now()
    event_type = "task_submit"
    source_table = "feishu_bitable"
    row_id =  str(uuid.uuid4())

    return (row_id, record_id, talent_id, event_time, source_table, event_type, content)


def write_to_bytehouse(rows):
    """批量写入 ByteHouse task_events。"""
    if not rows:
        return
    port = int(BH_PORT) if BH_PORT else 19000
    client = Client(
        host=BH_HOST,
        port=port,
        user=BH_USER,
        password=BH_PASSWORD,
        database=BH_DATABASE,
        secure=True,
        verify=False,
        settings={"virtual_warehouse": BH_VW_ID},
    )
    sql = """
    INSERT INTO task_db.task_events
    (id, record_id, talent_id, event_time, source_table, event_type, content)
    VALUES
    """
    client.execute(sql, rows)
    client.disconnect()


def main():
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
    for name, val in required:
        if not val:
            print(f"错误: 未设置环境变量 {name}", file=sys.stderr)
            sys.exit(1)

    print("1. 获取飞书 token ...")
    token = get_feishu_token()

    print("2. 从飞书多维表格获取记录 ...")
    if RECORD_ID:
        record = feishu_get_record(token, RECORD_ID)
    else:
        record = feishu_get_first_record(token)
        if not record:
            print("错误: 飞书表格暂无数据且未传 RECORD_ID", file=sys.stderr)
            sys.exit(1)

    record_id = record.get("record_id", "")
    print(f"    record_id: {record_id}")

    print("3. 映射字段并写入 ByteHouse ...")
    row = record_to_row(record)
    write_to_bytehouse([row])
    print(f"    已写入 1 条: id={row[0]}, event_type=task_submit, content 长度={len(row[6])}")
    print("完成。")


if __name__ == "__main__":
    main()
