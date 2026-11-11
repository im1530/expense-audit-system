"""
僵尸工作流收尾脚本（Phase D 运维）
    用途：
        - worker 崩溃/进程重启后，把永远停在 running 的 workflow_runs 收尾为 failed；
        - 同时把还在 running 的 AI 预审日志收尾，避免单据一直显示"预审中"。
    行为：
        - 默认按 WORKFLOW_STALE_MINUTES（15 分钟）判断僵尸；
        - 只改状态，不调用模型；需要重算时由用户重新触发或 requeue_reviews.py 补投；
        - 每处理一条单独提交，单条失败不影响其它记录。
    用法（在 backend 目录下）：
        python scripts/recover_workflows.py --dry-run          # 只统计，不改状态
        python scripts/recover_workflows.py                    # 按配置阈值收尾
        python scripts/recover_workflows.py --stale-minutes 30 --limit 500
    建议：
        - 用 Windows 任务计划 / cron 每 10~15 分钟执行一次；
        - 先跑 --dry-run 确认命中数量，再接定时任务。
"""
import argparse
import sys
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dependencies import SessionLocal                       # noqa: E402
from app.services.workflow_recovery_service import recover_stale_runs   # noqa: E402



# 解析命令行参数
def parse_args() -> argparse.Namespace:
    """
    解析脚本参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="收尾卡在 running 的僵尸工作流")
    parser.add_argument("--limit", type=int, default=100, help="单次最多处理多少条")          # 批量上限
    parser.add_argument("--stale-minutes", type=int, default=0, help="超时分钟数；0 表示读配置")   # 超时阈值
    parser.add_argument("--dry-run", action="store_true", help="只统计，不改状态")              # 预演开关
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    脚本入口
        :return: 进程退出码：0 成功，1 有记录处理失败
    """
    args = parse_args()
    stale_minutes = args.stale_minutes or None                 # 0 交给服务读配置
    db = SessionLocal()
    try:
        summary = recover_stale_runs(
            db,
            stale_minutes=stale_minutes,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        print(
            f"阈值={summary['stale_minutes']}分钟 命中={summary['scanned']} "
            f"收尾={summary['recovered']} dry_run={summary['dry_run']}"
        )
        for run_id in summary["run_ids"]:
            print(f"  run_id={run_id} -> {'[dry-run] 待收尾' if args.dry_run else '已收尾为 failed'}")
        for error in summary["errors"]:
            print(f"  [失败] {error}")
        return 0 if not summary["errors"] else 1
    finally:
        db.close()



if __name__ == "__main__":
    raise SystemExit(main())
