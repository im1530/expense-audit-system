"""
保留期清理定时任务（Phase F 运维）
    与 purge_expired_data.py 的区别：
        - 带进程互斥锁，避免定时任务重叠执行导致重复删文件/重复提交；
        - 每次执行写一条 maintenance_runs 审计（成功/失败、数量、参数），无人值守也能追溯；
        - 失败返回非 0 退出码，便于任务计划/监控告警。
    用法（在 backend 目录下）：
        python scripts/run_retention_job.py --dry-run     # 只统计，但同样写审计
        python scripts/run_retention_job.py               # 正式执行
        python scripts/run_retention_job.py --days 90 --raw-days 30 --limit 500
    建议：
        - 用 Windows 任务计划 / cron 每天凌晨执行一次；
        - 先跑几天 --dry-run，确认数量与耗时稳定后再正式启用；
        - 锁文件在系统临时目录，进程异常退出后超过 6 小时视为过期锁。
"""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dependencies import SessionLocal                                # noqa: E402
from app.models import MaintenanceStatus                                 # noqa: E402
from app.services.maintenance_service import begin_run, finish_run       # noqa: E402
from app.services.retention_service import run_retention                 # noqa: E402



# 任务名（写入 maintenance_runs.job）
JOB_NAME = "retention_purge"

# 互斥锁文件：系统临时目录，同一台机器上的多个进程共享
LOCK_FILE = Path(tempfile.gettempdir()) / "expense_audit_retention.lock"

# 锁超过该秒数视为过期（进程崩溃未清理）
STALE_LOCK_SECONDS = 6 * 3600



# 获取互斥锁
def acquire_lock(path: Path) -> bool:
    """
    以"创建独占文件"的方式获取互斥锁
        :param path: 锁文件路径
        :return: 获取成功 True；已有任务在跑 False
    """
    # 1.最多尝试两次：第二次用于清理过期锁后重试
    for attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # 独占创建
            os.write(fd, str(os.getpid()).encode("utf-8"))                  # 写入进程号便于排查
            os.close(fd)
            return True
        except FileExistsError:
            # 2.锁已存在：过期才允许清理，避免误删正在执行任务的锁
            try:
                age = time.time() - path.stat().st_mtime                    # 锁文件年龄
            except OSError:
                age = 0
            if attempt == 0 and age > STALE_LOCK_SECONDS:
                path.unlink(missing_ok=True)                                # 清理过期锁
                continue
            return False
    return False



# 释放互斥锁
def release_lock(path: Path) -> None:
    """
    删除锁文件
        :param path: 锁文件路径
        :return: 无
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # 释放失败不影响本次结果，过期锁会在下次执行时清理
        pass



# 解析参数
def parse_args() -> argparse.Namespace:
    """
    解析命令行参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="保留期清理定时任务（带互斥锁与执行审计）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除/不更新")   # 预演
    parser.add_argument("--days", type=int, default=None, help="覆盖附件/检查点保留天数")   # 保留期
    parser.add_argument("--raw-days", type=int, default=None, help="覆盖 raw_response 保留天数")   # 原文保留期
    parser.add_argument("--limit", type=int, default=500, help="单类数据单次最多处理条数")   # 批量上限
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    任务入口：抢锁 → 写审计 → 执行清理 → 收尾
        :return: 进程退出码：0 成功或已跳过，1 执行失败
    """
    args = parse_args()

    # 1.抢锁：已有任务在跑就直接跳过（退出码 0，避免误报）
    if not acquire_lock(LOCK_FILE):
        print("已有清理任务在执行，本次跳过")
        return 0

    db = SessionLocal()
    try:
        # 2.先落审计，进程崩溃也能看到 running
        run = begin_run(db, JOB_NAME)

        # 3.执行清理；失败写审计并返回非 0
        try:
            summary = run_retention(
                db,
                dry_run=args.dry_run,
                days=args.days,
                raw_days=args.raw_days,
                limit=args.limit,
            )
        except Exception as exc:
            finish_run(db, run, status=MaintenanceStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            print(f"清理失败: {type(exc).__name__}: {exc}")
            return 1

        # 4.成功：写审计并输出汇总
        finish_run(db, run, status=MaintenanceStatus.SUCCESS, summary=summary)
        print(
            f"清理完成 dry_run={summary['dry_run']} 附件={summary['attachments']} "
            f"raw_response={summary['raw_responses']} 检查点线程={summary['checkpoints']} "
            f"审计ID={run.id}"
        )
        return 0
    finally:
        # 5.无论成功失败都释放锁并关闭会话
        db.close()
        release_lock(LOCK_FILE)



if __name__ == "__main__":
    raise SystemExit(main())
