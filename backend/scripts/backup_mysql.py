"""
MySQL 逻辑备份脚本（上线运维用）
    职责：
        1. 用专用只读账号调用 mysqldump：账号 backup_ro 只授 SELECT/LOCK TABLES/SHOW VIEW/TRIGGER/EVENT
           + RELOAD/PROCESS，凭证放在 --defaults-extra-file 指向的 .cnf 里（ACL 已收紧到仅当前用户可读）；
        2. 输出 gzip 压缩的 SQL，文件名区分 库名/环境/主机/备份类型/时间，便于在同目录混放多种备份；
        3. 备份后做完整性校验（gzip 能完整解压 + 至少 1 条 CREATE TABLE），校验失败退出码非 0；
        4. 按保留天数清理旧备份，只清理本脚本自己命名格式的文件，不误删同目录其他文件。
    用法（在 backend 目录下执行）：
        python scripts/backup_mysql.py                        # 日常全量备份
        python scripts/backup_mysql.py --kind preupgrade      # 变更前备份，命名上可区分
        python scripts/backup_mysql.py --dry-run              # 只打印将要做什么
    安全约定：
        - 密码不出现在命令行、日志、仓库里，只存在于 .cnf；
        - 本脚本不读取 .env，数据库名与主机从 app.config.settings 解析，不含密码。
"""
import argparse
import gzip
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy.engine import make_url



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings   # noqa: E402 - 需先修好 sys.path 才能导入



logger = logging.getLogger("backup_mysql")   # 模块日志器

# 默认备份根目录（与凭证目录同盘，便于一起做离线拷贝）
DEFAULT_BACKUP_DIR = Path("D:/MySQLBackup")

# 默认凭证文件（只存 backup_ro 的账号密码）
DEFAULT_CNF = Path("D:/MySQLBackup/conf/backup.cnf")

# 默认 mysqldump 路径（PATH 里没有时使用）
DEFAULT_MYSQLDUMP = Path("C:/Program Files/MySQL/MySQL Server 5.7/bin/mysqldump.exe")

# 默认保留天数
DEFAULT_RETENTION_DAYS = 30

# 允许的备份类型（写进文件名，用于区分日常/变更前/手工）
ALLOWED_KINDS = ("full", "preupgrade", "manual")

# 本脚本产出的文件名格式：库名__环境__主机__类型__时间.sql.gz
BACKUP_NAME_PATTERN = re.compile(
    r"^(?P<db>[A-Za-z0-9_]+)__(?P<env>[A-Za-z0-9_]+)__(?P<host>[A-Za-z0-9_\-]+)"
    r"__(?P<kind>[a-z]+)__(?P<stamp>\d{8}_\d{6})\.sql\.gz$"
)



# 把主机名整理成可安全放进文件名的形式
def safe_host(host: str) -> str:
    """
    把主机名转成文件名安全片段
        :param host: 主机名或 IP
        :return: 只含字母数字与短横线的片段
    """
    return re.sub(r"[^A-Za-z0-9\-]+", "-", host or "localhost")   # 点号等替换为短横线



# 生成备份文件名
def build_backup_name(db_name: str, env: str, host: str, kind: str, when: datetime) -> str:
    """
    生成备份文件名（区分 库名/环境/主机/类型/时间）
        :param db_name: 库名
        :param env: 环境标识（APP_ENV）
        :param host: 数据库主机
        :param kind: 备份类型（full/preupgrade/manual）
        :param when: 备份时间
        :return: 文件名
        :raises ValueError: 类型不在允许范围
    """
    if kind not in ALLOWED_KINDS:                                  # 类型必须白名单化，避免文件名注入
        raise ValueError(f"不支持的备份类型: {kind}，可选 {ALLOWED_KINDS}")
    return (
        f"{db_name}__{env}__{safe_host(host)}__{kind}__{when:%Y%m%d_%H%M%S}.sql.gz"
    )



# 从连接串解析库名与主机（不碰密码）
def parse_db_target(database_url: str) -> tuple[str, str]:
    """
    从 DATABASE_URL 解析库名与主机
        :param database_url: SQLAlchemy 连接串
        :return: (库名, 主机)
        :raises ValueError: 连接串缺少库名
    """
    url = make_url(database_url)                                   # 解析连接串
    if not url.database:                                           # 必须有库名
        raise ValueError("DATABASE_URL 未指定数据库名")
    return url.database, (url.host or "localhost")                 # 库名与主机（忽略账号密码）



# 找出超过保留期的备份文件
def select_expired_backups(files: Iterable[Path], now: datetime, retention_days: int) -> list[Path]:
    """
    筛选出超过保留期的备份文件
        :param files: 候选文件（通常来自目录遍历）
        :param now: 当前时间
        :param retention_days: 保留天数（小于等于 0 表示不清理）
        :return: 需要删除的文件列表
    """
    if retention_days <= 0:                                        # 保留期关闭
        return []
    cutoff = now - timedelta(days=retention_days)                  # 早于该时间视为过期

    expired = []                                                   # 结果
    for path in files:
        if not BACKUP_NAME_PATTERN.match(path.name):               # 只处理本脚本的命名格式
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)    # 文件修改时间
        if modified < cutoff:                                      # 早于保留期
            expired.append(path)
    return expired



# 校验备份文件完整性
def verify_backup(path: Path, *, max_lines: int = 200) -> dict:
    """
    校验 gzip 备份完整性
        做法：完整解压一遍确认无 CRC 错误，同时统计前若干行里的 CREATE TABLE 数量。
        :param path: 备份文件
        :param max_lines: 统计表名时最多扫描多少行
        :return: {"bytes": 字节数, "tables": 建表语句数, "head_ok": 是否像 mysqldump 输出}
        :raises ValueError: 文件为空或不是合法 gzip
    """
    if not path.exists() or path.stat().st_size == 0:              # 空文件直接判失败
        raise ValueError(f"备份文件为空: {path}")

    tables = 0                                                     # 建表语句计数
    head_ok = False                                                # 是否是 mysqldump 输出
    scanned = 0                                                    # 已扫描行数
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:   # 解压读取
        for line in handle:
            if scanned == 0 and "MySQL dump" in line:              # 首行特征
                head_ok = True
            if scanned < max_lines and line.startswith("CREATE TABLE"):   # 统计建表语句
                tables += 1
            scanned += 1                                           # 计数
    return {"bytes": path.stat().st_size, "tables": tables, "head_ok": head_ok}



# 执行 mysqldump 并压缩落盘
def dump_database(
    mysqldump: Path,
    cnf: Path,
    db_name: str,
    out_path: Path,
) -> int:
    """
    调用 mysqldump 并把输出压缩写入目标文件
        :param mysqldump: mysqldump 可执行文件
        :param cnf: 凭证文件（--defaults-extra-file）
        :param db_name: 库名
        :param out_path: 输出 .sql.gz 路径
        :return: mysqldump 退出码
        :raises RuntimeError: 可执行文件或凭证文件缺失
    """
    # 1.前置检查：可执行文件与凭证文件都必须存在，缺一个就直接失败，避免生成半成品
    if not mysqldump.exists():
        raise RuntimeError(f"找不到 mysqldump: {mysqldump}")
    if not cnf.exists():
        raise RuntimeError(f"找不到凭证文件: {cnf}")

    # 2.组装参数：--single-transaction 保证 InnoDB 一致性快照且不锁表
    command = [
        str(mysqldump),                                            # 可执行文件
        f"--defaults-extra-file={cnf}",                            # 凭证（密码不落在命令行）
        "--single-transaction",                                    # 一致性快照，不阻塞业务写入
        "--quick",                                                 # 逐行读取，避免大表占满内存
        "--routines",                                              # 导出存储过程/函数
        "--events",                                                # 导出事件
        "--triggers",                                              # 导出触发器
        "--hex-blob",                                              # 二进制字段用十六进制，避免编码损坏
        "--default-character-set=utf8mb4",                         # 字符集与业务库一致
        "--databases", db_name,                                    # 带上 CREATE DATABASE / USE
    ]

    # 3.执行并流式压缩：边读 mysqldump 输出边写 gzip，不落中间文件
    process = subprocess.Popen(                                    # 启动 mysqldump
        command,                                                   # 命令
        stdout=subprocess.PIPE,                                    # 标准输出管道（逐块读取）
        stderr=subprocess.PIPE,                                    # 错误输出管道
    )
    with gzip.open(out_path, "wb", compresslevel=6) as sink:       # 目标压缩流
        assert process.stdout is not None                          # 管道一定存在
        while True:                                                # 逐块搬运
            chunk = process.stdout.read(1024 * 128)                # 读一块（128KB）
            if not chunk:                                          # 读完
                break
            sink.write(chunk)                                      # 写入压缩流
    code = process.wait()                                          # 等待退出
    stderr = process.stderr.read() if process.stderr else b""      # 错误输出
    if code != 0:                                                  # 失败时打印 stderr 便于排障
        logger.error("mysqldump 失败 rc=%s stderr=%s", code, stderr.decode("utf-8", "replace")[:500])
    return code



# 清理过期备份
def prune_backups(backup_dir: Path, now: datetime, retention_days: int, *, dry_run: bool = False) -> list[Path]:
    """
    删除过期备份
        :param backup_dir: 备份目录
        :param now: 当前时间
        :param retention_days: 保留天数
        :param dry_run: 只打印不删除
        :return: 被删除（或将要删除）的文件列表
    """
    candidates = [p for p in backup_dir.glob("*.sql.gz") if p.is_file()]   # 候选备份
    expired = select_expired_backups(candidates, now, retention_days)     # 过期文件
    for path in expired:
        if dry_run:                                                # 演练模式
            logger.info("将删除过期备份: %s", path.name)
            continue
        path.unlink(missing_ok=True)                               # 实际删除
        logger.info("已删除过期备份: %s", path.name)
    return expired



# 主流程
def main(argv: list[str] | None = None) -> int:
    """
    执行一次备份
        :param argv: 命令行参数（测试时可注入）
        :return: 退出码：0 成功，1 失败
    """
    parser = argparse.ArgumentParser(description="MySQL 逻辑备份")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="备份输出目录")
    parser.add_argument("--cnf", default=str(DEFAULT_CNF), help="凭证文件（--defaults-extra-file）")
    parser.add_argument("--mysqldump", default=str(DEFAULT_MYSQLDUMP), help="mysqldump 路径")
    parser.add_argument("--kind", default="full", choices=ALLOWED_KINDS, help="备份类型（写进文件名）")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="保留天数")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际备份")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    # 1.解析目标库与主机（不用密码）
    db_name, host = parse_db_target(settings.DATABASE_URL)
    env = settings.APP_ENV or "unknown"                            # 环境标识
    backup_dir = Path(args.backup_dir)                             # 备份目录
    now = datetime.now()                                           # 当前时间
    target = backup_dir / build_backup_name(db_name, env, host, args.kind, now)   # 目标文件

    logger.info("开始备份 db=%s env=%s kind=%s -> %s", db_name, env, args.kind, target)

    # 2.演练模式：只打印计划，不动文件
    if args.dry_run:
        logger.info("dry-run：将生成 %s（保留 %s 天）", target.name, args.retention_days)
        prune_backups(backup_dir, now, args.retention_days, dry_run=True)
        return 0

    # 3.确保目录存在
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 4.执行备份
    try:
        code = dump_database(Path(args.mysqldump), Path(args.cnf), db_name, target)
    except (RuntimeError, OSError) as exc:
        logger.error("备份失败: %s", exc)
        return 1
    if code != 0:                                                  # mysqldump 非 0
        target.unlink(missing_ok=True)                             # 删掉不完整文件，避免误以为备份成功
        logger.error("备份失败：mysqldump 退出码 %s", code)
        return 1

    # 5.完整性校验：gzip 能读通且至少有 1 条建表语句
    try:
        info = verify_backup(target)
    except (ValueError, OSError) as exc:
        logger.error("备份校验失败: %s", exc)
        return 1
    if info["tables"] < 1 or not info["head_ok"]:
        logger.error("备份校验失败：tables=%s head_ok=%s", info["tables"], info["head_ok"])
        target.unlink(missing_ok=True)                             # 删掉不可用文件，避免误判为成功备份
        return 1
    logger.info("备份完成 file=%s size=%.1fKB tables>=%s", target.name, info["bytes"] / 1024, info["tables"])

    # 6.清理过期备份
    removed = prune_backups(backup_dir, now, args.retention_days)
    logger.info("清理完成 removed=%s", len(removed))
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
