"""
报销单状态机测试
    覆盖主流程：草稿 → 待审批 → 审批通过 → 已支付，
    以及撤回、重新提交、撤销、非法流转拦截、审批资格校验。
    说明：直接调用路由函数（不经过 HTTP），既能覆盖业务逻辑，又不依赖 httpx。
"""
import unittest
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException

from app.api.v1.approval import approval_action, get_pending_approval_list
from app.api.v1.expense import (
    cancel_expense,
    confirm_received as confirm_received_route,
    pay_expense,
    recall_expense,
    register_expense as register_expense_route,
    resubmit_expense,
    submit_expense,
)
from app.schemas.common import BusinessFilter, PageParams
from app.schemas.settlement import ExpenseRegisterRequest
from app.models import Approval, ApprovalFlow, Department, Expense
from app.models.approval import ApprovalStatus
from app.models.expense import ExpenseStatus
from app.schemas.approval import ApprovalAction
from app.services.report_service import get_department_report
from tests.support import BaseTestCase, make_expense



class ExpenseStateMachineTest(BaseTestCase, unittest.TestCase):
    """报销单状态流转测试"""

    # 断言"抛出指定状态码的 HTTPException"
    def assert_http_error(self, context, status_code: int):
        """
        校验异常状态码与提示
            :param context: assertRaises 的上下文
            :param status_code: 期望的 HTTP 状态码
        """
        exception = context.exception
        self.assertIsInstance(exception, HTTPException)
        self.assertEqual(exception.status_code, status_code)

    # ---------- 提交 ----------
    def test_submit_draft_binds_flow_and_creates_pending_approval(self):
        """草稿提交：绑定审批流、状态转待审批、生成首条待办记录"""
        phone = self.fx["users"]["employee"]
        expense = make_expense(self.db, phone, status=ExpenseStatus.DRAFT)

        result = submit_expense(expense_id=expense.id, db=self.db, current_user=phone)

        self.assertEqual(result.status, ExpenseStatus.PENDING)
        self.assertEqual(result.flow_id, self.fx["flows"]["mgr"].id)
        self.assertIsNotNone(result.submitted_at)
        self.assertEqual(result.current_level, 1)
        self.assertEqual(result.department_id, phone.department_id)  # 提交时固化部门快照

        records = self.db.query(Approval).filter(Approval.expense_id == expense.id).all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, ApprovalStatus.PENDING)
        self.assertEqual(records[0].level, 1)
        self.assertIsNone(records[0].approver_id)

    def test_submit_non_draft_conflict(self):
        """非草稿单据不能再次提交（409）"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.PENDING, flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            submit_expense(expense_id=expense.id, db=self.db, current_user=user)
        self.assert_http_error(ctx, 409)

    def test_submit_without_items_conflict(self):
        """空单（没有明细）不能提交（409）"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.DRAFT, item_count=0)

        with self.assertRaises(HTTPException) as ctx:
            submit_expense(expense_id=expense.id, db=self.db, current_user=user)
        self.assert_http_error(ctx, 409)
        self.assertIn("明细", ctx.exception.detail)

    def test_submit_others_expense_not_found(self):
        """不能提交别人的单据（返回 404，不暴露是否存在）"""
        owner = self.fx["users"]["employee"]
        other = self.fx["users"]["manager2"]
        expense = make_expense(self.db, owner, status=ExpenseStatus.DRAFT)

        with self.assertRaises(HTTPException) as ctx:
            submit_expense(expense_id=expense.id, db=self.db, current_user=other)
        self.assert_http_error(ctx, 404)

    # ---------- 撤回 ----------
    def test_recall_pending_back_to_draft(self):
        """撤回：待审批 → 草稿，同时把待办记录标记为已撤回"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.DRAFT)
        submit_expense(expense_id=expense.id, db=self.db, current_user=user)

        result = recall_expense(expense_id=expense.id, db=self.db, current_user=user)

        self.assertEqual(result.status, ExpenseStatus.DRAFT)
        self.assertIsNone(result.submitted_at)
        record = self.db.query(Approval).filter(Approval.expense_id == expense.id).first()
        self.assertEqual(record.status, ApprovalStatus.RECALLED)

    def test_recall_draft_conflict(self):
        """草稿状态无需撤回（409）"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.DRAFT)

        with self.assertRaises(HTTPException) as ctx:
            recall_expense(expense_id=expense.id, db=self.db, current_user=user)
        self.assert_http_error(ctx, 409)

    # ---------- 撤销 ----------
    def test_cancel_draft(self):
        """草稿可以直接撤销，记录撤销时间"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.DRAFT)

        result = cancel_expense(expense_id=expense.id, db=self.db, current_user=user)

        self.assertEqual(result.status, ExpenseStatus.CANCELLED)
        self.assertIsNotNone(result.cancelled_at)

    def test_cancel_approved_conflict(self):
        """已审批通过的单据不允许撤销（409）"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.APPROVED, flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            cancel_expense(expense_id=expense.id, db=self.db, current_user=user)
        self.assert_http_error(ctx, 409)

    # ---------- 结算：登记 → 确认领取 ----------
    def test_pay_endpoint_deprecated(self):
        """旧支付接口返回 410，避免绕过登记"""
        employee = self.fx["users"]["employee"]
        finance = self.fx["users"]["finance"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                               flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            pay_expense(expense_id=expense.id, db=self.db, current_user=finance)
        self.assert_http_error(ctx, 410)

    def test_register_then_confirm_success(self):
        """财务登记 → 提交人确认 → 已支付"""
        employee = self.fx["users"]["employee"]
        finance = self.fx["users"]["finance"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                               flow_id=self.fx["flows"]["mgr"].id)

        registered = register_expense_route(
            expense.id, ExpenseRegisterRequest(note="请查收"),
            db=self.db, current_user=finance,
        )
        self.assertEqual(registered.status, ExpenseStatus.REGISTERED)

        paid = confirm_received_route(expense.id, db=self.db, current_user=employee)
        self.assertEqual(paid.status, ExpenseStatus.PAID)
        self.assertIsNotNone(paid.paid_at)

    def test_register_pending_conflict(self):
        """未审批通过的单据不能登记（409）"""
        employee = self.fx["users"]["employee"]
        finance = self.fx["users"]["finance"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            register_expense_route(expense.id, ExpenseRegisterRequest(), db=self.db, current_user=finance)
        self.assert_http_error(ctx, 409)

    def test_register_twice_conflict(self):
        """重复登记被拦截（409）"""
        employee = self.fx["users"]["employee"]
        finance = self.fx["users"]["finance"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                               flow_id=self.fx["flows"]["mgr"].id)
        register_expense_route(expense.id, ExpenseRegisterRequest(), db=self.db, current_user=finance)

        with self.assertRaises(HTTPException) as ctx:
            register_expense_route(expense.id, ExpenseRegisterRequest(), db=self.db, current_user=finance)
        self.assert_http_error(ctx, 409)

    # ---------- 重新提交 ----------
    def test_resubmit_rejected_obsoletes_old_records(self):
        """驳回后重新提交：旧审批记录标记作废，重新匹配流程并生成新待办"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.REJECTED,
                               flow_id=self.fx["flows"]["mgr_fin"].id)
        self.db.add(Approval(expense_id=expense.id, approver_id=manager.id,
                             flow_id=self.fx["flows"]["mgr_fin"].id, level=1,
                             status=ApprovalStatus.REJECTED, comment="票据不全"))
        self.db.commit()

        result = resubmit_expense(expense_id=expense.id, db=self.db, current_user=employee)

        self.assertEqual(result.status, ExpenseStatus.PENDING)
        self.assertEqual(result.current_level, 1)
        self.assertEqual(result.flow_id, self.fx["flows"]["mgr"].id)  # 金额 100 → 重新匹配到经理单人

        self.db.expire_all()
        records = self.db.query(Approval).filter(Approval.expense_id == expense.id).order_by(Approval.id).all()
        self.assertEqual(records[0].status, ApprovalStatus.OBSOLETED)   # 旧记录作废
        self.assertEqual(records[-1].status, ApprovalStatus.PENDING)    # 新待办
        self.assertEqual(records[-1].level, 1)

    # ---------- 审批执行 ----------
    def test_manager_then_finance_approval(self):
        """两级审批：同一条任务被更新，通过后为下一级创建新待办"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        finance2 = self.fx["users"]["finance2"]      # 非申请人本人
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT,
                               flow_id=self.fx["flows"]["mgr_fin"].id,
                               amount=Decimal("5000.00"))
        submit_expense(expense_id=expense.id, db=self.db, current_user=employee)
        level1_task = self.db.query(Approval).filter(
            Approval.expense_id == expense.id, Approval.level == 1,
            Approval.status == ApprovalStatus.PENDING,
        ).one()

        first = approval_action(expense_id=expense.id,
                                action=ApprovalAction(status="approved", comment="同意"),
                                db=self.db, current_user=manager)
        self.assertEqual(first.id, level1_task.id)       # 更新原任务，不新增结果行
        self.assertEqual(first.status, ApprovalStatus.APPROVED)
        self.assertIsNotNone(first.acted_at)
        self.db.refresh(expense)
        self.assertEqual(expense.status, ExpenseStatus.PENDING)
        self.assertEqual(expense.current_level, 2)

        pending_tasks = self.db.query(Approval).filter(
            Approval.expense_id == expense.id, Approval.status == ApprovalStatus.PENDING,
        ).all()
        self.assertEqual(len(pending_tasks), 1)          # 只为第 2 级新建一条待办
        self.assertEqual(pending_tasks[0].level, 2)

        second = approval_action(expense_id=expense.id,
                                 action=ApprovalAction(status="approved", comment="财务同意"),
                                 db=self.db, current_user=finance2)
        self.assertEqual(second.id, pending_tasks[0].id)
        self.assertEqual(second.level, 2)
        self.assertEqual(second.status, ApprovalStatus.APPROVED)
        self.db.refresh(expense)
        self.assertEqual(expense.status, ExpenseStatus.APPROVED)
        self.assertIsNotNone(expense.approved_at)

        # 一级一行：最终两条 approved，没有任何遗留 pending
        records = self.db.query(Approval).filter(Approval.expense_id == expense.id).all()
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r.status == ApprovalStatus.APPROVED for r in records))
        self.assertTrue(all(r.acted_at is not None for r in records))

    def test_reject_terminates_flow(self):
        """任一级驳回：流程终止，单据转已驳回"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr_fin"].id)

        approval_action(expense_id=expense.id,
                        action=ApprovalAction(status="rejected", comment="票据不全"),
                        db=self.db, current_user=manager)

        self.db.refresh(expense)
        self.assertEqual(expense.status, ExpenseStatus.REJECTED)

    def test_self_approval_forbidden(self):
        """不能审批自己的单据（403）"""
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, manager, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            approval_action(expense_id=expense.id,
                            action=ApprovalAction(status="approved"),
                            db=self.db, current_user=manager)
        self.assert_http_error(ctx, 403)

    def test_other_department_manager_forbidden(self):
        """别的部门经理不能审批本部门单据（403）"""
        employee = self.fx["users"]["employee"]
        manager2 = self.fx["users"]["manager2"]   # 产品部经理
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)

        with self.assertRaises(HTTPException) as ctx:
            approval_action(expense_id=expense.id,
                            action=ApprovalAction(status="approved"),
                            db=self.db, current_user=manager2)
        self.assert_http_error(ctx, 403)

    def test_finance_other_excludes_submitter(self):
        """finance_other 层级：申请人自己是财务时不能自己批，其他财务可以"""
        finance = self.fx["users"]["finance"]
        finance2 = self.fx["users"]["finance2"]
        expense = make_expense(self.db, finance, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["fin_other"].id)

        with self.assertRaises(HTTPException) as ctx:
            approval_action(expense_id=expense.id,
                            action=ApprovalAction(status="approved"),
                            db=self.db, current_user=finance)
        self.assert_http_error(ctx, 403)

        approval_action(expense_id=expense.id,
                        action=ApprovalAction(status="approved"),
                        db=self.db, current_user=finance2)
        self.db.refresh(expense)
        self.assertEqual(expense.status, ExpenseStatus.APPROVED)

    def test_department_report_uses_submission_snapshot(self):
        """员工调部门后，历史单据仍统计在原提交部门"""
        employee = self.fx["users"]["employee"]
        original_department_id = employee.department_id
        other_department = self.db.query(Department).filter(
            Department.id != original_department_id
        ).first()
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT,
                               amount=Decimal("100.00"))
        submit_expense(expense_id=expense.id, db=self.db, current_user=employee)

        # 模拟调部门：只改 users.department_id，不改已提交单据的快照
        employee.department_id = other_department.id
        self.db.commit()

        rows = get_department_report(self.db)
        original_row = next(row for row in rows if row["department_id"] == original_department_id)
        self.assertEqual(original_row["expense_count"], 1)
        self.assertEqual(original_row["gross_amount"], Decimal("100.00"))
        self.assertEqual(original_row["net_amount"], Decimal("0"))

    def test_inactive_flow_cannot_approve(self):
        """审批流提交后被停用：审批动作 400，待办列表也不再展示"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)
        expense.submitted_at = datetime.now()
        flow = self.db.query(ApprovalFlow).filter(ApprovalFlow.id == expense.flow_id).one()
        flow.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            approval_action(expense_id=expense.id,
                            action=ApprovalAction(status="approved"),
                            db=self.db, current_user=manager)
        self.assert_http_error(ctx, 400)
        self.assertIn("停用", ctx.exception.detail)

        response = get_pending_approval_list(
            page_params=PageParams(page=1, page_size=10),
            common_filter=BusinessFilter(),
            user_id=None,
            department_id=None,
            db=self.db,
            current_user=manager,
        )
        self.assertEqual(response.total, 0)


if __name__ == "__main__":
    unittest.main()
