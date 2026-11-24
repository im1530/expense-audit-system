"""
工作流节点实现（Phase D）
    节点只负责编排层逻辑，具体业务仍复用 Phase B/C 的 service：
        - load_context：构造预审上下文；
        - document：凭证识别 + 凭证风险；
        - rule：汇总规则/凭证的确定性结论（供审计与后续节点使用）；
        - rag：制度检索；
        - risk：LLM 风险评审；
        - decision：确定性建议。
    约定：
        - 单张凭证失败、制度检索失败、LLM 失败都降级，不阻断整个工作流；
        - 上下文/凭证结论异常才让节点失败，由引擎记录并终止。
"""
import asyncio
import logging

from app.agents.context import build_review_context
from app.agents.risk_agent import (
    RiskAgent,
    build_document_findings,
    build_fallback_outcome,
    build_rule_findings,
)
from app.services.document_service import collect_document_findings, extract_for_expense_with_usage
from app.services.llm_client import LLMError
from app.services.policy_service import retrieve_policy
from app.workflow.decision import decide_review
from app.workflow.state import WorkflowState



logger = logging.getLogger(__name__)



# 加载上下文
def load_context_node(state: WorkflowState, *, db, context: dict | None = None) -> dict:
    """
    加载/复用预审上下文
        :param state: 工作流状态
        :param db: 数据库会话
        :param context: 已构造的上下文；不传则重新构造
        :return: 状态增量
    """
    if context is None:
        context = build_review_context(db, int(state["expense_id"]))
    return {"context": context}



# 凭证识别
def document_node(state: WorkflowState, *, db) -> dict:
    """
    对单据下的凭证执行识别，并生成凭证风险
        单张失败只记录日志，不阻断后续节点。
        :param state: 工作流状态
        :param db: 数据库会话
        :return: 状态增量
    """
    expense_id = int(state["expense_id"])
    try:
        records, usage = extract_for_expense_with_usage(db, expense_id)
        findings = collect_document_findings(db, expense_id, records)
        context = dict(state.get("context") or {})          # 复制上下文，避免修改原对象
        context["document_findings"] = findings             # 风险 Agent 读取该字段
        return {
            "document_findings": findings,
            "document_usage": usage,
            "context": context,
        }
    except Exception:
        # 整单凭证节点异常不能静默降级：抛给工作流引擎，由 run 失败/外层规则兜底显式暴露，
        # 否则审批人看到的是“正常 AI 结论”，却少了整类凭证风险。
        logger.exception("凭证节点失败 expense_id=%s", expense_id)
        raise



# 规则汇总
def rule_node(state: WorkflowState) -> dict:
    """
    汇总规则命中与凭证结论，作为确定性审计摘要
        :param state: 工作流状态
        :return: 状态增量
    """
    context = state.get("context") or {}
    rule_findings = build_rule_findings(context)
    document_findings = build_document_findings(context)
    return {
        "rule_summary": {
            "rule_count": len(rule_findings),
            "document_count": len(document_findings),
        }
    }



# 制度检索
def rag_node(state: WorkflowState) -> dict:
    """
    检索制度条款
        :param state: 工作流状态
        :return: 状态增量
    """
    policy = retrieve_policy(state.get("context") or {})
    return {"policy": policy}



# 风险评审
def risk_node(state: WorkflowState, *, agent=None) -> dict:
    """
    调用风险 Agent；LLM 失败时降级为规则兜底
        :param state: 工作流状态
        :param agent: 可注入的 Agent（测试用）
        :return: 状态增量
    """
    context = state.get("context") or {}
    policy = state.get("policy")
    try:
        outcome = asyncio.run((agent or RiskAgent()).review(context, policy=policy))
        return {"outcome": outcome, "fallback_reason": None}
    except LLMError as exc:
        fallback = build_fallback_outcome(context, reason=exc.message)
        logger.warning("风险节点 LLM 失败，降级规则兜底 expense_id=%s error=%s", state.get("expense_id"), exc.message)
        return {"outcome": fallback, "fallback_reason": exc.message}



# 决策
def decision_node(state: WorkflowState) -> dict:
    """
    生成确定性建议决策
        :param state: 工作流状态
        :return: 状态增量
    """
    outcome = state.get("outcome")
    if outcome is None:
        return {
            "decision": {
                "decision": "manual_review",
                "reason": "工作流未产生风险结论，需人工复核",
            }
        }
    return {
        "decision": decide_review(
            outcome,
            fallback_reason=state.get("fallback_reason"),
        )
    }
