"""
制度引用标注与引用指标测试（离线，不联网、不调真实 LLM、不查库）
    覆盖：
        - 标注文件结构：18 条案例、条款编号合法、核心/可选不重叠；
        - 标注案例都来自 96 条真实评测集；
        - 单案例引用对照：命中、漏引、越界、检索缺口；
        - 汇总指标：条款级准确率、案例级召回率、检索覆盖率、条件召回率；
        - mock 模式引用指标不适用、Markdown 报告包含引用章节。
"""
import unittest
from decimal import Decimal

import tests  # noqa: F401  先设置测试环境变量（DATABASE_URL 指向测试库）
from app.agents.risk_agent import ReviewOutcome
from app.rag.loader import load_policy_document
from evaluation.harness import (
    CITATION_LABELS,
    DEFAULT_CASES,
    REAL_CASES,
    evaluate_cases,
    load_cases,
    load_citation_labels,
    render_markdown,
)



# 构造只含制度引用的评审结果
def make_outcome(citations: list[str], *, retrieved: list[str]) -> ReviewOutcome:
    """
    构造测试用 ReviewOutcome（只关心引用与检索条款）
        :param citations: 实际引用条款号
        :param retrieved: 本轮检索到的条款号
        :return: ReviewOutcome
    """
    return ReviewOutcome(
        risk_score=Decimal("50.00"),                    # 分数：中风险
        risk_level="mid",                               # 风险等级
        summary="测试结论",                              # 总结
        findings=[],                                    # 风险点：空
        policy_citations=[                              # 引用列表：只保留条款号
            {
                "clause_id": clause_id,                 # 条款编号
                "doc": "费用报销管理制度",                # 制度名称
                "chapter": "2 报销单据与凭证",            # 章节
                "quote": "测试引用原文",                  # 引用原文（本测试不校验）
            }
            for clause_id in citations
        ],
        is_fallback=False,                              # 非规则兜底
        model="test",                                   # 模型名
        prompt_version="test",                          # Prompt 版本
        latency_ms=1,                                   # 耗时
        prompt_tokens=1,                                # 输入 token
        completion_tokens=1,                            # 输出 token
        retrieved_clause_ids=list(retrieved),           # 本轮检索到的条款
    )



class CitationLabelsTest(unittest.TestCase):
    """引用标注文件本身"""

    @classmethod
    def setUpClass(cls):
        """加载标注、真实案例与制度条款，供全部用例复用"""
        cls.labels = load_citation_labels()                               # 标注索引
        cls.cases = {
            case["case_id"]: case for case in load_cases(REAL_CASES)      # 真实集案例
        }
        cls.clause_ids = {
            clause.clause_id for clause in load_policy_document().clauses   # 制度条款编号
        }

    def test_label_file_loaded(self):
        """标注覆盖 96 条案例，68 条有核心条款，全部为 reviewed"""
        self.assertEqual(len(self.labels), 96)
        for case_id, label in self.labels.items():
            self.assertIsInstance(label["core_clause_ids"], list, case_id)      # 核心条款必须是列表
            self.assertEqual(label["review_status"], "reviewed", case_id)        # 已确认
        core_cases = [case_id for case_id, label in self.labels.items() if label["core_clause_ids"]]
        self.assertEqual(len(core_cases), 68)                                   # 68 条要求引用
        self.assertEqual(len(self.labels), 96)                                  # 96 条全覆盖

    def test_document_failed_case_has_no_core(self):
        """识别失败不要求引用（系统能力问题），但允许引用 2.2"""
        for case_id in ("G069", "G070", "G071", "G072"):
            label = self.labels[case_id]
            self.assertEqual(label["core_clause_ids"], [], case_id)
            self.assertEqual(label["optional_clause_ids"], ["2.2"], case_id)

    def test_low_risk_case_has_no_core(self):
        """低风险标准场景（如餐饮 160 元）不要求引用，只约束准确率"""
        label = self.labels["G005"]
        self.assertEqual(label["core_clause_ids"], [])
        self.assertEqual(label["optional_clause_ids"], ["3.3"])

    def test_invoice_no_case_uses_v12_clause(self):
        """G041 在制度 v1.3 后核心改为 2.4（2.4 增补了发票号一致性）"""
        label = self.labels["G041"]
        self.assertEqual(label["core_clause_ids"], ["2.4"])
        self.assertEqual(sorted(label["optional_clause_ids"]), ["1.2", "2.1", "2.2"])

    def test_label_file_is_the_expected_one(self):
        """默认路径指向 citation_labels_v1.json"""
        self.assertEqual(CITATION_LABELS.name, "citation_labels_v1.json")

    def test_clause_ids_exist_in_policy(self):
        """标注条款编号都能在制度文档中找到，避免手误"""
        for case_id, label in self.labels.items():
            for clause_id in label["core_clause_ids"] + label["optional_clause_ids"]:
                self.assertIn(clause_id, self.clause_ids, case_id)

    def test_core_and_optional_disjoint(self):
        """同一案例的核心条款与可选条款不重叠"""
        for case_id, label in self.labels.items():
            overlap = set(label["core_clause_ids"]) & set(label["optional_clause_ids"])
            self.assertFalse(overlap, case_id)

    def test_labeled_cases_exist_in_real_set(self):
        """标注案例都能在 96 条真实评测集中找到"""
        for case_id in self.labels:
            self.assertIn(case_id, self.cases)



class CitationMetricsTest(unittest.TestCase):
    """引用指标计算"""

    @classmethod
    def setUpClass(cls):
        """缓存真实案例，避免每个用例重复读文件"""
        cls.cases = {case["case_id"]: case for case in load_cases(REAL_CASES)}

    def _report(self, case_id: str, citations: list[str], *, retrieved: list[str]) -> dict:
        """
        对单条案例做一次引用评测
            :param case_id: 案例编号
            :param citations: 实际引用条款
            :param retrieved: 检索到的条款
            :return: evaluate_cases 报告
        """
        case = self.cases[case_id]                                          # 案例
        outcome = make_outcome(citations, retrieved=retrieved)              # 风险结果
        return evaluate_cases([case], {case_id: outcome}, mode="replay")    # 单案例报告

    def test_core_clause_hit(self):
        """引用到核心条款：命中，准确率与召回率均为 100%"""
        report = self._report("G021", ["2.2"], retrieved=["2.2"])
        result = report["results"][0]
        self.assertTrue(result["citation_hit"])
        self.assertEqual(result["citation_missing_core"], [])
        self.assertEqual(result["citation_unexpected"], [])
        self.assertEqual(report["citation_precision"], 1.0)
        self.assertEqual(report["citation_recall"], 1.0)
        self.assertEqual(report["citation_recall_retrieved"], 1.0)

    def test_missing_core_lowers_recall(self):
        """完全不引用：案例级召回 0，准确率不适用"""
        report = self._report("G053", [], retrieved=["2.4"])
        result = report["results"][0]
        self.assertFalse(result["citation_hit"])
        self.assertEqual(result["citation_missing_core"], ["2.4"])
        self.assertTrue(result["citation_core_retrieved"])
        self.assertEqual(report["citation_recall"], 0.0)
        self.assertIsNone(report["citation_precision"])                    # 无引用时准确率不适用

    def test_unexpected_citation_lowers_precision(self):
        """引用越界条款：条款级准确率降到 50%"""
        report = self._report("G021", ["2.2", "7.1"], retrieved=["2.2", "7.1"])
        result = report["results"][0]
        self.assertEqual(result["citation_unexpected"], ["7.1"])
        self.assertEqual(report["citation_precision"], 0.5)
        self.assertEqual(report["citation_recall"], 1.0)

    def test_retrieval_gap_separated_from_citation(self):
        """核心条款未被检索到：覆盖率 0，条件召回不适用"""
        report = self._report("G085", [], retrieved=["4.3", "2.5"])
        self.assertEqual(report["citation_retrieval_coverage"], 0.0)
        self.assertIsNone(report["citation_recall_retrieved"])             # 无可引条款，条件召回不适用
        self.assertEqual(report["citation_recall"], 0.0)

    def test_mock_mode_not_applicable(self):
        """mock 模式不产生制度引用，引用指标标记为不适用"""
        report = evaluate_cases(load_cases(DEFAULT_CASES))
        self.assertFalse(report["citation_applicable"])
        self.assertIsNone(report["citation_precision"])
        self.assertNotIn("引用准确率", render_markdown(report))

    def test_markdown_lists_citation_table(self):
        """replay 报告包含引用指标与逐案对照"""
        report = self._report("G021", ["2.2"], retrieved=["2.2"])
        markdown = render_markdown(report)
        self.assertIn("引用准确率（条款级）", markdown)
        self.assertIn("引用逐案对照", markdown)
        self.assertIn("| G021 | 2.2 | 2.2 |", markdown)



if __name__ == "__main__":
    unittest.main()
