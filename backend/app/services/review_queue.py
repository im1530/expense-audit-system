"""
AI 预审任务投递服务（Phase F）
    职责：
        1. enqueue_review：把预审任务投递到 Celery；
        2. dispatch_review：统一投递入口，队列不可用时回退 FastAPI BackgroundTasks；
        3. trigger_auto_review：提交/重提后自动创建任务并投递，失败不影响主流程。
    降级原则：
        - Redis 不可用、Celery 投递异常时只记录日志，不阻断报销单提交与审批；
        - 任务会保持 pending，可由后续恢复任务或人工重新触发；
        - 自动触发失败不抛异常，调用方按"预审未执行"处理。
"""
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIReviewLog, AIReviewStatus
from app.services.ai_review_service import ReviewError, create_review, execute_review
from app.tasks.review_tasks import execute_review_task



logger = logging.getLogger(__name__)



# 投递到 Celery
def enqueue_review(review_id: int) -> bool:
    """
    把一条预审任务投递到 Celery 队列
        :param review_id: 预审日志ID
        :return: 投递成功 True；队列关闭或投递失败 False
    """
    # 1.队列开关关闭：交给调用方决定是否回退 BackgroundTasks
    if not settings.AI_REVIEW_QUEUE_ENABLED:
        return False

    # 2.投递任务；task_id 固定，便于日志检索与重复投递排查
    try:
        execute_review_task.apply_async(
            args=[review_id],                               # 任务参数：预审日志ID
            task_id=f"ai-review-{review_id}",               # 确定性任务ID，便于追踪
        )
        return True
    except Exception:
        # 3.Redis/Celery 不可用：记录完整堆栈，由上层决定降级
        logger.exception("AI 预审任务投递失败 review_id=%s", review_id)
        return False



# 统一投递入口
def dispatch_review(review_id: int, background_tasks=None) -> bool:
    """
    投递预审任务：优先 Celery，失败时回退 BackgroundTasks
        :param review_id: 预审日志ID
        :param background_tasks: FastAPI BackgroundTasks；为 None 时无法回退
        :return: 是否已安排执行
    """
    # 1.优先 Celery：可跨进程、可重试、可多实例消费
    if settings.AI_REVIEW_QUEUE_ENABLED and enqueue_review(review_id):
        return True

    # 2.队列不可用：回退到当前进程后台任务，保证接口仍能返回 202
    if background_tasks is not None:
        background_tasks.add_task(execute_review, review_id)
        logger.warning("Celery 队列不可用，回退 BackgroundTasks review_id=%s", review_id)
        return True

    # 3.既没有队列也没有后台任务：任务保持 pending，等待恢复
    logger.error("AI 预审任务未能安排执行 review_id=%s", review_id)
    return False



# 安全回滚：自动触发失败时不能反过来影响提交主流程
def _safe_rollback(db: Session) -> None:
    """
    尝试回滚当前会话，吞掉回滚本身的异常
        :param db: 数据库会话
    """
    try:
        db.rollback()
    except Exception:
        logger.exception("自动预审失败后回滚异常")



# 提交后自动触发
def trigger_auto_review(
    db: Session,
    *,
    expense_id: int,
    user_id: int | None,
) -> AIReviewLog | None:
    """
    报销单提交/重提后自动创建并投递预审任务
        任何异常都只记日志，不向上抛，避免影响提交主流程。
        :param db: 数据库会话
        :param expense_id: 报销单ID
        :param user_id: 触发人ID
        :return: 预审日志；未触发或失败时 None
    """
    # 1.开关校验：AI 预审关闭或自动触发关闭时直接跳过
    if not settings.AI_REVIEW_ENABLED or not settings.AI_REVIEW_AUTO_TRIGGER:
        return None

    # 2.创建任务；新提交视为新业务事件，force=True 不复用旧缓存，进行中的任务仍会复用
    try:
        log = create_review(
            db,
            expense_id=expense_id,
            triggered_by=user_id,
            trigger_source="submit",
            force=True,
        )
    except ReviewError as exc:
        # 3.业务校验失败（如状态已变）：回滚未提交的锁事务，只提示，不影响提交结果
        _safe_rollback(db)
        logger.warning("提交后自动预审未创建 expense_id=%s reason=%s", expense_id, exc.message)
        return None
    except Exception:
        # 4.未预期异常：同样回滚，避免把行锁带出本次请求
        _safe_rollback(db)
        logger.exception("提交后自动预审创建异常 expense_id=%s", expense_id)
        return None

    # 5.只有新建的 pending 任务才需要投递；缓存命中/已有结果直接复用
    if log.status == AIReviewStatus.PENDING:
        scheduled = enqueue_review(log.id)
        logger.info(
            "提交后自动预审已创建 expense_id=%s review_id=%s scheduled=%s",
            expense_id, log.id, scheduled,
        )
        if not scheduled:
            logger.error(
                "提交后自动预审投递失败，任务保持 pending 等待恢复 expense_id=%s review_id=%s",
                expense_id, log.id,
            )

    return log
