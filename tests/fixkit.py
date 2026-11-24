"""测试设施：在临时目录生成合成来源资料（含 manifest），并直接建库计算发布。

fixture 行只需给出关键字段，其余按来源格式约定补默认值；
manifest 与 schema 由本模块按实际行生成，保证入仓校验通过。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from escrow_dw import knowledge, loading, release as release_mod  # noqa: E402

SCHEMA = json.loads((ROOT / "data" / "schema.json").read_text(encoding="utf-8"))

DEFAULTS = {
    "bank": {"bank_id": "X1", "account": "A1", "kind": "receipt",
             "amount_cents": 0, "date": "2026-10-01", "coverage_key": "k1",
             "reference": None, "project": "P1", "building": "B1",
             "purpose": "construction"},
    "statements": {"coverage_key": "k1", "summary_id": "S1", "detail_ids": [],
                   "complete": False, "received": "2026-10-02"},
    "assignments": {"assignment_id": "AS1", "bank_id": "X1", "contract": "C1",
                    "amount_cents": 0, "purpose": "construction",
                    "effective": "2026-10-01"},
    "obligations": {"obligation_id": "O1", "contract": "C1", "kind": "delivery",
                    "building": "B1", "amount_cents": 0,
                    "effective": "2026-10-01", "reference": None},
    "nodes": {"proof_id": "N1", "building": "B1", "purpose": "construction",
              "amount_cents": 0, "effective": "2026-10-01", "depends_on": [],
              "supersedes": None, "received": "2026-10-01"},
    "transfers": {"record_id": "MV1", "contract": "C1", "from_building": "B1",
                  "to_building": "B2", "effective": "2026-10-05",
                  "received": "2026-10-05"},
}


def row(dataset: str, **fields) -> dict:
    """按来源格式约定补全字段；显式传入的字段覆盖默认值。"""
    out = dict(DEFAULTS[dataset])
    out.update(fields)
    assert set(out) == set(SCHEMA["datasets"][dataset]["fields"]), \
        f"{dataset} 字段不符：{set(out) ^ set(SCHEMA['datasets'][dataset]['fields'])}"
    return out


def write_source(directory: Path, **datasets) -> Path:
    """写出 NDJSON、schema.json 与 manifest.json。datasets 为行列表。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.json").write_text(
        json.dumps(SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")
    files = []
    for name in ("bank", "statements", "assignments", "obligations", "nodes",
                 "transfers"):
        rows = datasets.get(name, [])
        text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                       for r in rows)
        (directory / f"{name}.ndjson").write_text(text, encoding="utf-8")
        files.append({"path": f"{name}.ndjson", "dataset": name,
                      "rows": len(rows),
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    manifest = {"description": "测试合成资料", "files": files}
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


def build_con(directory: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    loading.ingest(con, Path(directory))
    knowledge.build_staging(con)
    return con


class ReleaseCase(unittest.TestCase):
    """基类：setUp 里用 self.source(**datasets) 准备资料。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def source(self, **datasets) -> Path:
        return write_source(self.dir, **datasets)

    def compute(self, knowledge_cutoff="9999-12-31", business_cutoff="2026-12-31"):
        con = build_con(self.dir)
        return release_mod.compute_release(con, knowledge_cutoff, business_cutoff)

    def persist_and_verify(self, knowledge_cutoff="9999-12-31",
                           business_cutoff="2026-12-31"):
        """落库生成发布并执行一致性检查，返回 (release_id, 问题列表)。"""
        con = build_con(self.dir)
        rid = release_mod.create_release(con, "test", knowledge_cutoff,
                                         business_cutoff)
        return rid, release_mod.verify_release(con, rid)

    @staticmethod
    def coverage_of(result, obligation_id, purpose=None):
        rows = [c for c in result["coverage"]
                if c["obligation_id"] == obligation_id
                and (purpose is None or c["purpose"] == purpose)]
        return rows

    @staticmethod
    def findings_of(result, category):
        return [f for f in result["findings"] if f["category"] == category]

    @staticmethod
    def bucket(result, building="B1", purpose="construction", account="A1"):
        for b in result["buckets"]:
            if (b["account"], b["building"], b["purpose"]) == (account, building,
                                                             purpose):
                return b
        return None
