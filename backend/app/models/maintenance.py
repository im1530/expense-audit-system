"""
运维任务审计模型（maintenance_runs 表）
    为什么需要：
        - 保留期清理、僵尸 run 收尾这类定时任务通常无人值守，出问题没人看到；
        - 每次执行落一行，能回答"昨晚跑了吗、处理了多少、失败原因是什么"；
        - 与业务表分开，避免把运维记录混进审批/预审审计。
    约定：
        - 一行 = 一次执行（job + started_at 唯一标识）；
        - summary 只放数量与参数，不放文件名、票面内容等敏感信息。
"""
import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    JSON,
    String,
)
from sqlalchemy.orm import relationship  # noqa: F401  （保持与其它模型一致的导入习惯）

from app.models.base import Base



# 运维任务状态
class MaintenanceStatus(str, enum.Enum):
    """
    一次运维任务的执行状态
        - RUNNING：执行中（进程崩溃会停在 RUNNING，可据此发现异常）；
        - SUCCESS：成功；
        - FAILED：失败；
        - SKIPPED：跳过（如锁被占用、开关关闭）。
    """
    RUNNING = "running"       # 执行中
    SUCCESS = "success"       # 成功
    FAILED = "failed"         # 失败
    SKIPPED = "skipped"       # 已跳过



# 运维任务执行记录
class MaintenanceRun(Base):
    """
    运维任务执行记录
        字段：
            - job：任务名（如 retention_purge / workflow_recover）；
            - status：执行状态；
            - started_at / finished_at：起止时间；
            - summary：执行汇总（数量、参数），JSON；
            - error_message：失败原因。
    """
    __tablename__ = "maintenance_runs"      # 表名：运维任务记录

    # ===== 主键 =====
    id = Column(
        BigInteger,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="运维任务记录ID",
    )

    # ===== 任务信息 =====
    job = Column(
        String(50),
        nullable=False,
        index=True,
        comment="任务名：retention_purge / workflow_recover 等",
    )
    status = Column(
        Enum(
            MaintenanceStatus,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=False,
            name="maintenance_status",
        ),
        nullable=False,
        default=MaintenanceStatus.RUNNING,
        index=True,
        comment="执行状态：running/success/failed/skipped",
    )

    # ===== 时间 =====
    started_at = Column(DateTime(timezone=True), nullable=False, comment="开始时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="结束时间")

    # ===== 结果 =====
    summary = Column(JSON, nullable=True, comment="执行汇总（数量与参数，不含敏感内容）")
    error_message = Column(String(500), nullable=True, comment="失败原因")

    def __repr__(self) -> str:
        """
        调试打印：只显示定位信息
            :return: 形如 <MaintenanceRun(id=1,job=retention_purge,status=success)>
        """
        return f"<MaintenanceRun(id={self.id},job={self.job},status={self.status})>"
