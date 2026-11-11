"""
Phase D 工作流测试（离线，不联网；使用测试库）
    覆盖：决策策略、节点顺序、run/step 审计、LLM 降级。
"""
import asyncio
import unittest
from decimal import Decimal

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.risk_agent import ReviewOutcome
from app.models import WorkflowRun, WorkflowRunStatus, WorkflowStep
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import create_review
from app.services.llm_client import LLMTimeoutError
from app.workflow.decision import (
    DECISION_FAST_TRACK,
    DECISION_MANUAL_REVIEW,
    DECISION_SUGGEST_REJECT,
    decide_review,
)
from app.workflow.service import resume_review_workflow, run_review_workflow
from tests.support import BaseTestCase, make_expense



# 假风险 Agent
class FakeAgent:
    """返回固定结果或抛错"""

    def __init__(self, outcome: ReviewOutcome | None = None, error: Exception | None = None):
        """
        :param outcome: 固定结果
        :param error: 要抛出的异常
        """
        self.outcome = outcome or make_outcome()
        self.error = error
        self.calls = 0

    async def review(self, context, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果
            :param context: 上下文
            :param policy: 制度结果
            :return: ReviewOutcome
        """
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.outcome



def make_outcome(
    *,
    score: str = "20.00",
    level: str = "low",
    findings: list | None = None,
    is_fallback: bool = False,
) -> ReviewOutcome:
    """
    构造风险结果
        :param score: 分数
        :param level: 等级
        :param findings: 风险点
        :param is_fallback: 是否兜底
        :return: ReviewOutcome
    """
    return ReviewOutcome(
        risk_score=Decimal(score),
        risk_level=level,
        summary="测试结论",
        findings=findings or [],
        policy_citations=[],
        is_fallback=is_fallback,
        model="fake",
        prompt_version="fake_v1",
        latency_ms=1,
        prompt_tokens=1,
        completion_tokens=1,
        raw_content="{}",
    )



class DecisionTest(unittest.TestCase):
    """确定性决策"""

    def test_high_is_suggest_reject(self):
        """高风险 → 建议退回"""
        outcome = make_outcome(score="80", level="high", findings=[
            {"type": "invoice", "level": "high", "message": "金额不足"},
        ])
        self.assertEqual(decide_review(outcome)["decision"], DECISION_SUGGEST_REJECT)

    def test_mid_is_manual(self):
        """中风险 → 人工复核"""
        outcome = make_outcome(score="55", level="mid", findings=[
            {"type": "invoice", "level": "mid", "message": "日期偏差"},
        ])
        self.assertEqual(decide_review(outcome)["decision"], DECISION_MANUAL_REVIEW)

    def test_low_no_findings_is_fast_track(self):
        """低风险无 finding → 建议快速审批"""
        self.assertEqual(decide_review(make_outcome())["decision"], DECISION_FAST_TRACK)

    def test_low_only_findings_fast_track(self):
        """低风险且只有 low finding：提示性信息不阻断 fast_track"""
        outcome = make_outcome(
            score="20",
            level="low",
            findings=[{"type": "other", "level": "low", "message": "说明较简略"}],
        )
        self.assertEqual(decide_review(outcome)["decision"], DECISION_FAST_TRACK)

    def test_fallback_is_manual(self):
        """降级 → 人工复核"""
        outcome = make_outcome(is_fallback=True)
        self.assertEqual(
            decide_review(outcome, fallback_reason="超时")["decision"],
            DECISION_MANUAL_REVIEW,
        )



class WorkflowServiceTest(BaseTestCase, unittest.TestCase):
    """工作流执行与审计"""

    def pending_expense(self):
        """
        造一张待审批单据
            :return: Expense
        """
        employee = self.fx["users"]["employee"]
        return make_expense(
            self.db,
            employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("100.00"),
            category_id=self.fx["categories"]["rail"].id,
        )

    def test_workflow_success_and_steps(self):
        """无附件：跳过 document，跑完 rule/rag/risk/decision"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent(outcome=make_outcome(score="20", level="low"))

        result = run_review_workflow(
            self.db,
            expense_id=expense.id,
            review_log_id=log.id,
            agent=agent,
        )

        self.assertEqual(result.decision["decision"], DECISION_FAST_TRACK)
        self.assertEqual(result.outcome.risk_level, "low")
        run = self.db.query(WorkflowRun).filter(WorkflowRun.id == result.run_id).first()
        self.assertEqual(run.status, WorkflowRunStatus.SUCCESS)
        steps = self.db.query(WorkflowStep).filter(WorkflowStep.run_id == result.run_id).all()
        self.assertEqual([step.node for step in steps], ["load_context", "rule", "rag", "risk", "decision"])

    def test_workflow_llm_failure_falls_back(self):
        """风险节点 LLM 失败：走规则兜底，决策人工复核"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent(error=LLMTimeoutError("超时", retryable=True))

        result = run_review_workflow(
            self.db,
            expense_id=expense.id,
            review_log_id=log.id,
            agent=agent,
        )

        self.assertTrue(result.outcome.is_fallback)
        self.assertEqual(result.decision["decision"], DECISION_MANUAL_REVIEW)
        run = self.db.query(WorkflowRun).filter(WorkflowRun.id == result.run_id).first()
        self.assertEqual(run.status, WorkflowRunStatus.SUCCESS)

    def test_resume_completed_run_returns_checkpoint_result(self):
        """已完成的 run 能从 MySQL 检查点恢复并返回原结论"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        first = run_review_workflow(
            self.db,
            expense_id=expense.id,
            review_log_id=log.id,
            agent=FakeAgent(outcome=make_outcome(score="20", level="low")),
        )

        resumed = resume_review_workflow(
            self.db,
            run_id=first.run_id,
            agent=FakeAgent(outcome=make_outcome(score="20", level="low")),
        )

        self.assertEqual(resumed.run_id, first.run_id)
        self.assertEqual(resumed.outcome.risk_level, "low")
        self.assertEqual(resumed.decision["decision"], DECISION_FAST_TRACK)

    def test_resume_missing_run_raises(self):
        """run 不存在：明确报错"""
        with self.assertRaises(ValueError):
            resume_review_workflow(self.db, run_id=999999)

    def test_resume_from_failed_node(self):
        """风险节点崩溃后，能从 MySQL 检查点续跑到决策"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        with self.assertRaises(RuntimeError):
            run_review_workflow(
                self.db,
                expense_id=expense.id,
                review_log_id=log.id,
                agent=FakeAgent(error=RuntimeError("agent boom")),
            )

        run = (
            self.db.query(WorkflowRun)
            .filter(WorkflowRun.expense_id == expense.id)
            .order_by(WorkflowRun.id.desc())
            .first()
        )
        self.assertEqual(run.status, WorkflowRunStatus.FAILED)

        resumed = resume_review_workflow(
            self.db,
            run_id=run.id,
            agent=FakeAgent(outcome=make_outcome(score="20", level="low")),
        )
        self.assertEqual(resumed.run_id, run.id)
        self.assertEqual(resumed.outcome.risk_level, "low")
        self.assertEqual(resumed.decision["decision"], DECISION_FAST_TRACK)


if __name__ == "__main__":
    unittest.main()
