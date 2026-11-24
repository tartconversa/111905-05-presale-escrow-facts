"""命令行端到端冒烟验证：入仓 → 发布 → 一致性检查 → 下钻查询。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from fixkit import row, write_source

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from escrow_dw.cli import main  # noqa: E402


class CliSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.db = tmp / "escrow.duckdb"
        self.src = write_source(
            tmp / "src",
            bank=[row("bank", bank_id="R1", amount_cents=6_000_000,
                      coverage_key="k1", date="2026-10-01"),
                  row("bank", bank_id="PAY1", kind="payment",
                      amount_cents=1_000_000, date="2026-11-02",
                      coverage_key="p1")],
            assignments=[row("assignments", assignment_id="A1", bank_id="R1",
                             contract="C1", amount_cents=6_000_000)],
            obligations=[row("obligations", obligation_id="O1", contract="C1",
                             amount_cents=6_000_000)],
            nodes=[row("nodes", proof_id="N1", amount_cents=2_000_000,
                       effective="2026-11-01", received="2026-11-01")])

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        self.assertEqual(code, 0, f"命令失败：{argv}\n{out.getvalue()}")
        return out.getvalue()

    def test_full_flow(self):
        text = self.run_cli("ingest", "--data", str(self.src), "--db",
                            str(self.db))
        self.assertIn("入仓完成", text)
        text = self.run_cli("release", "--db", str(self.db), "--as-of",
                            "2026-11-05")
        self.assertIn("一致性检查通过", text)
        text = self.run_cli("release", "--db", str(self.db), "--month",
                            "2026-11")
        self.assertIn("R0002", text)
        text = self.run_cli("releases", "--db", str(self.db))
        self.assertIn("R0001", text)
        self.assertIn("R0002", text)
        text = self.run_cli("verify", "--db", str(self.db))
        self.assertIn("一致性检查通过", text)
        text = self.run_cli("query", "--db", str(self.db), "project")
        self.assertIn("确定可覆盖", text)
        text = self.run_cli("query", "--db", str(self.db), "contract", "C1")
        self.assertIn("O1", text)
        text = self.run_cli("query", "--db", str(self.db), "obligation", "O1",
                            "--json")
        self.assertIn('"claim_cents": 6000000', text)
        text = self.run_cli("query", "--db", str(self.db), "account", "A1")
        self.assertIn("留存", text)
        text = self.run_cli("query", "--db", str(self.db), "nodes")
        self.assertIn("N1", text)
        text = self.run_cli("query", "--db", str(self.db), "findings")
        self.assertIn("对账发现合计", text)
        text = self.run_cli("query", "--db", str(self.db), "portions")
        self.assertIn("PT-k1-R1", text)


if __name__ == "__main__":
    unittest.main()
