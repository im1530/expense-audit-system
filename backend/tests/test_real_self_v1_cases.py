"""
real_self_v1 真实单据集的离线校验（不联网、不连库、不调 LLM）

    覆盖：
        1. 10 条、编号唯一、字段结构完整；
        2. 主单金额 = 明细金额，日期合法且提交不早于发生日期、未超 60 天；
        3. 风险/决策映射一致，must_findings 合法；
        4. 无 PII（姓名/手机号/身份证/长数字串）；
        5. 口径一致：模拟公司抬头，description 不得再出现“个人抬头”；
        6. 清单计数与案例一致。
"""
import json
import re
import unittest
from datetime import date
from pathlib import Path

CASES = Path(__file__).resolve().parents[1] / "evaluation" / "cases" / "real_self_v1.json"
MANIFEST = Path(__file__).resolve().parents[1] / "evaluation" / "cases" / "real_self_v1_manifest.json"

ALLOWED_TYPES = {"meal", "transportation", "accommodation", "office", "other"}
ALLOWED_FINDINGS = {
    "weak_evidence", "buyer_mismatch", "invoice_missing", "category_limit",
    "period_late", "duplicate_invoice", "amount_short", "amount_over",
    "invoice_no_mismatch", "document_failed", "personal_expense", "buyer_missing",
}
EXPECTED_DECISION = {"low": "fast_track", "mid": "manual_review", "high": "suggest_reject"}


class RealSelfV1CasesTest(unittest.TestCase):
    """真实单据集结构与口径"""

    @classmethod
    def setUpClass(cls):
        """加载案例与清单"""
        cls.cases = json.loads(CASES.read_text(encoding="utf-8"))  # 10 条
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))  # 清单

    def test_counts_and_ids(self):
        """10 条、编号唯一"""
        self.assertEqual(len(self.cases), 10)
        ids = [case["case_id"] for case in self.cases]
        self.assertEqual(len(set(ids)), 10)
        self.assertEqual(ids, [f"R{i:03d}" for i in range(1, 11)])

    def test_amounts_and_dates(self):
        """金额一致、日期合法、未超 60 天"""
        for case in self.cases:
            expense = case["context"]["expense"]
            item = case["context"]["items"][0]
            self.assertEqual(expense["total_amount"], item["amount"], case["case_id"])
            self.assertIn(item["expense_type"], ALLOWED_TYPES)
            expense_date = date.fromisoformat(item["expense_date"])
            submitted = date.fromisoformat(expense["submitted_at"][:10])
            self.assertLessEqual(expense_date, submitted, case["case_id"])
            self.assertLessEqual((submitted - expense_date).days, 60, case["case_id"])

    def test_labels(self):
        """风险/决策映射、must_findings 合法"""
        for case in self.cases:
            expected = case["expected"]
            self.assertEqual(expected["decision"], EXPECTED_DECISION[expected["risk_level"]], case["case_id"])
            for code in expected["must_findings"]:
                self.assertIn(code, ALLOWED_FINDINGS, case["case_id"])
            for code in expected["must_not"]:
                self.assertIn(code, {"fast_track", "manual_review", "suggest_reject"}, case["case_id"])

    def test_distribution(self):
        """分布：low 6 / mid 4 / high 0"""
        risk = {}
        for case in self.cases:
            risk[case["expected"]["risk_level"]] = risk.get(case["expected"]["risk_level"], 0) + 1
        self.assertEqual(risk, {"low": 6, "mid": 4})

    def test_no_pii(self):
        """不得出现姓名、手机号、身份证、长数字串"""
        text = json.dumps(self.cases, ensure_ascii=False)
        self.assertNotIn("张三", text)
        self.assertIsNone(re.search(r"1[3-9]\d{9}", text))
        self.assertIsNone(re.search(r"\d{17}[\dXx]", text))
        self.assertIsNone(re.search(r"\d{15,}", text))

    def test_simulated_company_policy(self):
        """口径为模拟公司抬头：不得残留“个人抬头”"""
        for case in self.cases:
            item = case["context"]["items"][0]
            self.assertNotIn("个人抬头", item["description"], case["case_id"])
            if not item["has_invoice"]:
                self.assertTrue(item["no_invoice_reason"], case["case_id"])

    def test_manifest_counts(self):
        """清单计数与案例一致"""
        self.assertEqual(self.manifest["n"], 10)
        self.assertEqual(self.manifest["status"], "frozen")
        self.assertEqual(self.manifest["counts"]["risk"], {"low": 6, "mid": 4})
        self.assertEqual(self.manifest["counts"]["decision"], {"fast_track": 6, "manual_review": 4})
        self.assertEqual(len(self.manifest["files"]), 10)

    def test_ocr_report(self):
        """OCR 试点报告：10 条、四项全对、无 PII"""
        path = CASES.parent.parent / "reports" / "real_self_v1_ocr.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["n"], 10)
        ids = {item["case_id"] for item in data["results"]}
        self.assertEqual(ids, {f"R{i:03d}" for i in range(1, 11)})
        for item in data["results"]:
            self.assertNotIn("error", item)
            self.assertTrue(item["type_ok"], item["case_id"])
            self.assertTrue(item["amount_ok"], item["case_id"])
            self.assertTrue(item["date_ok"], item["case_id"])
            self.assertTrue(item["party_ok"], item["case_id"])
            self.assertGreaterEqual(item["confidence"], 0.8)
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("张三", text)
        self.assertIsNone(re.search(r"1[3-9]\d{9}", text))


if __name__ == "__main__":
    unittest.main()
