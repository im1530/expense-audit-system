"""
AI 预审执行服务测试（不联网）
    覆盖：
        - 创建任务：状态校验、幂等复用、缓存复用、force 重跑；
        - 执行任务：成功落库并回写单据、重复执行只领取一次；
        - 降级：LLM 异常、AI 开关关闭、每日限额；
        - 失败：上下文失效（单据状态被改）；
        - 僵尸任务：超时 pending 被标记 failed 后允许新建。
"""
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.risk_agent import ReviewOutcome
from app.config import settings
from app.models import AIReviewLog, AIReviewStatus, Attachment, Expense, ExpenseItem
from app.models.expense import ExpenseStatus
from app.services.ai_review_service import (
    ReviewError,
    create_review,
    execute_review,
)
from app.services.llm_client import LLMTimeoutError
from tests.support import BaseTestCase, make_expense



# 构造 Agent 结果
def make_outcome(
    *,
    score: str = "50.00",
    level: str = "mid",
    summary: str = "测试结论",
    findings: list | None = None,
    is_fallback: bool = False,
    policy_version: str = "",
    citations: list | None = None,
    citation_stats: dict | None = None,
) -> ReviewOutcome:
    """
    构造 ReviewOutcome
        :param score: 分数
        :param level: 等级
        :param summary: 总结
        :param findings: 风险点
        :param is_fallback: 是否兜底
        :param policy_version: 制度版本
        :param citations: 制度引用
        :param citation_stats: 引用审计统计
        :return: ReviewOutcome
    """
    return ReviewOutcome(
        risk_score=Decimal(score),
        risk_level=level,
        summary=summary,
        findings=findings or [],
        policy_citations=citations or [],
        is_fallback=is_fallback,
        model="fake-model",
        prompt_version="fake_v1",
        latency_ms=10,
        prompt_tokens=100,
        completion_tokens=20,
        raw_content='{"ok": true}',
        policy_version=policy_version,
        retrieved_clause_ids=[],
        citation_stats=citation_stats or {},
    )



# 假 Agent
class FakeAgent:
    """测试用 Agent：返回固定结果或抛指定异常"""

    def __init__(self, *, outcome: ReviewOutcome | None = None, error: Exception | None = None):
        """
        :param outcome: 固定返回
        :param error: 要抛出的异常
        """
        self.outcome = outcome or make_outcome()
        self.error = error
        self.calls = 0

    async def review(self, context: dict, *, policy=None) -> ReviewOutcome:
        """
        记录调用次数并返回/抛错
            :param context: 上下文
            :param policy: 制度检索结果（本测试不关心）
            :return: ReviewOutcome
        """
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.outcome



# 记录制度结果的 Agent（用于验证 service 是否把检索结果传入）
class CapturingAgent(FakeAgent):
    """记录 policy 参数并返回固定结果"""

    def __init__(self, **kwargs):
        """
        :param kwargs: 透传给 FakeAgent
        """
        super().__init__(**kwargs)
        self.policy = None                          # 最近一次收到的制度结果

    async def review(self, context: dict, *, policy=None) -> ReviewOutcome:
        """
        记录 policy 后返回固定结果
            :param context: 上下文
            :param policy: 制度检索结果
            :return: ReviewOutcome
        """
        self.policy = policy                        # 记录制度结果
        return await super().review(context, policy=policy)



# 记录上下文/制度参数的 Agent
class ContextAgent(FakeAgent):
    """记录上下文并返回固定结果"""

    def __init__(self, **kwargs):
        """
        :param kwargs: 透传给 FakeAgent
        """
        super().__init__(**kwargs)
        self.context = None                         # 最近一次上下文

    async def review(self, context: dict, *, policy=None) -> ReviewOutcome:
        """
        记录上下文后返回固定结果
            :param context: 上下文
            :param policy: 制度结果
            :return: ReviewOutcome
        """
        self.context = context                      # 记录上下文
        return await super().review(context, policy=policy)



class AIReviewServiceTest(BaseTestCase, unittest.TestCase):
    """任务创建与执行"""

    def pending_expense(self, amount: str = "100.00"):
        """
        造一张待审批单据
            :param amount: 明细金额
            :return: Expense
        """
        employee = self.fx["users"]["employee"]
        return make_expense(
            self.db, employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal(amount),
            category_id=self.fx["categories"]["rail"].id,
        )

    def test_create_pending_and_idempotent(self):
        """首次创建 pending；再次创建复用同一任务"""
        expense = self.pending_expense()
        first = create_review(self.db, expense_id=expense.id, triggered_by=expense.user_id)
        second = create_review(self.db, expense_id=expense.id, triggered_by=expense.user_id)

        self.assertEqual(first.status, AIReviewStatus.PENDING)
        self.assertEqual(first.id, second.id)

    def test_create_rejects_non_pending(self):
        """草稿不能创建预审"""
        employee = self.fx["users"]["employee"]
        expense = make_expense(self.db, employee, status=ExpenseStatus.DRAFT)
        with self.assertRaises(ReviewError):
            create_review(self.db, expense_id=expense.id)

    def test_execute_success_writes_log_and_expense(self):
        """执行成功：日志 success，单据 risk_* 被回写"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent(outcome=make_outcome(score="72.50", level="high", summary="高风险结论"))

        result = execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, AIReviewStatus.SUCCESS)
        self.assertFalse(result.is_fallback)
        self.assertEqual(result.risk_score, Decimal("72.50"))
        self.assertEqual(int(result.prompt_tokens), 100)

        self.db.commit()
        refreshed = self.db.query(Expense).filter(Expense.id == expense.id).first()
        self.assertEqual(refreshed.risk_level, "high")
        self.assertEqual(refreshed.risk_source, "ai")
        self.assertEqual(refreshed.ai_reviewed_result, "高风险结论")

    def test_execute_twice_only_claimed_once(self):
        """同一个任务只能被领取一次"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent()

        first = execute_review(log.id, agent=agent, session_factory=self.Session)
        second = execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(agent.calls, 1)

    def test_policy_retrieval_passed_and_audited(self):
        """制度检索结果会传给 Agent，并写入审计字段"""
        expense = self.pending_expense("2500.00")
        log = create_review(self.db, expense_id=expense.id)
        citations = [{
            "clause_id": "3.1",
            "doc": "费用报销管理制度",
            "chapter": "3 费用类别与标准",
            "quote": "原则上乘坐高铁二等座或飞机经济舱",
        }]
        stats = {
            "policy_version": "v1.1",
            "retrieved_clause_ids": ["3.1"],
            "citation_count": 1,
            "dropped": [],
            "unverified_policy_findings": 0,
        }
        agent = CapturingAgent(outcome=make_outcome(
            policy_version="v1.1",
            citations=citations,
            citation_stats=stats,
        ))

        result = execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertIsNotNone(agent.policy)
        self.assertEqual(agent.policy.version, "v1.3")
        self.assertEqual(result.policy_version, "v1.1")
        self.assertEqual(result.retrieval_meta["citation_count"], 1)
        self.assertEqual(result.policy_citations[0]["clause_id"], "3.1")

    def test_policy_disabled_passes_empty_bundle(self):
        """制度 RAG 关闭：传给 Agent 的是空结果"""
        expense = self.pending_expense("2500.00")
        log = create_review(self.db, expense_id=expense.id)
        agent = CapturingAgent()

        with patch.object(settings, "POLICY_RAG_ENABLED", False):
            execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertIsNotNone(agent.policy)
        self.assertTrue(agent.policy.is_empty)

    def test_document_findings_injected_into_context(self):
        """凭证识别结论会写入预审上下文"""
        expense = self.pending_expense("2500.00")
        item = self.db.query(ExpenseItem).filter_by(expense_id=expense.id).first()
        attachment = Attachment(
            expense_item_id=item.id,
            file_name="park.jpg",
            file_path="invoices/test/park.jpg",
            content_type="image/jpeg",
            file_size=10,
            uploaded_by=expense.user_id,
            is_deleted=False,
        )
        self.db.add(attachment)
        self.db.commit()
        log = create_review(self.db, expense_id=expense.id)
        finding = {
            "type": "invoice",
            "level": "high",
            "message": "凭证金额低于明细金额",
            "evidence": "凭证 50 元",
            "suggestion": "核对",
            "clause_id": None,
        }
        agent = ContextAgent()

        with patch(
            "app.workflow.nodes.extract_for_expense_with_usage",
            return_value=([object()], {"prompt_tokens": 0, "completion_tokens": 0}),
        ), patch(
            "app.workflow.nodes.collect_document_findings",
            return_value=[finding],
        ):
            execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertIsNotNone(agent.context)
        self.assertEqual(agent.context["document_findings"], [finding])

    def test_llm_error_falls_back(self):
        """LLM 超时：降级为规则兜底，任务 status=fallback"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent(error=LLMTimeoutError("超时", retryable=True))

        result = execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertEqual(result.status, AIReviewStatus.FALLBACK)
        self.assertTrue(result.is_fallback)
        self.assertEqual(result.risk_level, "low")
        self.assertEqual(result.error_message, "超时")
        self.db.rollback()          # 结束本会话旧快照，读取 execute_review 独立会话的提交
        refreshed = self.db.query(Expense).filter(Expense.id == expense.id).first()
        self.assertEqual(refreshed.risk_source, "rules")

    def test_disabled_falls_back_without_agent(self):
        """AI 开关关闭：不调 Agent，直接兜底"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        agent = FakeAgent()

        with patch.object(settings, "AI_REVIEW_ENABLED", False):
            result = execute_review(log.id, agent=agent, session_factory=self.Session)

        self.assertEqual(result.status, AIReviewStatus.FALLBACK)
        self.assertEqual(agent.calls, 0)
        self.assertIn("关闭", result.error_message or "")

    def test_daily_limit_falls_back(self):
        """达到每日上限：第二次执行不调 Agent，直接兜底"""
        first_expense = self.pending_expense("100.00")
        second_expense = self.pending_expense("200.00")
        first_log = create_review(self.db, expense_id=first_expense.id)
        second_log = create_review(self.db, expense_id=second_expense.id)
        agent = FakeAgent()

        with patch.object(settings, "AI_REVIEW_DAILY_LIMIT", 1):
            execute_review(first_log.id, agent=agent, session_factory=self.Session)
            second = execute_review(second_log.id, agent=agent, session_factory=self.Session)

        self.assertEqual(second.status, AIReviewStatus.FALLBACK)
        self.assertEqual(agent.calls, 1)
        self.assertIn("上限", second.error_message or "")

    def test_cache_returns_success(self):
        """缓存期内重复创建返回同一个成功任务"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        execute_review(log.id, agent=FakeAgent(), session_factory=self.Session)

        again = create_review(self.db, expense_id=expense.id)
        self.assertEqual(again.id, log.id)
        self.assertEqual(again.status, AIReviewStatus.SUCCESS)

    def test_force_creates_new_task(self):
        """force=True 绕过缓存，新建 pending"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)
        execute_review(log.id, agent=FakeAgent(), session_factory=self.Session)

        self.db.commit()
        forced = create_review(self.db, expense_id=expense.id, force=True)
        self.assertNotEqual(forced.id, log.id)
        self.assertEqual(forced.status, AIReviewStatus.PENDING)

    def test_context_error_marks_failed(self):
        """创建后单据状态被改：执行时标记 failed"""
        expense = self.pending_expense()
        log = create_review(self.db, expense_id=expense.id)

        expense.status = ExpenseStatus.DRAFT
        self.db.commit()

        result = execute_review(log.id, agent=FakeAgent(), session_factory=self.Session)
        self.assertEqual(result.status, AIReviewStatus.FAILED)
        self.assertIn("待审批", result.error_message or "")

    def test_stale_pending_is_reaped(self):
        """超时的 pending 被标记 failed，允许重新创建"""
        expense = self.pending_expense()
        old = create_review(self.db, expense_id=expense.id)

        old.created_at = datetime.now() - timedelta(hours=1)
        old.updated_at = datetime.now() - timedelta(hours=1)
        self.db.commit()

        new_log = create_review(self.db, expense_id=expense.id)
        self.db.expire_all()
        refreshed = self.db.query(type(old)).filter_by(id=old.id).first()

        self.assertEqual(refreshed.status, AIReviewStatus.FAILED)
        self.assertNotEqual(new_log.id, old.id)


if __name__ == "__main__":
    unittest.main()
