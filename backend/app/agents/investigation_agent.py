"""
AI 核查 Agent（方案 B：LangGraph 预置 ReAct）
    定位：只做“调查取证”，不审批、不改单据、不写业务状态。

    最小闭环：
        - 6 个只读工具：单据概览、规则命中、制度检索、历史同类单、审批轨迹、凭证号查重；
        - LLM 通过原生 tool calling 自己决定调用顺序；
        - LangGraph `create_react_agent` 负责 plan → tool → observe 循环；
        - 最多 4 次工具调用（recursion_limit 约束），超时由外层控制；
        - 最终只产出核查报告和工具轨迹，审批人仍是决策者。

    降级：
        - 未配置 API Key / 依赖缺失 → InvestigationConfigError；
        - 工具异常 → ToolNode 把错误回给模型，模型继续或结束；
        - 整体失败由 API 层记录 failed，不影响原审批流。
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Callable

from langchain_core.tools import tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.agents.context import build_review_context
from app.config import settings
from app.models import (
    Approval,
    DocumentExtraction,
    Expense,
    ExpenseItem,
    User,
)
from app.models.expense import ExpenseStatus
from app.rag.retriever import get_retriever
from app.rag.schema import PolicyQuery

logger = logging.getLogger(__name__)

INVESTIGATION_PROMPT_VERSION = "investigation_v1"
MAX_TOOL_STEPS = 4

SYSTEM_PROMPT = """你是报销审核的“核查 Agent”，只负责调查取证，不负责审批决定。

你可以使用以下只读工具：
- get_expense_overview：查看单据、明细、规则命中
- search_policy：检索制度条款
- find_similar_expenses：查找同人/同日期/同类别/同金额的历史单据
- get_approval_trail：查看审批轨迹
- check_invoice_duplicate：检查凭证号是否重复

工作规则：
1. 先用工具核实，再下结论；不要编造单据内容、金额、条款编号。
2. 优先查规则命中、制度条款、历史同类单；需要时再查审批轨迹/凭证号。
3. 最多调用 4 次工具；信息足够就立即结束，不要凑步骤。
4. 工具返回错误时说明“该项核查失败”，不要假装查到。
5. 最终用中文输出简洁报告：已核实事实、疑点、对应制度条款、建议审批人核对什么。
6. 不要给出“通过/驳回/付款”的最终决定，只给核查建议。"""


class InvestigationConfigError(RuntimeError):
    """Agent 未配置或依赖不可用"""


def _json(data: Any) -> str:
    """工具返回值统一转 JSON 字符串，Decimal/date 等交给 default=str"""
    return json.dumps(data, ensure_ascii=False, default=str)


def build_tools(session_factory: Callable[[], Session], *, expense_id: int) -> list:
    """
    构造只读工具集
        :param session_factory: 会话工厂；每个工具独立开关会话，避免跨线程共享 Session
        :param expense_id: 服务端已授权并绑定的单据ID；模型不能修改
        :return: LangChain Tool 列表
    """
    target_id = int(expense_id)

    @tool
    def get_expense_overview() -> str:
        """查看当前报销单概览：标题、状态、金额、明细、已有规则命中。"""
        try:
            with session_factory() as db:
                context = build_review_context(db, target_id)
            return _json(context)
        except Exception as exc:
            return _json({"error": f"读取单据失败: {exc}"})

    @tool
    def search_policy(query: str) -> str:
        """按自然语言问题检索制度条款，返回 clause_id、条款原文和匹配分数。"""
        try:
            with session_factory() as db:
                expense = (
                    db.query(Expense)
                    .filter(Expense.id == target_id)
                    .first()
                )
                if expense is None:
                    return _json({"error": "报销单不存在"})
                expense_types = {
                    item.category.expense_type.value
                    for item in expense.items
                    if item.category is not None and item.category.expense_type is not None
                }
                role = expense.user.role.value if expense.user is not None else ""
                policy_query = PolicyQuery(
                    text=f"{query} {expense.title or ''}",
                    expense_types=frozenset(expense_types),
                    role=role,
                    total_amount=Decimal(str(expense.total_amount or 0)),
                )
                hits = get_retriever().retrieve(policy_query)[:3]
            return _json([
                {
                    "clause_id": hit.clause.clause_id,
                    "text": hit.clause.text,
                    "score": round(float(hit.score), 4),
                }
                for hit in hits
            ])
        except Exception as exc:
            return _json({"error": f"制度检索失败: {exc}"})

    @tool
    def find_similar_expenses() -> str:
        """查找当前单据提交人的同日期、同类别、同金额历史明细（排除草稿/取消/驳回）。"""
        try:
            with session_factory() as db:
                current = db.query(Expense).filter(Expense.id == target_id).first()
                if current is None:
                    return _json({"error": "报销单不存在"})
                conditions = [
                    and_(
                        ExpenseItem.category_id == item.category_id,
                        ExpenseItem.amount == item.amount,
                        ExpenseItem.expense_date == item.expense_date,
                    )
                    for item in current.items
                    if item.category_id is not None and item.expense_date is not None
                ]
                if not conditions:
                    return _json([])
                rows = (
                    db.query(Expense, ExpenseItem)
                    .join(ExpenseItem, ExpenseItem.expense_id == Expense.id)
                    .filter(
                        Expense.user_id == current.user_id,
                        Expense.id != current.id,
                        Expense.status.notin_([
                            ExpenseStatus.DRAFT,
                            ExpenseStatus.CANCELLED,
                            ExpenseStatus.REJECTED,
                        ]),
                        or_(*conditions),
                    )
                    .order_by(Expense.id.desc())
                    .limit(10)
                    .all()
                )
            return _json([
                {
                    "expense_id": expense.id,
                    "expense_no": expense.expense_no,
                    "status": expense.status.value,
                    "item_id": item.id,
                    "description": item.description,
                    "amount": str(item.amount),
                    "expense_date": item.expense_date.isoformat() if item.expense_date else None,
                }
                for expense, item in rows
            ])
        except Exception as exc:
            return _json({"error": f"历史单据查询失败: {exc}"})

    @tool
    def get_approval_trail() -> str:
        """查看当前单据的审批层级、审批状态、审批人和意见。"""
        try:
            with session_factory() as db:
                rows = (
                    db.query(Approval, User.full_name)
                    .outerjoin(User, Approval.approver_id == User.id)
                    .filter(Approval.expense_id == target_id)
                    .order_by(Approval.level.asc(), Approval.id.asc())
                    .all()
                )
            return _json([
                {
                    "level": approval.level,
                    "status": approval.status.value,
                    "approver": name,
                    "comment": approval.comment,
                    "acted_at": approval.acted_at.isoformat() if approval.acted_at else None,
                }
                for approval, name in rows
            ])
        except Exception as exc:
            return _json({"error": f"审批轨迹查询失败: {exc}"})

    @tool
    def check_invoice_duplicate() -> str:
        """检查当前单据的凭证号是否在同一提交人的其他报销单出现。"""
        try:
            with session_factory() as db:
                current = db.query(Expense).filter(Expense.id == target_id).first()
                if current is None:
                    return _json({"error": "报销单不存在"})
                identifiers = [
                    row[0]
                    for row in (
                        db.query(DocumentExtraction.identifier)
                        .join(ExpenseItem, DocumentExtraction.expense_item_id == ExpenseItem.id)
                        .filter(
                            ExpenseItem.expense_id == target_id,
                            DocumentExtraction.identifier.isnot(None),
                            DocumentExtraction.identifier != "",
                        )
                        .distinct()
                        .all()
                    )
                ]
                if not identifiers:
                    return _json([])
                rows = (
                    db.query(DocumentExtraction.identifier, Expense.expense_no, Expense.id)
                    .join(ExpenseItem, DocumentExtraction.expense_item_id == ExpenseItem.id)
                    .join(Expense, ExpenseItem.expense_id == Expense.id)
                    .filter(
                        DocumentExtraction.identifier.in_(identifiers),
                        Expense.id != target_id,
                        # 不跨提交人暴露凭证号/单号，避免核查工具成为信息泄露通道
                        Expense.user_id == current.user_id,
                    )
                    .limit(10)
                    .all()
                )
            return _json([
                {"identifier": identifier, "expense_no": expense_no, "expense_id": other_id}
                for identifier, expense_no, other_id in rows
            ])
        except Exception as exc:
            return _json({"error": f"凭证号查重失败: {exc}"})

    return [
        get_expense_overview,
        search_policy,
        find_similar_expenses,
        get_approval_trail,
        check_invoice_duplicate,
    ]


class InvestigationAgent:
    """有界 ReAct 核查 Agent"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        expense_id: int,
        graph: Any | None = None,
        max_steps: int = MAX_TOOL_STEPS,
        timeout_seconds: float | None = None,
    ):
        self.session_factory = session_factory
        self.expense_id = int(expense_id)
        self._graph = graph
        self.max_steps = max_steps
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else settings.AI_REVIEW_TIMEOUT_SECONDS
        )

    def _build_graph(self):
        """按 DeepSeek 的 OpenAI 兼容接口构造 ReAct 图"""
        api_key = settings.DEEPSEEK_API_KEY.get_secret_value().strip()
        if not api_key:
            raise InvestigationConfigError("未配置 DEEPSEEK_API_KEY，无法执行 AI 核查")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise InvestigationConfigError("缺少 langchain-openai 依赖") from exc

        model = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=api_key,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0,
            timeout=self.timeout_seconds,
            max_retries=0,          # 不在模型层重试，避免一次请求重复计费
        )
        return create_react_agent(
            model,
            build_tools(self.session_factory, expense_id=self.expense_id),
            prompt=SYSTEM_PROMPT,
        )

    async def run(self, question: str) -> dict:
        """
        执行一次核查
            :param question: 核查问题
            :return: {report, trace, model, prompt_tokens, completion_tokens}
        """
        graph = self._graph or self._build_graph()
        # 每次工具调用约 2 个 superstep（AI 决策 + 工具执行），留一点余量
        config = {"recursion_limit": max(6, self.max_steps * 2 + 2)}
        result = await asyncio.wait_for(
            graph.ainvoke(
                {"messages": [{"role": "user", "content": question}]},
                config=config,
            ),
            timeout=self.timeout_seconds,
        )
        return self._parse_result(result)

    @staticmethod
    def _parse_result(result: dict) -> dict:
        """从 ReAct 图输出中提取最终报告、工具轨迹与 token"""
        messages = result.get("messages") or []
        steps: list[dict] = []
        by_call_id: dict[str, dict] = {}
        report = ""
        prompt_tokens = 0
        completion_tokens = 0

        for message in messages:
            usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)

            if isinstance(message, AIMessage):
                for call in (message.tool_calls or []):
                    step = {
                        "tool": call.get("name"),
                        "args": call.get("args") or {},
                        "status": "called",
                        "result": None,
                    }
                    steps.append(step)
                    if call.get("id"):
                        by_call_id[call["id"]] = step
                # 最后一个不带 tool_calls 的 AI 消息就是最终报告
                if message.content and not message.tool_calls:
                    report = (
                        message.content
                        if isinstance(message.content, str)
                        else json.dumps(message.content, ensure_ascii=False)
                    )
            elif isinstance(message, ToolMessage):
                step = by_call_id.get(getattr(message, "tool_call_id", ""))
                if step is not None:
                    step["status"] = getattr(message, "status", "success") or "success"
                    step["result"] = str(message.content)[:1000]

        return {
            "report": report.strip(),
            "trace": steps,
            "model": settings.DEEPSEEK_MODEL,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
