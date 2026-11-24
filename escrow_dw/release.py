"""发布生成：在指定的知识时点与业务时点下，计算资金、义务、节点额度与覆盖关系。

一次发布 = 一组同一时点口径下原子写入的 rel_* 表，保证同一发布内
资金解释与覆盖解释一致（不会看到到账已冲正而覆盖仍显示充足）。

时点口径：
- 知识时点 K：只使用 knowledge_date <= K 的证据（资料到达视角）；
- 业务时点 D：只使用业务日期 <= D 的事实（业务发生视角）。
as-of 发布 K=D=某日；月度发布 D=月末、K 不设限（用已收到的全部资料解释该业务月份）。
"""

from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import datetime

import duckdb

INFO, WARN, ERROR = "info", "warn", "error"


def _yuan(cents) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) / 100:,.2f} 元"

RELEASE_DDL = """
create table if not exists release(
    release_id varchar, mode varchar, knowledge_cutoff varchar, business_cutoff varchar,
    label varchar, created_at varchar);
create table if not exists rel_portion(
    release_id varchar, portion_id varchar, coverage_key varchar, kind varchar,
    account varchar, project varchar, building varchar, purpose varchar,
    amount_cents bigint, live_cents bigint, evidence_state varchar,
    business_date varchar, knowledge_date varchar, member_ids varchar, reference varchar);
create table if not exists rel_assignment(
    release_id varchar, assignment_id varchar, contract varchar, bank_id varchar,
    portion_id varchar, amount_cents bigint, state varchar, note varchar);
create table if not exists rel_fund_bucket(
    release_id varchar, account varchar, project varchar, building varchar, purpose varchar,
    receipts_live bigint, payments_live bigint, refunds_live bigint, retained bigint,
    assigned_live bigint, assigned_disputed bigint, unassigned_pool bigint,
    excess_outflow bigint, pending_funds bigint);
create table if not exists rel_obligation(
    release_id varchar, obligation_id varchar, contract varchar, kind varchar,
    building varchar, amount_cents bigint, status varchar, settled_by varchar,
    note varchar);
create table if not exists rel_node_cap(
    release_id varchar, building varchar, purpose varchar, cap_cents bigint,
    payments_cents bigint, room_cents bigint, breach_cents bigint,
    valid_proof_ids varchar, invalid_proof_ids varchar);
create table if not exists rel_coverage(
    release_id varchar, obligation_id varchar, contract varchar, purpose varchar,
    building varchar, claim_cents bigint, certain_cents bigint, pending_cents bigint,
    gap_cents bigint, reasons_json varchar);
create table if not exists rel_contract_purpose(
    release_id varchar, contract varchar, project varchar, purpose varchar,
    building varchar, assigned_live bigint, refunds_paid bigint,
    open_refund_reserve bigint, attributed_payments bigint,
    exposure_pending bigint, misaligned_pending bigint, certain_cents bigint,
    note varchar);
create table if not exists rel_project_status(
    release_id varchar, project varchar, claim_cents bigint, certain_cents bigint,
    pending_cents bigint, gap_cents bigint, collective_shortfall_cents bigint,
    retained_cents bigint, unassigned_retained_cents bigint, status_flags varchar);
create table if not exists rel_account_view(
    release_id varchar, account varchar, project varchar, purpose varchar,
    receipts_live bigint, payments_live bigint, refunds_live bigint, retained bigint,
    assigned_live bigint, unassigned_retained bigint);
create table if not exists rel_finding(
    release_id varchar, severity varchar, category varchar, subject varchar,
    amount_cents bigint, message varchar);
"""

REL_TABLES = ("rel_portion", "rel_assignment", "rel_fund_bucket", "rel_obligation",
              "rel_node_cap", "rel_coverage", "rel_contract_purpose",
              "rel_project_status", "rel_account_view", "rel_finding")

FUND_KINDS = ("receipt", "payment", "refund")


def ensure_release_tables(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in RELEASE_DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)


def month_end(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year:04d}-{mon:02d}-{calendar.monthrange(year, mon)[1]:02d}"


class _Findings:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, severity: str, category: str, subject: str, amount, message: str):
        self.rows.append({"severity": severity, "category": category,
                          "subject": subject, "amount_cents": amount,
                          "message": message})

    def has(self, category: str, subject: str) -> bool:
        return any(r["category"] == category and r["subject"] == subject
                   for r in self.rows)


def _rows(con, sql: str, params=()) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def compute_release(con, knowledge_cutoff: str, business_cutoff: str) -> dict:
    """纯计算：给定 (K, D) 返回全部 rel_* 行。不写库，便于测试。"""
    K = knowledge_cutoff or "9999-12-31"
    D = business_cutoff
    findings = _Findings()

    bank_rows = [r for r in _rows(con, "select * from stg_bank_row")
                 if r["knowledge_date"] <= K]
    statements = [r for r in _rows(con, "select * from stg_statement")
                  if r["received"] <= K]
    assignments = [r for r in _rows(con, "select * from stg_assignment")
                   if r["knowledge_date"] <= K and r["effective"] <= D]
    obligations = [r for r in _rows(con, "select * from stg_obligation")
                   if r["knowledge_date"] <= K and r["effective"] <= D]
    nodes = [r for r in _rows(con, "select * from stg_node")
             if r["knowledge_date"] <= K]
    transfers = [r for r in _rows(con, "select * from stg_transfer")
                 if r["knowledge_date"] <= K and r["effective"] <= D]

    # ---- 重复单据与矛盾单据：在数据集中可区分 ----
    conflict_seen: set[str] = set()
    for dataset, rows, key in (("bank", bank_rows, "bank_id"),
                               ("assignments", assignments, "assignment_id"),
                               ("obligations", obligations, "obligation_id"),
                               ("nodes", nodes, "proof_id"),
                               ("transfers", transfers, "record_id")):
        for r in rows:
            if r["conflict"]:
                group_key = f"{dataset}:{r[key]}"
                if group_key in conflict_seen:
                    continue
                conflict_seen.add(group_key)
                findings.add(ERROR, "conflicting_document", group_key,
                             r.get("amount_cents"),
                             f"{dataset} 单据 {r[key]} 存在主键相同但内容不同的矛盾版本，"
                             "相关金额全部按待核实处理")
            elif r["copies"] > 1:
                findings.add(INFO, "duplicate_document", f"{dataset}:{r[key]}",
                             r.get("amount_cents"),
                             f"{dataset} 单据 {r[key]} 收到 {r['copies']} 份内容一致的重复，"
                             "只按一份生效")

    known_bank_ids = {r["bank_id"] for r in bank_rows}
    effective_bank = [r for r in bank_rows if r["effective_row"]]

    # ---- 冲正：减少被冲正行的可用性，不删除原始银行事实 ----
    reversal_of: dict[str, int] = defaultdict(int)
    for r in effective_bank:
        if r["kind"] != "reversal" or r["date"] > D:
            continue
        target = r["reference"]
        if not target or target not in known_bank_ids:
            findings.add(ERROR, "reversal_target_unknown", f"bank:{r['bank_id']}",
                         r["amount_cents"],
                         f"冲正行 {r['bank_id']} 引用的原行 {target} 未知")
            continue
        reversal_of[target] += r["amount_cents"]
    for target, amount in reversal_of.items():
        original = next((r for r in effective_bank if r["bank_id"] == target), None)
        if original is not None and amount > original["amount_cents"]:
            findings.add(ERROR, "reversal_exceeds_original", f"bank:{target}",
                         amount - original["amount_cents"],
                         f"对 {target} 的冲正合计超过原行金额")

    # ---- 证据归并：同一 coverage_key 的汇总与明细是同一资金的两种证据，只算一次 ----
    fund_rows = [r for r in effective_bank if r["kind"] in FUND_KINDS + ("internal",)]
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in fund_rows:
        by_key[r["coverage_key"]].append(r)
    stmts_by_key: dict[str, list[dict]] = defaultdict(list)
    for s in statements:
        stmts_by_key[s["coverage_key"]].append(s)

    portions: list[dict] = []
    row_to_portion: dict[str, str] = {}

    def _add_portion(key, kind, governing, members, amount, state, date,
                     suffix=""):
        pid = f"PT-{key}-{governing['bank_id']}{suffix}"
        portion = {"portion_id": pid, "coverage_key": key, "kind": kind,
                   "account": governing["account"], "project": governing["project"],
                   "building": governing["building"], "purpose": governing["purpose"],
                   "amount_cents": amount, "evidence_state": state,
                   "business_date": date, "governing_id": governing["bank_id"],
                   "knowledge_date": max(m["knowledge_date"] for m in members),
                   "member_ids": sorted(m["bank_id"] for m in members),
                   "reference": governing["reference"]}
        portions.append(portion)
        for m in members:
            row_to_portion[m["bank_id"]] = pid
        return portion

    for key, rows in sorted(by_key.items()):
        kinds = {r["kind"] for r in rows}
        kind = kinds.pop() if len(kinds) == 1 else "mixed"
        if kind == "mixed":
            findings.add(ERROR, "coverage_kind_mixed", f"coverage:{key}", None,
                         f"资金覆盖 {key} 中混有不同业务性质的银行记录")
        chain = sorted(stmts_by_key.get(key, []),
                       key=lambda s: (s["received"], s["complete"]))
        # 同一覆盖在同一到达时间出现内容不同的声明：矛盾，需人工核实
        by_received: dict[str, set] = defaultdict(set)
        for s in chain:
            by_received[s["received"]].add(
                (s["summary_id"], s["detail_ids_json"], s["complete"]))
        for received, variants in by_received.items():
            if len(variants) > 1:
                findings.add(ERROR, "statement_conflict", f"coverage:{key}", None,
                             f"资金覆盖 {key} 在 {received} 收到内容矛盾的汇总声明")
        latest = chain[-1] if chain else None
        if latest is None:
            for r in rows:  # 无汇总声明：逐笔各自独立成证
                _add_portion(key, kind, r, [r], r["amount_cents"], "standalone",
                             r["date"])
            continue
        summary_id = latest["summary_id"]
        detail_ids = list(json.loads(latest["detail_ids_json"] or "[]"))
        referenced = ([summary_id] if summary_id else []) + detail_ids
        missing = [i for i in referenced if i not in known_bank_ids]
        if missing:
            findings.add(ERROR, "statement_ref_missing", f"coverage:{key}", None,
                         f"汇总声明引用了未收到的银行记录：{','.join(missing)}")
        members = [r for r in rows if r["bank_id"] in set(referenced)]
        summary_rows = [r for r in members if r["bank_id"] == summary_id]
        extras = [r for r in rows if r["bank_id"] not in set(referenced)]
        for r in extras:
            findings.add(WARN, "statement_extra_row", f"bank:{r['bank_id']}",
                         r["amount_cents"],
                         f"银行记录 {r['bank_id']} 声称属于 {key} 但未被汇总声明引用")
            _add_portion(key, kind, r, [r], r["amount_cents"], "disputed", r["date"])
        if not summary_rows:
            if members:
                findings.add(ERROR, "summary_row_missing", f"coverage:{key}", None,
                             f"资金覆盖 {key} 缺少汇总行 {summary_id}，"
                             "明细小计不能替代全量")
                governing = members[0]
                _add_portion(key, kind, governing, members,
                             sum(m["amount_cents"] for m in members), "disputed",
                             governing["date"])
            continue
        governing = summary_rows[0]
        state = "summary_only"
        if latest["complete"]:
            detail_sum = sum(m["amount_cents"] for m in members
                             if m["bank_id"] != summary_id)
            if detail_sum != governing["amount_cents"]:
                findings.add(ERROR, "summary_detail_mismatch", f"coverage:{key}",
                             abs(detail_sum - governing["amount_cents"]),
                             f"资金覆盖 {key} 明细合计 {_yuan(detail_sum)} 与汇总 "
                             f"{_yuan(governing['amount_cents'])} 不符")
                state = "disputed"
            else:
                state = "confirmed"
        elif detail_ids:
            findings.add(INFO, "detail_incomplete", f"coverage:{key}", None,
                         f"资金覆盖 {key} 明细未齐，不以其小计替代全量，"
                         "以汇总金额为准")
        _add_portion(key, kind, governing, members, governing["amount_cents"],
                     state, governing["date"])

    # 矛盾单据（主键相同、内容不同）：不作为有效资金，但按待核实资金单列，
    # 使"互相矛盾的金额"在数据集中可见且与重复单据相区别
    stmt_referenced = set()
    for s in statements:
        if s["summary_id"]:
            stmt_referenced.add(s["summary_id"])
        stmt_referenced.update(json.loads(s["detail_ids_json"] or "[]"))
    conflict_groups: dict[str, list[dict]] = defaultdict(list)
    for r in bank_rows:
        if r["conflict"] and r["kind"] in FUND_KINDS + ("internal",):
            conflict_groups[r["bank_id"]].append(r)
    for bank_id, versions in sorted(conflict_groups.items()):
        if bank_id in stmt_referenced:
            continue  # 汇总/明细路径已按证据矛盾处理
        governing = versions[0]
        amount = max(v["amount_cents"] for v in versions)
        kinds = {v["kind"] for v in versions}
        _add_portion(governing["coverage_key"],
                     kinds.pop() if len(kinds) == 1 else "mixed",
                     governing, versions, amount, "disputed",
                     governing["date"], suffix="-CONFLICT")

    # ---- 资金桶：可用金额 = 到账(冲正后) - 工程支出 - 实际退款 ----
    buckets: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    portion_live: dict[str, int] = {}
    for p in portions:
        live = p["amount_cents"] - sum(reversal_of.get(m, 0) for m in p["member_ids"])
        live = max(0, live)
        portion_live[p["portion_id"]] = live
        p["live_cents"] = live
        bkey = (p["account"], p["project"], p["building"], p["purpose"])
        bucket = buckets[bkey]
        if p["kind"] == "internal":
            findings.add(INFO, "internal_excluded", p["portion_id"], live,
                         f"内部调拨 {p['portion_id']} 不产生新的购房款，不计入监管资金")
            continue
        if p["kind"] == "mixed" or p["evidence_state"] == "disputed":
            bucket["pending_funds"] += live
            continue
        if p["business_date"] > D:
            continue
        if p["kind"] == "receipt":
            bucket["receipts"] += live
        elif p["kind"] == "payment":
            bucket["payments"] += live
        elif p["kind"] == "refund":
            bucket["refunds"] += live
    for bkey, bucket in buckets.items():
        bucket["retained"] = bucket["receipts"] - bucket["payments"] - bucket["refunds"]

    # ---- 归集凭证：金额不得超过对应资金可归集部分；未归集余额保留账户层 ----
    row_live: dict[str, int] = {}
    for r in effective_bank:
        row_live[r["bank_id"]] = max(0, r["amount_cents"]
                                     - reversal_of.get(r["bank_id"], 0))
    portion_by_id = {p["portion_id"]: p for p in portions}
    assignment_rows: list[dict] = []
    assign_by_row: dict[str, list[dict]] = defaultdict(list)
    for a in assignments:
        assign_by_row[a["bank_id"]].append(a)
    for a in assignments:
        state, note, portion_id = "ok", "", None
        if not a["effective_row"]:
            state, note = "disputed", "归集凭证存在矛盾版本"
        elif a["bank_id"] not in known_bank_ids:
            state, note = "target_unknown", "归集引用的银行记录未知"
            findings.add(ERROR, "assignment_target_unknown",
                         f"assignment:{a['assignment_id']}", a["amount_cents"],
                         f"归集 {a['assignment_id']} 引用的银行记录 "
                         f"{a['bank_id']} 未知")
        elif a["bank_id"] not in row_to_portion:
            state, note = "target_not_fund", "归集目标不是有效资金行"
        else:
            portion_id = row_to_portion[a["bank_id"]]
            portion = portion_by_id[portion_id]
            if portion["evidence_state"] == "disputed":
                state, note = "disputed", "对应资金证据存在矛盾"
            elif portion["business_date"] > D:
                state, note = "target_unconfirmed", "对应资金在业务截止后才到账"
                findings.add(WARN, "assignment_target_unconfirmed",
                             f"assignment:{a['assignment_id']}",
                             a["amount_cents"],
                             f"归集 {a['assignment_id']} 对应资金在业务截止后才到账")
            elif (portion["evidence_state"] == "summary_only"
                  and a["bank_id"] != portion["governing_id"]):
                # 明细尚未确认时，针对明细行的归集只能按待核实处理
                state, note = "target_unconfirmed", "明细未补齐确认，归集待核实"
                findings.add(WARN, "assignment_target_unconfirmed",
                             f"assignment:{a['assignment_id']}",
                             a["amount_cents"],
                             f"归集 {a['assignment_id']} 针对尚未确认的明细行 "
                             f"{a['bank_id']}")
        assignment_rows.append({"assignment_id": a["assignment_id"],
                                "contract": a["contract"], "bank_id": a["bank_id"],
                                "portion_id": portion_id, "purpose": a["purpose"],
                                "amount_cents": a["amount_cents"], "state": state,
                                "note": note})
    # 超额检查：同一资金行、同一资金覆盖上的归集合计都不得超过可用部分
    overdrawn_rows, overdrawn_portions = set(), set()
    for bank_id, group in assign_by_row.items():
        total = sum(a["amount_cents"] for a in group)
        if bank_id in row_live and total > row_live[bank_id]:
            overdrawn_rows.add(bank_id)
            findings.add(ERROR, "assignment_overdraw", f"bank:{bank_id}",
                         total - row_live[bank_id],
                         f"资金行 {bank_id} 上归集合计 {_yuan(total)} 超过其可用 "
                         f"{_yuan(row_live[bank_id])}")
    assign_by_portion: dict[str, list[dict]] = defaultdict(list)
    for ar in assignment_rows:
        if ar["portion_id"]:
            assign_by_portion[ar["portion_id"]].append(ar)
    for pid, group in assign_by_portion.items():
        total = sum(a["amount_cents"] for a in group)
        if total > portion_live.get(pid, 0):
            overdrawn_portions.add(pid)
            findings.add(ERROR, "assignment_overdraw", f"portion:{pid}",
                         total - portion_live.get(pid, 0),
                         f"资金覆盖 {pid} 上归集合计 {_yuan(total)} 超过其可用 "
                         f"{_yuan(portion_live.get(pid, 0))}")
    for ar in assignment_rows:
        if ar["state"] == "ok" and (ar["bank_id"] in overdrawn_rows
                                    or ar["portion_id"] in overdrawn_portions):
            ar["state"] = "overdrawn"
            ar["note"] = "归集合计超过资金可用部分，相关归集全部待核实"

    for ar in assignment_rows:
        if not ar["portion_id"]:
            continue
        portion = portion_by_id[ar["portion_id"]]
        bkey = (portion["account"], portion["project"], portion["building"],
                portion["purpose"])
        if ar["state"] == "ok":
            buckets[bkey]["assigned_live"] += ar["amount_cents"]
        else:
            buckets[bkey]["assigned_disputed"] += ar["amount_cents"]
    for bkey, bucket in buckets.items():
        bucket["unassigned_pool"] = max(
            0, bucket["receipts"] - bucket["assigned_live"]
            - bucket["assigned_disputed"])

    # ---- 合同楼栋归属：以已收到且已生效的最新变更为准 ----
    transfer_by_contract: dict[str, list[dict]] = defaultdict(list)
    for t in transfers:
        if not t["effective_row"]:
            continue  # 矛盾版本已在通用检查中记录，不予采用
        transfer_by_contract[t["contract"]].append(t)
    current_building: dict[str, str] = {}
    for contract, items in transfer_by_contract.items():
        latest = max(items, key=lambda t: (t["effective"], t["received"],
                                           t["record_id"]))
        current_building[contract] = latest["to_building"]
        findings.add(INFO, "contract_building_changed", f"contract:{contract}", None,
                     f"合同 {contract} 自 {latest['from_building']} 变更至 "
                     f"{latest['to_building']}（{latest['effective']} 生效），"
                     "相应义务、可归集资金与节点覆盖已按新楼栋重新解释")

    # ---- 义务状态：退款申请只产生义务，银行实际退款才减少资金 ----
    obl_by_id = {o["obligation_id"]: o for o in obligations if o["effective_row"]}
    refund_paid_to: dict[str, list[dict]] = defaultdict(list)
    for p in portions:
        if p["kind"] != "refund" or p["live_cents"] <= 0 or p["business_date"] > D:
            continue
        ref = p["reference"]
        if ref and ref in obl_by_id and obl_by_id[ref]["kind"] == "refund_requested":
            refund_paid_to[ref].append(p)
        else:
            findings.add(ERROR, "refund_request_unknown", p["portion_id"],
                         p["live_cents"],
                         f"实际退款 {p['portion_id']} 引用的退款申请 {ref} 未知")
    cancel_of: dict[str, dict] = {}
    for o in obligations:
        if o["kind"] != "refund_cancelled":
            continue
        ref = o["reference"]
        if not ref or ref not in obl_by_id:
            findings.add(ERROR, "refund_cancel_unknown",
                         f"obligation:{o['obligation_id']}", o["amount_cents"],
                         f"退款撤销 {o['obligation_id']} 引用的原申请 {ref} 未知")
            continue
        cancel_of[ref] = o
    obligation_rows: list[dict] = []
    open_refund_reserve: dict[tuple, int] = defaultdict(int)  # (contract, building)
    for o in obligations:
        if o["kind"] == "refund_cancelled":
            continue
        oid = o["obligation_id"]
        building = current_building.get(o["contract"], o["building"])
        status, settled_by, note = "open", None, ""
        if not o["effective_row"]:
            status, note = "disputed", "义务单据存在矛盾版本"
        elif o["kind"] == "refund_requested":
            paid = refund_paid_to.get(oid, [])
            if len(paid) > 1:
                findings.add(ERROR, "refund_settled_twice", f"obligation:{oid}",
                             sum(p["live_cents"] for p in paid),
                             f"退款申请 {oid} 被多笔银行退款结算")
            if paid:
                status, settled_by = "settled", paid[0]["portion_id"]
                if oid in cancel_of:
                    findings.add(WARN, "refund_cancel_after_settle",
                                 f"obligation:{oid}", o["amount_cents"],
                                 f"退款申请 {oid} 已结算后又收到撤销")
            elif oid in cancel_of:
                status = "cancelled"
        if status == "open" and o["kind"] == "refund_requested":
            open_refund_reserve[(o["contract"], building)] += o["amount_cents"]
        obligation_rows.append({"obligation_id": oid, "contract": o["contract"],
                                "kind": o["kind"], "building": building,
                                "amount_cents": o["amount_cents"], "status": status,
                                "settled_by": settled_by, "note": note})

    # ---- 节点额度：按楼栋与用途限定；被更正的证明不再提供额度 ----
    node_by_id = {n["proof_id"]: n for n in nodes if n["effective_row"]}
    superseded = set()
    for n in nodes:
        if n["effective_row"] and n["supersedes"] and n["effective"] <= D:
            superseded.add(n["supersedes"])
    valid_memo: dict[str, bool] = {}

    def _node_valid(pid: str, stack: tuple = ()) -> bool:
        if pid in valid_memo:
            return valid_memo[pid]
        n = node_by_id.get(pid)
        if n is None:
            return False
        if pid in stack:
            findings.add(ERROR, "node_dependency_cycle", f"nodes:{pid}", None,
                         f"工程证明 {pid} 的前序依赖形成循环")
            valid_memo[pid] = False
            return False
        ok = n["effective"] <= D and pid not in superseded
        if ok:
            for dep in json.loads(n["depends_on_json"] or "[]"):
                if dep not in node_by_id:
                    findings.add(ERROR, "node_dependency_missing", f"nodes:{pid}",
                                 None,
                                 f"工程证明 {pid} 依赖的前序证明 {dep} 未收到")
                    ok = False
                elif not _node_valid(dep, stack + (pid,)):
                    findings.add(ERROR, "node_dependency_invalid", f"nodes:{pid}",
                                 None,
                                 f"工程证明 {pid} 依赖的前序证明 {dep} 已失效")
                    ok = False
        valid_memo[pid] = ok
        return ok

    caps: dict[tuple, int] = defaultdict(int)
    valid_proofs: dict[tuple, list] = defaultdict(list)
    invalid_proofs: dict[tuple, list] = defaultdict(list)
    for n in nodes:
        if not n["effective_row"]:
            continue
        key = (n["building"], n["purpose"])
        if _node_valid(n["proof_id"]):
            caps[key] += n["amount_cents"]
            valid_proofs[key].append(n["proof_id"])
        else:
            invalid_proofs[key].append(n["proof_id"])
    payments_bp: dict[tuple, int] = defaultdict(int)
    for p in portions:
        if p["kind"] == "payment" and p["evidence_state"] != "disputed" \
                and p["business_date"] <= D:
            payments_bp[(p["building"], p["purpose"])] += p["live_cents"]
    node_cap_rows: list[dict] = []
    cap_keys = set(caps) | set(payments_bp)
    for key in sorted(cap_keys):
        cap = caps.get(key, 0)
        paid = payments_bp.get(key, 0)
        breach = max(0, paid - cap)
        room = max(0, cap - paid)
        if breach > 0:
            findings.add(ERROR, "node_cap_breach", f"node:{key[0]}:{key[1]}",
                         breach,
                         f"{key[0]}/{key[1]} 已付款 {_yuan(paid)} 超出当前有效节点额度 "
                         f"{_yuan(cap)}，超出部分已失去合法资金支持；"
                         "未发生的支付计划不得沿用旧额度")
        node_cap_rows.append({"building": key[0], "purpose": key[1],
                              "cap_cents": cap, "payments_cents": paid,
                              "room_cents": room, "breach_cents": breach,
                              "valid_proof_ids": sorted(valid_proofs.get(key, [])),
                              "invalid_proof_ids": sorted(invalid_proofs.get(key, []))})

    # ---- 支付与退款归属 ----
    attr_payment: dict[tuple, int] = defaultdict(int)   # (contract, purpose, building)
    attr_refund: dict[tuple, int] = defaultdict(int)    # (contract, purpose, building)
    unattr_outflow: dict[tuple, int] = defaultdict(int)  # bucket key
    node_ids = set(node_by_id)
    for p in portions:
        if p["evidence_state"] == "disputed" or p["business_date"] > D:
            continue
        bkey = (p["account"], p["project"], p["building"], p["purpose"])
        ref = p["reference"]
        if p["kind"] == "payment":
            attributed = bool(ref and ref in obl_by_id)
            if attributed:
                attr_payment[(obl_by_id[ref]["contract"], p["purpose"],
                              p["building"])] += p["live_cents"]
            elif ref and ref in node_ids:
                pass  # 引用工程证明：只消耗对应节点额度，不归属合同
            elif ref:
                findings.add(WARN, "payment_reference_unknown", p["portion_id"],
                             p["live_cents"],
                             f"工程支出 {p['portion_id']} 的引用 {ref} 无法识别")
            if not attributed:
                unattr_outflow[bkey] += p["live_cents"]
        elif p["kind"] == "refund":
            if ref and ref in obl_by_id \
                    and obl_by_id[ref]["kind"] == "refund_requested":
                attr_refund[(obl_by_id[ref]["contract"], p["purpose"],
                             p["building"])] += p["live_cents"]
            else:
                unattr_outflow[bkey] += p["live_cents"]
    for bkey, bucket in buckets.items():
        outflow = unattr_outflow.get(bkey, 0)
        bucket["excess_outflow"] = max(0, outflow - bucket["unassigned_pool"])
        if bucket["excess_outflow"] > 0:
            findings.add(WARN, "payment_not_attributed",
                         f"bucket:{bkey[0]}:{bkey[1]}:{bkey[2]}:{bkey[3]}",
                         bucket["excess_outflow"],
                         f"资金桶 {bkey} 的未归属支出超出未归集余额，"
                         "超出部分影响了合同归集资金但无法唯一归属到具体合同")

    # ---- 合同可归集资金（分用途、分资金楼栋） ----
    assigned: dict[tuple, int] = defaultdict(int)        # (contract, purpose, bldg)
    disputed_assign: dict[tuple, int] = defaultdict(int)  # (contract, purpose)
    contract_projects: dict[str, set] = defaultdict(set)
    for ar in assignment_rows:
        portion = portion_by_id.get(ar["portion_id"]) if ar["portion_id"] else None
        if portion:
            contract_projects[ar["contract"]].add(portion["project"])
        if ar["state"] == "ok" and portion:
            assigned[(ar["contract"], portion["purpose"],
                      portion["building"])] += ar["amount_cents"]
        elif ar["state"] != "ok":
            disputed_assign[(ar["contract"], ar["purpose"])] += ar["amount_cents"]
    for contract, projects in contract_projects.items():
        if len(projects) > 1:
            findings.add(WARN, "contract_multi_project", f"contract:{contract}",
                         None,
                         f"合同 {contract} 的归集资金涉及多个项目 "
                         f"{sorted(projects)}，需人工核实")

    exposure_bp: dict[tuple, int] = defaultdict(int)  # (purpose, building)
    for (account, project, building, purpose), bucket in buckets.items():
        exposure_bp[(purpose, building)] += bucket["excess_outflow"]

    contracts = ({a["contract"] for a in assignment_rows}
                 | {o["contract"] for o in obligation_rows})
    net: dict[tuple, int] = defaultdict(int)  # (contract, purpose, building)
    purposes_of: dict[str, set] = defaultdict(set)
    for c in contracts:
        pbs = {(k[1], k[2]) for k in
               list(assigned) + list(attr_payment) + list(attr_refund) if k[0] == c}
        known_purposes = {p for (p, _b) in pbs} or {"（无归集用途）"}
        for (cc, building) in list(open_refund_reserve):
            if cc == c:
                for purpose in known_purposes:
                    pbs.add((purpose, building))
        for (purpose, building) in pbs:
            purposes_of[c].add(purpose)
            value = (assigned.get((c, purpose, building), 0)
                      - attr_payment.get((c, purpose, building), 0)
                      - attr_refund.get((c, purpose, building), 0)
                      - open_refund_reserve.get((c, building), 0))
            net[(c, purpose, building)] = value
            if value < 0:
                findings.add(ERROR, "refund_exceeds_assignment",
                             f"contract:{c}", value,
                             f"合同 {c} 在 {building}/{purpose} 的已归属支出"
                             "与应退保留超过可归集资金")
    # 待核实归集凭证上的用途同样构成合同的用途视角
    for ar in assignment_rows:
        purposes_of[ar["contract"]].add(ar["purpose"])
    for c in contracts:
        if not purposes_of[c]:
            for o in obligation_rows:
                if o["contract"] == c:
                    purposes_of[c].add("（无归集用途）")

    multi_purpose = {c for c, ps in purposes_of.items() if len(ps) > 1}
    for c in sorted(multi_purpose):
        findings.add(INFO, "multi_purpose_review", f"contract:{c}", None,
                     f"合同 {c} 涉及多个用途 {sorted(purposes_of[c])}，"
                     "不同用途的专项余额不允许互补，按用途分别核实")

    # ---- 覆盖关系：确定可覆盖 / 尚待核实 / 已缺口 ----
    breach_bp = {(r["building"], r["purpose"]): r["breach_cents"]
                 for r in node_cap_rows if r["breach_cents"] > 0}
    coverage_rows: list[dict] = []
    contract_purpose_rows: list[dict] = []
    group_certain: dict[tuple, int] = {}
    deliveries: dict[tuple, list] = defaultdict(list)  # (contract, purpose, bldg)
    for o in obligation_rows:
        if o["kind"] != "delivery":
            continue
        ps = purposes_of.get(o["contract"]) or {"（无归集用途）"}
        for purpose in ps:
            deliveries[(o["contract"], purpose, o["building"])].append(o)

    for (contract, purpose, building), group in sorted(deliveries.items()):
        aligned_net = max(0, net.get((contract, purpose, building), 0))
        exposure = min(aligned_net, exposure_bp.get((purpose, building), 0))
        certain_avail = aligned_net - exposure
        # 合同楼栋变更后，原楼栋的归集资金整体按待核实处理，需要重新解释
        misaligned = sum(max(0, net.get((contract, purpose, b), 0))
                         for (c, p, b) in net
                         if c == contract and p == purpose and b != building)
        disputed = disputed_assign.get((contract, purpose), 0)
        claims = [o["amount_cents"] for o in group]
        claims_total = sum(claims)
        certain_total = min(claims_total, certain_avail)
        rest = claims_total - certain_total
        pending_pool = exposure + misaligned + disputed
        pending_total = min(rest, pending_pool)
        gap_total = rest - pending_total
        reasons = []
        if exposure > 0:
            reasons.append({"reason": "payment_not_attributed",
                            "amount_cents": min(exposure, rest),
                            "refs": f"{building}/{purpose}",
                            "text": "存在无法唯一归属到合同的工程支出，"
                                    "影响部分待核实"})
        if misaligned > 0 and pending_total > 0:
            refs = ",".join(t["record_id"]
                            for t in transfer_by_contract.get(contract, []))
            reasons.append({"reason": "contract_building_changed",
                            "amount_cents": min(misaligned, rest),
                            "refs": refs,
                            "text": "合同楼栋已变更，原楼栋归集资金需重新核实"})
        if disputed > 0:
            reasons.append({"reason": "assignment_disputed",
                            "amount_cents": min(disputed, rest),
                            "refs": contract,
                            "text": "归集凭证待核实（超额/矛盾/明细未确认）"})
        reserve = open_refund_reserve.get((contract, building), 0)
        if reserve > 0 and gap_total > 0:
            reasons.append({"reason": "refund_reserve",
                            "amount_cents": min(reserve, gap_total),
                            "refs": contract,
                            "text": "存在未结算的应退义务，相应资金已专项保留"})
        settled_out = (attr_refund.get((contract, purpose, building), 0)
                       + attr_payment.get((contract, purpose, building), 0))
        if settled_out > 0 and gap_total > 0:
            reasons.append({"reason": "attributed_outflow",
                            "amount_cents": min(settled_out, gap_total),
                            "refs": contract,
                            "text": "银行实际退款或归属合同的工程支出"
                                    "已减少可归集资金"})
        if (building, purpose) in breach_bp:
            reasons.append({"reason": "node_support_lost",
                            "amount_cents": breach_bp[(building, purpose)],
                            "refs": f"{building}/{purpose}",
                            "text": "工程节点更正后部分已付款失去合法额度支持，"
                                    "现有交付义务的资金支持受影响"})
        has_any_assignment = any(ar["contract"] == contract
                                 for ar in assignment_rows)
        if not has_any_assignment and gap_total > 0:
            reasons.append({"reason": "no_assignment",
                            "amount_cents": gap_total,
                            "refs": contract,
                            "text": "没有可归集到合同的资金证据；"
                                    "账户层未归集余额不分配给具体合同"})
        shortfall = claims_total - certain_total
        for o in group:
            claim = o["amount_cents"]
            certain_i = max(0, claim - shortfall)
            pending_i = min(claim - certain_i, pending_total)
            gap_i = claim - certain_i - pending_i
            coverage_rows.append({"obligation_id": o["obligation_id"],
                                 "contract": contract, "purpose": purpose,
                                 "building": building, "claim_cents": claim,
                                 "certain_cents": certain_i,
                                 "pending_cents": pending_i,
                                 "gap_cents": gap_i,
                                 "reasons": reasons})
        group_certain[(contract, purpose, building)] = certain_total

    # 合同 → 项目映射（经归集资金对应的项目；无归集证据的合同单列）
    contract_project = {}
    for c in contracts:
        projects = sorted(contract_projects.get(c, set()))
        contract_project[c] = projects[0] if projects else "（未归集）"
        if not projects:
            findings.add(WARN, "contract_without_project", f"contract:{c}", None,
                         f"合同 {c} 没有归集资金证据，无法归属项目；"
                         "其义务在项目视图中单列")

    # 合同-用途-楼栋视角：覆盖有交付义务的分组，也列出仅有资金的分组
    cp_keys = set(group_certain) | {k for k in net if k[0] in contracts}
    for (contract, purpose, building) in sorted(cp_keys):
        has_obligations = (contract, purpose, building) in group_certain
        contract_purpose_rows.append({
            "contract": contract, "project": contract_project[contract],
            "purpose": purpose, "building": building,
            "assigned_live": assigned.get((contract, purpose, building), 0),
            "refunds_paid": attr_refund.get((contract, purpose, building), 0),
            "open_refund_reserve": open_refund_reserve.get((contract, building), 0),
            "attributed_payments": attr_payment.get((contract, purpose, building), 0),
            "exposure_pending": min(max(0, net.get((contract, purpose, building), 0)),
                                    exposure_bp.get((purpose, building), 0)),
            "misaligned_pending": 0 if has_obligations else max(
                0, net.get((contract, purpose, building), 0)),
            "certain_cents": group_certain.get((contract, purpose, building), 0),
            "note": ("多用途合同，用途间不互补" if contract in multi_purpose else "")
                    + ("" if has_obligations else "；该楼栋无对应交付义务")})

    # ---- 项目期间视图：不给出单一的"成功"结论 ----
    proj: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    obl_rollup: dict[str, list] = defaultdict(list)
    for row in coverage_rows:
        obl_rollup[row["obligation_id"]].append(row)
    for oid, rows in obl_rollup.items():
        contract = rows[0]["contract"]
        project = contract_project[contract]
        claim = rows[0]["claim_cents"]
        certain = max(r["certain_cents"] for r in rows)
        gap = min(r["gap_cents"] for r in rows)
        pending = claim - certain - gap
        proj[project]["claim"] += claim
        proj[project]["certain"] += certain
        proj[project]["pending"] += pending
        proj[project]["gap"] += gap
    retained_proj: dict[str, int] = defaultdict(int)
    unassigned_proj: dict[str, int] = defaultdict(int)
    for (account, project, building, purpose), bucket in buckets.items():
        retained_proj[project] += bucket["retained"]
        consumed_unassigned = min(bucket["unassigned_pool"],
                                  unattr_outflow.get((account, project, building,
                                                      purpose), 0))
        unassigned_proj[project] += (bucket["unassigned_pool"]
                                     - consumed_unassigned)
    project_rows: list[dict] = []
    for project in sorted(set(proj) | set(retained_proj)):
        p = proj[project]
        retained = retained_proj.get(project, 0)
        if project == "（未归集）":
            # 未归集合同的义务不与任何项目的账户留存直接比较
            shortfall = 0
        else:
            shortfall = max(0, p["claim"] - retained)
        flags = []
        if p["claim"] == 0:
            flags.append("无交付义务记录")
        else:
            if p["certain"] > 0 and p["pending"] == 0 and p["gap"] == 0 \
                    and shortfall == 0:
                flags.append("确定可覆盖")
            if p["pending"] > 0:
                flags.append("尚待核实")
            if p["gap"] > 0 or shortfall > 0:
                flags.append("已缺口")
        project_rows.append({"project": project, "claim_cents": p["claim"],
                             "certain_cents": p["certain"],
                             "pending_cents": p["pending"],
                             "gap_cents": p["gap"],
                             "collective_shortfall_cents": shortfall,
                             "retained_cents": retained,
                             "unassigned_retained_cents":
                                 unassigned_proj.get(project, 0),
                             "status_flags": flags})

    # ---- 账户层视图：暂不能归属的余额保留在账户层 ----
    account_rows: list[dict] = []
    acc: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for (account, project, building, purpose), bucket in buckets.items():
        key = (account, project, purpose)
        for field in ("receipts", "payments", "refunds", "retained",
                      "assigned_live"):
            acc[key][field] += bucket[field]
        consumed = min(bucket["unassigned_pool"],
                       unattr_outflow.get((account, project, building, purpose), 0))
        acc[key]["unassigned_retained"] += bucket["unassigned_pool"] - consumed
    for (account, project, purpose), a in sorted(acc.items()):
        account_rows.append({"account": account, "project": project,
                             "purpose": purpose,
                             "receipts_live": a["receipts"],
                             "payments_live": a["payments"],
                             "refunds_live": a["refunds"],
                             "retained": a["retained"],
                             "assigned_live": a["assigned_live"],
                             "unassigned_retained": a["unassigned_retained"]})

    bucket_rows = [{"account": k[0], "project": k[1], "building": k[2],
                    "purpose": k[3],
                    "receipts_live": b["receipts"], "payments_live": b["payments"],
                    "refunds_live": b["refunds"], "retained": b["retained"],
                    "assigned_live": b["assigned_live"],
                    "assigned_disputed": b["assigned_disputed"],
                    "unassigned_pool": b["unassigned_pool"],
                    "excess_outflow": b["excess_outflow"],
                    "pending_funds": b["pending_funds"]}
                   for k, b in sorted(buckets.items())]

    return {"portions": portions, "assignments": assignment_rows,
            "buckets": bucket_rows, "obligations": obligation_rows,
            "node_caps": node_cap_rows, "coverage": coverage_rows,
            "contract_purpose": contract_purpose_rows,
            "projects": project_rows, "accounts": account_rows,
            "findings": findings.rows}


def create_release(con: duckdb.DuckDBPyConnection, mode: str,
                   knowledge_cutoff: str | None, business_cutoff: str,
                   label: str = "") -> str:
    """计算并原子写入一次发布的全部 rel_* 表，返回 release_id。"""
    ensure_release_tables(con)
    K = knowledge_cutoff or "9999-12-31"
    result = compute_release(con, K, business_cutoff)
    seq = con.execute("select count(*)+1 from release").fetchone()[0]
    release_id = f"R{seq:04d}"
    created = datetime.now().isoformat(timespec="seconds")
    con.execute("begin")
    try:
        con.execute("insert into release values(?,?,?,?,?,?)",
                    [release_id, mode, K, business_cutoff, label, created])
        for p in result["portions"]:
            con.execute("insert into rel_portion values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [release_id, p["portion_id"], p["coverage_key"], p["kind"],
                         p["account"], p["project"], p["building"], p["purpose"],
                         p["amount_cents"], p["live_cents"], p["evidence_state"],
                         p["business_date"], p["knowledge_date"],
                         ",".join(p["member_ids"]), p["reference"]])
        for a in result["assignments"]:
            con.execute("insert into rel_assignment values(?,?,?,?,?,?,?,?)",
                        [release_id, a["assignment_id"], a["contract"], a["bank_id"],
                         a["portion_id"], a["amount_cents"], a["state"], a["note"]])
        for b in result["buckets"]:
            con.execute("insert into rel_fund_bucket values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [release_id, b["account"], b["project"], b["building"],
                         b["purpose"], b["receipts_live"], b["payments_live"],
                         b["refunds_live"], b["retained"], b["assigned_live"],
                         b["assigned_disputed"], b["unassigned_pool"],
                         b["excess_outflow"], b["pending_funds"]])
        for o in result["obligations"]:
            con.execute("insert into rel_obligation values(?,?,?,?,?,?,?,?,?)",
                        [release_id, o["obligation_id"], o["contract"], o["kind"],
                         o["building"], o["amount_cents"], o["status"],
                         o["settled_by"], o["note"]])
        for n in result["node_caps"]:
            con.execute("insert into rel_node_cap values(?,?,?,?,?,?,?,?,?)",
                        [release_id, n["building"], n["purpose"], n["cap_cents"],
                         n["payments_cents"], n["room_cents"], n["breach_cents"],
                         ",".join(n["valid_proof_ids"]),
                         ",".join(n["invalid_proof_ids"])])
        for c in result["coverage"]:
            con.execute("insert into rel_coverage values(?,?,?,?,?,?,?,?,?,?)",
                        [release_id, c["obligation_id"], c["contract"], c["purpose"],
                         c["building"], c["claim_cents"], c["certain_cents"],
                         c["pending_cents"], c["gap_cents"],
                         json.dumps(c["reasons"], ensure_ascii=False)])
        for c in result["contract_purpose"]:
            con.execute("insert into rel_contract_purpose values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [release_id, c["contract"], c["project"], c["purpose"],
                         c["building"], c["assigned_live"], c["refunds_paid"],
                         c["open_refund_reserve"], c["attributed_payments"],
                         c["exposure_pending"], c["misaligned_pending"],
                         c["certain_cents"], c["note"]])
        for p in result["projects"]:
            con.execute("insert into rel_project_status values(?,?,?,?,?,?,?,?,?,?)",
                        [release_id, p["project"], p["claim_cents"],
                         p["certain_cents"], p["pending_cents"], p["gap_cents"],
                         p["collective_shortfall_cents"], p["retained_cents"],
                         p["unassigned_retained_cents"],
                         ",".join(p["status_flags"])])
        for a in result["accounts"]:
            con.execute("insert into rel_account_view values(?,?,?,?,?,?,?,?,?,?)",
                        [release_id, a["account"], a["project"], a["purpose"],
                         a["receipts_live"], a["payments_live"], a["refunds_live"],
                         a["retained"], a["assigned_live"],
                         a["unassigned_retained"]])
        for f in result["findings"]:
            con.execute("insert into rel_finding values(?,?,?,?,?,?)",
                        [release_id, f["severity"], f["category"], f["subject"],
                         f["amount_cents"], f["message"]])
        con.execute("commit")
    except Exception:
        con.execute("rollback")
        raise
    return release_id


def verify_release(con: duckdb.DuckDBPyConnection, release_id: str) -> list[str]:
    """发布一致性检查：同一发布内资金解释与覆盖解释不得互相矛盾。"""
    problems: list[str] = []
    rows = _rows(con, "select * from rel_coverage where release_id=?", [release_id])
    for r in rows:
        if r["certain_cents"] + r["pending_cents"] + r["gap_cents"] \
                != r["claim_cents"]:
            problems.append(f"覆盖分解不平：{r['obligation_id']}")
    by_obl: dict[str, int] = defaultdict(int)
    for r in rows:
        by_obl[r["obligation_id"]] += r["certain_cents"]
    retained = {r["project"]: r["retained_cents"]
                for r in _rows(con,
                               "select project, sum(retained) as retained_cents "
                               "from rel_fund_bucket where release_id=? group by project",
                               [release_id])}
    certain_proj: dict[str, int] = defaultdict(int)
    for r in _rows(con, "select project, certain_cents from rel_project_status "
                        "where release_id=?", [release_id]):
        certain_proj[r["project"]] += r["certain_cents"]
    for project, certain in certain_proj.items():
        if certain > max(0, retained.get(project, 0)):
            problems.append(f"项目 {project} 确定可覆盖 {certain} 超过留存资金 "
                            f"{retained.get(project, 0)}")
    for b in _rows(con, "select * from rel_fund_bucket where release_id=?",
                   [release_id]):
        if b["retained"] != b["receipts_live"] - b["payments_live"] \
                - b["refunds_live"]:
            problems.append(f"资金桶不平：{b['account']}/{b['building']}")
        if b["assigned_live"] > b["receipts_live"]:
            problems.append(f"资金桶归集超过到账：{b['account']}/{b['building']}")
    # 已冲正资金不得出现在任何合同的确定可覆盖中
    for r in _rows(con,
                   "select p.portion_id, p.live_cents, "
                   "coalesce(sum(a.amount_cents),0) as assigned "
                   "from rel_portion p left join rel_assignment a "
                   "on a.release_id=p.release_id and a.portion_id=p.portion_id "
                   "and a.state='ok' where p.release_id=? "
                   "group by p.portion_id, p.live_cents", [release_id]):
        if r["assigned"] > r["live_cents"]:
            problems.append(f"资金覆盖 {r['portion_id']} 的归集超过冲正后可用额")
    return problems
