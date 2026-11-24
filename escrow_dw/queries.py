"""下钻查询：面向复核人员的中文查询结果。

每个查询返回 dict（金额单位为分），CLI 负责渲染为文本或 JSON。
"""

from __future__ import annotations

import json
from collections import defaultdict

import duckdb


def _rows(con, sql, params=()):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def resolve_release(con, ref: str | None) -> str:
    if ref and ref != "latest":
        found = _rows(con, "select release_id from release where release_id=?",
                      [ref])
        if not found:
            raise ValueError(f"发布不存在：{ref}")
        return ref
    row = _rows(con, "select release_id from release order by created_at desc, "
                     "release_id desc limit 1")
    if not row:
        raise ValueError("还没有任何发布，请先执行 release 命令")
    return row[0]["release_id"]


def list_releases(con) -> list[dict]:
    return _rows(con, "select * from release order by release_id")


def release_info(con, release_id: str) -> dict:
    rows = _rows(con, "select * from release where release_id=?", [release_id])
    if not rows:
        raise ValueError(f"发布不存在：{release_id}")
    return rows[0]


def project_overview(con, release_id: str, project: str | None = None) -> dict:
    sql = "select * from rel_project_status where release_id=?"
    params = [release_id]
    if project:
        sql += " and project=?"
        params.append(project)
    projects = _rows(con, sql + " order by project", params)
    all_findings = _rows(
        con, "select severity, category, subject, amount_cents, message "
             "from rel_finding where release_id=?", [release_id])
    for p in projects:
        p["status_flags"] = [f for f in p["status_flags"].split(",") if f]
        name = p["project"]
        contracts = {r["contract"] for r in _rows(
            con, "select distinct contract from rel_contract_purpose "
                 "where release_id=? and project=?", [release_id, name])}
        buildings = {(r["building"], r["purpose"]) for r in _rows(
            con, "select distinct building, purpose from rel_fund_bucket "
                 "where release_id=? and project=?", [release_id, name])}
        node_subjects = {f"node:{b}:{pu}" for (b, pu) in buildings}

        def _related(f):
            subject = f["subject"] or ""
            if f":{name}" in subject or subject in node_subjects:
                return True
            return any(f":{c}" in subject for c in contracts)

        p["findings"] = [f for f in all_findings if _related(f)]
    return {"release_id": release_id, "projects": projects}


def contract_detail(con, release_id: str, contract: str) -> dict:
    coverage = _rows(con, "select * from rel_coverage where release_id=? "
                          "and contract=? order by obligation_id, purpose",
                     [release_id, contract])
    for row in coverage:
        row["reasons"] = json.loads(row["reasons_json"])
    obligations = _rows(con, "select * from rel_obligation where release_id=? "
                             "and contract=? order by obligation_id",
                        [release_id, contract])
    backings = _rows(con, "select * from rel_contract_purpose where release_id=? "
                          "and contract=? order by purpose, building",
                     [release_id, contract])
    assignments = _rows(
        con, "select a.assignment_id, a.bank_id, a.portion_id, a.amount_cents, "
             "a.state, a.note, p.evidence_state, p.member_ids "
             "from rel_assignment a left join rel_portion p "
             "on p.release_id=a.release_id and p.portion_id=a.portion_id "
             "where a.release_id=? and a.contract=? order by a.assignment_id",
        [release_id, contract])
    findings = _rows(con, "select severity, category, subject, amount_cents, "
                          "message from rel_finding where release_id=? and "
                          "subject like ? order by severity desc",
                     [release_id, f"%:{contract}"])
    return {"release_id": release_id, "contract": contract, "coverage": coverage,
            "obligations": obligations, "backings": backings,
            "assignments": assignments, "findings": findings}


def obligation_detail(con, release_id: str, obligation_id: str) -> dict:
    obligations = _rows(con, "select * from rel_obligation where release_id=? "
                             "and obligation_id=?", [release_id, obligation_id])
    coverage = _rows(con, "select * from rel_coverage where release_id=? "
                          "and obligation_id=?", [release_id, obligation_id])
    for row in coverage:
        row["reasons"] = json.loads(row["reasons_json"])
    return {"release_id": release_id, "obligation": obligations[0] if obligations
            else None, "coverage": coverage}


def account_view(con, release_id: str, account: str | None = None) -> dict:
    sql = "select * from rel_account_view where release_id=?"
    params = [release_id]
    if account:
        sql += " and account=?"
        params.append(account)
    accounts = _rows(con, sql + " order by account, project, purpose", params)
    buckets = _rows(con, "select * from rel_fund_bucket where release_id=?"
                         + (" and account=?" if account else "")
                         + " order by account, building, purpose",
                    params)
    return {"release_id": release_id, "accounts": accounts, "buckets": buckets}


def node_caps(con, release_id: str, building: str | None = None,
              purpose: str | None = None) -> dict:
    sql = "select * from rel_node_cap where release_id=?"
    params = [release_id]
    if building:
        sql += " and building=?"
        params.append(building)
    if purpose:
        sql += " and purpose=?"
        params.append(purpose)
    caps = _rows(con, sql + " order by building, purpose", params)
    for row in caps:
        row["valid_proof_ids"] = [x for x in row["valid_proof_ids"].split(",") if x]
        row["invalid_proof_ids"] = [x for x in row["invalid_proof_ids"].split(",")
                                    if x]
    return {"release_id": release_id, "node_caps": caps}


def findings(con, release_id: str, severity: str | None = None) -> dict:
    sql = "select * from rel_finding where release_id=?"
    params = [release_id]
    if severity:
        sql += " and severity=?"
        params.append(severity)
    rows = _rows(con, sql + " order by case severity when 'error' then 0 "
                            "when 'warn' then 1 else 2 end, category, subject",
                 params)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["severity"]] += 1
    return {"release_id": release_id, "counts": dict(counts), "findings": rows}


def portions(con, release_id: str, coverage_key: str | None = None) -> dict:
    sql = "select * from rel_portion where release_id=?"
    params = [release_id]
    if coverage_key:
        sql += " and coverage_key=?"
        params.append(coverage_key)
    rows = _rows(con, sql + " order by coverage_key, portion_id", params)
    for row in rows:
        row["member_ids"] = [x for x in row["member_ids"].split(",") if x]
    return {"release_id": release_id, "portions": rows}
