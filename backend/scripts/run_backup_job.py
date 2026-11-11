"""
每日备份任务（数据库 + 附件目录，供 Windows 计划任务/cron 调用）
    职责：
        1. 调 scripts/backup_mysql.py 做一次全量 MySQL 备份（命名/自校验/保留期都在那边）；
        2. 把 uploads/ 附件目录增量镜像到备份盘：新增或变化的文件才复制，不重复搬全量；
        3. 源目录已删除、且超过保留天数的历史附件副本会被清理，避免备份盘无限增长；
        4. 全程写一份日志到 <备份目录>/logs/backup_YYYYMMDD.log，任何一步失败退出码非 0。
    用法（在 backend 目录下）：
        python scripts/run_backup_job.py                 # 正式执行
        python scripts/run_backup_job.py --dry-run       # 只打印计划
    约定：
        - 本脚本不读 .env 里的密钥，数据库连接由 backup_mysql 负责；
        - 计划任务示例（每天 02:00，当前用户级，不需要管理员）：
            schtasks /create /tn "ExpenseAudit_DailyBackup" /tr "<python> scripts\\run_backup_job.py" \
                     /sc daily /st 02:00 /f
"""
import argparse
import logging
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings   # noqa: E402 - 需先修好 sys.path
from scripts import backup_mysql   # noqa: E402 - 复用数据库备份逻辑



logger = logging.getLogger("run_backup_job")   # 模块日志器

# 默认备份根目录
DEFAULT_BACKUP_DIR = Path("D:/MySQLBackup")

# 默认保留天数（数据库与附件共用）
DEFAULT_RETENTION_DAYS = 30



# 计算附件同步计划
def plan_upload_sync(source_dir: Path, target_dir: Path) -> list[tuple[Path, Path, str]]:
    """
    计算附件目录的增量同步计划
        判断依据：目标缺失 -> copy；目标存在但大小或修改时间不同 -> update。
        :param source_dir: 源目录（backend/uploads）
        :param target_dir: 备份目录（<backup>/uploads）
        :return: [(源文件, 目标文件, 动作)]，动作为 copy/update
    """
    if not source_dir.exists():                                    # 源目录不存在视为无附件
        return []

    plan = []                                                      # 同步计划
    for source in sorted(source_dir.rglob("*")):                   # 递归遍历源文件
        if not source.is_file():                                   # 只要文件
            continue
        relative = source.relative_to(source_dir)                  # 相对路径
        target = target_dir / relative                             # 目标路径
        if not target.exists():                                    # 目标缺失
            plan.append((source, target, "copy"))
            continue
        source_stat = source.stat()                                # 源文件信息
        target_stat = target.stat()                                # 目标文件信息
        if source_stat.st_size != target_stat.st_size:             # 大小不同
            plan.append((source, target, "update"))
            continue
        if int(source_stat.st_mtime) != int(target_stat.st_mtime):  # 修改时间不同
            plan.append((source, target, "update"))
    return plan



# 计算过期附件副本清理计划
def plan_upload_prune(
    source_dir: Path,
    target_dir: Path,
    now: datetime,
    retention_days: int,
) -> list[Path]:
    """
    计算需要清理的历史附件副本
        只清理"源目录已不存在"且"超过保留期"的副本，避免误删仍在用的附件备份。
        :param source_dir: 源目录
        :param target_dir: 备份目录
        :param now: 当前时间
        :param retention_days: 保留天数（<=0 表示不清理）
        :return: 待删除的备份文件列表
    """
    if retention_days <= 0 or not target_dir.exists():             # 关闭清理或目录不存在
        return []

    cutoff = now - timedelta(days=retention_days)                  # 过期时间点
    doomed = []                                                    # 待删除文件
    for target in sorted(target_dir.rglob("*")):                   # 遍历备份
        if not target.is_file():                                   # 只要文件
            continue
        relative = target.relative_to(target_dir)                  # 相对路径
        if (source_dir / relative).exists():                       # 源文件还在，保留
            continue
        if datetime.fromtimestamp(target.stat().st_mtime) < cutoff:   # 超过保留期才删
            doomed.append(target)
    return doomed



# 执行附件同步
def sync_uploads(source_dir: Path, target_dir: Path, *, dry_run: bool = False) -> dict:
    """
    执行附件增量同步
        :param source_dir: 源目录
        :param target_dir: 备份目录
        :param dry_run: 只打印计划
        :return: {"copied": n, "updated": n, "bytes": n}
    """
    plan = plan_upload_sync(source_dir, target_dir)                # 同步计划
    stats = {"copied": 0, "updated": 0, "bytes": 0}                # 统计
    for source, target, action in plan:
        size = source.stat().st_size                               # 文件大小
        if dry_run:                                                # 演练模式
            logger.info("dry-run 将 %s: %s", action, target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)           # 目标目录
        shutil.copy2(source, target)                               # 复制并保留时间戳
        stats["copied" if action == "copy" else "updated"] += 1    # 计数
        stats["bytes"] += size                                     # 字节数
    return stats



# 执行附件清理
def prune_uploads(source_dir: Path, target_dir: Path, now: datetime, retention_days: int,
                  *, dry_run: bool = False) -> int:
    """
    清理过期的历史附件副本
        :param source_dir: 源目录
        :param target_dir: 备份目录
        :param now: 当前时间
        :param retention_days: 保留天数
        :param dry_run: 只打印
        :return: 删除数量
    """
    doomed = plan_upload_prune(source_dir, target_dir, now, retention_days)   # 待删列表
    for path in doomed:
        if dry_run:                                                # 演练模式
            logger.info("dry-run 将删除过期附件副本: %s", path)
            continue
        path.unlink(missing_ok=True)                               # 实际删除
        logger.info("已删除过期附件副本: %s", path)
    return len(doomed)



# 配置日志：同时写文件与控制台
def setup_logging(backup_dir: Path) -> None:
    """
    初始化日志（文件 + 控制台）
        :param backup_dir: 备份根目录（日志写到其 logs 子目录）
    """
    log_dir = backup_dir / "logs"                                  # 日志目录
    log_dir.mkdir(parents=True, exist_ok=True)                     # 确保存在
    log_file = log_dir / f"backup_{datetime.now():%Y%m%d}.log"     # 按天一个文件
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),        # 文件
            logging.StreamHandler(sys.stdout),                     # 控制台
        ],
    )



# 主流程
def main(argv: list[str] | None = None) -> int:
    """
    执行每日备份（数据库 + 附件）
        :param argv: 命令行参数（测试时可注入）
        :return: 退出码：0 成功，1 失败
    """
    parser = argparse.ArgumentParser(description="每日备份（数据库 + 附件）")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="备份根目录")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="保留天数")
    parser.add_argument("--kind", default="full", choices=backup_mysql.ALLOWED_KINDS, help="数据库备份类型")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = parser.parse_args(argv)

    backup_dir = Path(args.backup_dir)                             # 备份根目录
    setup_logging(backup_dir)                                      # 先日志后动作，失败也留痕

    # 1.数据库备份：复用专门脚本，失败即终止（不生成半套备份）
    logger.info("=== 步骤 1/3：MySQL 备份 ===")
    db_code = backup_mysql.main([
        "--backup-dir", str(backup_dir),
        "--retention-days", str(args.retention_days),
        "--kind", args.kind,
    ] + (["--dry-run"] if args.dry_run else []))
    if db_code != 0:
        logger.error("数据库备份失败，退出")
        return 1
    logger.info("数据库备份完成")

    # 2.附件目录同步
    logger.info("=== 步骤 2/3：附件目录同步 ===")
    source_dir = Path(settings.UPLOAD_DIR)                         # 附件源目录（相对 backend）
    if not source_dir.is_absolute():                               # 相对路径按 backend 解析
        source_dir = Path(__file__).resolve().parents[1] / source_dir
    target_dir = backup_dir / "uploads"                            # 备份目标
    stats = sync_uploads(source_dir, target_dir, dry_run=args.dry_run)
    logger.info("附件同步完成 copied=%s updated=%s bytes=%s", stats["copied"], stats["updated"], stats["bytes"])

    # 3.清理过期附件副本
    logger.info("=== 步骤 3/3：清理过期附件副本 ===")
    removed = prune_uploads(source_dir, target_dir, datetime.now(), args.retention_days, dry_run=args.dry_run)
    logger.info("附件清理完成 removed=%s", removed)

    logger.info("=== 每日备份完成 ===")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
