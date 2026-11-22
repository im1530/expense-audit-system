"""
规则引擎测试（P1）
    覆盖四条检查规则（类别限额 / 发票缺失 / 重复报销 / 跨期报销）的命中、不命中与边界，
    以及引擎容错、提交落库、block 拦截、预检不落库、管理端规则校验。
"""
import unittest
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import event

from app.api.v1.expense import precheck_expense, submit_expense
from app.models import Attachment, ExpenseItem, ExpenseRuleHit, Rule
from app.models.expense import ExpenseStatus
from app.services.rule_engine import _check_duplicate, run_rule_checks, summarize_level
from app.services.rule_service import create_rule
from tests.support import BaseTestCase, make_expense



class RuleEngineTest(BaseTestCase, unittest.TestCase):
    """规则引擎行为测试"""

    # 往单据里追加一条明细
    def add_item(self, expense, *, amount=Decimal("100.00"), category_id=None,
                 expense_date=None, invoice_no=None, no_invoice_reason=None,
                 description="测试明细") -> ExpenseItem:
        """
        给报销单追加一条明细并提交
            :param expense: 报销单
            :param amount: 金额
            :param category_id: 类别ID，默认用高铁票类别
            :param expense_date: 费用日期，默认今天
            :param invoice_no: 发票号
            :param no_invoice_reason: 无票说明
            :param description: 说明
            :return: 已持久化的明细
        """
        category_id = category_id or self.fx["categories"]["rail"].id
        item = ExpenseItem(
            expense_id=expense.id,
            category_id=category_id,
            description=description,
            amount=amount,
            expense_date=expense_date or date.today(),
            invoice_no=invoice_no,
            no_invoice_reason=no_invoice_reason,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(expense)
        return item

    # 取某一类规则的命中项
    def hits_of(self, hits, rule_type: str) -> list[dict]:
        """
        过滤出指定规则类型的命中项
            :param hits: run_rule_checks 结果
            :param rule_type: 规则类型
            :return: 命中列表
        """
        return [hit for hit in hits if hit["rule_type"] == rule_type]

    # ---------- 类别单次限额 ----------
    def test_category_limit_hit(self):
        """明细超过类别单次限额 → 命中，并带出实际值与阈值"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        # 高铁票类别 max_amount = 2000
        self.add_item(expense, amount=Decimal("2500.00"))

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_category_limit")

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["level"], "warn")
        self.assertEqual(hits[0]["clause_id"], "3.7")   # 对应制度 3.7 超限
        self.assertIn("超过", hits[0]["message"])
        self.assertEqual(hits[0]["evidence"]["max_amount"], "2000.00")
        self.assertEqual(hits[0]["evidence"]["amount"], "2500.00")

    def test_category_limit_not_hit_within_limit(self):
        """未超过类别限额 → 不命中"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("1500.00"))

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_category_limit")
        self.assertEqual(hits, [])

    def test_category_limit_uses_item_category(self):
        """限额按明细自己的类别判断：商务酒店 3000，2500 不超限"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("2500.00"),
                      category_id=self.fx["categories"]["hotel"].id)

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_category_limit")
        self.assertEqual(hits, [])

    # ---------- 发票缺失 ----------
    def test_invoice_missing_hit(self):
        """无附件、无发票号、无说明 → 命中"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("120.00"))

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_invoice_missing")
        self.assertEqual(len(hits), 1)
        self.assertIn("无票说明", hits[0]["message"])

    def test_invoice_missing_reason_allows(self):
        """填写了无票说明 → 不命中（允许无票）"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("120.00"), no_invoice_reason="出租车司机未给票")

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_invoice_missing")
        self.assertEqual(hits, [])

    def test_invoice_missing_invoice_no_allows(self):
        """有发票号 → 不命中"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("120.00"), invoice_no="INV-0001")

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_invoice_missing")
        self.assertEqual(hits, [])

    def test_invoice_missing_attachment_allows(self):
        """有未删除的附件 → 不命中"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        item = self.add_item(expense, amount=Decimal("120.00"))
        self.db.add(Attachment(
            expense_item_id=item.id, file_name="invoice.jpg", file_path="2026/11/xxx.jpg",
            content_type="image/jpeg", file_size=1024, uploaded_by=employee.id, is_deleted=False,
        ))
        self.db.commit()

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_invoice_missing")
        self.assertEqual(hits, [])

    def test_invoice_missing_ignores_deleted_attachment(self):
        """附件被软删 → 仍算缺失"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        item = self.add_item(expense, amount=Decimal("120.00"))
        self.db.add(Attachment(
            expense_item_id=item.id, file_name="invoice.jpg", file_path="2026/11/xxx.jpg",
            content_type="image/jpeg", file_size=1024, uploaded_by=employee.id, is_deleted=True,
        ))
        self.db.commit()

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_invoice_missing")
        self.assertEqual(len(hits), 1)

    # ---------- 重复报销 ----------
    def test_duplicate_hit_from_history(self):
        """历史单据同人/同日期/同类别/同金额 → 命中，并给出原单号"""
        employee = self.fx["users"]["employee"]
        historic = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                                amount=Decimal("100.00"), item_count=1)
        current = make_expense(self.db, employee, amount=Decimal("100.00"), item_count=1)

        hits = self.hits_of(run_rule_checks(self.db, current), "check_duplicate")

        self.assertEqual(len(hits), 1)
        matches = hits[0]["evidence"]["matches"]
        self.assertEqual(hits[0]["clause_id"], "4.3")   # 对应制度 4.3 重复报销
        self.assertTrue(any(match["expense_no"] == historic.expense_no for match in matches))

    def test_duplicate_ignores_draft_history(self):
        """草稿从未进入报销，不作为重复报销的历史证据"""
        employee = self.fx["users"]["employee"]
        make_expense(self.db, employee, status=ExpenseStatus.DRAFT,
                     amount=Decimal("100.00"), item_count=1)
        current = make_expense(self.db, employee, amount=Decimal("100.00"), item_count=1)

        hits = self.hits_of(run_rule_checks(self.db, current), "check_duplicate")
        self.assertEqual(hits, [])

    def test_duplicate_history_query_is_not_n_plus_one(self):
        """重复检查只查一次历史明细，不再每条当前明细查一遍历史单据"""
        employee = self.fx["users"]["employee"]
        for _ in range(20):
            make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                         amount=Decimal("100.00"), item_count=1)
        current = make_expense(self.db, employee, amount=Decimal("100.00"), item_count=1)
        self.db.commit()
        rule = self.db.query(Rule).filter(Rule.rule_type == "check_duplicate").one()
        current.items  # 预加载，避免把懒加载算进统计

        statements = []

        def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.db.get_bind(), "before_cursor_execute", before_cursor_execute)
        try:
            _check_duplicate(self.db, current, rule)
        finally:
            event.remove(self.db.get_bind(), "before_cursor_execute", before_cursor_execute)

        self.assertEqual(len(statements), 1)

    def test_duplicate_ignores_other_category(self):
        """金额相同但类别不同 → 不算重复"""
        employee = self.fx["users"]["employee"]
        make_expense(self.db, employee, status=ExpenseStatus.APPROVED, amount=Decimal("100.00"),
                     item_count=1, category_id=self.fx["categories"]["hotel"].id)
        current = make_expense(self.db, employee, amount=Decimal("100.00"), item_count=1)

        hits = self.hits_of(run_rule_checks(self.db, current), "check_duplicate")
        self.assertEqual(hits, [])

    def test_duplicate_ignores_outside_window(self):
        """费用日期超出 60 天窗口 → 不比对"""
        employee = self.fx["users"]["employee"]
        old_date = date.today() - timedelta(days=100)
        historic = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                                amount=Decimal("100.00"), item_count=0)
        historic_item = ExpenseItem(expense_id=historic.id,
                                    category_id=self.fx["categories"]["rail"].id,
                                    description="旧费用", amount=Decimal("100.00"),
                                    expense_date=old_date)
        self.db.add(historic_item)

        current = make_expense(self.db, employee, amount=Decimal("0.00"), item_count=0)
        ExpenseItem(expense_id=current.id, category_id=self.fx["categories"]["rail"].id,
                    description="旧费用", amount=Decimal("100.00"), expense_date=old_date)
        self.db.commit()
        self.db.refresh(current)

        hits = self.hits_of(run_rule_checks(self.db, current), "check_duplicate")
        self.assertEqual(hits, [])

    def test_duplicate_within_same_expense(self):
        """同一张单里两条相同明细 → 也算重复"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("88.00"), description="打车A")
        self.add_item(expense, amount=Decimal("88.00"), description="打车B")

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_duplicate")
        self.assertEqual(len(hits), 2)      # 两条明细各自命中一次

    # ---------- 跨期报销 ----------
    def test_period_late_hit(self):
        """费用日期距提交超过 60 天 → 命中"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("100.00"),
                      expense_date=date.today() - timedelta(days=70))

        hits = self.hits_of(run_rule_checks(self.db, expense), "check_period_late")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["evidence"]["late_days"], 70)

    def test_period_late_boundary_60_days(self):
        """正好 60 天不算跨期，61 天才算"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("100.00"),
                      expense_date=date.today() - timedelta(days=60))
        self.assertEqual(self.hits_of(run_rule_checks(self.db, expense), "check_period_late"), [])

        self.add_item(expense, amount=Decimal("100.00"),
                      expense_date=date.today() - timedelta(days=61))
        hits = self.hits_of(run_rule_checks(self.db, expense), "check_period_late")
        self.assertEqual(len(hits), 1)

    # ---------- 引擎容错与汇总 ----------
    def test_broken_rule_is_skipped(self):
        """某条规则配置坏了（window_days 不是数字）→ 只跳过它，其他规则照常执行"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, item_count=0)
        self.add_item(expense, amount=Decimal("120.00"))

        rule = self.db.query(Rule).filter(Rule.rule_type == "check_duplicate").first()
        rule.config = {"window_days": "not-a-number"}
        self.db.commit()

        hits = run_rule_checks(self.db, expense)
        self.assertEqual(self.hits_of(hits, "check_duplicate"), [])
        self.assertEqual(len(self.hits_of(hits, "check_invoice_missing")), 1)   # 其他规则不受影响

    def test_summarize_level(self):
        """汇总级别：无命中 ok，有 warn 则 warn"""
        self.assertEqual(summarize_level([]), "ok")
        self.assertEqual(summarize_level([{"level": "warn"}]), "warn")
        self.assertEqual(
            summarize_level([{"level": "warn"}, {"level": "block"}]),
            "block",
        )

    # ---------- 与提交/预检的集成 ----------
    def test_submit_persists_hits(self):
        """提交时把命中结果落库，并随详情返回"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT, item_count=1)

        result = submit_expense(expense_id=expense.id, db=self.db, current_user=employee)

        self.assertEqual(result.status, ExpenseStatus.PENDING)
        self.assertGreaterEqual(len(result.rule_hits), 1)
        stored = self.db.query(ExpenseRuleHit).filter(ExpenseRuleHit.expense_id == expense.id).count()
        self.assertGreaterEqual(stored, 1)

    def test_block_rule_prevents_submit(self):
        """action=block 的规则命中 → 提交被拒（409），单据保持草稿且不落命中记录"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT, item_count=1)

        rule = self.db.query(Rule).filter(Rule.rule_type == "check_invoice_missing").first()
        rule.action = "check"
        rule.severity = "block"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            submit_expense(expense_id=expense.id, db=self.db, current_user=employee)
        self.assertEqual(ctx.exception.status_code, 409)

        self.db.rollback()
        refreshed = self.db.query(type(expense)).filter_by(id=expense.id).first()
        self.assertEqual(refreshed.status, ExpenseStatus.DRAFT)
        self.assertEqual(self.db.query(ExpenseRuleHit).count(), 0)

    def test_precheck_does_not_persist(self):
        """预检只计算、不改状态、不落库"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT, item_count=1)

        result = precheck_expense(expense_id=expense.id, db=self.db, current_user=employee)

        self.assertEqual(result["level"], "warn")
        self.assertGreaterEqual(result["total"], 1)
        self.db.refresh(expense)
        self.assertEqual(expense.status, ExpenseStatus.DRAFT)
        self.assertEqual(self.db.query(ExpenseRuleHit).count(), 0)

    # ---------- 管理端规则校验 ----------
    def test_check_rule_requires_valid_action(self):
        """检查规则的 action 固定 check，severity 只能是 warn/review/block"""
        with self.assertRaises(HTTPException) as ctx:
            create_rule(self.db, name="非法检查规则", rule_type="check_duplicate",
                        condition="测试", action="reject", config={}, is_active=True)
        self.assertEqual(ctx.exception.status_code, 400)

        with self.assertRaises(HTTPException) as ctx:
            create_rule(self.db, name="非法级别规则", rule_type="check_duplicate",
                        condition="测试", action="check", severity="fatal",
                        config={}, is_active=True)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_amount_rule_action_must_be_assign_chain(self):
        """amount 路由规则的 action 固定 assign_chain"""
        with self.assertRaises(HTTPException) as ctx:
            create_rule(self.db, name="非法路由规则", rule_type="amount",
                        condition="测试", action="warn",
                        config={"submitter_roles": ["employee"], "min_amount": 0,
                                "max_amount": 100, "flow_id": self.fx["flows"]["mgr"].id},
                        is_active=False)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_check_rule_normalizes_config(self):
        """检查规则的 config 只保留引擎认识的参数，并有默认值"""
        rule = create_rule(self.db, name="30天重复提醒", rule_type="check_duplicate",
                           condition="30天内重复", action="check", severity="warn",
                           config={"window_days": 30, "unknown": "ignored"}, is_active=True)
        self.assertEqual(rule.config, {"window_days": 30})
        self.assertEqual(rule.severity, "warn")

        rule2 = create_rule(self.db, name="默认跨期提醒", rule_type="check_period_late",
                            condition="默认 60 天", action="check", severity="warn",
                            config={}, is_active=False)
        self.assertEqual(rule2.config, {"max_days": 60})



if __name__ == "__main__":
    unittest.main()
