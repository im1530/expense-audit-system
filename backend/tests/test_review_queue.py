"""
AI 预审异步队列测试（Phase F）
    覆盖：
        - 队列开关关闭时不投递；
        - 投递成功/失败的状态返回；
        - 队列不可用时回退 BackgroundTasks；
        - 自动触发开关、幂等复用、异常降级；
        - 提交接口确实调用自动触发。
    约束：不连真实 Redis，不调用真实 LLM，全部用 mock 替换外部依赖。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.background import BackgroundTasks

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1.expense import submit_expense
from app.config import settings
from app.models import AIReviewStatus
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import ReviewError
from app.services.review_queue import dispatch_review, enqueue_review, trigger_auto_review
from tests.support import BaseTestCase, make_expense



class ReviewQueueTest(unittest.TestCase):
    """任务投递与自动触发"""

    def test_enqueue_disabled_returns_false(self):
        """队列关闭：不投递，返回 False"""
        with patch.object(settings, "AI_REVIEW_QUEUE_ENABLED", False):
            self.assertFalse(enqueue_review(1))

    def test_enqueue_success_uses_deterministic_task_id(self):
        """队列开启：投递成功，任务ID可追踪"""
        with (
            patch.object(settings, "AI_REVIEW_QUEUE_ENABLED", True),
            patch("app.services.review_queue.execute_review_task") as task,
        ):
            self.assertTrue(enqueue_review(7))

        task.apply_async.assert_called_once_with(args=[7], task_id="ai-review-7")

    def test_enqueue_failure_returns_false(self):
        """Redis/Celery 异常：记录日志并返回 False，不向上抛"""
        with (
            patch.object(settings, "AI_REVIEW_QUEUE_ENABLED", True),
            patch("app.services.review_queue.execute_review_task") as task,
        ):
            task.apply_async.side_effect = RuntimeError("redis down")
            self.assertFalse(enqueue_review(8))

    def test_dispatch_falls_back_to_background_tasks(self):
        """队列关闭：回退 BackgroundTasks"""
        background = BackgroundTasks()
        with patch.object(settings, "AI_REVIEW_QUEUE_ENABLED", False):
            self.assertTrue(dispatch_review(3, background))
        self.assertEqual(len(background.tasks), 1)

    def test_dispatch_uses_queue_without_background_task(self):
        """队列可用：只投递 Celery，不重复加后台任务"""
        background = BackgroundTasks()
        with (
            patch.object(settings, "AI_REVIEW_QUEUE_ENABLED", True),
            patch("app.services.review_queue.enqueue_review", return_value=True),
        ):
            self.assertTrue(dispatch_review(3, background))
        self.assertEqual(len(background.tasks), 0)

    def test_auto_trigger_disabled_skips_create(self):
        """自动触发关闭：不创建任务"""
        with (
            patch.object(settings, "AI_REVIEW_AUTO_TRIGGER", False),
            patch("app.services.review_queue.create_review") as create,
        ):
            self.assertIsNone(trigger_auto_review(None, expense_id=1, user_id=1))
        create.assert_not_called()

    def test_auto_trigger_pending_enqueues(self):
        """自动触发：新建 pending 任务后投递"""
        log = SimpleNamespace(id=9, status=AIReviewStatus.PENDING)
        with (
            patch.object(settings, "AI_REVIEW_ENABLED", True),
            patch.object(settings, "AI_REVIEW_AUTO_TRIGGER", True),
            patch("app.services.review_queue.create_review", return_value=log),
            patch("app.services.review_queue.enqueue_review", return_value=True) as enqueue,
        ):
            result = trigger_auto_review(MagicMock(), expense_id=1, user_id=2)

        self.assertIs(result, log)
        enqueue.assert_called_once_with(9)

    def test_auto_trigger_reuses_cached_result(self):
        """缓存命中：已有结果不重复投递"""
        log = SimpleNamespace(id=10, status=AIReviewStatus.SUCCESS)
        with (
            patch.object(settings, "AI_REVIEW_ENABLED", True),
            patch.object(settings, "AI_REVIEW_AUTO_TRIGGER", True),
            patch("app.services.review_queue.create_review", return_value=log),
            patch("app.services.review_queue.enqueue_review") as enqueue,
        ):
            result = trigger_auto_review(MagicMock(), expense_id=1, user_id=2)

        self.assertIs(result, log)
        enqueue.assert_not_called()

    def test_auto_trigger_business_error_returns_none(self):
        """单据状态变化等业务异常：只记日志，不向上抛"""
        with (
            patch.object(settings, "AI_REVIEW_ENABLED", True),
            patch.object(settings, "AI_REVIEW_AUTO_TRIGGER", True),
            patch(
                "app.services.review_queue.create_review",
                side_effect=ReviewError("仅待审批状态可以执行 AI 预审", code="CONFLICT"),
            ),
        ):
            self.assertIsNone(trigger_auto_review(MagicMock(), expense_id=1, user_id=2))

    def test_auto_trigger_unexpected_error_returns_none(self):
        """未预期异常：自动触发失败不影响提交"""
        with (
            patch.object(settings, "AI_REVIEW_ENABLED", True),
            patch.object(settings, "AI_REVIEW_AUTO_TRIGGER", True),
            patch("app.services.review_queue.create_review", side_effect=RuntimeError("db down")),
        ):
            self.assertIsNone(trigger_auto_review(MagicMock(), expense_id=1, user_id=2))



class SubmitAutoTriggerTest(BaseTestCase, unittest.TestCase):
    """提交接口接入自动触发"""

    def test_submit_calls_auto_trigger(self):
        """提交成功后按当前用户与单据ID触发预审"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.DRAFT)

        with patch("app.api.v1.expense.trigger_auto_review") as trigger:
            submit_expense(expense_id=expense.id, db=self.db, current_user=user)

        trigger.assert_called_once_with(self.db, expense_id=expense.id, user_id=user.id)
