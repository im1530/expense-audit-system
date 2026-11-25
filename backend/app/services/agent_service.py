"""
AI 核查服务
    负责：创建/更新 agent_investigations 记录、调用 InvestigationAgent、把异常落库。
    Agent 本身只读；这里不做审批决策，也不修改 Expense 状态。
"""
import asyncio
import logging
import time

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.agents.investigation_agent import (
    INVESTIGATION_PROMPT_VERSION,
    InvestigationAgent,
    InvestigationConfigError,
)
from app.dependencies import SessionLocal
from app.models import AgentInvestigation, AgentInvestigationStatus, Expense, User

logger = logging.getLogger(__name__)

DEFAULT_QUESTION = (
    "请核查这张报销单：先看单据和已有规则命中，再查相关制度条款、"
    "同人同日同类同额的历史单据；如有必要再查审批轨迹和凭证号重复。"
    "请给出已核实事实、疑点和建议审批人核对的重点。"
)


async def investigate_expense(
    db: Session,
    *,
    expense: Expense,
    user: User,
    question: str | None = None,
    agent: InvestigationAgent | None = None,
) -> AgentInvestigation:
    """
    执行一次 AI 核查并持久化
        :param db: 请求会话（用于创建/更新记录，不参与 Agent 工具查询）
        :param expense: 已通过权限校验的单据
        :param user: 触发人
        :param question: 可选核查问题
        :param agent: 可注入测试 Agent
        :return: 已完成的核查记录
        :raises HTTPException: 503 未配置/502 调用失败/504 超时
    """
    question = (question or DEFAULT_QUESTION).strip()[:500]
    record = AgentInvestigation(
        expense_id=expense.id,
        created_by=user.id,
        status=AgentInvestigationStatus.RUNNING,
        question=question,
        model=None,
        prompt_version=INVESTIGATION_PROMPT_VERSION,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    runner = agent or InvestigationAgent(SessionLocal)
    started = time.perf_counter()
    try:
        result = await runner.run(expense.id, question)
        record.status = AgentInvestigationStatus.SUCCESS
        record.report = (result.get("report") or "")[:20000]
        record.trace = result.get("trace") or []
        record.model = result.get("model")
        record.prompt_tokens = int(result.get("prompt_tokens") or 0)
        record.completion_tokens = int(result.get("completion_tokens") or 0)
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        db.commit()
        db.refresh(record)
        return record
    except InvestigationConfigError as exc:
        _mark_failed(db, record, str(exc), started)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        _mark_failed(db, record, "AI 核查超时", started)
        raise HTTPException(status_code=504, detail="AI 核查超时，请稍后重试") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("AI 核查失败 expense_id=%s", expense.id)
        _mark_failed(db, record, f"{type(exc).__name__}: {exc}", started)
        raise HTTPException(status_code=502, detail="AI 核查失败，请稍后重试") from exc


def _mark_failed(db: Session, record: AgentInvestigation, message: str, started: float) -> None:
    """失败落库，保留耗时，便于排查"""
    record.status = AgentInvestigationStatus.FAILED
    record.error_message = (message or "未知错误")[:500]
    record.latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        db.commit()
    except Exception:
        logger.exception("AI 核查失败状态落库失败 id=%s", record.id)
        db.rollback()
