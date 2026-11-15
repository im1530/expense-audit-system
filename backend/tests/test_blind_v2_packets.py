"""
blind_v2 盲包的离线校验（不联网、不连库、不调 LLM）

    覆盖：
        1. 发放清单里的哈希与文件一致（指南/制度/样本/两份包）；
        2. 两份包各 60 行、盲编号集合与冻结样本一致；
        3. A/B 顺序不同（独立乱序）；
        4. 待填列全部为空白；
        5. 包内不含案例原始编号（G0xx）；
        6. 三页齐全、制度条款 33 条、风险/决策下拉存在。
"""
import json
import re
import unittest

from openpyxl import load_workbook

from evaluation.build_blind_v2_packets import (
    DIST,
    FILL_FIRST_COL,
    DATA_FIRST_ROW,
    GUIDE,
    PACKETS,
    POLICY,
    SAMPLE,
    sha256_file,
)


class BlindV2PacketsTest(unittest.TestCase):
    """盲包内容、顺序与泄题检查"""

    @classmethod
    def setUpClass(cls):
        """加载发放清单、冻结样本与两份工作簿"""
        cls.manifest = json.loads(DIST.read_text(encoding="utf-8"))  # 发放清单
        cls.sample = json.loads(SAMPLE.read_text(encoding="utf-8"))  # 冻结样本
        cls.sample_ids = {row["blind_id"] for row in cls.sample["cases"]}  # 60 个盲编号
        cls.books = {}  # 标注人 → 工作簿
        cls.paths = {}  # 标注人 → 路径
        for annotator in ("A", "B"):
            path = PACKETS[annotator]["file"]  # 包路径
            cls.paths[annotator] = path
            cls.books[annotator] = load_workbook(path)  # 只读打开

    def test_manifest_hashes(self):
        """指南、制度、样本与两份包的哈希必须与清单一致"""
        self.assertEqual(sha256_file(GUIDE), self.manifest["guide"]["sha256"])
        self.assertEqual(sha256_file(POLICY), self.manifest["policy"]["sha256"])
        self.assertEqual(sha256_file(SAMPLE), self.manifest["sample"]["sha256"])
        for annotator, info in self.manifest["packets"].items():
            self.assertEqual(sha256_file(self.paths[annotator]), info["sha256"])

    def test_sheets(self):
        """三页齐全且顺序固定"""
        for annotator, wb in self.books.items():
            self.assertEqual(wb.sheetnames, ["标注表", "标注说明", "制度条款"], annotator)

    def test_rows_and_ids(self):
        """每份包 60 行，盲编号集合与冻结样本一致"""
        for annotator, wb in self.books.items():
            sheet = wb["标注表"]
            ids = [sheet.cell(row=row, column=1).value for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60)]
            self.assertEqual(len(set(ids)), 60, annotator)
            self.assertEqual(set(ids), self.sample_ids, annotator)

    def test_orders_differ(self):
        """A/B 两份包的行顺序必须不同"""
        orders = {}
        for annotator, wb in self.books.items():
            sheet = wb["标注表"]
            orders[annotator] = [sheet.cell(row=row, column=1).value for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60)]
        self.assertNotEqual(orders["A"], orders["B"])

    def test_answer_cells_empty(self):
        """风险/决策/引用/理由/备注列必须空白"""
        for annotator, wb in self.books.items():
            sheet = wb["标注表"]
            for row in range(DATA_FIRST_ROW, DATA_FIRST_ROW + 60):
                for col in range(FILL_FIRST_COL, FILL_FIRST_COL + 5):
                    self.assertIsNone(sheet.cell(row=row, column=col).value, f"{annotator} 行{row} 列{col}")

    def test_no_case_id(self):
        """包内不得出现案例原始编号（G0xx）"""
        pattern = re.compile(r"\bG\d{3}\b")
        for annotator, wb in self.books.items():
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    for value in row:
                        if isinstance(value, str):
                            self.assertIsNone(pattern.search(value), f"{annotator}/{sheet.title} 泄题：{value[:40]}")

    def test_clauses_sheet(self):
        """制度条款页 33 条，首条为 1.1"""
        for annotator, wb in self.books.items():
            sheet = wb["制度条款"]
            self.assertEqual(sheet.max_row, 34, annotator)
            self.assertEqual(sheet["A2"].value, "1.1")
            self.assertEqual(sheet["A34"].value, "8.2")

    def test_dropdowns(self):
        """风险/决策列有下拉校验（避免手填错值）"""
        for annotator, wb in self.books.items():
            validations = wb["标注表"].data_validations.dataValidation  # 校验规则
            self.assertGreaterEqual(len(validations), 2, annotator)
            formulas = " ".join(item.formula1 for item in validations)  # 下拉公式
            self.assertIn("low", formulas)
            self.assertIn("suggest_reject", formulas)

    def test_manifest_checks(self):
        """清单里记录的自检项必须全部通过"""
        checks = self.manifest["checks"]
        self.assertTrue(checks["orders_differ"])
        self.assertTrue(checks["answer_cells_empty"])
        self.assertEqual(checks["leak_scan"], "pass")
        self.assertEqual(checks["clauses"], 33)


if __name__ == "__main__":
    unittest.main()
