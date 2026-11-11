"""
僵尸工作流收尾测试（离线，不联网；使用测试库）
    覆盖：
        - 超时 run 收尾为 failed，并写结束时间与原因；
        - 关联的 running 预审日志一并收尾；
        - 新鲜 run / 已完成 run 不受影响；
        - dry-run 不改状态；
        - 重复执行幂等；
        - limit 生效。
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.models import AIReviewLog, AIReviewStatus, WorkflowRun, WorkflowRunStatus
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import create_review
from app.services.workflow_recovery_service import STALE_ERROR, recover_stale_runs
from tests.support import BaseTestCase, make_expense



class WorkflowRecoveryTest(BaseTestCase, unittest.TestCase):
    """僵尸 run 收尾"""

    def pending_expense(self):
        """
        造一张待审批单据
            :return: Expense
        """
        return make_expense(
            self.db,
            self.fx["users"]["employee"],
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("100.00"),
            category_id=self.fx["categories"]["rail"].id,
        )

    def make_run(self, expense, *, log=None, minutes_ago: int = 60,
                 status: WorkflowRunStatus = WorkflowRunStatus.RUNNING):
        """
        造一条指定"年龄"的工作流运行
            :param expense: 报销单
            :param log: 关联的预审日志
            :param minutes_ago: 创建/开始时间距离现在多少分钟
            :param status: run 状态
            :return: WorkflowRun
        """
        old = datetime.now() - timedelta(minutes=minutes_ago)
        run = WorkflowRun(
            expense_id=expense.id,
            review_log_id=log.id if log is not None else None,
            status=status,
            engine="langgraph",
            started_at=old,
            created_at=old,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def test_stale_run_closed(self):
        """超时 run 收尾为 failed，并同步收尾 running 日志"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        log.status = AIReviewStatus.RUNNING
        self.db.commit()
        run = self.make_run(expense, log=log)

        summary = recover_stale_runs(self.db, stale_minutes=15, limit=100)

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(summary["run_ids"], [run.id])
        self.db.refresh(run)
        self.assertEqual(run.status, WorkflowRunStatus.FAILED)
        self.assertIn("超时", run.error_message)
        self.assertIsNotNone(run.finished_at)
        self.db.refresh(log)
        self.assertEqual(log.status, AIReviewStatus.FAILED)
        self.assertEqual(log.error_message, STALE_ERROR)

    def test_fresh_run_untouched(self):
        """刚启动的 run 不算僵尸"""
        expense = self.pending_expense()
        run = self.make_run(expense, minutes_ago=0)

        summary = recover_stale_runs(self.db, stale_minutes=15, limit=100)

        self.assertEqual(summary["scanned"], 0)
        self.db.refresh(run)
        self.assertEqual(run.status, WorkflowRunStatus.RUNNING)

    def test_finished_run_untouched(self):
        """已完成/已失败的 run 不会再被处理"""
        expense = self.pending_expense()
        run = self.make_run(expense, minutes_ago=120, status=WorkflowRunStatus.SUCCESS)

        summary = recover_stale_runs(self.db, stale_minutes=15, limit=100)

        self.assertEqual(summary["scanned"], 0)
        self.db.refresh(run)
        self.assertEqual(run.status, WorkflowRunStatus.SUCCESS)

    def test_dry_run_keeps_status(self):
        """dry-run 只统计，不改状态"""
        expense = self.pending_expense()
        run = self.make_run(expense, minutes_ago=60)

        summary = recover_stale_runs(self.db, stale_minutes=15, limit=100, dry_run=True)

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["recovered"], 0)
        self.assertEqual(summary["run_ids"], [run.id])
        self.db.refresh(run)
        self.assertEqual(run.status, WorkflowRunStatus.RUNNING)

    def test_idempotent(self):
        """重复执行：第二次命中 0 条"""
        expense = self.pending_expense()
        self.make_run(expense, minutes_ago=60)

        first = recover_stale_runs(self.db, stale_minutes=15, limit=100)
        second = recover_stale_runs(self.db, stale_minutes=15, limit=100)

        self.assertEqual(first["recovered"], 1)
        self.assertEqual(second["scanned"], 0)

    def test_limit_applied(self):
        """limit 生效：只处理指定条数"""
        expense = self.pending_expense()
        runs = [self.make_run(expense, minutes_ago=60) for _ in range(3)]

        summary = recover_stale_runs(self.db, stale_minutes=15, limit=2)

        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["recovered"], 2)
        self.db.refresh(runs[2])
        self.assertEqual(runs[2].status, WorkflowRunStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
