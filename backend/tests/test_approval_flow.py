"""
审批流匹配测试（match_approval_flow）
    覆盖：
        - boss 角色免线上审批（返回 0）
        - 按「角色 + 金额区间」命中不同审批流，包括边界值
        - 全部规则都不命中时回落默认流
        - 没有可用默认流时抛 500（阻断提交）
        - 规则指向的审批流被停用时抛 500（配置错误不能静默放过）
"""
import unittest
from decimal import Decimal

from fastapi import HTTPException

from app.models import ApprovalFlow, Rule
from app.services.expense_service import match_approval_flow
from tests.support import BaseTestCase



class ApprovalFlowMatchingTest(BaseTestCase, unittest.TestCase):
    """审批流匹配规则测试"""

    # 便捷方法：按角色 key + 金额调用匹配
    def match(self, user_key: str, amount) -> int:
        """
        调用被测函数
            :param user_key: 种子数据里的用户键（employee/manager/finance/boss）
            :param amount: 报销金额（int/Decimal 均可）
            :return: 匹配到的审批流ID
        """
        return match_approval_flow(self.db, self.fx["users"][user_key], float(amount))

    # ---------- 特殊角色 ----------
    def test_boss_returns_zero(self):
        """boss 的报销单不走线上审批，返回 0 作为标识"""
        self.assertEqual(self.match("boss", 100), 0)
        self.assertEqual(self.match("boss", 999999), 0)

    # ---------- 员工金额分级 ----------
    def test_employee_small_amount(self):
        """员工 500 元 → 经理单人审批"""
        self.assertEqual(self.match("employee", 500), self.fx["flows"]["mgr"].id)

    def test_employee_boundary_999_99(self):
        """边界：999.99 仍属于小额档"""
        self.assertEqual(self.match("employee", Decimal("999.99")), self.fx["flows"]["mgr"].id)

    def test_decimal_amount_direct_input(self):
        """直接传 Decimal 时边界仍正确（不经过 float 中转）"""
        user = self.fx["users"]["employee"]
        self.assertEqual(
            match_approval_flow(self.db, user, Decimal("999.99")),
            self.fx["flows"]["mgr"].id,
        )
        self.assertEqual(
            match_approval_flow(self.db, user, Decimal("1000.00")),
            self.fx["flows"]["mgr_fin"].id,
        )
        self.assertEqual(
            match_approval_flow(self.db, user, Decimal("19999.99")),
            self.fx["flows"]["mgr_fin"].id,
        )

    def test_overlapping_rules_use_lowest_id(self):
        """重叠规则按 id 升序取第一条，结果可复现"""
        self.db.query(Rule).update({"is_active": False})
        self.db.commit()
        flow_a = self.fx["flows"]["mgr"]
        flow_b = self.fx["flows"]["mgr_fin"]
        self.db.add_all([
            Rule(
                name="重叠规则A", rule_type="amount", condition="0~5000", action="assign_chain",
                config={"submitter_roles": ["employee"], "min_amount": 0,
                        "max_amount": 5000, "flow_id": flow_a.id},
                is_active=True,
            ),
            Rule(
                name="重叠规则B", rule_type="amount", condition="1000~20000", action="assign_chain",
                config={"submitter_roles": ["employee"], "min_amount": 1000,
                        "max_amount": 20000, "flow_id": flow_b.id},
                is_active=True,
            ),
        ])
        self.db.commit()
        # 2000 同时命中两条，按 id 升序应命中先写入的 A
        self.assertEqual(self.match("employee", 2000), flow_a.id)

    def test_employee_boundary_1000(self):
        """边界：1000 进入中额档（经理→财务）"""
        self.assertEqual(self.match("employee", 1000), self.fx["flows"]["mgr_fin"].id)

    def test_employee_large_amount(self):
        """员工 25000 元 → 经理→财务→老板"""
        self.assertEqual(self.match("employee", 25000), self.fx["flows"]["mgr_fin_boss"].id)

    # ---------- 其他角色 ----------
    def test_manager_routes_to_finance(self):
        """经理报销 → 财务单人审批（经理不审自己的单）"""
        self.assertEqual(self.match("manager", 10000), self.fx["flows"]["fin"].id)

    def test_finance_routes_to_finance_other(self):
        """财务报销 → 其他财务审批（finance_other）"""
        self.assertEqual(self.match("finance", 5000), self.fx["flows"]["fin_other"].id)

    # ---------- 兜底与异常 ----------
    def test_default_flow_fallback(self):
        """所有规则停用后，回落到默认审批流"""
        self.db.query(Rule).update({"is_active": False})
        self.db.commit()
        self.assertEqual(self.match("employee", 500), self.fx["flows"]["mgr_fin_boss"].id)

    def test_no_available_default_flow_raises(self):
        """规则全停 + 默认流也停用 → 抛 500，阻断提交"""
        self.db.query(Rule).update({"is_active": False})
        self.db.query(ApprovalFlow).filter(ApprovalFlow.is_default == True).update({"is_active": False})
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.match("employee", 500)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("默认审批流", ctx.exception.detail)

    def test_rule_pointing_to_inactive_flow_raises(self):
        """规则命中的审批流被停用 → 抛 500，而不是挂到无效流程上"""
        flow = self.db.query(ApprovalFlow).filter(ApprovalFlow.id == self.fx["flows"]["mgr"].id).first()
        flow.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.match("employee", 500)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("审批流", ctx.exception.detail)

    def test_rule_pointing_to_missing_flow_raises(self):
        """规则指向不存在的审批流（比如流程被删）→ 抛 500"""
        rule = self.db.query(Rule).filter(Rule.name == "员工小额-经理").first()
        rule.config = {**rule.config, "flow_id": 999999}
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.match("employee", 500)
        self.assertEqual(ctx.exception.status_code, 500)



if __name__ == "__main__":
    unittest.main()
