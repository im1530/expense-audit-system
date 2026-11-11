"""
补投待执行的 AI 预审任务（Phase F 运维脚本）
    适用场景：
        - 提交时 Redis/Celery 不可用，任务停留在 pending；
        - worker 长时间停机后恢复，需要把积压任务重新投递。
    幂等保证：
        - 重复投递同一 review_id 不会重复执行，execute_review 会用原子 UPDATE 领取；
        - 只补投超过 min-age-minutes 的任务，避免和正常入队中的任务抢跑。
    用法（在 backend 目录下）：
        python scripts/requeue_reviews.py                      # 补投 1 分钟前的 pending，最多 100 条
        python scripts/requeue_reviews.py --limit 500           # 扩大批量
        python scripts/requeue_reviews.py --min-age-minutes 10  # 只补投 10 分钟前的
        python scripts/requeue_reviews.py --dry-run             # 只统计，不投递
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dependencies import SessionLocal                       # noqa: E402
from app.models import AIReviewLog, AIReviewStatus              # noqa: E402
from app.services.review_queue import enqueue_review            # noqa: E402



# 解析命令行参数
def parse_args() -> argparse.Namespace:
    """
    解析脚本参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="补投 pending 状态的 AI 预审任务")
    parser.add_argument("--limit", type=int, default=100, help="单次最多补投多少条")          # 批量上限
    parser.add_argument("--min-age-minutes", type=int, default=1, help="只补投创建超过多少分钟的任务")   # 最小年龄
    parser.add_argument("--dry-run", action="store_true", help="只统计，不真正投递")           # 预演开关
    return parser.parse_args()



# 查询待补投任务
def select_pending(db, *, limit: int, min_age_minutes: int) -> list[AIReviewLog]:
    """
    查询需要补投的 pending 任务
        :param db: 数据库会话
        :param limit: 批量上限
        :param min_age_minutes: 最小创建时长（分钟）
        :return: 预审日志列表
    """
    cutoff = datetime.now() - timedelta(minutes=max(0, min_age_minutes))   # 时间下限
    return (
        db.query(AIReviewLog)
        .filter(
            AIReviewLog.status == AIReviewStatus.PENDING,                  # 只补投待执行
            AIReviewLog.created_at < cutoff,                               # 避免抢跑刚创建的任务
        )
        .order_by(AIReviewLog.id.asc())                                    # 先创建先补投
        .limit(max(1, limit))                                              # 控制单次批量
        .all()
    )



# 主流程
def main() -> int:
    """
    脚本入口
        :return: 进程退出码：0 成功，1 有投递失败
    """
    args = parse_args()
    db = SessionLocal()
    try:
        logs = select_pending(db, limit=args.limit, min_age_minutes=args.min_age_minutes)
        print(f"待补投任务：{len(logs)} 条")
        if args.dry_run:
            for log in logs:
                print(f"  [dry-run] review_id={log.id} expense_id={log.expense_id}")
            return 0

        queued = 0                                                  # 投递成功数
        failed = 0                                                  # 投递失败数
        for log in logs:
            ok = enqueue_review(log.id)                             # 单条投递结果
            if ok:
                queued += 1
            else:
                failed += 1
            print(f"  review_id={log.id} expense_id={log.expense_id} -> {'已投递' if ok else '投递失败'}")
        print(f"完成：成功 {queued} 条，失败 {failed} 条")
        return 0 if failed == 0 else 1
    finally:
        db.close()



if __name__ == "__main__":
    raise SystemExit(main())
