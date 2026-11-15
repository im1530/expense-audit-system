"""
blind_v2 终版标签的离线校验（不联网、不连库、不调 LLM）

    覆盖：
        1. 60 条、编号唯一、与冻结样本/映射一一对应；
        2. 终版分布 16/23/21，决策与风险口径一致；
        3. 只有 B002、B037 经过裁决，其余条目终版 = A = B；
        4. 裁决记录字段齐全（adjudicated_by / at / note）；
        5. 与 v1 的差异数量（16/24 确认、8 下调、5 上调）与哈希清单。
"""
import json
import unittest

from openpyxl import load_workbook

from evaluation.finalize_blind_v2 import (
    ADJUDICATION,
    MAPPING,
    OUT_BOOK,
    OUT_FINAL,
    OUT_HASH,
    SAMPLE,
    sha256_file,
)


class BlindV2FinalTest(unittest.TestCase):
    """终版标签完整性、裁决覆盖与分布"""

    @classmethod
    def setUpClass(cls):
        """加载终版、样本、映射与裁决记录"""
        cls.final = json.loads(OUT_FINAL.read_text(encoding="utf-8"))  # 终版
        cls.sample = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 样本
        cls.mapping = json.loads(MAPPING.read_text(encoding="utf-8"))["mapping"]  # 映射
        cls.adjudication = json.loads(ADJUDICATION.read_text(encoding="utf-8"))  # 裁决
        cls.cases = cls.final["cases"]  # 60 条

    def test_counts_and_ids(self):
        """60 条、编号唯一、与样本一致"""
        self.assertEqual(len(self.cases), 60)
        self.assertEqual(len({case["blind_id"] for case in self.cases}), 60)
        self.assertEqual({case["blind_id"] for case in self.cases}, {item["blind_id"] for item in self.mapping})

    def test_hash_manifest(self):
        """哈希清单与终版文件一致"""
        digest = OUT_HASH.read_text(encoding="utf-8").split()[0]
        self.assertEqual(digest, sha256_file(OUT_FINAL))

    def test_distribution(self):
        """终版分布：low 16 / mid 23 / high 21"""
        risk = {}
        decision = {}
        for case in self.cases:
            risk[case["risk_level"]] = risk.get(case["risk_level"], 0) + 1
            decision[case["decision"]] = decision.get(case["decision"], 0) + 1
        self.assertEqual(risk, {"low": 16, "mid": 23, "high": 21})
        self.assertEqual(decision, {"fast_track": 16, "manual_review": 23, "suggest_reject": 21})

    def test_decision_matches_risk(self):
        """决策与风险保持硬边界"""
        expected = {"low": "fast_track", "mid": "manual_review", "high": "suggest_reject"}
        for case in self.cases:
            self.assertEqual(case["decision"], expected[case["risk_level"]], case["blind_id"])

    def test_adjudicated_only_two(self):
        """只有 B002、B037 经过裁决，其余条目 A=B=终版"""
        adjudicated = {case["blind_id"] for case in self.cases if case["adjudicated"]}
        self.assertEqual(adjudicated, {"B002", "B037"})
        for case in self.cases:
            if case["adjudicated"]:
                self.assertEqual(case["risk_level"], "mid")
                self.assertEqual(case["decision"], "manual_review")
                self.assertTrue(case["adjudication_note"])
                continue
            self.assertEqual(case["a_risk"], case["b_risk"], case["blind_id"])
            self.assertEqual(case["a_decision"], case["b_decision"], case["blind_id"])
            self.assertEqual(case["risk_level"], case["a_risk"], case["blind_id"])
            self.assertEqual(case["decision"], case["a_decision"], case["blind_id"])

    def test_adjudication_meta(self):
        """裁决人、时间、依据齐全"""
        meta = self.final["meta"]["adjudication"]
        self.assertEqual(meta["count"], 2)
        self.assertTrue(meta["adjudicated_by"])
        self.assertTrue(meta["adjudicated_at"])
        self.assertTrue(meta["basis"])
        self.assertEqual(meta["items"][0]["blind_id"], "B002")

    def test_vs_v1(self):
        """v1 高危 24 条：16 条确认、8 条下调、5 条上调、与原标签一致 47 条"""
        meta = self.final["meta"]
        self.assertEqual(meta["vs_v1_high"]["sampled"], 24)
        self.assertEqual(meta["vs_v1_high"]["confirmed"], 16)
        self.assertEqual(meta["vs_v1_high"]["downgraded"], 8)
        self.assertEqual(meta["vs_v1_high"]["upgraded"], 5)
        self.assertEqual(meta["vs_v1_golden"]["agree"], 47)

    def test_usage_scope(self):
        """使用定位必须是方案 C：人工标准基准，不直接作为系统金标准"""
        scope = self.final["meta"]["usage_scope"]
        self.assertEqual(scope["decision"], "C")
        self.assertEqual(scope["decided_at"], "2026-11-14")
        self.assertIn("人工标准基准", scope["positioning"])
        self.assertIn("不直接", scope["positioning"])
        self.assertTrue(scope["gate"])

    def test_adjudicated_book(self):
        """已填裁决清单：两行都有裁决值"""
        ws = load_workbook(OUT_BOOK)["分歧清单"]
        values = [(ws.cell(row=row, column=15).value, ws.cell(row=row, column=16).value) for row in (5, 6)]
        self.assertEqual(values, [("mid", "manual_review"), ("mid", "manual_review")])


if __name__ == "__main__":
    unittest.main()
