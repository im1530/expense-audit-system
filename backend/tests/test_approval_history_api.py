"""
审批历史接口的回归测试（直接调用路由函数）

    覆盖：待办记录 approver_role 为空时的响应校验，
          以及“一条任务一行”模型下，已处理记录直接返回审批人与处理时间。
"""
import unittest
from datetime import datetime
from decimal import Decimal

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1.expense import get_expense_approval_history
from app.models.approval import Approval, ApprovalFlow, ApprovalStatus
from app.models.expense import ExpenseStatus
from app.schemas.approval import ApprovalRecordItem
from tests.support import BaseTestCase, make_expense


class ApprovalHistoryApiTest(BaseTestCase, unittest.TestCase):
    """审批轨迹的角色字段与处理时间"""

    def _expense(self) -> tuple:
        flow = self.db.query(ApprovalFlow).first()
        expense = make_expense(
            self.db, self.fx["users"]["employee"],
            status=ExpenseStatus.PENDING, flow_id=flow.id, amount=Decimal("100.00"),
        )
        return expense, flow

    def test_pending_record_role_is_none(self):
        """待处理记录 approver_role 为 None，且能通过响应模型校验"""
        expense, flow = self._expense()
        self.db.add(Approval(
            expense_id=expense.id, flow_id=flow.id, level=1,
            approver_id=None, status=ApprovalStatus.PENDING,
            comment="单据提交，等待审批",
        ))
        self.db.commit()

        rows = get_expense_approval_history(expense.id, db=self.db, current_user=self.fx["users"]["manager"])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["approver_role"])
        self.assertIsNone(rows[0]["acted_at"])
        ApprovalRecordItem.model_validate(rows[0])

    def test_acted_record_has_approver_and_time(self):
        """已处理记录只有一行：审批人、状态、处理时间都在同一条任务上"""
        expense, flow = self._expense()
        manager = self.fx["users"]["manager"]
        acted = datetime(2026, 11, 21, 10, 0, 0)
        self.db.add(Approval(
            expense_id=expense.id, flow_id=flow.id, level=1,
            approver_id=manager.id, status=ApprovalStatus.APPROVED,
            comment="同意", acted_at=acted,
        ))
        self.db.commit()

        rows = get_expense_approval_history(expense.id, db=self.db, current_user=manager)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approver_role"], "manager")
        self.assertEqual(rows[0]["acted_at"], acted)
        ApprovalRecordItem.model_validate(rows[0])


if __name__ == "__main__":
    unittest.main()
