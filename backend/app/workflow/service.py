"""
工作流服务（Phase D 入口）
    对外提供：
        - run_review_workflow：新建 run 并执行；
        - resume_review_workflow：从 MySQL 检查点恢复失败的 run。
    边界：
        - 本模块只做 run 生命周期与结果整理，节点逻辑在 nodes.py；
        - 失败时记录 run failed，由调用方决定是否降级；
        - 恢复依赖 workflow_runs 中已有的 run_id 与 workflow_checkpoints 中的检查点。
"""
import logging
from dataclasses import dataclass

from app.agents.risk_agent import ReviewOutcome
from app.config import settings
from app.models import WorkflowRun, WorkflowRunStatus
from app.workflow.checkpoint import build_checkpointer
from app.workflow.engine import WorkflowRecorder, build_graph
from app.workflow.state import WorkflowState



logger = logging.getLogger(__name__)



# 工作流最终结果
@dataclass(frozen=True)
class WorkflowResult:
    """
    一次工作流的输出
        :ivar run_id: 工作流运行ID
        :ivar outcome: 风险评审结果
        :ivar decision: 建议决策
        :ivar fallback_reason: 降级原因
    """
    run_id: int                          # 运行ID
    outcome: ReviewOutcome               # 风险结果
    decision: dict                       # 建议决策
    fallback_reason: str | None = None   # 降级原因



# 整理最终结果
def _finalize(recorder: WorkflowRecorder, final_state: WorkflowState) -> WorkflowResult:
    """
    把 LangGraph 最终状态整理成 WorkflowResult
        :param recorder: 记录器
        :param final_state: 工作流最终状态
        :return: WorkflowResult
        :raises RuntimeError: 未产生风险结果
    """
    # 1.取风险结果；没有结果视为失败
    outcome = final_state.get("outcome")     # 风险结果
    decision = final_state.get("decision") or {
        "decision": "manual_review",
        "reason": "工作流未产生决策，需人工复核",
    }
    if outcome is None:
        recorder.finish(
            WorkflowRunStatus.FAILED,
            state=final_state,
            error="工作流未产生风险结果",
        )
        raise RuntimeError("工作流未产生风险结果")

    # 2.成功收尾并返回
    recorder.finish(WorkflowRunStatus.SUCCESS, state=final_state)
    return WorkflowResult(
        run_id=recorder.run_id,
        outcome=outcome,
        decision=decision,
        fallback_reason=final_state.get("fallback_reason"),
    )



# 执行一次预审工作流
def run_review_workflow(
    db,
    *,
    expense_id: int,
    review_log_id: int | None,
    context: dict | None = None,
    agent=None,
) -> WorkflowResult:
    """
    执行一次 AI 预审工作流
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param review_log_id: 预审日志ID
        :param context: 已构造的上下文
        :param agent: 可注入的风险 Agent
        :return: WorkflowResult
        :raises Exception: 工作流节点失败（调用方负责标记 failed）
    """
    # 1.创建 run 记录
    recorder = WorkflowRecorder(
        db,
        expense_id=expense_id,
        review_log_id=review_log_id,
    )

    # 2.构建图
    graph = build_graph(db, context=context, agent=agent, recorder=recorder)

    # 3.初始状态（只放轻量字段；上下文由 load_context 节点放入）
    initial: WorkflowState = {
        "expense_id": expense_id,
        "review_log_id": review_log_id,
        "errors": [],
        "timings": {},
    }

    try:
        final_state = graph.invoke(
            initial,
            config={"configurable": {"thread_id": str(recorder.run_id)}},
        )
    except Exception as exc:
        recorder.finish(WorkflowRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        logger.exception("AI 预审工作流失败 run_id=%s", recorder.run_id)
        raise

    # 4.整理结果
    return _finalize(recorder, final_state)



# 从检查点恢复
def resume_review_workflow(
    db,
    *,
    run_id: int,
    agent=None,
) -> WorkflowResult:
    """
    从 MySQL 检查点恢复一次失败/中断的预审工作流
        :param db: 数据库会话
        :param run_id: 已存在的 workflow_run ID
        :param agent: 可注入的风险 Agent
        :return: WorkflowResult
        :raises ValueError: run 不存在或没有可用检查点
    """
    # 1.查 run
    run = db.query(WorkflowRun).filter(WorkflowRun.id == run_id).first()
    if run is None:
        raise ValueError(f"工作流运行不存在: {run_id}")
    if not settings.WORKFLOW_CHECKPOINT_ENABLED:
        raise ValueError("检查点已关闭，无法恢复工作流")

    # 2.确认存在检查点；没有则不能恢复
    checkpointer = build_checkpointer()
    config = {"configurable": {"thread_id": str(run.id)}}
    if checkpointer.get_tuple(config) is None:
        raise ValueError(f"工作流没有可恢复的检查点: {run_id}")

    # 3.复用 run，构建同一 thread_id 的图，输入 None 表示从检查点续跑
    recorder = WorkflowRecorder.resume(db, run)
    graph = build_graph(
        db,
        context=None,
        agent=agent,
        recorder=recorder,
        checkpointer=checkpointer,
    )
    try:
        final_state = graph.invoke(None, config=config)
    except Exception as exc:
        recorder.finish(WorkflowRunStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
        logger.exception("AI 预审工作流恢复失败 run_id=%s", run_id)
        raise

    # 4.整理结果
    return _finalize(recorder, final_state)
