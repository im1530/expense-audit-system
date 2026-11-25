"""
成本控制测试（Phase F）
    覆盖：
        - 费用估算；
        - token 预算 / 费用预算触发降级原因；
        - 凭证识别每日限额判断；
        - 工作流节点与整单的 token/费用落库。
    约束：使用测试库，不联网；LLM 用 FakeAgent。
"""
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.risk_agent import ReviewOutcome
from app.config import settings
from app.models import AgentInvestigation, AIReviewLog, AIReviewStatus, WorkflowRun, WorkflowStep
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import create_review
from app.services.document_service import DocumentRunResult, extract_for_expense_with_usage
from app.services.cost_service import (
    document_daily_limit_reached,
    estimate_cost,
    review_budget_reason,
)
from app.workflow.service import run_review_workflow
from tests.support import BaseTestCase, make_expense



# 假风险 Agent
class FakeAgent:
    """返回固定低风险结果"""

    def __init__(self, outcome: ReviewOutcome):
        """
        :param outcome: 固定结果
        """
        self.outcome = outcome

    async def review(self, context, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果
            :param context: 上下文
            :param policy: 制度结果
            :return: ReviewOutcome
        """
        return self.outcome



class CostEstimateTest(unittest.TestCase):
    """费用估算"""

    def test_estimate_cost(self):
        """按百万 token 单价换算"""
        with (
            patch.object(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0),
            patch.object(settings, "MODEL_OUTPUT_PRICE_PER_1M", 2.0),
        ):
            cost = estimate_cost(1000, 500)
        self.assertEqual(cost, Decimal("0.002000"))



class BudgetTest(BaseTestCase, unittest.TestCase):
    """每日预算"""

    def make_log(self, *, prompt_tokens: int = 100, completion_tokens: int = 50) -> AIReviewLog:
        """
        造一条今天的成功预审记录
            :param prompt_tokens: 输入 token
            :param completion_tokens: 输出 token
            :return: 预审日志
        """
        expense = make_expense(self.db, self.fx["users"]["employee"], status=ExpenseStatus.PENDING)
        log = AIReviewLog(
            expense_id=expense.id,
            status=AIReviewStatus.SUCCESS,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            is_fallback=False,
            created_at=datetime.now(),
        )
        self.db.add(log)
        self.db.commit()
        return log

    def test_token_budget(self):
        """token 超预算：返回 daily_token_limit"""
        self.make_log(prompt_tokens=100, completion_tokens=50)
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 100):
            self.assertEqual(review_budget_reason(self.db), "daily_token_limit")

    def test_agent_tokens_share_daily_budget(self):
        """AI 核查 Agent 的 token 也计入同一份每日预算"""
        expense = make_expense(self.db, self.fx["users"]["employee"], status=ExpenseStatus.PENDING)
        record = AgentInvestigation(
            expense_id=expense.id,
            status="success",
            prompt_tokens=100,
            completion_tokens=50,
            created_at=datetime.now(),
        )
        self.db.add(record)
        self.db.commit()
        with patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 100):
            self.assertEqual(review_budget_reason(self.db), "daily_token_limit")

    def test_cost_budget(self):
        """费用超预算：返回 daily_cost_limit"""
        self.make_log(prompt_tokens=1000, completion_tokens=500)
        with (
            patch.object(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0),
            patch.object(settings, "MODEL_OUTPUT_PRICE_PER_1M", 2.0),
            patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.001),
        ):
            self.assertEqual(review_budget_reason(self.db), "daily_cost_limit")

    def test_no_budget_returns_none(self):
        """未配置预算：不拦截"""
        self.make_log()
        with (
            patch.object(settings, "AI_REVIEW_DAILY_TOKEN_LIMIT", 0),
            patch.object(settings, "AI_REVIEW_DAILY_COST_LIMIT", 0.0),
            patch.object(settings, "AI_REVIEW_DAILY_LIMIT", 0),
        ):
            self.assertIsNone(review_budget_reason(self.db))

    def test_document_daily_limit(self):
        """凭证识别达到每日上限"""
        with (
            patch.object(settings, "DOCUMENT_DAILY_LIMIT", 1),
            patch("app.services.cost_service.document_calls_today", return_value=1),
        ):
            self.assertTrue(document_daily_limit_reached(MagicMock()))



class DocumentUsageTest(unittest.TestCase):
    """凭证识别用量统计"""

    def test_usage_counts_only_new_extractions(self):
        """新建识别计入 token，缓存命中不计"""
        item = SimpleNamespace(id=1, attachments=[SimpleNamespace(id=1, is_deleted=False)])
        expense = SimpleNamespace(
            user=SimpleNamespace(ai_consent_at=datetime.now(), ai_consent_version="v1"),
            items=[item],
        )
        extraction = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        run = DocumentRunResult(extraction=extraction, created=True)

        with (
            patch.object(settings, "DOCUMENT_AI_ENABLED", True),
            patch("app.services.document_service._load_expense", return_value=expense),
            patch("app.services.document_service.has_ai_consent", return_value=True),
            patch("app.services.document_service.document_daily_limit_reached", return_value=False),
            patch("app.services.document_service.extract_attachment", return_value=run),
        ):
            records, usage = extract_for_expense_with_usage(MagicMock(), 1)

        self.assertEqual(len(records), 1)
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 5})

        cached_run = DocumentRunResult(extraction=extraction, created=False)   # 模拟缓存命中
        with (
            patch.object(settings, "DOCUMENT_AI_ENABLED", True),
            patch("app.services.document_service._load_expense", return_value=expense),
            patch("app.services.document_service.has_ai_consent", return_value=True),
            patch("app.services.document_service.document_daily_limit_reached", return_value=False),
            patch("app.services.document_service.extract_attachment", return_value=cached_run),
        ):
            _records, cached_usage = extract_for_expense_with_usage(MagicMock(), 1)
        self.assertEqual(cached_usage, {"prompt_tokens": 0, "completion_tokens": 0})



class WorkflowCostTest(BaseTestCase, unittest.TestCase):
    """工作流节点成本"""

    def test_workflow_records_node_and_run_cost(self):
        """风险节点 token 与费用写入 step/run"""
        user = self.fx["users"]["employee"]
        expense = make_expense(
            self.db,
            user,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("100.00"),
            category_id=self.fx["categories"]["rail"].id,
        )
        log = create_review(self.db, expense_id=expense.id)
        outcome = ReviewOutcome(
            risk_score=Decimal("20"),
            risk_level="low",
            summary="ok",
            findings=[],
            policy_citations=[],
            is_fallback=False,
            model="fake",
            prompt_version="v1",
            latency_ms=1,
            prompt_tokens=10,
            completion_tokens=5,
        )

        with (
            patch.object(settings, "MODEL_INPUT_PRICE_PER_1M", 1.0),
            patch.object(settings, "MODEL_OUTPUT_PRICE_PER_1M", 2.0),
        ):
            result = run_review_workflow(
                self.db,
                expense_id=expense.id,
                review_log_id=log.id,
                agent=FakeAgent(outcome),
            )

        step = (
            self.db.query(WorkflowStep)
            .filter(WorkflowStep.run_id == result.run_id, WorkflowStep.node == "risk")
            .one()
        )
        self.assertEqual(step.prompt_tokens, 10)
        self.assertEqual(step.completion_tokens, 5)
        self.assertEqual(step.cost, Decimal("0.000020"))

        run = self.db.query(WorkflowRun).filter(WorkflowRun.id == result.run_id).one()
        self.assertEqual(run.prompt_tokens, 10)
        self.assertEqual(run.completion_tokens, 5)
        self.assertEqual(run.cost, Decimal("0.000020"))
