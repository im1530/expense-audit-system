"""
僵尸工作流收尾服务（Phase D 运维）
    背景：
        - worker 崩溃、进程被重启、节点卡死时，workflow_runs 会永远停在 running；
        - 单据的 AI 预审日志也会停在 running，前端一直显示"预审中"；
        - 重新触发预审时虽然有 _reap_stale_tasks 兜底，但那是按单据触发的，不会全局清理。
    策略（为什么不自动 resume）：
        - resume_review_workflow 只重跑图，不会把最终结果回写 AIReviewLog，
          自动 resume 会留下"run 成功但日志还是 running"的不一致；
        - 因此这里只做"收尾"：把超时 run 与仍在 running 的关联日志标记 failed，
          需要重算时由用户重新触发或 requeue_reviews.py 补投，避免无人值守时产生 LLM 费用。
    边界：
        - 只读判断 + 状态收尾，不调用模型、不发请求；
        - 每处理一条单独提交，单条失败不影响其它记录；
        - 幂等：已 failed/success 的 run 不会被再次处理。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AIReviewLog, AIReviewStatus, WorkflowRun, WorkflowRunStatus


logger = logging.getLogger(__name__)

# 收尾原因文案：同时写入 run 与日志的 error_message
STALE_ERROR = "工作流超时未完成（自动收尾），需重新触发预审"



# 计算超时时间点
def _cutoff(stale_minutes: int) -> datetime:
    """
    计算"超过多久算僵尸"的时间点
        :param stale_minutes: 超时分钟数
        :return: 截止时间
    """
    return datetime.now() - timedelta(minutes=max(1, int(stale_minutes)))



# 查询僵尸 run
def find_stale_runs(db: Session, *, stale_minutes: int | None = None, limit: int = 100) -> list[WorkflowRun]:
    """
    查询卡在 running 且长时间没有进展的工作流
        :param db: 数据库会话
        :param stale_minutes: 超时分钟数；为空时读配置
        :param limit: 单次最多处理多少条
        :return: WorkflowRun 列表（按 ID 升序）
    """
    # 1.超时阈值：默认取配置
    minutes = settings.WORKFLOW_STALE_MINUTES if stale_minutes is None else stale_minutes
    cutoff = _cutoff(minutes)

    # 2.只挑 running，且创建时间与更新时间都早于截止点（更新时间每写一个 step 会刷新）
    return (
        db.query(WorkflowRun)
        .filter(
            WorkflowRun.status == WorkflowRunStatus.RUNNING,
            WorkflowRun.created_at < cutoff,
            or_(WorkflowRun.updated_at.is_(None), WorkflowRun.updated_at < cutoff),
        )
        .order_by(WorkflowRun.id.asc())
        .limit(max(1, int(limit)))
        .all()
    )



# 收尾僵尸 run
def recover_stale_runs(
    db: Session,
    *,
    stale_minutes: int | None = None,
    limit: int = 100,
    dry_run: bool = False,
) -> dict:
    """
    把僵尸 run 及其仍在 running 的预审日志标记为 failed
        :param db: 数据库会话
        :param stale_minutes: 超时分钟数；为空时读配置
        :param limit: 单次最多处理多少条
        :param dry_run: 只统计不改状态
        :return: 汇总字典：{"stale_minutes","scanned","recovered","dry_run","run_ids","errors"}
    """
    # 1.查出待处理的 run
    minutes = settings.WORKFLOW_STALE_MINUTES if stale_minutes is None else stale_minutes
    runs = find_stale_runs(db, stale_minutes=minutes, limit=limit)
    summary = {
        "stale_minutes": int(minutes),     # 使用的超时阈值
        "scanned": len(runs),              # 命中的僵尸 run 数
        "recovered": 0,                    # 实际收尾数
        "dry_run": bool(dry_run),          # 是否预演
        "run_ids": [],                     # 收尾的 run ID
        "errors": [],                      # 单条失败原因
    }

    # 2.逐条收尾：单条失败不影响其它记录
    for run in runs:
        if dry_run:
            summary["run_ids"].append(int(run.id))
            continue
        try:
            _close_run(db, run)
            summary["recovered"] += 1
            summary["run_ids"].append(int(run.id))
        except Exception as exc:                       # noqa: BLE001 - 脚本需要汇总所有失败
            db.rollback()
            logger.exception("僵尸工作流收尾失败 run_id=%s", run.id)
            summary["errors"].append(f"run_id={run.id}: {type(exc).__name__}: {exc}")

    # 3.返回汇总
    logger.info(
        "僵尸工作流扫描完成 stale_minutes=%s scanned=%s recovered=%s dry_run=%s",
        minutes, summary["scanned"], summary["recovered"], dry_run,
    )
    return summary



# 收尾单条 run
def _close_run(db: Session, run: WorkflowRun) -> None:
    """
    把一条 run 与它关联的预审日志收尾为 failed
        :param db: 数据库会话
        :param run: 工作流运行
        :return: 无
    """
    # 1.标记 run 失败并写结束时间
    run.status = WorkflowRunStatus.FAILED
    run.error_message = STALE_ERROR[:500]
    run.finished_at = datetime.now()

    # 2.关联日志仍在 running/pending 时一并收尾，避免前端永远显示"预审中"
    if run.review_log_id:
        log = db.query(AIReviewLog).filter(AIReviewLog.id == run.review_log_id).first()
        if log is not None and log.status in (AIReviewStatus.PENDING, AIReviewStatus.RUNNING):
            log.status = AIReviewStatus.FAILED
            log.error_message = STALE_ERROR[:500]

    # 3.单条提交，保证后续记录不受本次失败影响
    db.commit()
