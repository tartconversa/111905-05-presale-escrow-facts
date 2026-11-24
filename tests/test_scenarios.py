"""场景化自动验证：分次到账、冲正与节点更正相遇、退款两环节、汇总与明细、
内部调拨、重复与矛盾、跨楼栋变更、用途不互补、未归集余额、节点依赖、双时钟。
"""

from fixkit import ReleaseCase, row


class SplitArrivals(ReleaseCase):
    """一笔流水可能归集多个合同，一份合同也可能分几笔到账。"""

    def test_one_contract_many_flows_and_one_flow_many_contracts(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=3_000_000,
                      coverage_key="k1", date="2026-10-01"),
                  row("bank", bank_id="R2", amount_cents=2_000_000,
                      coverage_key="k2", date="2026-10-02"),
                  row("bank", bank_id="R3", amount_cents=5_000_000,
                      coverage_key="k3", date="2026-10-03")],
            assignments=[
                row("assignments", assignment_id="A1", bank_id="R1",
                    contract="C1", amount_cents=3_000_000, effective="2026-10-01"),
                row("assignments", assignment_id="A2", bank_id="R2",
                    contract="C1", amount_cents=2_000_000, effective="2026-10-02"),
                row("assignments", assignment_id="A3", bank_id="R3",
                    contract="C1", amount_cents=1_000_000, effective="2026-10-03"),
                row("assignments", assignment_id="A4", bank_id="R3",
                    contract="C2", amount_cents=4_000_000, effective="2026-10-03")],
            obligations=[
                row("obligations", obligation_id="O1", contract="C1",
                    amount_cents=6_000_000, effective="2026-09-01"),
                row("obligations", obligation_id="O2", contract="C2",
                    amount_cents=4_000_000, effective="2026-09-01")])
        result = self.compute()
        cov1 = self.coverage_of(result, "O1")[0]
        cov2 = self.coverage_of(result, "O2")[0]
        self.assertEqual(cov1["certain_cents"], 6_000_000)   # 合同分几笔到账
        self.assertEqual(cov2["certain_cents"], 4_000_000)   # 流水归集多合同
        bucket = self.bucket(result)
        self.assertEqual(bucket["receipts_live"], 10_000_000)
        self.assertEqual(bucket["assigned_live"], 10_000_000)
        self.assertEqual(self.findings_of(result, "assignment_overdraw"), [])


class Reversal(ReleaseCase):
    """冲正影响原资金可用性；同一发布内不得看到到账已冲正而覆盖仍充足。"""

    def test_reversed_receipt_not_counted_in_same_release(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1", date="2026-10-01"),
                  row("bank", bank_id="RV1", kind="reversal", amount_cents=2_000_000,
                      date="2026-11-06", coverage_key="rv1", reference="R1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=4_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=6_000_000)])
        before = self.compute(knowledge_cutoff="2026-11-05",
                              business_cutoff="2026-11-05")
        self.assertEqual(self.bucket(before)["receipts_live"], 6_000_000)
        after = self.compute(knowledge_cutoff="2026-11-30",
                             business_cutoff="2026-11-30")
        bucket = self.bucket(after)
        self.assertEqual(bucket["receipts_live"], 4_000_000)
        cov = self.coverage_of(after, "O1")[0]
        self.assertEqual(cov["certain_cents"], 4_000_000)
        self.assertEqual(cov["gap_cents"], 2_000_000)
        # 原始银行事实（到账与冲正）都保留在 portions 中
        kinds = {p["kind"] for p in after["portions"]}
        self.assertIn("receipt", kinds)


class ReversalMeetsNodeCorrection(ReleaseCase):
    """冲正与节点更正相遇：已付款事实保留，但失去合法支持的部分必须显现。"""

    def _source(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=10_000_000,
                      coverage_key="k1", date="2026-10-01"),
                  row("bank", bank_id="PAY1", kind="payment", amount_cents=4_000_000,
                      date="2026-11-02", coverage_key="p1", reference="N1"),
                  row("bank", bank_id="RV1", kind="reversal", amount_cents=3_000_000,
                      date="2026-11-06", coverage_key="rv1", reference="R1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=7_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=9_000_000)],
            nodes=[row("nodes", proof_id="N1", amount_cents=5_000_000,
                       effective="2026-11-01", received="2026-11-01"),
                   row("nodes", proof_id="N2", amount_cents=2_000_000,
                       effective="2026-11-01", received="2026-11-08",
                       supersedes="N1")])

    def test_combined_reversal_and_node_correction(self):
        self._source()
        result = self.compute(business_cutoff="2026-11-30")
        bucket = self.bucket(result)
        self.assertEqual(bucket["receipts_live"], 7_000_000)   # 冲正后
        self.assertEqual(bucket["payments_live"], 4_000_000)   # 已付款事实保留
        self.assertEqual(bucket["retained"], 3_000_000)
        cap = result["node_caps"][0]
        self.assertEqual(cap["cap_cents"], 2_000_000)          # 更正后额度
        self.assertEqual(cap["payments_cents"], 4_000_000)
        self.assertEqual(cap["breach_cents"], 2_000_000)       # 失去合法支持
        self.assertEqual(cap["room_cents"], 0)                 # 旧额度不得沿用
        self.assertEqual(cap["valid_proof_ids"], ["N2"])
        self.assertTrue(self.findings_of(result, "node_cap_breach"))
        cov = self.coverage_of(result, "O1")[0]
        self.assertEqual(cov["certain_cents"], 3_000_000)
        self.assertEqual(cov["pending_cents"], 4_000_000)
        self.assertEqual(cov["gap_cents"], 2_000_000)
        reasons = {r["reason"] for r in cov["reasons"]}
        self.assertIn("node_support_lost", reasons)
        self.assertIn("payment_not_attributed", reasons)

    def test_release_consistency_invariants(self):
        self._source()
        rid, problems = self.persist_and_verify(business_cutoff="2026-11-30")
        self.assertEqual(problems, [], f"发布 {rid} 一致性检查失败：{problems}")

    def test_before_correction_old_cap_governs(self):
        self._source()
        early = self.compute(knowledge_cutoff="2026-11-05",
                             business_cutoff="2026-11-05")
        cap = early["node_caps"][0]
        self.assertEqual(cap["cap_cents"], 5_000_000)   # 更正未到达时旧额度有效
        self.assertEqual(cap["breach_cents"], 0)
        self.assertEqual(cap["room_cents"], 1_000_000)


class RefundTwoStages(ReleaseCase):
    """退款申请只产生义务，银行实际退款才减少资金，两个环节不得各扣一次。"""

    def test_request_then_bank_refund_no_double_deduction(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1", date="2026-10-01"),
                  row("bank", bank_id="REF1", kind="refund", amount_cents=1_000_000,
                      date="2026-11-08", coverage_key="rf1", reference="REQ1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=6_000_000),
                         row("obligations", obligation_id="REQ1",
                             kind="refund_requested", amount_cents=1_000_000,
                             effective="2026-11-05")])
        # 申请后、银行实际退款前：只产生义务（专项保留），资金未减
        mid = self.compute(knowledge_cutoff="2026-11-06",
                           business_cutoff="2026-11-06")
        bucket = self.bucket(mid)
        self.assertEqual(bucket["refunds_live"], 0)
        self.assertEqual(bucket["retained"], 6_000_000)
        cov = self.coverage_of(mid, "O1")[0]
        self.assertEqual(cov["certain_cents"], 5_000_000)
        self.assertEqual(cov["gap_cents"], 1_000_000)
        req = [o for o in mid["obligations"] if o["obligation_id"] == "REQ1"][0]
        self.assertEqual(req["status"], "open")
        # 银行实际退款后：义务结算、资金减少，合计仍只扣一次
        end = self.compute(business_cutoff="2026-11-30")
        bucket = self.bucket(end)
        self.assertEqual(bucket["refunds_live"], 1_000_000)
        self.assertEqual(bucket["retained"], 5_000_000)
        cov = self.coverage_of(end, "O1")[0]
        self.assertEqual(cov["certain_cents"], 5_000_000)   # 不是 4M
        self.assertEqual(cov["gap_cents"], 1_000_000)
        req = [o for o in end["obligations"] if o["obligation_id"] == "REQ1"][0]
        self.assertEqual(req["status"], "settled")
        self.assertEqual(req["settled_by"], "PT-rf1-REF1")

    def test_refund_cancelled_releases_reserve(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=6_000_000),
                         row("obligations", obligation_id="REQ1",
                             kind="refund_requested", amount_cents=1_000_000,
                             effective="2026-11-05"),
                         row("obligations", obligation_id="CX1",
                             kind="refund_cancelled", amount_cents=1_000_000,
                             effective="2026-11-07", reference="REQ1")])
        result = self.compute(business_cutoff="2026-11-30")
        req = [o for o in result["obligations"] if o["obligation_id"] == "REQ1"][0]
        self.assertEqual(req["status"], "cancelled")
        cov = self.coverage_of(result, "O1")[0]
        self.assertEqual(cov["certain_cents"], 6_000_000)

    def test_refund_anomalies_visible(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1"),
                  row("bank", bank_id="RF1", kind="refund", amount_cents=500_000,
                      date="2026-11-08", coverage_key="rf1", reference="REQ1"),
                  row("bank", bank_id="RF2", kind="refund", amount_cents=500_000,
                      date="2026-11-09", coverage_key="rf2", reference="REQ1"),
                  row("bank", bank_id="RF3", kind="refund", amount_cents=100_000,
                      date="2026-11-10", coverage_key="rf3", reference="NOPE")],
            obligations=[row("obligations", obligation_id="REQ1",
                             kind="refund_requested", amount_cents=500_000,
                             effective="2026-11-05")])
        result = self.compute(business_cutoff="2026-11-30")
        self.assertTrue(self.findings_of(result, "refund_settled_twice"))
        self.assertTrue(self.findings_of(result, "refund_request_unknown"))


class SummaryDetailEvidence(ReleaseCase):
    """银行汇总与后来补齐的明细是同一资金覆盖的两种证据，不能相加。"""

    def _write(self, statements, detail_amount=4_000_000):
        self.source(
            bank=[row("bank", bank_id="SUM1", amount_cents=10_000_000,
                      coverage_key="oct", date="2026-10-31"),
                  row("bank", bank_id="D1", amount_cents=6_000_000,
                      coverage_key="oct", date="2026-10-31"),
                  row("bank", bank_id="D2", amount_cents=detail_amount,
                      coverage_key="oct", date="2026-10-31")],
            statements=statements,
            assignments=[
                row("assignments", assignment_id="A1", bank_id="D1",
                    contract="C1", amount_cents=6_000_000, effective="2026-10-31"),
                row("assignments", assignment_id="A2", bank_id="D2",
                    contract="C2", amount_cents=4_000_000, effective="2026-10-31")],
            obligations=[
                row("obligations", obligation_id="O1", contract="C1",
                    amount_cents=6_000_000),
                row("obligations", obligation_id="O2", contract="C2",
                    amount_cents=4_000_000)])

    def test_summary_and_detail_not_added(self):
        self._write(statements=[
            row("statements", coverage_key="oct", summary_id="SUM1",
                detail_ids=[], complete=False, received="2026-11-01"),
            row("statements", coverage_key="oct", summary_id="SUM1",
                detail_ids=["D1", "D2"], complete=True, received="2026-11-04")])
        result = self.compute(business_cutoff="2026-11-30")
        # 汇总 10M + 明细 6M + 4M 不得相加为 20M
        self.assertEqual(self.bucket(result)["receipts_live"], 10_000_000)
        portion = result["portions"][0]
        self.assertEqual(portion["evidence_state"], "confirmed")
        self.assertEqual(self.coverage_of(result, "O1")[0]["certain_cents"],
                         6_000_000)
        self.assertEqual(self.coverage_of(result, "O2")[0]["certain_cents"],
                         4_000_000)

    def test_incomplete_detail_keeps_summary_and_pends_assignments(self):
        self._write(statements=[
            row("statements", coverage_key="oct", summary_id="SUM1",
                detail_ids=["D1", "D2"], complete=False, received="2026-11-01")])
        result = self.compute(knowledge_cutoff="2026-11-02",
                              business_cutoff="2026-11-02")
        # 细节未齐不以其小计替代全量：以汇总 10M 为准
        self.assertEqual(self.bucket(result)["receipts_live"], 10_000_000)
        self.assertTrue(self.findings_of(result, "detail_incomplete"))
        # 针对未确认明细的归集按待核实处理
        self.assertTrue(self.findings_of(result, "assignment_target_unconfirmed"))
        self.assertEqual(self.coverage_of(result, "O1")[0]["pending_cents"],
                         6_000_000)

    def test_summary_detail_mismatch_disputes_funds(self):
        self._write(detail_amount=3_000_000, statements=[
            row("statements", coverage_key="oct", summary_id="SUM1",
                detail_ids=["D1", "D2"], complete=True, received="2026-11-04")])
        result = self.compute(business_cutoff="2026-11-30")
        self.assertTrue(self.findings_of(result, "summary_detail_mismatch"))
        bucket = self.bucket(result)
        self.assertEqual(bucket["receipts_live"], 0)         # 矛盾资金不确定
        self.assertEqual(bucket["pending_funds"], 10_000_000)
        self.assertEqual(self.coverage_of(result, "O1")[0]["certain_cents"], 0)


class InternalTransfer(ReleaseCase):
    """账户内部调拨不能产生新的购房款。"""

    def test_internal_transfer_creates_no_funds(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1"),
                  row("bank", bank_id="T1", kind="internal", amount_cents=6_000_000,
                      coverage_key="t1", date="2026-10-05")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=6_000_000)])
        result = self.compute()
        self.assertEqual(self.bucket(result)["receipts_live"], 6_000_000)
        self.assertTrue(self.findings_of(result, "internal_excluded"))
        self.assertEqual(self.coverage_of(result, "O1")[0]["certain_cents"],
                         6_000_000)


class DuplicateVsConflict(ReleaseCase):
    """来源重复单据和互相矛盾的金额要能从数据集中看出区别。"""

    def test_duplicate_and_conflict_are_distinguished(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1"),
                  row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1"),                      # 完全重复
                  row("bank", bank_id="R2", amount_cents=3_000_000,
                      coverage_key="k2", date="2026-10-02"),
                  row("bank", bank_id="R2", amount_cents=5_000_000,
                      coverage_key="k2", date="2026-10-02")],  # 矛盾金额
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1",
                             amount_cents=6_000_000)])
        result = self.compute()
        dup = self.findings_of(result, "duplicate_document")
        conflict = self.findings_of(result, "conflicting_document")
        self.assertEqual(len(dup), 1)
        self.assertIn("R1", dup[0]["subject"])
        self.assertEqual(len(conflict), 1)
        self.assertIn("R2", conflict[0]["subject"])
        bucket1 = self.bucket(result)
        self.assertEqual(bucket1["receipts_live"], 6_000_000)  # 重复只算一份
        bucket2 = self.bucket(result, building="B1", account="A1")
        # 矛盾资金不进入确定到账，按待核实单列
        pending_total = sum(b["pending_funds"] for b in result["buckets"])
        self.assertEqual(pending_total, 5_000_000)
        self.assertEqual(self.coverage_of(result, "O1")[0]["certain_cents"],
                         6_000_000)


class CrossBuildingMove(ReleaseCase):
    """合同跨楼栋变更后，义务、可归集资金和节点覆盖一起重新解释。"""

    def test_move_reinterprets_obligation_and_backing(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=4_000_000,
                      coverage_key="k1", building="B1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C2", amount_cents=4_000_000)],
            obligations=[row("obligations", obligation_id="O2", contract="C2",
                             amount_cents=4_000_000, building="B1")],
            transfers=[row("transfers", record_id="MV1", contract="C2",
                           from_building="B1", to_building="B2",
                           effective="2026-11-03", received="2026-11-06")])
        before = self.compute(knowledge_cutoff="2026-11-05",
                              business_cutoff="2026-11-05")
        cov = self.coverage_of(before, "O2")[0]
        self.assertEqual(cov["building"], "B1")       # 变更尚未收到
        self.assertEqual(cov["certain_cents"], 4_000_000)
        after = self.compute(business_cutoff="2026-11-30")
        cov = self.coverage_of(after, "O2")[0]
        self.assertEqual(cov["building"], "B2")       # 义务按新楼栋解释
        self.assertEqual(cov["certain_cents"], 0)
        self.assertEqual(cov["pending_cents"], 4_000_000)
        reasons = {r["reason"] for r in cov["reasons"]}
        self.assertIn("contract_building_changed", reasons)
        self.assertTrue(self.findings_of(after, "contract_building_changed"))


class PurposeSeparation(ReleaseCase):
    """不同用途的专项余额不允许互补。"""

    def test_purposes_do_not_complement(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=3_000_000,
                      coverage_key="k1", purpose="construction"),
                  row("bank", bank_id="R2", amount_cents=5_000_000,
                      coverage_key="k2", purpose="decoration")],
            assignments=[
                row("assignments", assignment_id="A1", bank_id="R1",
                    contract="C1", amount_cents=3_000_000, purpose="construction"),
                row("assignments", assignment_id="A2", bank_id="R2",
                    contract="C1", amount_cents=5_000_000, purpose="decoration")],
            obligations=[row("obligations", obligation_id="O1", contract="C1",
                             amount_cents=6_000_000)])
        result = self.compute()
        cons = self.coverage_of(result, "O1", "construction")[0]
        deco = self.coverage_of(result, "O1", "decoration")[0]
        self.assertEqual((cons["certain_cents"], cons["gap_cents"]),
                         (3_000_000, 3_000_000))
        self.assertEqual((deco["certain_cents"], deco["gap_cents"]),
                         (5_000_000, 1_000_000))
        # 项目汇总取单一用途最优，不制造 3M+5M>=6M 的互补假象
        proj = [p for p in result["projects"] if p["project"] == "P1"][0]
        self.assertEqual(proj["certain_cents"], 5_000_000)
        self.assertEqual(proj["gap_cents"], 1_000_000)
        self.assertTrue(self.findings_of(result, "multi_purpose_review"))


class UnassignedBalance(ReleaseCase):
    """暂不能归属的余额保留在账户层，不能为凑齐合同回款而分给任意项目。"""

    def test_unassigned_stays_at_account_level(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=10_000_000,
                      coverage_key="k1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1", contract="C1",
                             amount_cents=6_000_000),
                         row("obligations", obligation_id="O2", contract="C2",
                             amount_cents=4_000_000)])
        result = self.compute()
        cov2 = self.coverage_of(result, "O2")[0]
        self.assertEqual(cov2["certain_cents"], 0)
        self.assertEqual(cov2["gap_cents"], 4_000_000)
        self.assertIn("no_assignment", {r["reason"] for r in cov2["reasons"]})
        acc = result["accounts"][0]
        self.assertEqual(acc["unassigned_retained"], 4_000_000)
        self.assertEqual(acc["retained"], 10_000_000)


class NodeDependency(ReleaseCase):
    """有的节点要求前序节点证明仍有效；前序失效则后续额度一并失效。"""

    def test_dependency_invalidated(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=10_000_000,
                      coverage_key="k1"),
                  row("bank", bank_id="PAY1", kind="payment",
                      amount_cents=2_500_000, date="2026-11-02",
                      coverage_key="p1")],
            nodes=[row("nodes", proof_id="N1", amount_cents=2_000_000,
                       effective="2026-11-01", received="2026-11-01"),
                   row("nodes", proof_id="N2", amount_cents=3_000_000,
                       effective="2026-11-02", received="2026-11-02",
                       depends_on=["N1"]),
                   row("nodes", proof_id="N3", amount_cents=1_000_000,
                       effective="2026-11-01", received="2026-11-09",
                       supersedes="N1")])
        result = self.compute(business_cutoff="2026-11-30")
        cap = result["node_caps"][0]
        self.assertEqual(cap["cap_cents"], 1_000_000)   # 只剩 N3
        self.assertEqual(cap["valid_proof_ids"], ["N3"])
        self.assertEqual(cap["breach_cents"], 1_500_000)
        self.assertTrue(self.findings_of(result, "node_dependency_invalid"))


class AssignmentOverdraw(ReleaseCase):
    """归集金额不得超过对应资金可归集部分；超额时不虚构唯一分配。"""

    def test_overdraw_makes_all_assignments_pending(self):
        self.source(
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=4_000_000),
                         row("assignments", assignment_id="A2", bank_id="R1",
                             contract="C2", amount_cents=4_000_000)],
            obligations=[row("obligations", obligation_id="O1", contract="C1",
                             amount_cents=4_000_000),
                         row("obligations", obligation_id="O2", contract="C2",
                             amount_cents=4_000_000)])
        result = self.compute()
        self.assertTrue(self.findings_of(result, "assignment_overdraw"))
        for oid in ("O1", "O2"):
            cov = self.coverage_of(result, oid)[0]
            self.assertEqual(cov["certain_cents"], 0)
            self.assertEqual(cov["pending_cents"], 4_000_000)
            self.assertEqual(cov["gap_cents"], 0)


class TwoClocksAndPeriods(ReleaseCase):
    """截至某次收到资料时的监管解释 vs 业务月份的现状。"""

    def test_as_of_and_month_views(self):
        self.source(
            bank=[row("bank", bank_id="SUM1", amount_cents=6_000_000,
                      coverage_key="nov", date="2026-11-01"),
                  row("bank", bank_id="D1", amount_cents=6_000_000,
                      coverage_key="nov", date="2026-11-01")],
            statements=[row("statements", coverage_key="nov", summary_id="SUM1",
                            detail_ids=[], complete=False, received="2026-11-02"),
                        row("statements", coverage_key="nov", summary_id="SUM1",
                            detail_ids=["D1"], complete=True,
                            received="2026-11-05")],
            assignments=[row("assignments", assignment_id="A1", bank_id="D1",
                             contract="C1", amount_cents=6_000_000,
                             effective="2026-11-01")],
            obligations=[row("obligations", obligation_id="O1", contract="C1",
                             amount_cents=6_000_000, effective="2026-10-20")])
        # 项目期间：10 月只有义务、没有资金
        october = self.compute(business_cutoff="2026-10-31")
        self.assertEqual(self.coverage_of(october, "O1")[0]["certain_cents"], 0)
        self.assertEqual(self.coverage_of(october, "O1")[0]["gap_cents"],
                         6_000_000)
        # 截至 11-03：只有汇总证据，明细行未知，归集待核实
        asof = self.compute(knowledge_cutoff="2026-11-03",
                            business_cutoff="2026-11-03")
        self.assertEqual(self.coverage_of(asof, "O1")[0]["pending_cents"],
                         6_000_000)
        # 11 月业务现状：明细已确认，确定可覆盖
        month = self.compute(business_cutoff="2026-11-30")
        self.assertEqual(self.coverage_of(month, "O1")[0]["certain_cents"],
                         6_000_000)


if __name__ == "__main__":
    import unittest
    unittest.main()
