"""
LangGraph 工作流引擎（Phase D）
    图结构：
        START → load_context
              → document（有凭证时）/ rule（无凭证时）
              → rule → rag → risk → decision → END
    设计：
        - 节点函数保持纯业务调用，引擎负责节点耗时与状态记录；
        - 每次节点完成写 workflow_steps，run 级状态写 workflow_runs；
        - 节点失败按配置重试，workflow_steps 记录每次 attempt（恢复后不回退）；
        - 检查点默认落 MySQL（Phase F），关闭开关时回退进程内 MemorySaver；
        - 恢复入口见 workflow/service.py 的 resume_review_workflow。
"""
import logging
import time
from functools import partial
from datetime import datetime
from decimal import Decimal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func
from sqlalchemy.exc import InterfaceError, OperationalError

from app.agents.prompts import PROMPT_VERSION
from app.config import settings
from app.models import WorkflowRun, WorkflowRunStatus, WorkflowStep, WorkflowStepStatus
from app.services.cost_service import estimate_cost
from app.workflow.checkpoint import build_checkpointer
from app.workflow.nodes import (
    decision_node,
    document_node,
    load_context_node,
    rag_node,
    risk_node,
    rule_node,
)
from app.workflow.state import WorkflowState



logger = logging.getLogger(__name__)



# 生成脱敏状态摘要
def _state_summary(state: WorkflowState) -> dict:
    """
    提取可安全落库的状态摘要
        :param state: 工作流状态
        :return: 不含原始票据/完整 Prompt 的摘要
    """
    context = state.get("context") or {}
    outcome = state.get("outcome")
    policy = state.get("policy")
    return {
        "expense_id": state.get("expense_id"),                       # 单号
        "node_timings": state.get("timings") or {},                  # 节点耗时
        "document_findings": len(state.get("document_findings") or []),   # 凭证风险数
        "policy_version": getattr(policy, "version", "") or "",      # 制度版本
        "risk_level": getattr(outcome, "risk_level", None),          # 风险等级
        "risk_score": str(outcome.risk_score) if outcome is not None else None,   # 风险分
        "is_fallback": bool(getattr(outcome, "is_fallback", False)), # 是否兜底
        "decision": (state.get("decision") or {}).get("decision"),   # 决策
        "errors": state.get("errors") or [],                         # 错误
    }



# 工作流记录器
class WorkflowRecorder:
    """
    负责创建 run、记录节点、结束 run
        :ivar run: WorkflowRun 实体
    """

    def __init__(
        self,
        db,
        *,
        expense_id: int,
        review_log_id: int | None,
        engine: str = "langgraph",
    ):
        """
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param review_log_id: 预审日志ID
        :param engine: 编排引擎名
        """
        self.db = db
        self.run = WorkflowRun(
            expense_id=expense_id,
            review_log_id=review_log_id,
            status=WorkflowRunStatus.RUNNING,
            engine=engine,
            prompt_version=PROMPT_VERSION,
            model=settings.DEEPSEEK_MODEL,
            started_at=datetime.now(),
        )
        db.add(self.run)
        db.commit()
        db.refresh(self.run)

    @classmethod
    def resume(cls, db, run: WorkflowRun) -> "WorkflowRecorder":
        """
        从已有 run 恢复记录器
            :param db: 数据库会话
            :param run: 已存在的 workflow_run
            :return: 记录器实例
        """
        obj = cls.__new__(cls)                          # 跳过 __init__，不新建 run
        obj.db = db                                      # 数据库会话
        obj.run = run                                    # 复用原 run
        run.status = WorkflowRunStatus.RUNNING           # 重新置为执行中
        run.error_message = None                         # 清理上次失败原因
        run.finished_at = None                           # 清理结束时间
        db.commit()
        return obj

    @property
    def run_id(self) -> int:
        """
        当前 run ID
            :return: 数据库ID
        """
        return int(self.run.id)

    # 记录节点
    def record(
        self,
        node: str,
        status: WorkflowStepStatus,
        *,
        attempt: int,
        latency_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal | None = None,
        summary: dict | None = None,
        error: str | None = None,
    ) -> None:
        """
        写一条节点记录，并更新 run 当前节点
            :param node: 节点名
            :param status: 节点状态
            :param attempt: 本次尝试序号（内部会按历史记录单调递增）
            :param latency_ms: 耗时
            :param prompt_tokens: 输入 token
            :param completion_tokens: 输出 token
            :param cost: 估算费用；为空时按 token 现算
            :param summary: 脱敏摘要
            :param error: 错误信息
        """
        # 1.取该 run + node 已有的最大尝试次数，保证恢复/重试后编号不回退
        max_attempt = (
            self.db.query(func.max(WorkflowStep.attempt))
            .filter(
                WorkflowStep.run_id == self.run.id,
                WorkflowStep.node == node,
            )
            .scalar()
            or 0
        )
        effective_attempt = max(int(attempt), int(max_attempt) + 1)   # 单调递增
        effective_cost = cost if cost is not None else estimate_cost(prompt_tokens, completion_tokens)

        # 2.落节点记录并刷新 run 当前节点
        step = WorkflowStep(
            run_id=self.run.id,
            node=node,
            attempt=effective_attempt,
            status=status,
            latency_ms=latency_ms,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            cost=effective_cost,
            summary=summary,
            error_message=(error or "")[:500] or None,
        )
        self.db.add(step)
        self.run.current_node = node
        self.run.state_json = {
            "current_node": node,
            "last_summary": summary or {},
            "last_attempt": effective_attempt,
        }
        self.db.commit()

    # 结束 run
    def finish(
        self,
        status: WorkflowRunStatus,
        *,
        state: WorkflowState | None = None,
        error: str | None = None,
    ) -> None:
        """
        更新 run 最终状态
            :param status: 最终状态
            :param state: 最终状态快照
            :param error: 失败原因
        """
        if state is not None:
            self.run.state_json = _state_summary(state)
            decision = state.get("decision") or {}
            self.run.decision = str(decision.get("decision") or "")[:30] or None
            self.run.decision_reason = str(decision.get("reason") or "")[:500] or None
            policy = state.get("policy")
            self.run.policy_version = str(getattr(policy, "version", "") or "")[:50]
        self.run.status = status
        self.run.error_message = (error or "")[:500] or None
        self.run.finished_at = datetime.now()

        # 汇总节点用量：只统计成功/降级的尝试，失败尝试不计费
        prompt_sum, completion_sum, cost_sum = (
            self.db.query(
                func.coalesce(func.sum(WorkflowStep.prompt_tokens), 0),
                func.coalesce(func.sum(WorkflowStep.completion_tokens), 0),
                func.coalesce(func.sum(WorkflowStep.cost), 0),
            )
            .filter(
                WorkflowStep.run_id == self.run.id,
                WorkflowStep.status.in_([
                    WorkflowStepStatus.SUCCESS,
                    WorkflowStepStatus.FALLBACK,
                ]),
            )
            .one()
        )
        self.run.prompt_tokens = int(prompt_sum or 0)
        self.run.completion_tokens = int(completion_sum or 0)
        self.run.cost = Decimal(str(cost_sum or 0))
        self.db.commit()



# 判断异常是否值得重试
def _is_retryable(exc: Exception) -> bool:
    """
    判断节点异常是否属于瞬态、可重试错误
        :param exc: 节点抛出的异常
        :return: 可重试 True
    """
    if getattr(exc, "retryable", False):                     # LLMError 等自带 retryable 标记
        return True
    return isinstance(exc, (OperationalError, InterfaceError, TimeoutError, ConnectionError))



# 计算重试退避
def _retry_delay(attempt: int) -> float:
    """
    计算第 attempt 次失败后的退避时间
        :param attempt: 已失败的尝试序号（从 1 开始）
        :return: 秒
    """
    base = max(0.0, float(settings.WORKFLOW_NODE_RETRY_BACKOFF_SECONDS))
    cap = max(base, float(settings.WORKFLOW_NODE_RETRY_MAX_BACKOFF_SECONDS))
    return min(cap, base * (2 ** max(0, attempt - 1)))



# 提取节点 token 用量
def _node_usage(patch: dict) -> tuple[int, int]:
    """
    从节点返回增量中提取 token 用量
        :param patch: 节点返回的状态增量
        :return: (prompt_tokens, completion_tokens)
    """
    # 1.凭证节点：本次新增识别的用量
    usage = patch.get("document_usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    # 2.风险节点：ReviewOutcome 自带 token
    outcome = patch.get("outcome")
    if outcome is not None:
        prompt_tokens += int(getattr(outcome, "prompt_tokens", 0) or 0)
        completion_tokens += int(getattr(outcome, "completion_tokens", 0) or 0)
    return prompt_tokens, completion_tokens



# 节点包装：记录耗时、状态与重试
def _wrap_node(
    recorder: WorkflowRecorder,
    node: str,
    func,
    *,
    fallback_node: bool = False,
    max_attempts: int | None = None,
):
    """
    包装 LangGraph 节点函数，统一记录 step 并处理节点级重试
        :param recorder: 记录器
        :param node: 节点名
        :param func: 节点函数
        :param fallback_node: 是否允许节点返回 fallback 状态
        :param max_attempts: 最大尝试次数；为空时读配置
        :return: LangGraph 节点函数
    """
    attempts = max(1, int(max_attempts or settings.WORKFLOW_NODE_MAX_ATTEMPTS))

    def wrapped(state: WorkflowState) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()

            # 1.只把"节点函数本身"放进重试范围，审计写入失败不重跑节点，避免副作用重复
            try:
                patch = func(state) or {}
            except Exception as exc:
                # 2.回滚失败尝试可能留下的半成品写入，避免重试时重复落库
                rollback = getattr(getattr(recorder, "db", None), "rollback", None)
                if callable(rollback):
                    try:
                        rollback()
                    except Exception:
                        logger.exception("节点失败后回滚异常 node=%s attempt=%s", node, attempt)

                # 3.记录本次失败
                latency = int((time.perf_counter() - started) * 1000)
                last_exc = exc
                try:
                    recorder.record(
                        node,
                        WorkflowStepStatus.FAILED,
                        attempt=attempt,
                        latency_ms=latency,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                except Exception:
                    logger.exception("节点失败记录写入异常 node=%s attempt=%s", node, attempt)

                # 4.不可重试或次数用尽：直接抛出，由引擎标记 run failed
                if attempt >= attempts or not _is_retryable(exc):
                    raise

                # 5.可重试：退避后进入下一轮
                delay = _retry_delay(attempt)
                logger.warning(
                    "节点执行失败，准备重试 run_id=%s node=%s attempt=%s/%s delay=%ss error=%s",
                    recorder.run_id, node, attempt, attempts, delay, exc,
                )
                if delay > 0:
                    time.sleep(delay)
                continue

            # 6.节点成功：计算耗时、合并状态，再单独写审计
            latency = int((time.perf_counter() - started) * 1000)
            step_status = WorkflowStepStatus.SUCCESS
            if fallback_node and patch.get("fallback_reason"):
                step_status = WorkflowStepStatus.FALLBACK
            timings = dict(state.get("timings") or {})
            timings[node] = latency
            patch["timings"] = timings

            # 7.审计写入失败只记录日志，不重跑已经成功的节点
            prompt_tokens, completion_tokens = _node_usage(patch)   # 节点 token 用量
            node_cost = estimate_cost(prompt_tokens, completion_tokens)
            try:
                recorder.record(
                    node,
                    step_status,
                    attempt=attempt,
                    latency_ms=latency,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=node_cost,
                    summary={
                        "fallback": bool(patch.get("fallback_reason")),
                        "findings": len(patch.get("document_findings") or []),
                        "attempts": attempt,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                    },
                )
            except Exception:
                logger.exception("节点成功但审计写入失败 node=%s attempt=%s", node, attempt)
            return patch

        # 理论上不可达；保留兜底
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"节点执行失败: {node}")
    return wrapped



# 构建 LangGraph 图
def build_graph(db, *, context: dict | None, agent=None, recorder: WorkflowRecorder, checkpointer=None):
    """
    构建预审工作流
        :param db: 数据库会话
        :param context: 已构造的上下文
        :param agent: 可注入的风险 Agent
        :param recorder: 记录器
        :param checkpointer: 可注入的检查点存储；默认按配置使用 MySQL/MemorySaver
        :return: 编译后的 graph
    """
    builder = StateGraph(WorkflowState)

    # 1.节点
    builder.add_node(
        "load_context",
        _wrap_node(recorder, "load_context", partial(load_context_node, db=db, context=context)),
    )
    builder.add_node(
        "document",
        _wrap_node(recorder, "document", partial(document_node, db=db)),
    )
    builder.add_node("rule", _wrap_node(recorder, "rule", rule_node))
    builder.add_node("rag", _wrap_node(recorder, "rag", rag_node))
    builder.add_node(
        "risk",
        _wrap_node(recorder, "risk", partial(risk_node, agent=agent), fallback_node=True),
    )
    builder.add_node("decision", _wrap_node(recorder, "decision", decision_node))

    # 2.边
    builder.add_edge(START, "load_context")

    def route_after_load(state: WorkflowState) -> str:
        """
        有凭证才进入 document，否则直接 rule
            :param state: 工作流状态
            :return: 下一节点
        """
        items = (state.get("context") or {}).get("items") or []
        return "document" if any(item.get("has_invoice") for item in items) else "rule"

    builder.add_conditional_edges(
        "load_context",
        route_after_load,
        {"document": "document", "rule": "rule"},
    )
    builder.add_edge("document", "rule")
    builder.add_edge("rule", "rag")
    builder.add_edge("rag", "risk")
    builder.add_edge("risk", "decision")
    builder.add_edge("decision", END)

    # 3.编译（默认 MySQL 持久化检查点；关闭开关时回退进程内 MemorySaver）
    if checkpointer is None:
        checkpointer = build_checkpointer() if settings.WORKFLOW_CHECKPOINT_ENABLED else MemorySaver()
    return builder.compile(checkpointer=checkpointer)
