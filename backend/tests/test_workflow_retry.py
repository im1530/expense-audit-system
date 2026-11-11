"""
工作流节点重试测试（Phase F）
    覆盖：
        - 可重试异常自动重试，attempt 递增；
        - 不可重试异常立即失败；
        - 重试耗尽后抛出；
        - WorkflowRecorder 的 attempt 单调递增（恢复后不回退）；
        - 真实工作流中节点失败一次后重试成功。
    约束：使用测试库，不联网；重试退避用 mock 跳过。
"""
import unittest
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.risk_agent import ReviewOutcome
from app.config import settings
from app.models import WorkflowStep, WorkflowStepStatus
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import create_review
from app.workflow.engine import WorkflowRecorder, _wrap_node
from app.workflow.service import run_review_workflow
from tests.support import BaseTestCase, make_expense



# 记录器替身
class DummyRecorder:
    """只收集 record 调用，不写库"""

    def __init__(self):
        """初始化空调用列表"""
        self.run_id = 1                                      # 假 run ID
        self.calls: list[dict] = []                          # 记录调用

    def record(self, node, status, *, attempt, latency_ms, prompt_tokens=0, completion_tokens=0, cost=None, summary=None, error=None):
        """
        收集一次记录
            :param node: 节点名
            :param status: 状态
            :param attempt: 尝试序号
            :param latency_ms: 耗时
            :param prompt_tokens: 输入 token
            :param completion_tokens: 输出 token
            :param cost: 费用
            :param summary: 摘要
            :param error: 错误
        """
        self.calls.append({
            "node": node,
            "status": status,
            "attempt": attempt,
            "latency_ms": latency_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": cost,
            "summary": summary,
            "error": error,
        })



class WrapNodeRetryTest(unittest.TestCase):
    """节点包装器重试"""

    def test_retryable_error_retries_then_succeeds(self):
        """瞬态异常：重试后成功，记录 FAILED + SUCCESS"""
        recorder = DummyRecorder()
        calls = {"n": 0}

        def flaky(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("temporary")
            return {"ok": True}

        with (
            patch.object(settings, "WORKFLOW_NODE_MAX_ATTEMPTS", 2),
            patch.object(settings, "WORKFLOW_NODE_RETRY_BACKOFF_SECONDS", 0.0),
            patch("app.workflow.engine.time.sleep"),
        ):
            wrapped = _wrap_node(recorder, "risk", flaky)
            result = wrapped({})

        self.assertTrue(result["ok"])
        self.assertEqual([(c["status"], c["attempt"]) for c in recorder.calls], [
            (WorkflowStepStatus.FAILED, 1),
            (WorkflowStepStatus.SUCCESS, 2),
        ])

    def test_non_retryable_error_fails_immediately(self):
        """不可重试异常：只执行一次"""
        recorder = DummyRecorder()

        def boom(state):
            raise ValueError("bad data")

        with patch.object(settings, "WORKFLOW_NODE_MAX_ATTEMPTS", 3):
            wrapped = _wrap_node(recorder, "rule", boom)
            with self.assertRaises(ValueError):
                wrapped({})

        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["status"], WorkflowStepStatus.FAILED)

    def test_retry_exhausted_raises(self):
        """重试耗尽：记录两次失败后抛出"""
        recorder = DummyRecorder()

        def always_timeout(state):
            raise TimeoutError("db timeout")

        with (
            patch.object(settings, "WORKFLOW_NODE_MAX_ATTEMPTS", 2),
            patch.object(settings, "WORKFLOW_NODE_RETRY_BACKOFF_SECONDS", 0.0),
            patch("app.workflow.engine.time.sleep"),
        ):
            wrapped = _wrap_node(recorder, "decision", always_timeout)
            with self.assertRaises(TimeoutError):
                wrapped({})

        self.assertEqual([c["attempt"] for c in recorder.calls], [1, 2])



class WorkflowRecorderAttemptTest(BaseTestCase, unittest.TestCase):
    """attempt 单调递增"""

    def test_attempt_increases_even_if_caller_passes_one(self):
        """重复传 attempt=1 时，实际落库仍递增，恢复后不会回退"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.PENDING)
        recorder = WorkflowRecorder(self.db, expense_id=expense.id, review_log_id=None)

        recorder.record("risk", WorkflowStepStatus.SUCCESS, attempt=1, latency_ms=1)
        recorder.record("risk", WorkflowStepStatus.SUCCESS, attempt=1, latency_ms=1)

        steps = (
            self.db.query(WorkflowStep)
            .filter(WorkflowStep.run_id == recorder.run_id, WorkflowStep.node == "risk")
            .order_by(WorkflowStep.id.asc())
            .all()
        )
        self.assertEqual([step.attempt for step in steps], [1, 2])



# 假风险 Agent（不联网）
class FakeAgent:
    """返回固定低风险结果"""

    async def review(self, context, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果
            :param context: 上下文
            :param policy: 制度结果
            :return: ReviewOutcome
        """
        return ReviewOutcome(
            risk_score=Decimal("20.00"),
            risk_level="low",
            summary="测试结论",
            findings=[],
            policy_citations=[],
            is_fallback=False,
            model="fake",
            prompt_version="fake_v1",
            latency_ms=1,
            prompt_tokens=0,
            completion_tokens=0,
        )



class WorkflowNodeRetryIntegrationTest(BaseTestCase, unittest.TestCase):
    """真实工作流中的节点重试"""

    def test_rule_node_retries_and_records_attempts(self):
        """rule 节点第一次超时，第二次成功，workflow_steps 记录两次尝试"""
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

        from app.workflow import engine

        original_rule = engine.rule_node
        calls = {"n": 0}

        def flaky_rule(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("temporary")
            return original_rule(state)

        with (
            patch("app.workflow.engine.rule_node", flaky_rule),
            patch("app.workflow.engine.time.sleep"),
            patch.object(settings, "WORKFLOW_NODE_MAX_ATTEMPTS", 2),
            patch.object(settings, "WORKFLOW_NODE_RETRY_BACKOFF_SECONDS", 0.0),
        ):
            result = run_review_workflow(
                self.db,
                expense_id=expense.id,
                review_log_id=log.id,
                agent=FakeAgent(),
            )

        steps = (
            self.db.query(WorkflowStep)
            .filter(WorkflowStep.run_id == result.run_id, WorkflowStep.node == "rule")
            .order_by(WorkflowStep.id.asc())
            .all()
        )
        self.assertEqual(
            [(step.attempt, step.status) for step in steps],
            [(1, WorkflowStepStatus.FAILED), (2, WorkflowStepStatus.SUCCESS)],
        )
