"""资料入仓：校验来源字节与形状，把 NDJSON 原始行装入 raw_* 表。

raw 层只保存来源事实（含重复单据、互相矛盾的行），不在入仓时做业务推断；
每行附带来源文件、行号与内容哈希，供后续区分"重复单据"与"矛盾金额"。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb

DATASETS = ("bank", "statements", "assignments", "obligations", "nodes", "transfers")

# raw 层列顺序与各来源字段一致，另加 _line_no / _payload_hash 两个入仓技术列。
RAW_COLUMNS = {
    "bank": ["bank_id", "account", "kind", "amount_cents", "date", "coverage_key",
             "reference", "project", "building", "purpose"],
    "statements": ["coverage_key", "summary_id", "detail_ids", "complete", "received"],
    "assignments": ["assignment_id", "bank_id", "contract", "amount_cents", "purpose",
                    "effective"],
    "obligations": ["obligation_id", "contract", "kind", "building", "amount_cents",
                    "effective", "reference"],
    "nodes": ["proof_id", "building", "purpose", "amount_cents", "effective",
              "depends_on", "supersedes", "received"],
    "transfers": ["record_id", "contract", "from_building", "to_building", "effective",
                  "received"],
}

DDL = """
create table if not exists raw_bank(
    src_file varchar, line_no integer,
    bank_id varchar, account varchar, kind varchar, amount_cents bigint,
    date varchar, coverage_key varchar, reference varchar,
    project varchar, building varchar, purpose varchar,
    payload_hash varchar);
create table if not exists raw_statements(
    src_file varchar, line_no integer,
    coverage_key varchar, summary_id varchar, detail_ids_json varchar,
    complete boolean, received varchar, payload_hash varchar);
create table if not exists raw_assignments(
    src_file varchar, line_no integer,
    assignment_id varchar, bank_id varchar, contract varchar, amount_cents bigint,
    purpose varchar, effective varchar, payload_hash varchar);
create table if not exists raw_obligations(
    src_file varchar, line_no integer,
    obligation_id varchar, contract varchar, kind varchar, building varchar,
    amount_cents bigint, effective varchar, reference varchar, payload_hash varchar);
create table if not exists raw_nodes(
    src_file varchar, line_no integer,
    proof_id varchar, building varchar, purpose varchar, amount_cents bigint,
    effective varchar, depends_on_json varchar, supersedes varchar, received varchar,
    payload_hash varchar);
create table if not exists raw_transfers(
    src_file varchar, line_no integer,
    record_id varchar, contract varchar, from_building varchar, to_building varchar,
    effective varchar, received varchar, payload_hash varchar);
create table if not exists ingest_batch(
    batch_id integer, data_dir varchar, ingested_at varchar, datasets_json varchar);
"""


def _payload_hash(row: dict) -> str:
    canon = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def verify_source(directory: Path) -> list[dict]:
    """按 manifest 校验字节哈希、行数与字段形状，返回每个数据集的行对象列表。"""
    schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    datasets: dict[str, list[dict]] = {name: [] for name in DATASETS}
    seen = set()
    for entry in manifest["files"]:
        path = directory / entry["path"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("资料哈希不符：" + entry["path"])
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        fields = schema["datasets"][entry["dataset"]]["fields"]
        if len(rows) != entry["rows"]:
            raise ValueError("资料行数不符：" + entry["path"])
        for index, row in enumerate(rows, 1):
            if set(row) != set(fields):
                raise ValueError("字段不符：" + entry["path"] + ":" + str(index))
        if entry["dataset"] in seen:
            raise ValueError("manifest 中数据集重复：" + entry["dataset"])
        seen.add(entry["dataset"])
        datasets[entry["dataset"]] = rows
    missing = [name for name in DATASETS if name not in seen]
    if missing:
        raise ValueError("manifest 缺少数据集：" + ",".join(missing))
    return [{"dataset": name, "rows": rows} for name, rows in datasets.items()]


def _json_text(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scalar(value):
    """reference/supersedes 为 any 类型：标量原样存，复合值存 JSON 文本。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return None if value is None else str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def ingest(con: duckdb.DuckDBPyConnection, directory: Path) -> dict:
    """校验并装入 raw 层。重复执行会先清空 raw 层（入仓是幂等重建）。"""
    datasets = verify_source(directory)
    con.execute("begin")
    try:
        for stmt in DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        for name in DATASETS:
            con.execute(f"delete from raw_{name}")
        rows_by_name = {item["dataset"]: item["rows"] for item in datasets}
        for name in DATASETS:
            cols = RAW_COLUMNS[name]
            table_rows = []
            for line_no, row in enumerate(rows_by_name[name], 1):
                record = {"src_file": name + ".ndjson", "line_no": line_no}
                for col in cols:
                    value = row[col]
                    if col in ("detail_ids", "depends_on"):
                        record[col + "_json"] = _json_text(value)
                    elif col in ("reference", "supersedes"):
                        record[col] = _scalar(value)
                    else:
                        record[col] = value
                record["payload_hash"] = _payload_hash(row)
                table_rows.append(record)
            if table_rows:
                col_list = list(table_rows[0].keys())
                placeholders = ",".join(["?"] * len(col_list))
                con.executemany(
                    f"insert into raw_{name}({','.join(col_list)}) values({placeholders})",
                    [[r[c] for c in col_list] for r in table_rows],
                )
        summary = {name: len(rows_by_name[name]) for name in DATASETS}
        con.execute(
            "insert into ingest_batch values(?,?,?,?)",
            [int(con.execute("select coalesce(max(batch_id),0)+1 from ingest_batch").fetchone()[0]),
             str(directory),
             datetime.now().isoformat(timespec="seconds"),
             json.dumps(summary, ensure_ascii=False)],
        )
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    return summary


def open_db(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for stmt in DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    return con
