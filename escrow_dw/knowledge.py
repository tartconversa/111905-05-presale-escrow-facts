"""staging 层：与发布时点无关的来源整理。

完成三件事：
1. 推导每类资料的"知识日期"（监管方何时收到该证据）；
2. 按业务主键识别重复单据（内容完全一致）与矛盾单据（主键相同、内容不同），
   二者在 stg_* 与后续 rel_finding 中可以被明确区分；
3. 把 statements 的汇总/明细引用关系整理成 stg_statement_link，供发布时归并证据。

知识日期推导规则（来源没有 received 字段时的约定，见 docs/runbook.md）：
- statements / nodes / transfers：用自身的 received；
- bank：取引用该行的 statement（汇总或明细）的最早 received；
  没有任何 statement 引用时视为随单到账，用业务日期 date；
- assignments / obligations：来源无 received 字段，用 effective。
"""

from __future__ import annotations

import json

import duckdb

STG_DDL = """
create table if not exists stg_bank_row(
    bank_id varchar, account varchar, kind varchar, amount_cents bigint,
    date varchar, coverage_key varchar, reference varchar,
    project varchar, building varchar, purpose varchar,
    knowledge_date varchar, copies integer, conflict boolean, effective_row boolean,
    src_refs varchar);
create table if not exists stg_statement(
    coverage_key varchar, summary_id varchar, detail_ids_json varchar,
    complete boolean, received varchar);
create table if not exists stg_assignment(
    assignment_id varchar, bank_id varchar, contract varchar, amount_cents bigint,
    purpose varchar, effective varchar, knowledge_date varchar,
    copies integer, conflict boolean, effective_row boolean);
create table if not exists stg_obligation(
    obligation_id varchar, contract varchar, kind varchar, building varchar,
    amount_cents bigint, effective varchar, reference varchar, knowledge_date varchar,
    copies integer, conflict boolean, effective_row boolean);
create table if not exists stg_node(
    proof_id varchar, building varchar, purpose varchar, amount_cents bigint,
    effective varchar, depends_on_json varchar, supersedes varchar, received varchar,
    knowledge_date varchar, copies integer, conflict boolean, effective_row boolean);
create table if not exists stg_transfer(
    record_id varchar, contract varchar, from_building varchar, to_building varchar,
    effective varchar, received varchar, knowledge_date varchar,
    copies integer, conflict boolean, effective_row boolean);
"""

STG_TABLES = ("stg_bank_row", "stg_statement", "stg_assignment", "stg_obligation",
              "stg_node", "stg_transfer")


def _collapse(con, raw_table: str, key_col: str, value_cols: list[str],
              extra_cols: dict[str, str] | None = None) -> list[dict]:
    """按业务主键折叠 raw 行：完全相同的多份算重复单据（保留一份为有效行），
    主键相同但内容不同的各行全部标记 conflict 且均不作为有效行。"""
    rows = con.execute(f"select * from raw_{raw_table}").fetchall()
    cols = [d[0] for d in con.description]
    records = [dict(zip(cols, r)) for r in rows]
    groups: dict[str, list[dict]] = {}
    for rec in records:
        groups.setdefault(str(rec[key_col]), []).append(rec)
    out = []
    for key, members in groups.items():
        hashes = {m["payload_hash"] for m in members}
        conflict = len(hashes) > 1
        copies = len(members)
        # 同一主键下内容相同的行可能有多份；每种内容取一份代表行。
        representatives = {}
        for m in members:
            representatives.setdefault(m["payload_hash"], m)
        for rep in representatives.values():
            row = {col: rep[col] for col in value_cols}
            row.update(extra_cols or {})
            row["copies"] = copies
            row["conflict"] = conflict
            row["effective_row"] = not conflict
            row["src_refs"] = ",".join(
                f"{m['src_file']}:{m['line_no']}" for m in members)
            out.append(row)
    return out


def build_staging(con: duckdb.DuckDBPyConnection) -> dict:
    """重建 stg 层（幂等）。返回各表行数。"""
    for stmt in STG_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    for table in STG_TABLES:
        con.execute(f"delete from {table}")

    # statements：同一 coverage_key 允许多次到达（先汇总后补明细）；
    # 内容完全一致的重复声明只保留一份（幂等声明，不产生业务差异）。
    con.execute(
        """insert into stg_statement
           select distinct coverage_key, summary_id, detail_ids_json, complete,
                  received
           from raw_statements""")

    # bank 知识日期：引用该行的 statement 的最早 received；无引用则用业务日期。
    bank_rows = _collapse(
        con, "bank", "bank_id",
        ["bank_id", "account", "kind", "amount_cents", "date", "coverage_key",
         "reference", "project", "building", "purpose"])
    knowledge: dict[str, str] = {}
    for coverage_key, summary_id, detail_ids_json, received in con.execute(
            "select coverage_key, summary_id, detail_ids_json, received from raw_statements"
    ).fetchall():
        ids = [summary_id] if summary_id else []
        if detail_ids_json:
            ids.extend(json.loads(detail_ids_json))
        for bank_id in ids:
            if bank_id is None:
                continue
            prev = knowledge.get(str(bank_id))
            if prev is None or received < prev:
                knowledge[str(bank_id)] = received
    for row in bank_rows:
        row["knowledge_date"] = knowledge.get(row["bank_id"], row["date"])
        con.execute(
            """insert into stg_bank_row values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [row["bank_id"], row["account"], row["kind"], row["amount_cents"],
             row["date"], row["coverage_key"], row["reference"], row["project"],
             row["building"], row["purpose"], row["knowledge_date"], row["copies"],
             row["conflict"], row["effective_row"], row["src_refs"]])

    simple_specs = [
        ("assignments", "stg_assignment", "assignment_id",
         ["assignment_id", "bank_id", "contract", "amount_cents", "purpose", "effective"],
         "effective"),
        ("obligations", "stg_obligation", "obligation_id",
         ["obligation_id", "contract", "kind", "building", "amount_cents", "effective",
          "reference"], "effective"),
        ("nodes", "stg_node", "proof_id",
         ["proof_id", "building", "purpose", "amount_cents", "effective",
          "depends_on_json", "supersedes", "received"], "received"),
        ("transfers", "stg_transfer", "record_id",
         ["record_id", "contract", "from_building", "to_building", "effective",
          "received"], "received"),
    ]
    for raw_table, stg_table, key_col, value_cols, knowledge_col in simple_specs:
        rows = _collapse(con, raw_table, key_col, value_cols)
        for row in rows:
            row["knowledge_date"] = row[knowledge_col]
            placeholders = ",".join(["?"] * (len(value_cols) + 4))
            con.execute(
                f"insert into {stg_table} values({placeholders})",
                [row[c] for c in value_cols] +
                [row["knowledge_date"], row["copies"], row["conflict"],
                 row["effective_row"]])

    return {table: con.execute(f"select count(*) from {table}").fetchone()[0]
            for table in STG_TABLES}
