"""
两阶段结算回归测试：财务登记 → 提交人确认领取，以及通知/权限/幂等
"""
import unittest
from decimal import Decimal

import tests  # noqa: F401
from app.models import ApprovalFlow, Notification
from app.models.expense import ExpenseStatus
from app.services.settlement_service import (
    SettlementError,
    cancel_registration,
    confirm_received,
    force_close,
    list_settlements,
    register,
)
from tests.support import BaseTestCase, make_expense


class SettlementServiceTest(BaseTestCase, unittest.TestCase):
    """登记 / 撤销 / 确认 / 强制关闭"""

    def _expense(self, status=ExpenseStatus.APPROVED):
        flow = self.db.query(ApprovalFlow).first()
        expense = make_expense(
            self.db, self.fx["users"]["employee"],
            status=status, flow_id=flow.id, amount=Decimal("1200.00"),
        )
        self.db.commit()
        return expense

    def test_register_then_confirm(self):
        """登记后通知提交人；提交人确认后变为 paid"""
        expense = self._expense()
        finance = self.fx["users"]["finance"]

        registered = register(self.db, expense.id, finance, note="请查收")
        self.assertEqual(registered.status, ExpenseStatus.REGISTERED)
        self.assertEqual(registered.registered_by, finance.id)
        self.assertIsNotNone(registered.registered_at)
        note = self.db.query(Notification).filter(Notification.user_id == expense.user_id).one()
        self.assertIn("请查收", note.content)

        confirmed = confirm_received(self.db, expense.id, self.fx["users"]["employee"])
        self.assertEqual(confirmed.status, ExpenseStatus.PAID)
        self.assertIsNotNone(confirmed.paid_at)
        self.assertIsNotNone(confirmed.confirmed_at)
        self.assertEqual(len(list_settlements(self.db, expense.id, self.fx["users"]["employee"])), 2)

    def test_cannot_register_own(self):
        """财务不能登记自己提交的单据"""
        finance = self.fx["users"]["finance"]
        flow = self.db.query(ApprovalFlow).first()
        expense = make_expense(self.db, finance, status=ExpenseStatus.APPROVED, flow_id=flow.id)
        self.db.commit()
        with self.assertRaises(SettlementError) as ctx:
            register(self.db, expense.id, finance)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_register_requires_approved(self):
        """只有 approved 可以登记"""
        expense = self._expense(status=ExpenseStatus.PENDING)
        with self.assertRaises(SettlementError) as ctx:
            register(self.db, expense.id, self.fx["users"]["finance"])
        self.assertEqual(ctx.exception.status_code, 409)

    def test_cancel_registration(self):
        """撤销登记回到 approved 并清空登记字段"""
        expense = self._expense()
        finance = self.fx["users"]["finance"]
        register(self.db, expense.id, finance)
        cancelled = cancel_registration(self.db, expense.id, finance, reason="登记错误")
        self.assertEqual(cancelled.status, ExpenseStatus.APPROVED)
        self.assertIsNone(cancelled.registered_at)
        self.assertIsNone(cancelled.registered_by)

    def test_confirm_only_owner(self):
        """非提交人不能确认领取"""
        expense = self._expense()
        register(self.db, expense.id, self.fx["users"]["finance"])
        with self.assertRaises(SettlementError) as ctx:
            confirm_received(self.db, expense.id, self.fx["users"]["manager"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_force_close_admin_only(self):
        """只有 admin 能强制关闭，且原因必填（schema 层校验）"""
        expense = self._expense()
        register(self.db, expense.id, self.fx["users"]["finance"])
        with self.assertRaises(SettlementError) as ctx:
            force_close(self.db, expense.id, self.fx["users"]["finance"], reason="越权")
        self.assertEqual(ctx.exception.status_code, 403)
        closed = force_close(self.db, expense.id, self.fx["users"]["admin"], reason="员工离职")
        self.assertEqual(closed.status, ExpenseStatus.PAID)

    def test_transaction_no_unique(self):
        """重复流水号登记报 409"""
        first = self._expense()
        second = self._expense()
        finance = self.fx["users"]["finance"]
        register(self.db, first.id, finance, transaction_no="TXN-001")
        with self.assertRaises(SettlementError) as ctx:
            register(self.db, second.id, finance, transaction_no="TXN-001")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_duplicate_register(self):
        """重复登记报 409"""
        expense = self._expense()
        finance = self.fx["users"]["finance"]
        register(self.db, expense.id, finance)
        with self.assertRaises(SettlementError) as ctx:
            register(self.db, expense.id, finance)
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
