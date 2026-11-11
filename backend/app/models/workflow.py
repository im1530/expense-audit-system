"""
AI 预审工作流审计模型（Phase D）
    为什么单独建表：
        - `ai_review_logs` 只保存最终结论，无法回答"卡在哪个节点、重试了几次、每个节点耗时多少"；
        - 工作流需要断点恢复与排障，必须有 run 级和 step 级记录；
        - 决策建议（fast_track/manual_review/suggest_reject）需要单独审计，不能只藏在 JSON 里。
    关系：
        - WorkflowRun N:1 Expense，N:1 AIReviewLog（可空，日志删除后保留审计）；
        - WorkflowStep N:1 WorkflowRun，节点完成/失败各写一条。
"""
import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.models.base import Base



# 工作流 run 状态
class WorkflowRunStatus(str, enum.Enum):
    """
    一次预审工作流的状态
        - RUNNING：执行中；
        - SUCCESS：全部节点完成；
        - FAILED：节点异常，未产生最终结论；
        - CANCELLED：人工取消/超时终止。
    """
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 已取消



# 工作流节点状态
class WorkflowStepStatus(str, enum.Enum):
    """
    单个节点的状态
        - RUNNING：开始执行；
        - SUCCESS：成功；
        - FALLBACK：节点内降级（如 LLM 失败走规则兜底）；
        - FAILED：失败。
    """
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FALLBACK = "fallback"     # 已降级
    FAILED = "failed"         # 失败



# 工作流 run
class WorkflowRun(Base):
    """
    一次 AI 预审工作流
        字段：
            - 归属：expense_id、review_log_id；
            - 执行：status、current_node、engine、prompt/model/policy 版本；
            - 状态：state_json（脱敏摘要，不存原始票据/证件）；
            - 决策：decision、decision_reason；
            - 时间：started_at、finished_at。
    """
    __tablename__ = "workflow_runs"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True, comment="工作流运行ID")
    expense_id = Column(
        BigInteger,
        ForeignKey("expenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="报销单ID",
    )
    review_log_id = Column(
        BigInteger,
        ForeignKey("ai_review_logs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="最终预审日志ID",
    )
    status = Column(
        Enum(
            WorkflowRunStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            name="workflow_run_status",
        ),
        nullable=False,
        default=WorkflowRunStatus.RUNNING,
        index=True,
        comment="运行状态：running/success/failed/cancelled",
    )
    current_node = Column(String(50), nullable=True, comment="当前/最后执行节点")
    engine = Column(String(30), nullable=False, default="langgraph", comment="编排引擎")
    prompt_version = Column(String(50), nullable=False, default="", comment="Prompt版本")
    model = Column(String(100), nullable=False, default="", comment="模型名")
    policy_version = Column(String(50), nullable=False, default="", comment="制度版本")
    prompt_tokens = Column(Integer, nullable=True, default=0, comment="全流程输入 token 合计")
    completion_tokens = Column(Integer, nullable=True, default=0, comment="全流程输出 token 合计")
    cost = Column(Numeric(12, 6), nullable=True, default=0, comment="全流程估算费用（元）")
    state_json = Column(JSON, nullable=True, comment="脱敏状态摘要")
    decision = Column(String(30), nullable=True, comment="建议决策：fast_track/manual_review/suggest_reject")
    decision_reason = Column(String(500), nullable=True, comment="决策说明")
    error_message = Column(String(500), nullable=True, comment="失败原因")
    started_at = Column(DateTime(timezone=True), nullable=True, comment="开始时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="结束时间")

    expense = relationship("Expense")             # 所属报销单
    steps = relationship(
        "WorkflowStep",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.id",
    )

    def __repr__(self) -> str:
        """
        调试打印：只显示定位信息
            :return: 形如 <WorkflowRun(id=1,expense_id=2,status=success)>
        """
        return f"<WorkflowRun(id={self.id},expense_id={self.expense_id},status={self.status})>"



# 工作流节点记录
class WorkflowStep(Base):
    """
    单个工作流节点的执行记录
        字段：
            - run_id、node、attempt；
            - status、latency_ms、summary（脱敏摘要）；
            - error_message。
    """
    __tablename__ = "workflow_steps"

    id = Column(BigInteger, primary_key=True, index=True, autoincrement=True, comment="节点记录ID")
    run_id = Column(
        BigInteger,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="工作流运行ID",
    )
    node = Column(String(50), nullable=False, index=True, comment="节点名")
    attempt = Column(Integer, nullable=False, default=1, comment="第几次尝试")
    status = Column(
        Enum(
            WorkflowStepStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            name="workflow_step_status",
        ),
        nullable=False,
        default=WorkflowStepStatus.RUNNING,
        index=True,
        comment="节点状态",
    )
    latency_ms = Column(Integer, nullable=True, comment="节点耗时（毫秒）")
    prompt_tokens = Column(Integer, nullable=True, default=0, comment="本节点输入 token")
    completion_tokens = Column(Integer, nullable=True, default=0, comment="本节点输出 token")
    cost = Column(Numeric(12, 6), nullable=True, default=0, comment="本节点估算费用（元）")
    summary = Column(JSON, nullable=True, comment="脱敏输出摘要")
    error_message = Column(String(500), nullable=True, comment="失败原因")

    run = relationship("WorkflowRun", back_populates="steps")

    def __repr__(self) -> str:
        """
        调试打印
            :return: 形如 <WorkflowStep(id=1,node=risk,status=success)>
        """
        return f"<WorkflowStep(id={self.id},node={self.node},status={self.status})>"
