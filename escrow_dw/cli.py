"""命令行入口：入仓、发布、查询、验证。

用法概览（详见 docs/runbook.md）：
    python3 -m escrow_dw ingest --data data --db build/escrow.duckdb
    python3 -m escrow_dw release --db build/escrow.duckdb --as-of 2026-11-02
    python3 -m escrow_dw release --db build/escrow.duckdb --month 2026-11
    python3 -m escrow_dw releases --db build/escrow.duckdb
    python3 -m escrow_dw verify --db build/escrow.duckdb
    python3 -m escrow_dw query --db build/escrow.duckdb project
    python3 -m escrow_dw query --db build/escrow.duckdb contract C1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import knowledge, loading, queries, release as release_mod


def yuan(cents) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) / 100:,.2f} 元"


def _print(data, as_json: bool, text):
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


def _open(args):
    db = Path(args.db)
    if not db.exists() and getattr(args, "command", "") != "ingest":
        raise SystemExit(f"数据库不存在：{db}（请先执行 ingest）")
    return loading.open_db(db)


def cmd_ingest(args) -> int:
    con = _open(args)
    summary = loading.ingest(con, Path(args.data))
    counts = knowledge.build_staging(con)
    print("入仓完成：")
    for name, n in summary.items():
        print(f"  原始 {name}: {n} 行")
    for name, n in counts.items():
        print(f"  整理 {name}: {n} 行")
    return 0


def cmd_release(args) -> int:
    con = _open(args)
    if args.as_of:
        mode, knowledge, business = "as_of", args.as_of, args.as_of
    elif args.month:
        mode, knowledge, business = "month", None, release_mod.month_end(args.month)
    else:
        raise SystemExit("release 需要 --as-of 日期 或 --month 业务月份")
    release_id = release_mod.create_release(
        con, mode, knowledge, business, label=args.label or "")
    problems = release_mod.verify_release(con, release_id)
    print(f"发布 {release_id} 已生成：模式={mode} 知识截止={knowledge or '（不限）'} "
          f"业务截止={business}")
    if problems:
        print("一致性检查未通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("一致性检查通过：同一发布内资金解释与覆盖解释一致。")
    return 0


def cmd_releases(args) -> int:
    con = _open(args)
    rows = queries.list_releases(con)
    _print(rows, args.json, "\n".join(
        f"{r['release_id']}  模式={r['mode']}  知识截止={r['knowledge_cutoff']}  "
        f"业务截止={r['business_cutoff']}  生成于={r['created_at']}"
        + (f"  备注={r['label']}" if r["label"] else "")
        for r in rows) or "（无发布）")
    return 0


def cmd_verify(args) -> int:
    con = _open(args)
    release_id = queries.resolve_release(con, args.release)
    problems = release_mod.verify_release(con, release_id)
    if problems:
        print(f"发布 {release_id} 一致性检查未通过：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"发布 {release_id} 一致性检查通过。")
    return 0


def _render_project(data) -> str:
    lines = []
    for p in data["projects"]:
        flags = "、".join(p["status_flags"]) or "（无状态标记）"
        lines.append(f"项目 {p['project']}：{flags}")
        lines.append(f"  已签约交付义务合计：{yuan(p['claim_cents'])}")
        lines.append(f"  确定可覆盖：{yuan(p['certain_cents'])}"
                     f"　尚待核实：{yuan(p['pending_cents'])}"
                     f"　已缺口：{yuan(p['gap_cents'])}")
        lines.append(f"  账户留存资金：{yuan(p['retained_cents'])}"
                     f"　整体缺口（义务-留存）：{yuan(p['collective_shortfall_cents'])}"
                     f"　账户层未归集余额：{yuan(p['unassigned_retained_cents'])}")
        if p["findings"]:
            lines.append("  相关对账发现：")
            for f in p["findings"]:
                lines.append(f"    [{f['severity']}] {f['message']}")
    return "\n".join(lines) or "（无项目数据）"


def _render_contract(data) -> str:
    lines = [f"合同 {data['contract']}："]
    if not data["coverage"]:
        lines.append("  （无交付义务覆盖记录）")
    for c in data["coverage"]:
        lines.append(f"  义务 {c['obligation_id']}（{c['building']}/{c['purpose']}）"
                     f" 责任 {yuan(c['claim_cents'])}")
        lines.append(f"    确定可覆盖 {yuan(c['certain_cents'])}"
                     f"　尚待核实 {yuan(c['pending_cents'])}"
                     f"　已缺口 {yuan(c['gap_cents'])}")
        for r in c["reasons"]:
            lines.append(f"    - [{r['reason']}] {r['text']}（{yuan(r['amount_cents'])}）")
    if data["backings"]:
        lines.append("  可归集资金（分用途分楼栋）：")
        for b in data["backings"]:
            parts = [f"    {b['building']}/{b['purpose']}：归集 {yuan(b['assigned_live'])}"
                     f"　已退 {yuan(b['refunds_paid'])}"
                     f"　应退保留 {yuan(b['open_refund_reserve'])}"
                     f"　归属支出 {yuan(b['attributed_payments'])}"]
            if b["exposure_pending"]:
                parts.append(f"　受未归属支出影响 {yuan(b['exposure_pending'])}")
            if b["misaligned_pending"]:
                parts.append(f"　错位待核实 {yuan(b['misaligned_pending'])}")
            if b["note"]:
                parts.append(f"（{b['note']}）")
            lines.append("".join(parts))
    if data["assignments"]:
        lines.append("  归集凭证：")
        for a in data["assignments"]:
            note = f"（{a['state']}：{a['note']}）" if a["state"] != "ok" else ""
            lines.append(f"    {a['assignment_id']} ← 银行行 {a['bank_id']} "
                         f"{yuan(a['amount_cents'])} {note}")
    if data["obligations"]:
        lines.append("  义务状态：")
        for o in data["obligations"]:
            extra = f"，由 {o['settled_by']} 结算" if o["settled_by"] else ""
            lines.append(f"    {o['obligation_id']} {o['kind']} "
                         f"{yuan(o['amount_cents'])} 状态={o['status']}{extra}")
    if data["findings"]:
        lines.append("  相关对账发现：")
        for f in data["findings"]:
            lines.append(f"    [{f['severity']}] {f['message']}")
    return "\n".join(lines)


def _render_obligation(data) -> str:
    o = data["obligation"]
    if o is None:
        return "义务不存在于该发布。"
    lines = [f"义务 {o['obligation_id']}（合同 {o['contract']}，{o['kind']}，"
             f"{o['building']}）：责任 {yuan(o['amount_cents'])}，状态 {o['status']}"]
    for c in data["coverage"]:
        lines.append(f"  用途 {c['purpose']}：确定可覆盖 {yuan(c['certain_cents'])}"
                     f"　尚待核实 {yuan(c['pending_cents'])}"
                     f"　已缺口 {yuan(c['gap_cents'])}")
        for r in c["reasons"]:
            lines.append(f"    - [{r['reason']}] {r['text']}（{yuan(r['amount_cents'])}）")
    return "\n".join(lines)


def _render_account(data) -> str:
    lines = []
    for a in data["accounts"]:
        lines.append(f"账户 {a['account']}（{a['project']}/{a['purpose']}）：")
        lines.append(f"  到账(冲正后) {yuan(a['receipts_live'])}"
                     f"　工程支出 {yuan(a['payments_live'])}"
                     f"　实际退款 {yuan(a['refunds_live'])}"
                     f"　留存 {yuan(a['retained'])}")
        lines.append(f"  已归集到合同 {yuan(a['assigned_live'])}"
                     f"　暂不能归属、保留在账户层 {yuan(a['unassigned_retained'])}")
    return "\n".join(lines) or "（无账户数据）"


def _render_nodes(data) -> str:
    lines = []
    for n in data["node_caps"]:
        lines.append(f"{n['building']}/{n['purpose']}：有效额度 {yuan(n['cap_cents'])}"
                     f"　已付款 {yuan(n['payments_cents'])}"
                     f"　剩余可释放 {yuan(n['room_cents'])}"
                     f"　失去支持 {yuan(n['breach_cents'])}")
        lines.append(f"  有效证明：{','.join(n['valid_proof_ids']) or '（无）'}"
                     f"　失效证明：{','.join(n['invalid_proof_ids']) or '（无）'}")
    return "\n".join(lines) or "（无节点数据）"


def _render_findings(data) -> str:
    lines = [f"对账发现合计：{data['counts'] or '（无）'}"]
    for f in data["findings"]:
        amount = f"（{yuan(f['amount_cents'])}）" if f["amount_cents"] else ""
        lines.append(f"  [{f['severity']}] {f['category']} {f['subject']}{amount}"
                     f"：{f['message']}")
    return "\n".join(lines)


def _render_portions(data) -> str:
    lines = []
    for p in data["portions"]:
        lines.append(f"{p['portion_id']}（{p['kind']}，{p['evidence_state']}）："
                     f"声明 {yuan(p['amount_cents'])}　冲正后可用 {yuan(p['live_cents'])}"
                     f"　成员行 {','.join(p['member_ids'])}")
    return "\n".join(lines) or "（无资金覆盖记录）"


def cmd_query(args) -> int:
    con = _open(args)
    release_id = queries.resolve_release(con, args.release)
    info = queries.release_info(con, release_id)
    header = (f"发布 {release_id}（模式={info['mode']} "
              f"知识截止={info['knowledge_cutoff']} "
              f"业务截止={info['business_cutoff']}）")
    what = args.what
    if what == "project":
        data = queries.project_overview(con, release_id, args.subject)
        text = _render_project(data)
    elif what == "contract":
        if not args.subject:
            raise SystemExit("query contract 需要合同编号")
        data = queries.contract_detail(con, release_id, args.subject)
        text = _render_contract(data)
    elif what == "obligation":
        if not args.subject:
            raise SystemExit("query obligation 需要义务编号")
        data = queries.obligation_detail(con, release_id, args.subject)
        text = _render_obligation(data)
    elif what == "account":
        data = queries.account_view(con, release_id, args.subject)
        text = _render_account(data)
    elif what == "nodes":
        data = queries.node_caps(con, release_id, args.building, args.purpose)
        text = _render_nodes(data)
    elif what == "findings":
        data = queries.findings(con, release_id, args.severity)
        text = _render_findings(data)
    elif what == "portions":
        data = queries.portions(con, release_id, args.coverage_key)
        text = _render_portions(data)
    else:
        raise SystemExit(f"未知查询对象：{what}")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(header)
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="escrow_dw",
        description="预售监管资金的可覆盖履约事实仓（本地合成资料，不执行付款或审批）")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="build/escrow.duckdb",
                        help="DuckDB 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", parents=[common],
                       help="校验并装入来源资料，重建整理层")
    p.add_argument("--data", default="data", help="来源资料目录")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("release", parents=[common],
                       help="生成一次发布（同一时点口径的快照）")
    p.add_argument("--as-of", dest="as_of", help="截至某日收到资料的监管解释 YYYY-MM-DD")
    p.add_argument("--month", help="业务月份现状 YYYY-MM（用全部已收资料解释）")
    p.add_argument("--label", help="发布备注")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("releases", parents=[common], help="列出全部发布")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_releases)

    p = sub.add_parser("verify", parents=[common], help="发布一致性检查")
    p.add_argument("--release", help="发布编号，默认最新")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("query", parents=[common], help="下钻查询")
    p.add_argument("what", choices=["project", "contract", "obligation", "account",
                                    "nodes", "findings", "portions"])
    p.add_argument("subject", nargs="?", help="项目/合同/义务/账户编号")
    p.add_argument("--release", help="发布编号，默认最新")
    p.add_argument("--building", help="按楼栋过滤（nodes）")
    p.add_argument("--purpose", help="按用途过滤（nodes）")
    p.add_argument("--severity", choices=["info", "warn", "error"],
                   help="按级别过滤（findings）")
    p.add_argument("--coverage-key", help="按资金覆盖标识过滤（portions）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
