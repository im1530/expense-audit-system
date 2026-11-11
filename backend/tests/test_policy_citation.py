"""
制度引用校验测试（离线，不联网、不查库）
    覆盖：
        - 合法引用：编号存在、原文连续；
        - 伪造条款号、改写原文、过短、缺字段、重复、超限；
        - 无制度上下文时全部丢弃；
        - 空白归一化与服务端回填 doc/chapter。
"""
import unittest

import tests  # noqa: F401  先设置测试环境变量
from app.rag.citation import validate_citations
from app.rag.loader import load_policy_document
from app.rag.schema import PolicyBundle, PolicyHit



class PolicyCitationTest(unittest.TestCase):
    """引用校验"""

    @classmethod
    def setUpClass(cls):
        """加载真实制度，取几条条款构造结果集"""
        cls.document = load_policy_document()               # 真实制度
        cls.by_id = {clause.clause_id: clause for clause in cls.document.clauses}   # 编号索引
        cls.bundle = PolicyBundle(
            doc_id=cls.document.doc_id,
            doc_title=cls.document.title,
            version=cls.document.version,
            hits=(
                PolicyHit(clause=cls.by_id["3.1"], score=10.0),
                PolicyHit(clause=cls.by_id["3.3"], score=8.0),
            ),
        )

    def test_valid_citation(self):
        """编号存在 + 原文连续 → 保留，doc/chapter 由服务端回填"""
        quote = "原则上乘坐高铁二等座或飞机经济舱"
        result = validate_citations([{"clause_id": "3.1", "quote": quote}], self.bundle)
        self.assertEqual(len(result.citations), 1)
        citation = result.citations[0]
        self.assertEqual(citation["clause_id"], "3.1")
        self.assertEqual(citation["doc"], "费用报销管理制度")
        self.assertEqual(citation["chapter"], "3 费用类别与标准")
        self.assertEqual(citation["quote"], quote)

    def test_unknown_clause_dropped(self):
        """引用未检索到的条款 → 丢弃"""
        result = validate_citations([{"clause_id": "9.9", "quote": "编造内容"}], self.bundle)
        self.assertEqual(result.citations, [])
        self.assertEqual(result.stats["dropped"][0]["reason"], "not_retrieved")

    def test_paraphrased_quote_dropped(self):
        """改写原文 → 丢弃"""
        result = validate_citations(
            [{"clause_id": "3.1", "quote": "员工应该优先乘坐高铁二等座"}],
            self.bundle,
        )
        self.assertEqual(result.citations, [])
        self.assertEqual(result.stats["dropped"][0]["reason"], "quote_not_found")

    def test_short_quote_dropped(self):
        """引用过短 → 丢弃"""
        result = validate_citations([{"clause_id": "3.1", "quote": "高铁"}], self.bundle)
        self.assertEqual(result.citations, [])
        self.assertEqual(result.stats["dropped"][0]["reason"], "quote_too_short")

    def test_missing_clause_id_dropped(self):
        """缺少条款号 → 丢弃"""
        result = validate_citations([{"quote": "原则上乘坐高铁二等座"}], self.bundle)
        self.assertEqual(result.citations, [])
        self.assertEqual(result.stats["dropped"][0]["reason"], "missing_clause_id")

    def test_whitespace_is_ignored(self):
        """原文中的空白差异不影响校验"""
        quote = "原则上 乘坐 高铁二等座"
        result = validate_citations([{"clause_id": "3.1", "quote": quote}], self.bundle)
        self.assertEqual(len(result.citations), 1)

    def test_wrapping_quotes_are_stripped(self):
        """模型给引用多包一层引号仍可通过"""
        quote = "“原则上乘坐高铁二等座或飞机经济舱”"
        result = validate_citations([{"clause_id": "3.1", "quote": quote}], self.bundle)
        self.assertEqual(len(result.citations), 1)

    def test_duplicate_citation_dropped(self):
        """同一条款重复引用只保留一条"""
        quote = "原则上乘坐高铁二等座或飞机经济舱"
        result = validate_citations(
            [
                {"clause_id": "3.1", "quote": quote},
                {"clause_id": "3.1", "quote": quote},
            ],
            self.bundle,
        )
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.stats["dropped"][0]["reason"], "duplicate")

    def test_over_limit_dropped(self):
        """超过数量上限 → 丢弃"""
        quote_a = "原则上乘坐高铁二等座或飞机经济舱"
        quote_b = "餐饮费单次限额为"
        result = validate_citations(
            [
                {"clause_id": "3.1", "quote": quote_a},
                {"clause_id": "3.3", "quote": quote_b},
            ],
            self.bundle,
            max_citations=1,
        )
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.stats["dropped"][0]["reason"], "over_limit")

    def test_no_policy_drops_all(self):
        """无制度上下文：全部引用丢弃"""
        result = validate_citations(
            [{"clause_id": "3.1", "quote": "原则上乘坐高铁二等座"}],
            None,
        )
        self.assertEqual(result.citations, [])
        self.assertEqual(result.stats["dropped"][0]["reason"], "no_policy")

    def test_stats_include_retrieved_ids(self):
        """审计统计包含本轮检索到的条款编号"""
        result = validate_citations([], self.bundle)
        self.assertEqual(result.stats["retrieved_clause_ids"], ["3.1", "3.3"])
        self.assertEqual(result.stats["policy_version"], "v1.2")


if __name__ == "__main__":
    unittest.main()
