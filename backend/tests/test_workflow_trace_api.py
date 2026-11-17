"""
AI 预审工作流轨迹接口测试（离线，不联网；使用测试库）
    覆盖：
        - 成功 run：节点顺序、决策、token、制度引用；
        - 只有预审任务、没有 run：404；
        - 权限：本人/财务可看，无审批资格的其他部门经理 403；
        - 失败 run：状态与错误信息；
        - 凭证识别结论：按附件返回最新一条。
"""
import unittest
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.prompts import PROMPT_VERSION
from app.agents.risk_agent import ReviewOutcome
from app.api.v1.agent import get_review_trace
from app.models import (
    Attachment,
    DocumentExtraction,
    DocumentExtractionStatus,
    ExpenseItem,
)
from app.models.expense import ExpenseStatus
from app.schemas.workflow_trace import WorkflowTraceResponse
from app.services.ai_review_service import create_review, execute_review
from tests.support import BaseTestCase, make_expense



# 假 Agent：返回固定结果
class FakeAgent:
    """测试用风险 Agent"""

    def __init__(self, outcome: ReviewOutcome | None = None, error: Exception | None = None):
        """
        :param outcome: 返回结果
        :param error: 要抛出的异常
        """
        self.outcome = outcome or make_outcome()
        self.error = error

    async def review(self, context, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果或抛错
            :param context: 上下文
            :param policy: 制度结果（本测试不关心）
            :return: ReviewOutcome
        """
        if self.error is not None:
            raise self.error
        return self.outcome



# 构造 Agent 结果
def make_outcome(
    level: str = "low",
    score: str = "20.00",
    *,
    citations: list | None = None,
    is_fallback: bool = False,
) -> ReviewOutcome:
    """
    构造风险评审结果
        :param level: 风险等级
        :param score: 风险分数
        :param citations: 制度引用
        :param is_fallback: 是否兜底
        :return: ReviewOutcome
    """
    return ReviewOutcome(
        risk_score=Decimal(score),
        risk_level=level,
        summary="测试结论",
        findings=[],
        policy_citations=citations or [],
        is_fallback=is_fallback,
        model="fake-model",
        prompt_version="fake_v1",
        latency_ms=5,
        prompt_tokens=10,
        completion_tokens=5,
        raw_content="{}",
        policy_version="v1.3",
        retrieved_clause_ids=["2.2"],
    )



class WorkflowTraceApiTest(BaseTestCase, unittest.TestCase):
    """工作流轨迹接口"""

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

    def run_success(self, expense, *, outcome=None):
        """
        执行一次成功的预审
            :param expense: 报销单
            :param outcome: Agent 返回结果
            :return: 预审日志
        """
        log = create_review(self.db, expense_id=expense.id)               # 创建任务
        execute_review(
            log.id,
            agent=FakeAgent(outcome or make_outcome()),
            session_factory=self.Session,
        )                                                                 # 执行并落库
        self.db.rollback()                                                # 结束旧事务，看到另一会话的提交
        return log

    def test_trace_success(self):
        """成功 run：节点顺序、决策、token 与引用可查"""
        expense = self.pending_expense()
        citation = {
            "clause_id": "2.2",
            "doc": "费用报销管理制度",
            "chapter": "2 报销单据与凭证",
            "quote": "报销应提供合法有效的发票或有效凭证",
        }
        self.run_success(expense, outcome=make_outcome(citations=[citation]))

        trace = get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["employee"])

        self.assertEqual(trace["expense_id"], expense.id)
        self.assertEqual(trace["status"], "success")
        self.assertEqual(
            [step["node"] for step in trace["steps"]],
            ["load_context", "rule", "rag", "risk", "decision"],
        )
        self.assertEqual(trace["decision"], "fast_track")
        self.assertEqual(trace["citations"][0]["clause_id"], "2.2")
        self.assertGreater(trace["prompt_tokens"], 0)
        self.assertEqual(trace["prompt_version"], PROMPT_VERSION)         # run 记录的是线上 Prompt 版本
        self.assertEqual(trace["policy_version"], "v1.3")
        WorkflowTraceResponse.model_validate(trace)                       # 响应契约必须能校验通过

    def test_trace_requires_run(self):
        """只有预审任务、还没跑工作流：404"""
        expense = self.pending_expense()
        create_review(self.db, expense_id=expense.id)                     # 只创建任务

        with self.assertRaises(HTTPException) as ctx:
            get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["employee"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_trace_missing_expense(self):
        """单据不存在：404"""
        with self.assertRaises(HTTPException) as ctx:
            get_review_trace(999999, db=self.db, current_user=self.fx["users"]["employee"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_trace_permission_denied(self):
        """无审批资格的其他部门经理：403"""
        expense = self.pending_expense()
        self.run_success(expense)

        with self.assertRaises(HTTPException) as ctx:
            get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["manager2"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_trace_finance_can_read(self):
        """财务有全局查看权限：可看"""
        expense = self.pending_expense()
        self.run_success(expense)
        trace = get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["finance"])
        self.assertEqual(trace["status"], "success")

    def test_trace_failed_run(self):
        """失败的 run：状态 failed，带错误信息，引用为空"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        execute_review(
            log.id,
            agent=FakeAgent(error=RuntimeError("agent boom")),
            session_factory=self.Session,
        )
        self.db.rollback()                                                # 结束旧事务，看到 run 已写完

        trace = get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["employee"])
        self.assertEqual(trace["status"], "failed")
        self.assertIn("agent boom", trace["error_message"] or "")
        self.assertEqual(trace["citations"], [])

    def test_trace_documents(self):
        """凭证识别结论：每条附件返回最新一条"""
        expense = self.pending_expense()
        self.run_success(expense)                                         # 先跑工作流，避免识别节点再写新记录

        item = self.db.query(ExpenseItem).filter(ExpenseItem.expense_id == expense.id).first()
        attachment = Attachment(
            expense_item_id=item.id,
            file_name="invoice.png",
            file_path="invoices/2026/11/a.png",
            content_type="image/png",
            file_size=128,
            uploaded_by=self.fx["users"]["employee"].id,
        )
        self.db.add(attachment)
        self.db.flush()
        self.db.add(DocumentExtraction(
            attachment_id=attachment.id,
            expense_item_id=item.id,
            status=DocumentExtractionStatus.SUCCESS,
            document_type="vat_invoice",
            evidence_level="strong",
            amount=Decimal("100.00"),
            document_date=date(2026, 11, 1),
            counterparty="测试商户",
            identifier="INV-20261101",
            confidence=Decimal("0.900"),
            needs_manual_review=False,
        ))
        self.db.commit()
        attachment_id = attachment.id
        self.db.rollback()

        trace = get_review_trace(expense.id, db=self.db, current_user=self.fx["users"]["employee"])
        self.assertEqual(len(trace["documents"]), 1)
        document = trace["documents"][0]
        self.assertEqual(document["attachment_id"], attachment_id)
        self.assertEqual(document["document_type"], "vat_invoice")
        self.assertEqual(document["identifier"], "INV-20261101")
        self.assertFalse(document["needs_manual_review"])


if __name__ == "__main__":
    unittest.main()
