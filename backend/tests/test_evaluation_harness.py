"""
Phase D 评测 Harness 测试（离线，不联网、不查库）
    覆盖：案例加载、指标计算、高危漏放、Markdown 报告。
"""
import unittest

import tests  # noqa: F401  先设置测试环境变量
from app.rag.loader import load_policy_document
from evaluation.harness import DEFAULT_CASES, evaluate_cases, load_cases, render_markdown
from evaluation.rag_harness import COLLOQUIAL_QUERIES, evaluate_retrieval, load_queries



class EvaluationHarnessTest(unittest.TestCase):
    """评测指标"""

    def test_smoke_cases_loaded(self):
        """冒烟集 30 条，编号唯一"""
        cases = load_cases(DEFAULT_CASES)
        self.assertEqual(len(cases), 30)
        self.assertEqual(len({case["case_id"] for case in cases}), 30)

    def test_metrics_shape(self):
        """指标字段完整，且能识别模拟漏放"""
        report = evaluate_cases(load_cases(DEFAULT_CASES))
        self.assertEqual(report["total"], 30)
        self.assertGreaterEqual(report["high_risk_total"], 1)
        self.assertGreaterEqual(report["high_risk_missed"], 1)   # 案例 C029 故意漏放
        self.assertAlmostEqual(report["decision_accuracy"], 0.9)
        self.assertIn("finding_recall", report)

    def test_markdown_contains_summary(self):
        """Markdown 报告包含指标与说明"""
        report = evaluate_cases(load_cases(DEFAULT_CASES))
        markdown = render_markdown(report)
        self.assertIn("AI 预审评测报告", markdown)
        self.assertIn("高危漏放率", markdown)
        self.assertIn("mock 模式", markdown)

    def test_bm25_recall_baseline(self):
        """BM25 查询集可评测，Recall@3 达到当前门槛"""
        report = evaluate_retrieval()
        self.assertEqual(report["total"], 16)
        self.assertGreaterEqual(report["recall_at_3"], 0.85)
        self.assertFalse(report["need_chroma"])

    def test_colloquial_query_set_is_valid(self):
        """口语化压测集 20 条，期望条款都在制度文档里（只校验数据，不断言命中率）"""
        queries = load_queries(COLLOQUIAL_QUERIES)
        self.assertEqual(len(queries), 20)
        clause_ids = {clause.clause_id for clause in load_policy_document().clauses}
        for item in queries:
            for clause_id in item["expected"]:
                self.assertIn(clause_id, clause_ids, item["query_id"])


if __name__ == "__main__":
    unittest.main()
