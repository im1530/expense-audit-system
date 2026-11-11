"""
保留期清理服务（Phase F：隐私与留存）
    清理范围：
        1. 超过保留期的发票/凭证：删除磁盘文件、识别记录与附件元数据；
        2. 超过保留期的 raw_response：置空，保留结构化结论与审计字段；
        3. 超过保留期的工作流检查点：按 thread_id 删除，保留 workflow_runs/steps 审计。
    设计原则：
        - dry_run 只统计，不删不改；
        - 逐条提交，单条失败不影响其它数据；
        - 只删文件/影像与原文，不删除报销单、审批记录等业务主数据；
        - 日志里只打 ID 与数量，不打文件名、票面内容。
    边界：
        - 本模块只做清理，不负责定时调度（调度见 scripts/run_retention_job.py）；
        - 不写 maintenance_runs，审计由调用方（运维脚本）负责。
"""
import logging
from datetime import datetime, timedelta

from app.config import settings
from app.models import (
    AIReviewLog,
    Attachment,
    DocumentExtraction,
    WorkflowRun,
)
from app.services.storage_service import get_storage
from app.workflow.checkpoint import MySQLCheckpointSaver


logger = logging.getLogger(__name__)



# 清理过期附件
def purge_attachments(db, *, cutoff: datetime, limit: int, dry_run: bool) -> int:
    """
    删除过期附件文件、识别记录与附件元数据
        :param db: 数据库会话
        :param cutoff: 早于该时间的附件视为过期
        :param limit: 单次最多处理条数
        :param dry_run: 是否只统计
        :return: 处理的附件数
    """
    # 1.取出过期附件
    rows = (
        db.query(Attachment)
        .filter(Attachment.created_at < cutoff)
        .order_by(Attachment.id.asc())
        .limit(max(1, limit))
        .all()
    )
    if dry_run:
        return len(rows)

    # 2.逐条删除：文件 → 识别记录 → 附件元数据
    storage = get_storage()                                     # 存储实现
    handled = 0                                                 # 成功处理数
    for attachment in rows:
        try:
            storage.delete(attachment.file_path)                # 删除磁盘文件（不存在则忽略）
            db.query(DocumentExtraction).filter(
                DocumentExtraction.attachment_id == attachment.id
            ).delete(synchronize_session=False)                 # 清理识别记录
            db.delete(attachment)                               # 清理附件元数据
            db.commit()
            handled += 1
        except Exception:
            db.rollback()
            logger.warning("附件清理失败 attachment_id=%s", attachment.id, exc_info=True)
    return handled



# 清理 raw_response
def purge_raw_responses(db, *, cutoff: datetime, limit: int, dry_run: bool) -> int:
    """
    把过期记录的 raw_response 置空
        :param db: 数据库会话
        :param cutoff: 早于该时间的原始返回视为过期
        :param limit: 单次最多处理条数
        :param dry_run: 是否只统计
        :return: 处理的记录数
    """
    # 1.统计待处理 ID（dry_run 与正常都先查一遍）
    review_ids = [
        row[0]
        for row in db.query(AIReviewLog.id)
        .filter(AIReviewLog.created_at < cutoff, AIReviewLog.raw_response.isnot(None))
        .order_by(AIReviewLog.id.asc())
        .limit(max(1, limit))
        .all()
    ]
    document_ids = [
        row[0]
        for row in db.query(DocumentExtraction.id)
        .filter(DocumentExtraction.created_at < cutoff, DocumentExtraction.raw_response.isnot(None))
        .order_by(DocumentExtraction.id.asc())
        .limit(max(1, limit))
        .all()
    ]
    if dry_run:
        return len(review_ids) + len(document_ids)

    # 2.批量置空并提交
    if review_ids:
        db.query(AIReviewLog).filter(AIReviewLog.id.in_(review_ids)).update(
            {AIReviewLog.raw_response: None},
            synchronize_session=False,
        )
    if document_ids:
        db.query(DocumentExtraction).filter(DocumentExtraction.id.in_(document_ids)).update(
            {DocumentExtraction.raw_response: None},
            synchronize_session=False,
        )
    db.commit()
    return len(review_ids) + len(document_ids)



# 清理过期检查点
def purge_checkpoints(db, *, cutoff: datetime, limit: int, dry_run: bool) -> int:
    """
    删除已结束且超过保留期的 run 对应的检查点
        :param db: 数据库会话
        :param cutoff: 早于该时间结束的 run 视为过期
        :param limit: 单次最多处理条数
        :param dry_run: 是否只统计
        :return: 处理的 run 数
    """
    # 1.取出已结束且过期的 run
    run_ids = [
        row[0]
        for row in db.query(WorkflowRun.id)
        .filter(WorkflowRun.finished_at.isnot(None), WorkflowRun.finished_at < cutoff)
        .order_by(WorkflowRun.id.asc())
        .limit(max(1, limit))
        .all()
    ]
    if dry_run:
        return len(run_ids)

    # 2.逐条删除检查点；失败只记日志，不影响其它 run
    saver = MySQLCheckpointSaver()                              # 检查点存储
    handled = 0                                                 # 成功处理数
    for run_id in run_ids:
        try:
            saver.delete_thread(str(run_id))                    # 按 thread_id 删除检查点
            handled += 1
        except Exception:
            logger.warning("检查点清理失败 run_id=%s", run_id, exc_info=True)
    return handled



# 执行一次完整清理
def run_retention(
    db,
    *,
    dry_run: bool = False,
    days: int | None = None,
    raw_days: int | None = None,
    limit: int = 500,
) -> dict:
    """
    跑一次保留期清理，返回汇总
        :param db: 数据库会话
        :param dry_run: 是否只统计
        :param days: 附件/检查点保留天数；为空读配置
        :param raw_days: raw_response 保留天数；为空读配置
        :param limit: 单类数据单次最多处理条数
        :return: 汇总字典（参数 + 三类处理数量）
    """
    # 1.计算保留期与截止时间
    now = datetime.now()                                        # 当前时间
    days = settings.DATA_RETENTION_DAYS if days is None else days
    raw_days = settings.RAW_RESPONSE_RETENTION_DAYS if raw_days is None else raw_days
    cutoff = now - timedelta(days=max(1, int(days)))            # 附件/检查点截止
    raw_cutoff = now - timedelta(days=max(1, int(raw_days)))    # raw_response 截止

    # 2.依次执行三类清理
    attachments = purge_attachments(db, cutoff=cutoff, limit=limit, dry_run=dry_run)
    raw_responses = purge_raw_responses(db, cutoff=raw_cutoff, limit=limit, dry_run=dry_run)
    checkpoints = purge_checkpoints(db, cutoff=cutoff, limit=limit, dry_run=dry_run)

    # 3.汇总（参数 + 数量，不含敏感内容）
    return {
        "dry_run": bool(dry_run),                               # 是否预演
        "days": int(days),                                      # 附件/检查点保留天数
        "raw_days": int(raw_days),                              # raw_response 保留天数
        "limit": int(limit),                                    # 单类批量上限
        "attachments": attachments,                             # 处理的附件数
        "raw_responses": raw_responses,                         # 置空的原文数
        "checkpoints": checkpoints,                             # 清理的检查点线程数
    }
