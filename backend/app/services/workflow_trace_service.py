"""
AI 预审工作流轨迹服务（只读）
    职责：
        1. 取某张报销单最近一次工作流 run；
        2. 汇总 run 级信息、节点执行记录、最终预审日志（决策/引用/风险结论）；
        3. 汇总凭证识别结论（每条附件取最近一次）；
        4. 组装成可直接返回前端的轨迹结构。
    边界：
        - 只读：不写库、不调用模型、不触发任何状态变化；
        - 不返回 raw_response、票面原文等敏感字段；
        - run 不存在返回 None，由 API 层转 404。
"""
import logging

from sqlalchemy.orm import Session

from app.models import (
    AIReviewLog,
    Attachment,
    DocumentExtraction,
    ExpenseItem,
    WorkflowRun,
    WorkflowStep,
)


logger = logging.getLogger(__name__)

# 轨迹里最多返回多少条凭证记录：防止异常数据把响应撑大
MAX_DOCUMENTS = 20



# 取最近一次工作流 run
def _latest_run(db: Session, expense_id: int) -> WorkflowRun | None:
    """
    取一张单据最近一次工作流运行
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: WorkflowRun；没有则 None
    """
    return (
        db.query(WorkflowRun)
        .filter(WorkflowRun.expense_id == expense_id)
        .order_by(WorkflowRun.id.desc())
        .first()
    )



# 取该 run 的节点记录
def _steps(db: Session, run_id: int) -> list[WorkflowStep]:
    """
    按执行顺序取节点记录
        :param db: 数据库会话
        :param run_id: 工作流运行ID
        :return: WorkflowStep 列表
    """
    return (
        db.query(WorkflowStep)
        .filter(WorkflowStep.run_id == run_id)
        .order_by(WorkflowStep.id.asc())
        .all()
    )



# 取最终预审日志
def _review_log(db: Session, run: WorkflowRun) -> AIReviewLog | None:
    """
    取该 run 关联的预审日志
        :param db: 数据库会话
        :param run: 工作流运行
        :return: AIReviewLog；没有则 None
    """
    # 1.没有关联日志：说明这次 run 没有产生最终结论（失败/取消）
    if not run.review_log_id:
        return None

    # 2.按 ID 取日志；日志被清理时返回 None
    return db.query(AIReviewLog).filter(AIReviewLog.id == run.review_log_id).first()



# 取凭证识别结论（每条附件取最新一条）
def _documents(db: Session, expense_id: int) -> list[DocumentExtraction]:
    """
    取该单据下每条附件最近一次识别记录
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: DocumentExtraction 列表（按 ID 倒序，每条附件一条）
    """
    # 1.联表取出该单据的全部识别记录，按新到旧排列
    rows = (
        db.query(DocumentExtraction)
        .join(Attachment, Attachment.id == DocumentExtraction.attachment_id)
        .join(ExpenseItem, ExpenseItem.id == Attachment.expense_item_id)
        .filter(
            ExpenseItem.expense_id == expense_id,
            Attachment.is_deleted.is_(False),
        )
        .order_by(DocumentExtraction.id.desc())
        .limit(MAX_DOCUMENTS * 5)
        .all()
    )

    # 2.按附件去重，保留每个附件最新的一条
    latest: list[DocumentExtraction] = []
    seen: set[int] = set()
    for row in rows:
        if row.attachment_id in seen:
            continue
        seen.add(row.attachment_id)
        latest.append(row)
        if len(latest) >= MAX_DOCUMENTS:
            break
    return latest



# 组装轨迹
def build_workflow_trace(db: Session, expense_id: int) -> dict | None:
    """
    组装一张单据最近一次 AI 预审的轨迹
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :return: 轨迹字典；没有 run 时返回 None
    """
    # 1.取最近一次 run
    run = _latest_run(db, expense_id)
    if run is None:
        return None

    # 2.节点记录
    steps = _steps(db, run.id)

    # 3.最终预审日志（决策与引用以它为准）
    log = _review_log(db, run)

    # 4.凭证识别结论
    documents = _documents(db, expense_id)

    # 5.风险结论：优先用日志，失败 run 退回 run 的状态摘要
    state = run.state_json or {}
    risk_level = log.risk_level if log is not None else state.get("risk_level")
    risk_score = log.risk_score if log is not None else state.get("risk_score")

    # 6.组装响应（字段名与 Pydantic 模型一致）
    return {
        "expense_id": expense_id,                                   # 报销单ID
        "run_id": int(run.id),                                      # 运行ID
        "review_id": int(run.review_log_id) if run.review_log_id else None,   # 日志ID
        "status": run.status.value if hasattr(run.status, "value") else str(run.status),   # 运行状态
        "engine": run.engine or "",                                 # 编排引擎
        "current_node": run.current_node,                           # 当前节点
        "model": run.model or "",                                   # 模型名
        "prompt_version": run.prompt_version or "",                 # Prompt 版本
        "policy_version": run.policy_version or "",                 # 制度版本
        "decision": (log.decision if log is not None else run.decision) or "",        # 建议决策
        "decision_reason": (log.decision_reason if log is not None else run.decision_reason) or "",   # 决策说明
        "risk_level": risk_level,                                   # 风险等级
        "risk_score": risk_score,                                   # 风险分数
        "is_fallback": bool(log.is_fallback) if log is not None else bool(state.get("is_fallback")),   # 是否兜底
        "prompt_tokens": int(run.prompt_tokens or 0),               # 输入 token 合计
        "completion_tokens": int(run.completion_tokens or 0),       # 输出 token 合计
        "cost": run.cost,                                           # 费用合计
        "started_at": run.started_at,                               # 开始时间
        "finished_at": run.finished_at,                             # 结束时间
        "error_message": run.error_message,                         # 失败原因
        "state": state,                                             # 脱敏状态摘要
        "steps": [
            {
                "node": step.node,                                  # 节点名
                "attempt": int(step.attempt or 1),                  # 尝试序号
                "status": step.status.value if hasattr(step.status, "value") else str(step.status),   # 节点状态
                "latency_ms": step.latency_ms,                      # 耗时
                "prompt_tokens": int(step.prompt_tokens or 0),      # 输入 token
                "completion_tokens": int(step.completion_tokens or 0),   # 输出 token
                "cost": step.cost,                                  # 费用
                "summary": step.summary or {},                      # 脱敏摘要
                "error_message": step.error_message,                # 失败原因
            }
            for step in steps
        ],
        "citations": list(log.policy_citations or []) if log is not None else [],   # 制度引用
        "documents": [
            {
                "attachment_id": int(item.attachment_id),          # 附件ID
                "expense_item_id": int(item.expense_item_id),      # 明细ID
                "status": item.status.value if hasattr(item.status, "value") else str(item.status),   # 识别状态
                "document_type": item.document_type,               # 凭证类型
                "evidence_level": item.evidence_level,             # 证据强度
                "amount": item.amount,                             # 票面金额
                "document_date": item.document_date,               # 票面日期
                "counterparty": item.counterparty,                 # 商户/收款方
                "identifier": item.identifier,                     # 发票号/订单号
                "confidence": item.confidence,                     # 置信度
                "needs_manual_review": bool(item.needs_manual_review),   # 是否建议人工核对
                "error_message": item.error_message,               # 失败原因
            }
            for item in documents
        ],
    }
