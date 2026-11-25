"""
Agent 核查记录模型（agent_investigations）
    一次「AI 补充核查」对应一条记录：
        - report：Agent 最终给审批人看的核查结论；
        - trace：工具调用轨迹（只读工具名、参数、结果摘要、耗时）；
        - 记录模型/token/延迟/失败原因，便于审计与成本统计。
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import relationship

from app.models.base import Base


class AgentInvestigationStatus(str):
    """核查状态常量（避免引入新的 MySQL ENUM）"""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class AgentInvestigation(Base):
    """AI 核查记录"""

    __tablename__ = "agent_investigations"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="核查记录ID")
    expense_id = Column(
        BigInteger,
        ForeignKey("expenses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="报销单ID",
    )
    created_by = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="触发人ID",
    )
    status = Column(String(20), nullable=False, default=AgentInvestigationStatus.RUNNING, comment="running/success/failed")
    question = Column(String(500), nullable=True, comment="核查问题")
    report = Column(Text, nullable=True, comment="核查结论")
    trace = Column(JSON, nullable=True, comment="工具调用轨迹")
    model = Column(String(100), nullable=True, comment="模型名")
    prompt_version = Column(String(50), nullable=True, comment="Prompt版本")
    latency_ms = Column(Integer, nullable=True, comment="耗时毫秒")
    prompt_tokens = Column(Integer, nullable=True, comment="输入token")
    completion_tokens = Column(Integer, nullable=True, comment="输出token")
    error_message = Column(String(500), nullable=True, comment="失败原因")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="创建时间")

    # 核查记录只写不改，移除基类自动继承的 updated_at
    updated_at = None

    expense = relationship("Expense", backref="agent_investigations")
    creator = relationship("User", foreign_keys=[created_by])

    def __repr__(self) -> str:
        return f"<AgentInvestigation(id={self.id},expense_id={self.expense_id},status={self.status})>"
