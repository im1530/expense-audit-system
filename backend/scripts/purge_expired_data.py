"""
过期数据清理脚本（Phase F：隐私与留存）
    职责：
        - 本脚本只做参数解析与输出，清理逻辑在 app/services/retention_service.py；
        - 清理附件/凭证文件、raw_response、工作流检查点，不删业务主数据。
    用法（在 backend 目录下）：
        python scripts/purge_expired_data.py --dry-run
        python scripts/purge_expired_data.py
        python scripts/purge_expired_data.py --days 90 --raw-days 30 --limit 500
    建议：
        - 日常无人值守请用 scripts/run_retention_job.py（带互斥锁 + 执行审计）；
        - 执行前确认 backups 与磁盘加密策略已到位。
"""
import argparse
import sys
from pathlib import Path



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dependencies import SessionLocal                       # noqa: E402
from app.services.retention_service import run_retention        # noqa: E402



# 解析参数
def parse_args() -> argparse.Namespace:
    """
    解析命令行参数
        :return: 参数对象
    """
    parser = argparse.ArgumentParser(description="清理超过保留期的凭证文件、raw_response 与检查点")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除/不更新")   # 预演
    parser.add_argument("--days", type=int, default=None, help="覆盖附件/检查点保留天数")   # 保留期
    parser.add_argument("--raw-days", type=int, default=None, help="覆盖 raw_response 保留天数")   # 原文保留期
    parser.add_argument("--limit", type=int, default=500, help="单类数据单次最多处理条数")   # 批量上限
    return parser.parse_args()



# 主流程
def main() -> int:
    """
    脚本入口
        :return: 进程退出码：0 成功，1 有失败
    """
    args = parse_args()
    db = SessionLocal()
    try:
        summary = run_retention(
            db,
            dry_run=args.dry_run,
            days=args.days,
            raw_days=args.raw_days,
            limit=args.limit,
        )
        print(
            f"清理开始 dry_run={summary['dry_run']} "
            f"附件/检查点={summary['days']}天 raw_response={summary['raw_days']}天 "
            f"limit={summary['limit']}"
        )
        print(
            f"清理完成 附件={summary['attachments']} "
            f"raw_response={summary['raw_responses']} 检查点线程={summary['checkpoints']}"
        )
        return 0
    except Exception as exc:
        print(f"清理失败: {type(exc).__name__}: {exc}")
        return 1
    finally:
        db.close()



if __name__ == "__main__":
    raise SystemExit(main())
