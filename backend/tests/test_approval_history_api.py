"""
审批历史接口的回归测试（直接调用路由函数）

    覆盖：提交时生成的“待处理”占位记录 approver_id 为空，
          响应里 approver_role 必须是 None（不能是空字符串），否则 response_model 校验会 500。
"""
import unittest
from decimal import Decimal

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1.expense import get_expense_approval_history
from app.models.approval import Approval, ApprovalFlow, ApprovalStatus
from app.models.expense import ExpenseStatus
from app.schemas.approval import ApprovalRecordItem
from tests.support import BaseTestCase, make_expense


class ApprovalHistoryApiTest(BaseTestCase, unittest.TestCase):
    """待处理占位记录的角色字段"""

    def test_pending_record_role_is_none(self):
        """待处理记录 approver_role 为 None，且能通过响应模型校验"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        flow = self.db.query(ApprovalFlow).first()

        expense = make_expense(
            self.db, employee,
            status=ExpenseStatus.PENDING,
            flow_id=flow.id,
            amount=Decimal("100.00"),
        )
        self.db.add_all([
            Approval(
                expense_id=expense.id, flow_id=flow.id, level=1,
                approver_id=None, status=ApprovalStatus.PENDING,
                comment="单据提交，等待审批",
            ),
            Approval(
                expense_id=expense.id, flow_id=flow.id, level=1,
                approver_id=manager.id, status=ApprovalStatus.APPROVED,
                comment="同意",
            ),
        ])
        self.db.commit()

        rows = get_expense_approval_history(expense.id, db=self.db, current_user=manager)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["approver_role"])
        self.assertEqual(rows[1]["approver_role"], "manager")
        for row in rows:
            ApprovalRecordItem.model_validate(row)  # 不抛异常即通过 response_model 校验


if __name__ == "__main__":
    unittest.main()
