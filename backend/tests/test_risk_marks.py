"""
规则风险标记与命中率统计测试
    覆盖：
        - build_risk_marks：规则类型 → 中文短标签、级别 → 中文名、关联明细提取、按严重程度排序；
        - load_risk_map / attach_risk_marks：批量汇总与挂载，无命中单据返回 ok；
        - 审批待办 / 已办列表：返回的列表项带上 rule_risk_* 字段；
        - get_rule_hit_stats：命中次数、涉及单据数、命中率与日期区间；
        - 管理端 "/hit-stats" 必须声明在 "/{rule_id}" 之前，否则会被当成规则ID解析。
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1 import admin_rule
from app.api.v1.approval import get_pending_approval_list, get_processed_approval_list
from app.models import ExpenseRuleHit, Rule, Approval
from app.models.approval import ApprovalStatus
from app.models.expense import ExpenseStatus
from app.schemas.common import PageParams, BusinessFilter
from app.schemas.expense import PendingApprovalItem
from app.services.rule_engine import run_rule_checks, save_rule_hits
from app.services.rule_risk_service import (
    attach_risk_marks,
    build_risk_marks,
    get_rule_hit_stats,
    load_risk_map,
    summarize_risk_marks,
)
from tests.support import BaseTestCase, make_expense



# 构造一条"看起来像 ExpenseRuleHit"的对象（纯函数测试不需要落库）
# 参数顺序：rule_id 规则ID、rule_type 规则类型、level 级别、message 提示文案、evidence 命中证据
def fake_hit(
    rule_id: int,
    rule_type: str,
    level: str,
    message: str,
    evidence: dict | None = None,
) -> SimpleNamespace:
    """
    构造命中记录的轻量替身
        :param rule_id: 规则ID
        :param rule_type: 规则类型
        :param level: 级别
        :param message: 提示文案
        :param evidence: 命中证据
        :return: 具备 build_risk_marks 所需属性的对象
    """
    return SimpleNamespace(
        rule_id=rule_id,
        rule_type=rule_type,
        rule_name=f"规则{rule_id}",
        level=level,
        message=message,
        evidence=evidence or {},
    )



class RiskMarkPureTest(unittest.TestCase):
    """风险标记翻译（不依赖数据库）"""

    def test_labels_and_item_fields(self):
        """规则类型被翻译成中文短标签，并带出关联明细"""
        marks = build_risk_marks([
            fake_hit(
                1,
                "check_category_limit",
                "warn",
                "明细「高铁票」申请金额 2500.00 元，超过「高铁票」单次报销限额 2000.00 元",
                {"item_id": 7, "item_description": "高铁票", "amount": "2500.00", "max_amount": "2000.00"},
            )
        ])

        self.assertEqual(len(marks), 1)
        mark = marks[0]
        self.assertEqual(mark["rule_label"], "明细超类别限额")     # 列表短标签
        self.assertEqual(mark["level_label"], "提示")              # 级别中文名
        self.assertEqual(mark["item_id"], 7)                       # 风险落在哪条明细
        self.assertEqual(mark["item_description"], "高铁票")
        self.assertIn("单次报销限额", mark["message"])             # 具体提示原样保留
        self.assertIn("单次报销限额", mark["rule_hint"])           # 解释这条规则在查什么

    def test_sorted_by_severity_then_rule_id(self):
        """多条命中按 block > review > warn 排序，同级按规则ID升序"""
        marks = build_risk_marks([
            fake_hit(9, "check_invoice_missing", "warn", "缺票"),
            fake_hit(3, "check_period_late", "block", "跨期"),
            fake_hit(5, "check_duplicate", "review", "重复"),
            fake_hit(1, "check_duplicate", "review", "重复2"),
        ])

        self.assertEqual([mark["rule_id"] for mark in marks], [3, 1, 5, 9])

    def test_unknown_rule_type_falls_back_to_raw_value(self):
        """未登记的新规则类型不报错，直接用类型字符串作为标签"""
        marks = build_risk_marks([fake_hit(1, "check_new_rule", "warn", "新规则提示")])

        self.assertEqual(marks[0]["rule_label"], "check_new_rule")
        self.assertEqual(marks[0]["rule_hint"], "")

    def test_summarize_empty_is_ok(self):
        """没有命中时汇总为 ok，且每次返回独立的列表对象"""
        first = summarize_risk_marks([])
        second = summarize_risk_marks([])
        self.assertEqual(first["level"], "ok")
        self.assertEqual(first["count"], 0)
        first["labels"].append("污染")
        self.assertEqual(second["labels"], [])

    def test_summarize_takes_highest_level_and_dedup_labels(self):
        """整体级别取最严重的一条，短标签去重"""
        marks = build_risk_marks([
            fake_hit(1, "check_category_limit", "warn", "超限A"),
            fake_hit(2, "check_category_limit", "warn", "超限B"),
            fake_hit(3, "check_invoice_missing", "review", "缺票"),
        ])
        summary = summarize_risk_marks(marks)

        self.assertEqual(summary["level"], "review")
        self.assertEqual(summary["count"], 3)
        # 标签顺序跟随标记排序：review 的"发票缺失"排在 warn 的"类别限额"之前
        self.assertEqual(summary["labels"], ["发票缺失", "明细超类别限额"])



class RuleRiskDbTest(BaseTestCase, unittest.TestCase):
    """风险汇总与审批列表集成（使用独立测试库）"""

    # 取种子数据里的检查规则
    def rule(self, rule_type: str) -> Rule:
        """
        按类型取一条种子检查规则
            :param rule_type: 规则类型
            :return: Rule 实体
        """
        return self.db.query(Rule).filter(Rule.rule_type == rule_type).first()

    # 给某张单插入一条命中记录
    # 参数顺序：expense 所属单据、rule_type 规则类型、level 级别、evidence 命中证据、message 提示文案
    def add_hit(self, expense, rule_type: str, level: str = "warn", evidence: dict | None = None,
                message: str = "测试提示") -> ExpenseRuleHit:
        """
        插入一条命中记录（模拟提交时规则引擎落库的结果）
            :param expense: 所属报销单
            :param rule_type: 规则类型
            :param level: 级别
            :param evidence: 命中证据
            :param message: 提示文案
            :return: 已持久化的命中记录
        """
        rule = self.rule(rule_type)
        hit = ExpenseRuleHit(
            expense_id=expense.id,
            rule_id=rule.id,
            rule_type=rule_type,
            rule_name=rule.name,
            level=level,
            message=message,
            evidence=evidence or {},
        )
        self.db.add(hit)
        self.db.commit()
        return hit

    def test_load_risk_map_groups_and_summarizes(self):
        """批量加载按单据分组：级别取最高、条数与标签正确"""
        employee = self.fx["users"]["employee"]
        hit_expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                                   flow_id=self.fx["flows"]["mgr"].id)
        clean_expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                                     flow_id=self.fx["flows"]["mgr"].id)

        self.add_hit(hit_expense, "check_category_limit", "warn",
                     {"item_id": 1, "item_description": "高铁票"}, "超限")
        self.add_hit(hit_expense, "check_invoice_missing", "warn",
                     {"item_id": 2, "item_description": "住宿"}, "缺票")

        risk_map = load_risk_map(self.db, [hit_expense.id, clean_expense.id])

        self.assertEqual(risk_map[hit_expense.id]["level"], "warn")
        self.assertEqual(risk_map[hit_expense.id]["count"], 2)
        self.assertEqual(risk_map[hit_expense.id]["labels"], ["明细超类别限额", "发票缺失"])
        self.assertEqual(risk_map[clean_expense.id], {"level": "ok", "count": 0, "labels": [], "marks": []})

    def test_pending_item_serializes_risk_fields(self):
        """挂载后 PendingApprovalItem 能直接序列化出 rule_risk_* 字段"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)
        self.add_hit(expense, "check_category_limit", "warn",
                     {"item_id": 11, "item_description": "高铁票"}, "金额超限")

        attach_risk_marks([expense], load_risk_map(self.db, [expense.id]))
        item = PendingApprovalItem.model_validate(expense)

        self.assertEqual(item.rule_risk_level, "warn")
        self.assertEqual(item.rule_risk_count, 1)
        self.assertEqual(item.rule_risk_labels, ["明细超类别限额"])
        self.assertEqual(item.rule_risk_marks[0].item_id, 11)
        self.assertEqual(item.rule_risk_marks[0].level_label, "提示")

    def test_pending_list_returns_risk_marks(self):
        """审批待办列表：当前经理能看到的单据带上风险标记"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)
        expense.submitted_at = datetime.now()
        self.db.commit()
        self.add_hit(expense, "check_duplicate", "review",
                     {"item_id": 5, "item_description": "测试明细1"}, "疑似重复")

        response = get_pending_approval_list(
            page_params=PageParams(page=1, page_size=10),   # 第 1 页，每页 10 条
            common_filter=BusinessFilter(),                 # 不加业务筛选
            user_id=None,                                   # 不按提交人筛选
            department_id=None,                             # 不按部门筛选
            db=self.db,                                     # 数据库会话
            current_user=manager,                           # 以研发部经理身份查看待办
        )

        self.assertEqual(response.total, 1)
        item = response.items[0]
        self.assertEqual(item.rule_risk_level, "review")
        self.assertEqual(item.rule_risk_count, 1)
        self.assertEqual(item.rule_risk_labels, ["疑似重复报销"])
        self.assertEqual(item.rule_risk_marks[0].rule_type, "check_duplicate")

    def test_processed_list_returns_risk_marks(self):
        """我的已办列表：同样带上风险标记，便于复盘"""
        employee = self.fx["users"]["employee"]
        manager = self.fx["users"]["manager"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                               flow_id=self.fx["flows"]["mgr"].id)
        expense.submitted_at = datetime.now()
        self.db.add(Approval(
            expense_id=expense.id,                  # 所属报销单
            approver_id=manager.id,                 # 审批人（当前登录的经理）
            flow_id=self.fx["flows"]["mgr"].id,     # 所属审批流
            status=ApprovalStatus.APPROVED,         # 审批结果：通过
            level=1,                                # 第一级审批
            comment="同意",                          # 审批意见
        ))
        self.db.commit()
        self.add_hit(expense, "check_period_late", "warn",
                     {"item_id": 3, "item_description": "测试明细1"}, "跨期 70 天")

        response = get_processed_approval_list(
            page_params=PageParams(page=1, page_size=10),   # 第 1 页，每页 10 条
            common_filter=BusinessFilter(),                 # 不加业务筛选
            status=None,                                    # 不按单据状态筛选
            approval_result=None,                           # 不按我的审批结果筛选
            approval_start_date=None,                       # 不限制审批起始日期
            approval_end_date=None,                         # 不限制审批结束日期
            user_id=None,                                   # 不按提交人筛选
            department_id=None,                             # 不按部门筛选
            db=self.db,                                     # 数据库会话
            current_user=manager,                           # 以当前审批人身份查看已办
        )

        self.assertEqual(response.total, 1)
        self.assertEqual(response.items[0].rule_risk_labels, ["跨期报销"])

    def test_rule_hit_stats_counts_and_rates(self):
        """命中率统计：分母是已提交单据数，分子是命中该规则的单据数"""
        employee = self.fx["users"]["employee"]

        # 两张已提交单据：一张命中类别限额，一张干净
        hit_expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                                   flow_id=self.fx["flows"]["mgr"].id)
        hit_expense.submitted_at = datetime.now()
        clean_expense = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                                     flow_id=self.fx["flows"]["mgr"].id)
        clean_expense.submitted_at = datetime.now()
        self.db.commit()

        self.add_hit(hit_expense, "check_category_limit", "warn", {"item_id": 1}, "超限")
        self.add_hit(hit_expense, "check_category_limit", "warn", {"item_id": 2}, "超限2")

        stats = get_rule_hit_stats(self.db)

        self.assertEqual(stats["submitted_total"], 2)
        self.assertEqual(stats["hit_expense_count"], 1)
        self.assertEqual(stats["total_hits"], 2)
        self.assertEqual(stats["overall_hit_rate"], 0.5)

        limit_stat = next(item for item in stats["items"] if item["rule_type"] == "check_category_limit")
        self.assertEqual(limit_stat["hit_count"], 2)        # 两条明细各命中一次
        self.assertEqual(limit_stat["expense_count"], 1)    # 但只涉及一张单
        self.assertEqual(limit_stat["hit_rate"], 0.5)
        self.assertEqual(limit_stat["rule_label"], "明细超类别限额")

        # 没命中的检查规则也要出现（便于发现"配了但没生效"）
        self.assertTrue(any(item["rule_type"] == "check_period_late" and item["hit_count"] == 0
                            for item in stats["items"]))

    def test_rule_hit_stats_date_range(self):
        """日期区间按提交时间过滤：区间外的单据不计入分母"""
        employee = self.fx["users"]["employee"]
        old_expense = make_expense(self.db, employee, status=ExpenseStatus.APPROVED,
                                   flow_id=self.fx["flows"]["mgr"].id)
        old_expense.submitted_at = datetime.now() - timedelta(days=400)
        recent_expense = make_expense(self.db, employee, status=ExpenseStatus.PENDING,
                                      flow_id=self.fx["flows"]["mgr"].id)
        recent_expense.submitted_at = datetime.now()
        self.db.commit()

        stats = get_rule_hit_stats(
            self.db,
            start_date=(datetime.now() - timedelta(days=30)).date(),
            end_date=datetime.now().date(),
        )

        self.assertEqual(stats["submitted_total"], 1)

    def test_real_rule_hit_carries_item_detail(self):
        """真实规则引擎产出的命中：标记里能定位到具体明细与金额"""
        employee = self.fx["users"]["employee"]
        category = self.fx["categories"]["rail"]        # 高铁票，单次限额 2000
        expense = make_expense(
            self.db, employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("2500.00"),                  # 超过类别限额
            category_id=category.id,
        )
        hits = run_rule_checks(self.db, expense)
        save_rule_hits(self.db, expense, hits)
        self.db.commit()

        risk_map = load_risk_map(self.db, [expense.id])
        mark = next(m for m in risk_map[expense.id]["marks"]
                    if m["rule_type"] == "check_category_limit")

        self.assertEqual(mark["item_description"], "测试明细1")   # 定位到哪条明细
        self.assertEqual(mark["item_id"], expense.items[0].id)
        self.assertIn("超过", mark["message"])                     # 提示里写明"超过限额"
        self.assertEqual(mark["evidence"]["max_amount"], "2000.00")

    def test_hit_stats_route_declared_before_rule_detail(self):
        """路由顺序：/hit-stats 必须在 /{rule_id} 之前，否则会被当成规则ID"""
        paths = [route.path for route in admin_rule.router.routes]
        self.assertIn("/hit-stats", paths)
        self.assertLess(paths.index("/hit-stats"), paths.index("/{rule_id}"))


if __name__ == "__main__":
    unittest.main()
