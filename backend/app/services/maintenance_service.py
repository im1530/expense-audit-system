"""
运维任务审计服务（maintenance_runs）
    职责：
        - begin_run：任务开始时落一条 running 记录，进程崩溃也能看出"没跑完"；
        - finish_run：任务结束时写状态、汇总与错误原因。
    边界：
        - 只写 maintenance_runs，不碰业务表；
        - summary 只放数量与参数，调用方负责不传敏感内容。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import MaintenanceRun, MaintenanceStatus


logger = logging.getLogger(__name__)



# 开始一次运维任务
def begin_run(db: Session, job: str) -> MaintenanceRun:
    """
    创建一条 running 的运维任务记录
        :param db: 数据库会话
        :param job: 任务名（如 retention_purge）
        :return: 已持久化的记录
    """
    # 1.组装记录
    run = MaintenanceRun(
        job=job,
        status=MaintenanceStatus.RUNNING,
        started_at=datetime.now(),
    )

    # 2.落库并刷新，确保拿到自增 ID
    db.add(run)
    db.commit()
    db.refresh(run)
    return run



# 结束一次运维任务
def finish_run(
    db: Session,
    run: MaintenanceRun,
    *,
    status: MaintenanceStatus,
    summary: dict | None = None,
    error: str | None = None,
) -> MaintenanceRun:
    """
    写入运维任务的最终状态
        :param db: 数据库会话
        :param run: begin_run 返回的记录
        :param status: 最终状态
        :param summary: 执行汇总（数量与参数）
        :param error: 失败原因
        :return: 更新后的记录
    """
    # 1.更新字段
    run.status = status
    run.finished_at = datetime.now()
    run.summary = summary or {}
    run.error_message = (error or "")[:500] or None

    # 2.提交并返回
    db.commit()
    db.refresh(run)
    return run
