"""
AI 预审执行服务（任务创建 + 后台执行 + 审计落库）
    职责：
        1. create_review：校验单据、幂等复用、缓存复用、创建 pending 任务；
        2. execute_review：原子领取任务 → 构造上下文 → 调 Agent → 成功/降级/失败落库；
        3. 回写 expenses.risk_*（仅当没有更新的评审结果时，避免旧结果覆盖新结果）。
    企业级约束：
        - 幂等：同一单据同时只允许一个 pending/running 任务；成功结果在缓存期内复用；
        - 并发：用 UPDATE ... WHERE status='pending' 原子领取，抢不到直接返回；
        - 成本：执行前检查每日调用上限，超限直接规则兜底，不调用 LLM；
        - 兜底：LLM 超时/报错/输出非法 → fallback；上下文/数据库异常 → failed；
        - 审计：模型、Prompt 版本、耗时、token、原始返回、错误原因全部落库；
        - 事务：每个状态变更单独提交，后台任务使用独立 Session，不复用请求会话。
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.agents.context import ContextError, build_review_context
from app.agents.prompts import PROMPT_VERSION
from app.agents.risk_agent import ReviewOutcome, RiskAgent, build_fallback_outcome
from app.config import settings
from app.core.logging import request_id_var
from app.dependencies import SessionLocal
from app.models import AIReviewLog, AIReviewStatus, Expense
from app.models.expense import ExpenseStatus
from app.services.document_service import collect_document_findings, extract_for_expense
from app.services.cost_service import estimate_cost, review_budget_reason
from app.services.llm_client import LLMError
from app.services.policy_service import retrieve_policy
from app.workflow.decision import decide_review
from app.workflow.service import run_review_workflow



logger = logging.getLogger(__name__)

# raw_response 入库上限：防止超长响应把表撑大
MAX_RAW_RESPONSE_CHARS = 20000



class ReviewError(Exception):
    """
    预审业务错误（由 API 层转成 400/404/409）
        :ivar code: 机器可读错误码
    """
    def __init__(self, message: str, *, code: str = "AI_REVIEW_ERROR"):
        """
        :param message: 错误说明
        :param code: 机器可读错误码
        """
        super().__init__(message)
        self.message = message      # 错误说明
        self.code = code            # 错误码



# 创建任务
def create_review(
    db: Session,
    *,
    expense_id: int,
    triggered_by: int | None = None,
    trigger_source: str = "manual",
    force: bool = False,
) -> AIReviewLog:
    """
    创建一次预审任务（不执行，由调用方决定是否投递后台）
        复用规则：
            1. 已有 pending/running（未超时）→ 直接返回该任务；
            2. 缓存期内已有 success 且 force=False → 返回该结果；
            3. 否则新建 pending。
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param triggered_by: 触发人ID
        :param trigger_source: manual / submit
        :param force: 是否忽略缓存（不忽略进行中的任务）
        :return: 预审日志
        :raises ReviewError: 单据不存在/状态不允许
    """
    # 1.锁定单据行，避免同一单据并发创建多个任务
    expense = (
        db.query(Expense)
        .populate_existing()
        .filter(Expense.id == expense_id)
        .with_for_update()
        .first()
    )
    if expense is None:
        raise ReviewError("报销单不存在", code="NOT_FOUND")
    if expense.status != ExpenseStatus.PENDING:
        raise ReviewError("仅待审批状态的报销单可以执行 AI 预审", code="CONFLICT")

    now = datetime.now()

    # 2.把超时未执行的僵尸任务标记为 failed，避免永远卡住
    _reap_stale_tasks(db, expense_id, now=now)

    # 3.已有进行中的任务：直接复用
    active = (
        db.query(AIReviewLog)
        .populate_existing()
        .filter(
            AIReviewLog.expense_id == expense_id,
            AIReviewLog.status.in_([AIReviewStatus.PENDING, AIReviewStatus.RUNNING]),
        )
        .order_by(AIReviewLog.id.desc())
        .first()
    )
    if active is not None:
        db.commit()
        return active

    # 4.缓存期内已有成功结果：默认复用（force=True 可重跑）
    if not force:
        cache_since = now - timedelta(minutes=max(0, settings.AI_REVIEW_CACHE_MINUTES))
        cached = (
            db.query(AIReviewLog)
            .populate_existing()
            .filter(
                AIReviewLog.expense_id == expense_id,
                AIReviewLog.status == AIReviewStatus.SUCCESS,
                AIReviewLog.created_at >= cache_since,
            )
            .order_by(AIReviewLog.id.desc())
            .first()
        )
        if cached is not None:
            db.commit()
            return cached

    # 5.新建 pending 任务
    log = _new_log(
        expense_id=expense_id,
        triggered_by=triggered_by,
        trigger_source=trigger_source,
        status=AIReviewStatus.PENDING,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    db.commit()          # 结束 refresh 开启的只读事务，避免调用方复用会话时读到旧快照
    return log



# 执行任务（后台入口，同步函数，内部 asyncio.run）
def execute_review(
    review_id: int,
    *,
    agent: RiskAgent | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> AIReviewLog | None:
    """
    执行一条预审任务
        :param review_id: 预审日志ID
        :param agent: 可注入的 Agent（测试用）
        :param session_factory: 可注入的会话工厂（测试用）
        :return: 执行后的日志；任务已被领取/不存在时返回 None
    """
    factory = session_factory or SessionLocal
    db = factory()
    log_to_return: AIReviewLog | None = None
    try:
        # 1.原子领取：只有 pending 能被改成 running
        log = _claim_task(db, review_id)
        if log is None:
            return None
        log_to_return = log

        # 2.构造上下文（失败直接标记 failed）
        try:
            context = build_review_context(db, log.expense_id)
        except ContextError as exc:
            _mark_failed(db, log, exc.message)
            return log

        # 3.AI 开关关闭 / 超过每日限额：直接规则兜底，不调 LLM
        if not settings.AI_REVIEW_ENABLED:
            outcome = build_fallback_outcome(context, reason="AI_REVIEW_ENABLED=false")
            _save_outcome(
                db,
                log,
                outcome,
                error_message="AI 预审已关闭",
                decision={"decision": "manual_review", "reason": "AI 预审已关闭，需人工复核"},
            )
            return log
        if budget_reason := review_budget_reason(db, exclude_id=log.id):
            message = {
                "daily_limit": "已达到每日调用上限",
                "daily_token_limit": "已达到每日 token 预算上限",
                "daily_cost_limit": "已达到每日费用预算上限",
            }.get(budget_reason, "已达到每日预算上限")
            outcome = build_fallback_outcome(context, reason=budget_reason)
            _save_outcome(
                db,
                log,
                outcome,
                error_message=message,
                decision={"decision": "manual_review", "reason": f"{message}，需人工复核"},
            )
            return log

        # 4.显式工作流（Phase D：document → rule → rag → risk → decision）
        if settings.WORKFLOW_ENABLED:
            try:
                workflow = run_review_workflow(
                    db,
                    expense_id=log.expense_id,
                    review_log_id=log.id,
                    context=context,
                    agent=agent,
                )
            except Exception as exc:
                logger.exception("AI 预审工作流异常，降级规则兜底 review_id=%s", review_id)
                fallback = build_fallback_outcome(context, reason=f"workflow: {exc}")
                _save_outcome(
                    db,
                    log,
                    fallback,
                    error_message=f"工作流异常，规则兜底：{exc}",
                    decision={"decision": "manual_review", "reason": f"工作流异常，需人工复核：{exc}"[:500]},
                )
                return log

            _save_outcome(
                db,
                log,
                workflow.outcome,
                error_message=workflow.fallback_reason,
                decision=workflow.decision,
                workflow_run_id=workflow.run_id,
            )
            return log

        # 5.旧线性流程（WORKFLOW_ENABLED=false 时兼容）
        context["document_findings"] = []
        if settings.DOCUMENT_AI_ENABLED:
            try:
                document_records = extract_for_expense(db, log.expense_id)
                context["document_findings"] = collect_document_findings(
                    db,
                    log.expense_id,
                    document_records,
                )
            except Exception:
                logger.warning("凭证识别流程异常，本次预审跳过凭证风险", exc_info=True)

        # 6.检索制度条款（失败自动降级为空结果，不阻断预审）
        policy = retrieve_policy(context)

        # 7.调 Agent（同步入口里用 asyncio.run）
        try:
            outcome = asyncio.run((agent or RiskAgent()).review(context, policy=policy))
        except LLMError as exc:
            # LLM 失败/超时/输出非法：规则兜底
            fallback = build_fallback_outcome(context, reason=exc.message)
            _save_outcome(
                db,
                log,
                fallback,
                error_message=exc.message,
                decision={"decision": "manual_review", "reason": f"LLM 调用失败，规则兜底：{exc.message}"},
            )
            return log
        except Exception as exc:
            logger.exception("AI 预审执行异常 review_id=%s", review_id)
            _mark_failed(db, log, f"未预期异常: {exc}")
            return log

        # 8.成功落库并回写单据
        _save_outcome(db, log, outcome, decision=decide_review(outcome))
        return log
    finally:
        # 9.返回前刷新并分离：调用方拿到的是已加载的普通对象，不再依赖已关闭的会话
        if log_to_return is not None:
            try:
                db.refresh(log_to_return)
                db.expunge(log_to_return)
            except Exception:
                logger.warning("预审日志分离失败 review_id=%s", review_id, exc_info=True)
        db.close()



# 查询最新一次预审
def get_latest_review(db: Session, expense_id: int) -> AIReviewLog | None:
    """
    取某张单据最新一次预审记录
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: 最新记录；没有则为 None
    """
    return (
        db.query(AIReviewLog)
        .populate_existing()
        .filter(AIReviewLog.expense_id == expense_id)
        .order_by(AIReviewLog.id.desc())
        .first()
    )



# 新建日志对象（不落库）
def _new_log(
    *,
    expense_id: int,
    triggered_by: int | None,
    trigger_source: str,
    status: AIReviewStatus,
) -> AIReviewLog:
    """
    构造一条预审日志
        :param expense_id: 报销单ID
        :param triggered_by: 触发人ID
        :param trigger_source: 触发来源
        :param status: 初始状态
        :return: AIReviewLog 实例
    """
    trace_id = request_id_var.get()                       # 当前请求ID
    if not trace_id or trace_id == "-":
        trace_id = uuid.uuid4().hex                       # 后台任务没有请求上下文时生成
    return AIReviewLog(
        expense_id=expense_id,
        triggered_by=triggered_by,
        trace_id=trace_id[:64],
        status=status,
        trigger_source=trigger_source,
        provider="deepseek",
        model=settings.DEEPSEEK_MODEL,
        prompt_version=PROMPT_VERSION,
    )



# 把超时的 pending/running 标记为 failed
def _reap_stale_tasks(db: Session, expense_id: int, *, now: datetime) -> None:
    """
    清理僵尸任务：pending/running 超过阈值的记录改为 failed
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param now: 当前时间
        """
    cutoff = now - timedelta(minutes=max(1, settings.AI_REVIEW_STALE_MINUTES))
    db.query(AIReviewLog).filter(
        AIReviewLog.expense_id == expense_id,
        AIReviewLog.status.in_([AIReviewStatus.PENDING, AIReviewStatus.RUNNING]),
        or_(AIReviewLog.updated_at.is_(None), AIReviewLog.updated_at < cutoff),
        AIReviewLog.created_at < cutoff,
    ).update(
        {
            AIReviewLog.status: AIReviewStatus.FAILED,
            AIReviewLog.error_message: "任务超时未执行（僵尸任务）",
        },
        synchronize_session=False,
    )



# 原子领取任务
def _claim_task(db: Session, review_id: int) -> AIReviewLog | None:
    """
    把 pending 任务原子地改成 running
        :param db: 数据库会话
        :param review_id: 预审日志ID
        :return: 领取成功返回日志；失败返回 None
    """
    updated = (
        db.query(AIReviewLog)
        .filter(
            AIReviewLog.id == review_id,
            AIReviewLog.status == AIReviewStatus.PENDING,
        )
        .update(
            {AIReviewLog.status: AIReviewStatus.RUNNING},
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        return None
    return db.query(AIReviewLog).filter(AIReviewLog.id == review_id).first()



# 保存成功/降级结果
def _save_outcome(
    db: Session,
    log: AIReviewLog,
    outcome: ReviewOutcome,
    *,
    error_message: str | None = None,
    decision: dict | None = None,
    workflow_run_id: int | None = None,
) -> None:
    """
    把 Agent 结果写入日志，并按需回写单据
        :param db: 数据库会话
        :param log: 预审日志
        :param outcome: Agent 结果
        :param error_message: 失败/降级原因
        :param decision: 建议决策
        :param workflow_run_id: 工作流运行ID
    """
    log.status = AIReviewStatus.FALLBACK if outcome.is_fallback else AIReviewStatus.SUCCESS
    log.risk_score = outcome.risk_score
    log.risk_level = outcome.risk_level
    log.summary = outcome.summary
    log.findings = outcome.findings
    log.policy_citations = outcome.policy_citations
    log.is_fallback = outcome.is_fallback
    log.model = outcome.model
    log.prompt_version = outcome.prompt_version
    log.policy_version = (outcome.policy_version or "")[:50]
    log.retrieval_meta = outcome.citation_stats or None
    log.decision = str((decision or {}).get("decision") or "")[:30] or None
    log.decision_reason = str((decision or {}).get("reason") or "")[:500] or None
    log.workflow_run_id = workflow_run_id
    log.latency_ms = outcome.latency_ms
    log.prompt_tokens = outcome.prompt_tokens
    log.completion_tokens = outcome.completion_tokens
    log.cost = estimate_cost(outcome.prompt_tokens, outcome.completion_tokens)
    log.raw_response = (
        (outcome.raw_content or "")[:MAX_RAW_RESPONSE_CHARS]
        if settings.AI_REVIEW_RAW_RESPONSE_ENABLED
        else None
    )
    log.error_message = (error_message or "")[:500] or None

    _update_expense_if_latest(db, log, outcome)
    db.commit()



# 回写单据（仅当没有更新的结果）
def _update_expense_if_latest(db: Session, log: AIReviewLog, outcome: ReviewOutcome) -> None:
    """
    把结果回写到 expenses.risk_*，旧结果不覆盖新结果
        :param db: 数据库会话
        :param log: 本次预审日志
        :param outcome: Agent 结果
    """
    newer = (
        db.query(AIReviewLog.id)
        .filter(
            AIReviewLog.expense_id == log.expense_id,
            AIReviewLog.id > log.id,
            AIReviewLog.status != AIReviewStatus.FAILED,
        )
        .first()
    )
    if newer is not None:
        return

    expense = db.query(Expense).filter(Expense.id == log.expense_id).first()
    if expense is None:
        return
    expense.risk_score = outcome.risk_score
    expense.risk_level = outcome.risk_level
    expense.ai_reviewed_result = outcome.summary



# 标记失败
def _mark_failed(db: Session, log: AIReviewLog, message: str) -> None:
    """
    把任务标记为 failed
        :param db: 数据库会话
        :param log: 预审日志
        :param message: 失败原因
    """
    log.status = AIReviewStatus.FAILED
    log.error_message = (message or "未知错误")[:500]
    db.commit()



# 队列兜底：按 ID 标记失败
def mark_review_failed(review_id: int, message: str) -> None:
    """
    队列层兜底：把仍未完成的任务标记为 failed
        使用独立会话，避免 Celery worker 复用已关闭的请求会话。
        :param review_id: 预审日志ID
        :param message: 失败原因
    """
    db = SessionLocal()                                     # 独立数据库会话
    try:
        log = (                                            # 查询预审日志
            db.query(AIReviewLog)
            .filter(AIReviewLog.id == review_id)
            .first()
        )
        if log is None:                                     # 记录不存在：无需处理
            return
        if log.status not in (AIReviewStatus.PENDING, AIReviewStatus.RUNNING):   # 已完成的任务不覆盖
            return
        _mark_failed(db, log, message)                      # 复用统一失败落库逻辑
    finally:
        db.close()                                          # 确保连接归还连接池

