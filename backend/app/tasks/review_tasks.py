"""
AI 预审异步任务（Phase F）
    职责：
        1. 从 Celery 队列取出 review_id，调用 service 执行预审；
        2. service 返回后任务结束；service 内部已处理 LLM 失败降级，不会把可预期错误抛给队列；
        3. 只有数据库连接、进程崩溃等未预期异常才触发 Celery 重试；
        4. 重试耗尽后把仍为 pending/running 的记录标记为 failed，避免永远卡住。
    幂等：
        - execute_review 内部用 UPDATE ... WHERE status='pending' 原子领取；
        - 同一 review_id 被重复投递时，只有一个任务能领取成功，其余直接返回 None。
"""
import logging

from app.config import settings
from app.services.ai_review_service import execute_review, mark_review_failed
from app.worker import celery_app



logger = logging.getLogger(__name__)



# 预审执行任务
@celery_app.task(
    bind=True,                                              # 绑定 self，用于读取重试次数
    name="ai_review.execute",                               # 任务名：跨进程/监控使用
    max_retries=settings.AI_REVIEW_TASK_MAX_RETRIES,        # 最大重试次数（不含首次）
    acks_late=True,                                         # 执行完成后确认，worker 崩溃可重投
)
def execute_review_task(self, review_id: int) -> None:
    """
    Celery 入口：执行一条 AI 预审任务
        :param self: Celery 绑定任务对象（提供 retry/request）
        :param review_id: 预审日志ID
        :return: 无返回值；业务结果写 MySQL
    """
    # 1.执行预审；service 内部负责状态流转、LLM 降级与审计落库
    try:
        log = execute_review(review_id)
    except Exception as exc:
        # 2.未预期异常：记录日志，按重试策略重新入队
        retries = int(self.request.retries)                 # 已重试次数
        if retries < int(self.max_retries):
            countdown = min(60, 5 * (2 ** retries))         # 指数退避：5s、10s、20s...
            logger.warning(
                "AI 预审任务异常，准备第 %s 次重试 review_id=%s countdown=%ss error=%s",
                retries + 1, review_id, countdown, exc,
            )
            raise self.retry(exc=exc, countdown=countdown)

        # 3.重试耗尽：兜底标记失败，保证不会永久卡在 running
        logger.exception("AI 预审任务重试耗尽 review_id=%s", review_id)
        try:
            mark_review_failed(review_id, f"队列任务重试耗尽：{exc}")
        except Exception:
            logger.exception("预审任务失败状态回写失败 review_id=%s", review_id)
        raise

    # 4.正常结束：service 返回 None 表示任务已被其他 worker 领取或不存在，属于幂等分支
    if log is None:
        logger.info("AI 预审任务未执行（可能已被领取或不存在）review_id=%s", review_id)
        return

    logger.info(
        "AI 预审任务完成 review_id=%s expense_id=%s status=%s",
        review_id, log.expense_id, log.status,
    )
