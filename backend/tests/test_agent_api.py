"""
AI 预审接口测试（直接调用路由函数，不启动 HTTP 服务）
    覆盖：
        - 触发权限：本人/部门经理/财务/老板/无关经理/admin；
        - 状态码：202 新建、200 已有结果、409 非待审批、404 不存在；
        - 查询权限：本人/财务/admin 可看，无关经理不可看；
        - 查询结果：成功执行后能拿到 AI 结论。
"""
import unittest
from decimal import Decimal

from fastapi import HTTPException
from starlette.background import BackgroundTasks
from starlette.responses import Response

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.risk_agent import ReviewOutcome
from app.api.v1.agent import get_ai_review, trigger_ai_review
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import execute_review
from tests.support import BaseTestCase, make_expense



# 构造 Agent 结果
def make_outcome(
    level: str = "high",
    score: str = "75.00",
    *,
    policy_version: str = "",
    citations: list | None = None,
) -> ReviewOutcome:
    """
    构造一份成功的 Agent 结果
        :param level: 等级
        :param score: 分数
        :param policy_version: 制度版本
        :param citations: 制度引用
        :return: ReviewOutcome
    """
    return ReviewOutcome(
        risk_score=Decimal(score),
        risk_level=level,
        summary="存在超限风险",
        findings=[{
            "type": "rule",
            "level": level,
            "message": "明细超过类别限额",
            "evidence": "高铁票",
            "suggestion": "人工核对",
        }],
        policy_citations=citations or [],
        is_fallback=False,
        model="fake-model",
        prompt_version="fake_v1",
        latency_ms=5,
        prompt_tokens=10,
        completion_tokens=5,
        raw_content="{}",
        policy_version=policy_version,
    )



# 假 Agent
class FakeAgent:
    """测试用 Agent"""

    def __init__(self, outcome: ReviewOutcome | None = None):
        """
        :param outcome: 返回结果
        """
        self.outcome = outcome or make_outcome()

    async def review(self, context: dict, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果
            :param context: 上下文
            :param policy: 制度检索结果（本测试不关心）
            :return: ReviewOutcome
        """
        return self.outcome



class AgentApiTest(BaseTestCase, unittest.TestCase):
    """接口权限与状态码"""

    def pending_expense(self):
        """
        造一张待审批单据
            :return: Expense
        """
        employee = self.fx["users"]["employee"]
        return make_expense(
            self.db, employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("2500.00"),
            category_id=self.fx["categories"]["rail"].id,
        )

    def trigger(self, expense, user, *, force: bool = False):
        """
        调用触发接口
            :param expense: 报销单
            :param user: 当前用户
            :param force: 是否强制重跑
            :return: (响应模型, Response, BackgroundTasks)
        """
        background = BackgroundTasks()
        response = Response()
        result = trigger_ai_review(
            expense_id=expense.id,
            response=response,
            background_tasks=background,
            db=self.db,
            current_user=user,
            force=force,
        )
        return result, response, background

    def test_owner_trigger_returns_202(self):
        """本人触发：202 + 投递后台任务"""
        expense = self.pending_expense()
        result, response, background = self.trigger(expense, self.fx["users"]["employee"])

        self.assertEqual(response.status_code, 202)
        self.assertTrue(result.scheduled)
        self.assertEqual(len(background.tasks), 1)
        self.assertEqual(result.expense_id, expense.id)

    def test_other_department_manager_forbidden(self):
        """其他部门经理不能触发"""
        expense = self.pending_expense()
        with self.assertRaises(HTTPException) as ctx:
            self.trigger(expense, self.fx["users"]["manager2"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_same_department_manager_allowed(self):
        """本部门经理可以触发"""
        expense = self.pending_expense()
        result, response, _ = self.trigger(expense, self.fx["users"]["manager"])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(result.expense_id, expense.id)

    def test_finance_allowed(self):
        """财务可以触发"""
        expense = self.pending_expense()
        result, response, _ = self.trigger(expense, self.fx["users"]["finance"])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(result.expense_id, expense.id)

    def test_admin_cannot_trigger(self):
        """admin 没有审批权限：不能触发"""
        expense = self.pending_expense()
        with self.assertRaises(HTTPException) as ctx:
            self.trigger(expense, self.fx["users"]["admin"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_draft_returns_409(self):
        """草稿触发：409"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT)
        with self.assertRaises(HTTPException) as ctx:
            self.trigger(expense, employee)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_missing_expense_returns_404(self):
        """单据不存在：404"""
        with self.assertRaises(HTTPException) as ctx:
            self.trigger(type("E", (), {"id": 999999})(), self.fx["users"]["employee"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_owner_get_after_success(self):
        """执行成功后，本人能查到 AI 结论"""
        expense = self.pending_expense()
        result, _, _ = self.trigger(expense, self.fx["users"]["employee"])
        execute_review(result.review_id, agent=FakeAgent(), session_factory=self.Session)
        self.db.commit()

        data = get_ai_review(
            expense_id=expense.id,
            db=self.db,
            current_user=self.fx["users"]["employee"],
        )
        self.assertEqual(data.risk_level, "high")
        self.assertEqual(data.summary, "存在超限风险")
        self.assertEqual(len(data.findings), 1)
        self.assertEqual(data.decision, "suggest_reject")

    def test_policy_citation_returned(self):
        """查询结果包含制度版本与引用"""
        expense = self.pending_expense()
        result, _, _ = self.trigger(expense, self.fx["users"]["employee"])
        citation = {
            "clause_id": "3.1",
            "doc": "费用报销管理制度",
            "chapter": "3 费用类别与标准",
            "quote": "原则上乘坐高铁二等座或飞机经济舱",
        }
        execute_review(
            result.review_id,
            agent=FakeAgent(outcome=make_outcome(
                policy_version="v1.0",
                citations=[citation],
            )),
            session_factory=self.Session,
        )
        self.db.commit()

        data = get_ai_review(
            expense_id=expense.id,
            db=self.db,
            current_user=self.fx["users"]["employee"],
        )
        self.assertEqual(data.policy_version, "v1.0")
        self.assertEqual(len(data.policy_citations), 1)
        self.assertEqual(data.policy_citations[0].clause_id, "3.1")

    def test_admin_can_read(self):
        """admin 有 view_all：可以查看"""
        expense = self.pending_expense()
        self.trigger(expense, self.fx["users"]["employee"])
        data = get_ai_review(
            expense_id=expense.id,
            db=self.db,
            current_user=self.fx["users"]["admin"],
        )
        self.assertEqual(data.expense_id, expense.id)

    def test_other_department_manager_cannot_read(self):
        """其他部门经理不能查看"""
        expense = self.pending_expense()
        self.trigger(expense, self.fx["users"]["employee"])
        with self.assertRaises(HTTPException) as ctx:
            get_ai_review(
                expense_id=expense.id,
                db=self.db,
                current_user=self.fx["users"]["manager2"],
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_get_without_review_returns_404(self):
        """没有预审记录：404"""
        expense = self.pending_expense()
        with self.assertRaises(HTTPException) as ctx:
            get_ai_review(
                expense_id=expense.id,
                db=self.db,
                current_user=self.fx["users"]["employee"],
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_force_creates_new_task(self):
        """force=True：缓存期内仍创建新任务"""
        expense = self.pending_expense()
        first, _, _ = self.trigger(expense, self.fx["users"]["employee"])
        execute_review(first.review_id, agent=FakeAgent(), session_factory=self.Session)
        self.db.commit()

        second, response, background = self.trigger(
            expense, self.fx["users"]["employee"], force=True
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(second.scheduled)
        self.assertNotEqual(second.review_id, first.review_id)
        self.assertEqual(len(background.tasks), 1)


if __name__ == "__main__":
    unittest.main()
