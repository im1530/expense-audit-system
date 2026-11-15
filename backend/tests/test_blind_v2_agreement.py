"""
blind_v2 一致性统计的离线校验（不联网、不连库、不调 LLM）

    覆盖：
        1. 报告里的条数、一致数、分歧编号；
        2. 报告 κ / CI 与重新计算结果一致（固定种子可复现）；
        3. κ 只基于裁决前标注，报告 JSON 结构完整；
        4. 裁决清单只含 2 条分歧、裁决列空白。
"""
import json
import unittest

from openpyxl import load_workbook

from evaluation.analyze_blind_v2 import (
    DEFAULT_A,
    DEFAULT_B,
    OUT_JSON,
    OUT_XLSX,
    analyze,
)


class BlindV2AgreementTest(unittest.TestCase):
    """一致性报告可复现、口径正确"""

    @classmethod
    def setUpClass(cls):
        """加载报告并按同一份回填件重算"""
        cls.report = json.loads(OUT_JSON.read_text(encoding="utf-8"))  # 报告
        cls.recomputed = analyze(DEFAULT_A, DEFAULT_B)  # 重算

    def test_counts(self):
        """60 条、58 条两维一致、60 条都有标注"""
        self.assertEqual(self.report["risk"]["n"], 60)
        self.assertEqual(self.report["both_agree"], 58)
        self.assertEqual(self.report["integrity"]["fact_issues"], 0)
        self.assertEqual(self.report["integrity"]["value_issues"], 0)

    def test_disagreements(self):
        """分歧编号必须恰好是 B002、B037"""
        ids = {item["blind_id"] for item in self.report["disagreements"]}
        self.assertEqual(ids, {"B002", "B037"})
        for item in self.report["disagreements"]:
            self.assertEqual(item["a"]["risk"], "high")
            self.assertEqual(item["b"]["risk"], "mid")

    def test_kappa_recomputed(self):
        """κ 与重算结果一致（浮点容差）"""
        for dimension in ("risk", "decision"):
            self.assertAlmostEqual(self.report[dimension]["kappa"], self.recomputed[dimension]["kappa"], places=9)
            self.assertAlmostEqual(self.report[dimension]["po"], self.recomputed[dimension]["po"], places=9)
            self.assertEqual(self.report[dimension]["kappa_ci95"], self.recomputed[dimension]["kappa_ci95"])

    def test_kappa_ci(self):
        """κ 区间应在 [0,1] 且下界明显高于 0.6"""
        low, high = self.report["risk"]["kappa_ci95"]
        self.assertGreaterEqual(low, 0.0)
        self.assertLessEqual(high, 1.0)
        self.assertGreater(low, 0.6)

    def test_adjudication_sheet(self):
        """裁决清单只有 2 行，裁决列为空"""
        wb = load_workbook(OUT_XLSX)  # 打开
        self.assertEqual(wb.sheetnames, ["分歧清单", "裁决说明"])
        sheet = wb["分歧清单"]
        self.assertEqual(sheet.max_row, 6)  # 表头 4 行 + 2 条
        for row in (5, 6):
            for col in range(15, 20):  # 裁决风险/决策/说明/人/时间
                self.assertIsNone(sheet.cell(row=row, column=col).value)


if __name__ == "__main__":
    unittest.main()
