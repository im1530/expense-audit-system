"""
制度 BM25 检索测试（离线，不联网、不查库）
    覆盖：
        - 各费用大类的首要召回条款；
        - 按角色过滤审批权限条款；
        - 无匹配、分数阈值、Top-K 截断；
        - 结果排序可复现。
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  先设置测试环境变量
from app.config import settings
from app.rag.loader import load_policy_document
from app.rag.retriever import BM25PolicyRetriever
from app.rag.schema import PolicyQuery



class PolicyRetrieverTest(unittest.TestCase):
    """BM25 检索"""

    @classmethod
    def setUpClass(cls):
        """整个测试类共用一个检索器"""
        cls.document = load_policy_document()               # 真实制度文档
        cls.retriever = BM25PolicyRetriever(cls.document)   # BM25 检索器

    def query(self, text: str, *, types=(), role: str = "", amount: str = "100") -> list:
        """
        执行一次检索
            :param text: 查询文本
            :param types: 费用大类
            :param role: 提交人角色
            :param amount: 单据金额
            :return: 命中列表
        """
        return self.retriever.retrieve(PolicyQuery(
            text=text,
            expense_types=frozenset(types),
            role=role,
            total_amount=Decimal(amount),
        ))

    def test_transport_first_hit(self):
        """交通费查询首选 3.1"""
        hits = self.query("高铁票 交通费 差旅", types=("transportation",))
        self.assertEqual(hits[0].clause.clause_id, "3.1")

    def test_meal_first_hit(self):
        """餐饮费查询首选 3.3"""
        hits = self.query("业务招待餐 客户 餐饮", types=("meal",))
        self.assertEqual(hits[0].clause.clause_id, "3.3")

    def test_accommodation_first_hit(self):
        """住宿费查询首选 3.4"""
        hits = self.query("酒店住宿 差旅 房费", types=("accommodation",))
        self.assertEqual(hits[0].clause.clause_id, "3.4")

    def test_invoice_first_hit(self):
        """发票缺失查询首选 2.5"""
        hits = self.query("发票缺失 未上传发票 发票号")
        self.assertEqual(hits[0].clause.clause_id, "2.5")

    def test_duplicate_first_hit(self):
        """重复报销查询首选 4.3"""
        hits = self.query("重复报销 同一日期 同金额")
        self.assertEqual(hits[0].clause.clause_id, "4.3")

    def test_employee_approval_first_hit(self):
        """员工审批查询首选 5.1"""
        hits = self.query("员工 审批 金额 20000 部门经理 财务", role="employee", amount="20000")
        self.assertEqual(hits[0].clause.clause_id, "5.1")

    def test_manager_approval_filtered(self):
        """经理审批：5.1 被角色过滤，首选 5.2"""
        hits = self.query("审批 金额 20000 部门经理 财务", role="manager", amount="20000")
        self.assertEqual(hits[0].clause.clause_id, "5.2")

    def test_finance_approval_filtered(self):
        """财务审批：首选 5.3"""
        hits = self.query("审批 金额 20000 财务 其他财务", role="finance", amount="20000")
        self.assertEqual(hits[0].clause.clause_id, "5.3")

    def test_boss_clause(self):
        """老板报销：首选 5.4（董事会）"""
        hits = self.query("老板 报销 董事会", role="boss")
        self.assertEqual(hits[0].clause.clause_id, "5.4")

    def test_type_filter_excludes_other_categories(self):
        """费用大类过滤：餐饮查询不会召回交通条款"""
        hits = self.query("高铁票 餐饮费", types=("meal",))
        self.assertTrue(all(
            not hit.clause.expense_types or "meal" in hit.clause.expense_types
            for hit in hits
        ))

    def test_no_match_returns_empty(self):
        """完全无关的查询返回空"""
        self.assertEqual(self.query("zzzz qqqq"), [])

    def test_min_score_threshold(self):
        """绝对分阈值抬高后返回空"""
        with patch.object(settings, "POLICY_MIN_SCORE", 999.0):
            self.assertEqual(self.query("交通费 高铁票", types=("transportation",)), [])

    def test_top_k_limit(self):
        """Top-K 截断生效"""
        with patch.object(settings, "POLICY_TOP_K", 1):
            hits = self.query("报销 费用 制度")
        self.assertEqual(len(hits), 1)

    def test_order_is_deterministic(self):
        """相同查询两次结果一致"""
        first = [hit.clause.clause_id for hit in self.query("发票 报销 费用")]
        second = [hit.clause.clause_id for hit in self.query("发票 报销 费用")]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
