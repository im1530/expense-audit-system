"""
报销单金额不变量测试

    目标：无论建单、加明细、改明细、删明细，主表 total_amount 都必须等于明细表的 SUM(amount)。
    这条测试专门防止“建单用内存 sum、明细操作用 DB SUM”两套算法未来漂移。
"""
import unittest
from datetime import date
from decimal import Decimal

from sqlalchemy import func

import tests  # noqa: F401
from app.api.v1.expense import (
    add_expense_item,
    create_expense,
    delete_expense_item,
    update_expense_item,
)
from app.models import Category, ExpenseItem
from app.schemas.expense import ExpenseCreate, ExpenseItemCreate, ExpenseItemUpdate
from tests.support import BaseTestCase


class ExpenseAmountInvariantTest(BaseTestCase, unittest.TestCase):
    """建单与明细增删改后的金额一致性"""

    def _assert_total_matches_items(self, expense):
        """断言主表金额 == 明细表 SUM"""
        db_sum = self.db.query(func.sum(ExpenseItem.amount)).filter(
            ExpenseItem.expense_id == expense.id
        ).scalar() or Decimal("0")
        self.assertEqual(expense.total_amount, db_sum)

    def test_total_always_equals_item_sum(self):
        """建单 → 加明细 → 改金额 → 删明细，全程金额不变式成立"""
        employee = self.fx["users"]["employee"]
        category_id = self.db.query(Category).first().id

        payload = ExpenseCreate(
            title="金额不变量测试单",
            items=[
                ExpenseItemCreate(
                    category_id=category_id, description="高铁票",
                    amount=Decimal("100.10"), expense_date=date.today(),
                ),
                ExpenseItemCreate(
                    category_id=category_id, description="打车费",
                    amount=Decimal("20.20"), expense_date=date.today(),
                ),
            ],
        )
        expense = create_expense(payload=payload, db=self.db, current_user=employee)
        self.assertEqual(expense.total_amount, Decimal("120.30"))
        self._assert_total_matches_items(expense)

        expense = add_expense_item(
            expense.id,
            ExpenseItemCreate(
                category_id=category_id, description="新增餐费",
                amount=Decimal("30.30"), expense_date=date.today(),
            ),
            db=self.db, current_user=employee,
        )
        self.assertEqual(expense.total_amount, Decimal("150.60"))
        self._assert_total_matches_items(expense)

        first_item = self.db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).first()
        expense = update_expense_item(
            expense.id, first_item.id,
            ExpenseItemUpdate(amount=Decimal("99.99")),
            db=self.db, current_user=employee,
        )
        self._assert_total_matches_items(expense)

        expense = delete_expense_item(
            expense.id, first_item.id, db=self.db, current_user=employee,
        )
        self._assert_total_matches_items(expense)


if __name__ == "__main__":
    unittest.main()
