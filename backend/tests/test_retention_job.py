"""
保留期清理与运维审计测试（离线，不联网；使用测试库）
    覆盖：
        - 附件清理：删文件/识别记录/附件元数据，dry-run 不动数据；
        - raw_response 置空、过期检查点清理；
        - 运维审计记录：开始/成功/失败；
        - 互斥锁：重复获取失败、过期锁可清理；
        - 任务入口：dry-run 执行成功并写审计，锁被占用时跳过。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.models import (
    AIReviewLog,
    Attachment,
    DocumentExtraction,
    DocumentExtractionStatus,
    ExpenseItem,
    MaintenanceRun,
    MaintenanceStatus,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import create_review
from app.services.maintenance_service import begin_run, finish_run
from app.services.retention_service import (
    purge_attachments,
    purge_checkpoints,
    purge_raw_responses,
    run_retention,
)
from scripts import run_retention_job
from tests.support import BaseTestCase, make_expense



# 造一个"过期时间"
def old_time(days: int = 120) -> datetime:
    """
    返回指定天数之前的时间
        :param days: 天数
        :return: datetime
    """
    return datetime.now() - timedelta(days=days)



class RetentionServiceTest(BaseTestCase, unittest.TestCase):
    """清理逻辑"""

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

    def make_attachment(self, expense, *, created_at: datetime | None = None):
        """
        造一条附件与识别记录
            :param expense: 报销单
            :param created_at: 附件创建时间
            :return: (Attachment, DocumentExtraction)
        """
        item = self.db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).first()
        attachment = Attachment(
            expense_item_id=item.id,
            file_name="invoice.png",
            file_path="invoices/2026/11/a.png",
            content_type="image/png",
            file_size=128,
            uploaded_by=self.fx["users"]["employee"].id,
        )
        if created_at is not None:
            attachment.created_at = created_at
        self.db.add(attachment)
        self.db.flush()
        extraction = DocumentExtraction(
            attachment_id=attachment.id,
            expense_item_id=item.id,
            status=DocumentExtractionStatus.SUCCESS,
        )
        if created_at is not None:
            extraction.created_at = created_at
        self.db.add(extraction)
        self.db.commit()
        return attachment, extraction

    def test_purge_attachments(self):
        """过期附件：文件记录与识别记录一并删除"""
        expense = self.pending_expense()
        attachment, extraction = self.make_attachment(expense, created_at=old_time(120))
        attachment_id = attachment.id
        extraction_id = extraction.id

        handled = purge_attachments(self.db, cutoff=old_time(90), limit=50, dry_run=False)

        self.assertEqual(handled, 1)
        self.assertIsNone(self.db.query(Attachment).filter(Attachment.id == attachment_id).first())
        self.assertIsNone(
            self.db.query(DocumentExtraction).filter(DocumentExtraction.id == extraction_id).first()
        )

    def test_dry_run_keeps_attachments(self):
        """dry-run 只统计，不删除"""
        expense = self.pending_expense()
        attachment, _ = self.make_attachment(expense, created_at=old_time(120))

        handled = purge_attachments(self.db, cutoff=old_time(90), limit=50, dry_run=True)

        self.assertEqual(handled, 1)
        self.assertIsNotNone(self.db.query(Attachment).filter(Attachment.id == attachment.id).first())

    def test_purge_raw_responses(self):
        """过期 raw_response 置空，结构化字段保留"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        log.raw_response = "{}"
        log.created_at = old_time(60)
        self.db.commit()

        handled = purge_raw_responses(self.db, cutoff=old_time(30), limit=50, dry_run=False)

        self.assertEqual(handled, 1)
        self.db.refresh(log)
        self.assertIsNone(log.raw_response)
        self.assertEqual(log.expense_id, expense.id)

    def test_purge_checkpoints(self):
        """过期且已结束的 run：检查点被清理"""
        expense = self.pending_expense()
        run = WorkflowRun(
            expense_id=expense.id,
            status=WorkflowRunStatus.SUCCESS,
            engine="langgraph",
            started_at=old_time(120),
            finished_at=old_time(120),
            created_at=old_time(120),
        )
        self.db.add(run)
        self.db.commit()

        handled = purge_checkpoints(self.db, cutoff=old_time(90), limit=50, dry_run=False)

        self.assertEqual(handled, 1)

    def test_run_retention_summary(self):
        """一次清理返回完整汇总"""
        expense = self.pending_expense()
        self.make_attachment(expense, created_at=old_time(120))

        summary = run_retention(self.db, dry_run=False, days=90, raw_days=30, limit=50)

        self.assertEqual(summary["days"], 90)
        self.assertEqual(summary["raw_days"], 30)
        self.assertEqual(summary["attachments"], 1)
        self.assertEqual(summary["raw_responses"], 0)
        self.assertEqual(summary["checkpoints"], 0)



class MaintenanceServiceTest(BaseTestCase, unittest.TestCase):
    """运维审计记录"""

    def test_begin_and_finish_success(self):
        """开始为 running，结束写成功与汇总"""
        run = begin_run(self.db, "retention_purge")
        self.assertEqual(run.status, MaintenanceStatus.RUNNING)
        self.assertIsNone(run.finished_at)

        finish_run(self.db, run, status=MaintenanceStatus.SUCCESS, summary={"attachments": 1})

        self.db.refresh(run)
        self.assertEqual(run.status, MaintenanceStatus.SUCCESS)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.summary["attachments"], 1)

    def test_finish_failed(self):
        """失败记录带错误原因"""
        run = begin_run(self.db, "retention_purge")
        finish_run(self.db, run, status=MaintenanceStatus.FAILED, error="磁盘不可写")

        self.db.refresh(run)
        self.assertEqual(run.status, MaintenanceStatus.FAILED)
        self.assertEqual(run.error_message, "磁盘不可写")



class RetentionJobTest(BaseTestCase, unittest.TestCase):
    """定时任务入口与互斥锁"""

    def test_lock_exclusive(self):
        """同一时刻只允许一个任务持锁"""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "retention.lock"
            self.assertTrue(run_retention_job.acquire_lock(lock))
            self.assertFalse(run_retention_job.acquire_lock(lock))
            run_retention_job.release_lock(lock)
            self.assertTrue(run_retention_job.acquire_lock(lock))
            run_retention_job.release_lock(lock)

    def test_stale_lock_recovered(self):
        """过期锁会被清理并重新获取"""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "retention.lock"
            lock.write_text("99999", encoding="utf-8")
            ancient = datetime.now().timestamp() - run_retention_job.STALE_LOCK_SECONDS - 60
            os.utime(lock, (ancient, ancient))

            self.assertTrue(run_retention_job.acquire_lock(lock))
            run_retention_job.release_lock(lock)

    def test_main_dry_run_writes_audit(self):
        """任务入口：dry-run 成功执行并写入审计"""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "retention.lock"
            with patch.object(run_retention_job, "LOCK_FILE", lock), \
                    patch.object(sys, "argv", ["run_retention_job.py", "--dry-run", "--days", "90"]):
                code = run_retention_job.main()

        self.assertEqual(code, 0)
        self.db.rollback()
        record = (
            self.db.query(MaintenanceRun)
            .filter(MaintenanceRun.job == run_retention_job.JOB_NAME)
            .order_by(MaintenanceRun.id.desc())
            .first()
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.status, MaintenanceStatus.SUCCESS)
        self.assertTrue(record.summary["dry_run"])

    def test_main_skips_when_locked(self):
        """锁被占用时直接跳过，不写审计"""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "retention.lock"
            lock.write_text("1", encoding="utf-8")
            with patch.object(run_retention_job, "LOCK_FILE", lock), \
                    patch.object(sys, "argv", ["run_retention_job.py", "--dry-run"]):
                code = run_retention_job.main()

        self.assertEqual(code, 0)
        self.db.rollback()
        self.assertEqual(self.db.query(MaintenanceRun).count(), 0)


if __name__ == "__main__":
    unittest.main()
