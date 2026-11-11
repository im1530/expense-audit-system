"""
备份恢复验证脚本（上线/演练用）
    职责：
        1. 把 .sql.gz 备份导入一个临时库，验证备份真的可恢复（不是"文件存在就算成功"）；
        2. 对比临时库与源库的表集合、关键表行数，任一项不一致退出码非 0；
        3. 演练结束自动删除临时库，绝不动正式库。
    为什么导入前要过滤语句：
        mysqldump --databases 会写出 CREATE DATABASE / USE，直接导入会覆盖正式库；
        本脚本把这两类库级语句过滤掉，只把对象导入临时库。
    用法（在 backend 目录下）：
        python scripts/verify_restore.py --backup "D:\\MySQLBackup\\xxx.sql.gz"
        python scripts/verify_restore.py --backup ... --keep    # 保留临时库供人工检查
    前置：
        - 需要 mysql 客户端（默认 C:\\Program Files\\MySQL\\MySQL Server 5.7\\bin\\mysql.exe）；
        - 账号取 app.config.settings.DATABASE_URL（演练需要建库权限，通常用应用账号即可）；
        - 临时凭证文件放在纯 ASCII 目录：MySQL 5.7 客户端打不开含中文的路径。
"""
import argparse
import gzip
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url



# 允许从 backend 目录直接运行脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings   # noqa: E402 - 需先修好 sys.path



logger = logging.getLogger("verify_restore")   # 模块日志器

# 默认 mysql 客户端路径
DEFAULT_MYSQL = Path("C:/Program Files/MySQL/MySQL Server 5.7/bin/mysql.exe")

# 默认临时库名
DEFAULT_TEMP_DB = "expense_audit_restore_check"

# 默认凭证临时目录（必须是纯 ASCII 路径）
DEFAULT_CNF_DIR = Path("D:/MySQLBackup/conf")

# 需要逐表对比行数的关键表
KEY_TABLES = ("users", "expenses", "expense_items", "ai_review_logs", "workflow_runs")



# 判断一行是否属于库级语句（需要过滤）
def should_skip_line(line: str) -> bool:
    """
    判断备份中的某一行是否要在导入临时库时跳过
        :param line: SQL 文本行
        :return: 需要跳过返回 True
    """
    stripped = line.lstrip()                                       # 去掉前导空白再判断
    if stripped.startswith("CREATE DATABASE"):                     # 建库语句
        return True
    if stripped.startswith("USE "):                                # 切库语句
        return True
    return False



# 查某库的表集合
def table_names(conn, db_name: str) -> set[str]:
    """
    查询指定库的全部表名
        :param conn: 数据库连接
        :param db_name: 库名
        :return: 表名集合
    """
    rows = conn.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = :db"),
        {"db": db_name},
    )
    return {row[0] for row in rows}                                # 转集合便于比较



# 把备份导入临时库
def import_dump(mysql_bin: Path, cnf: Path, backup: Path, temp_db: str) -> int:
    """
    解压备份并导入临时库
        :param mysql_bin: mysql 客户端路径
        :param cnf: 临时凭证文件
        :param backup: 备份文件
        :param temp_db: 临时库名
        :return: mysql 客户端退出码
        :raises RuntimeError: 客户端不存在
    """
    if not mysql_bin.exists():                                     # 客户端缺失
        raise RuntimeError(f"找不到 mysql 客户端: {mysql_bin}")

    process = subprocess.Popen(                                    # 启动客户端，SQL 从 stdin 灌入
        [str(mysql_bin), f"--defaults-extra-file={cnf}", temp_db],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None                               # 管道一定存在
    skipped = 0                                                    # 过滤掉的行数
    with gzip.open(backup, "rt", encoding="utf-8", errors="replace") as dump:   # 解压读取
        for line in dump:
            if should_skip_line(line):                             # 库级语句直接丢弃
                skipped += 1
                continue
            try:
                process.stdin.write(line.encode("utf-8"))          # 写给客户端
            except BrokenPipeError:                                # 客户端提前退出
                break
    try:
        process.stdin.close()                                      # 关闭输入
    except BrokenPipeError:
        pass
    code = process.wait()                                          # 等待退出
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    if code != 0:
        logger.error("导入失败 rc=%s stderr=%s", code, stderr[:400])
    else:
        logger.info("导入完成，过滤库级语句 %s 行", skipped)
    return code



# 主流程
def main(argv: list[str] | None = None) -> int:
    """
    执行一次恢复验证
        :param argv: 命令行参数（测试时可注入）
        :return: 退出码：0 通过，1 不一致/失败
    """
    parser = argparse.ArgumentParser(description="备份恢复验证")
    parser.add_argument("--backup", required=True, help="备份文件（.sql.gz）")
    parser.add_argument("--mysql", default=str(DEFAULT_MYSQL), help="mysql 客户端路径")
    parser.add_argument("--temp-db", default=DEFAULT_TEMP_DB, help="临时库名")
    parser.add_argument("--cnf-dir", default=str(DEFAULT_CNF_DIR), help="临时凭证目录（纯 ASCII）")
    parser.add_argument("--keep", action="store_true", help="保留临时库供人工检查")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    backup = Path(args.backup)                                     # 备份文件
    if not backup.exists():
        logger.error("备份文件不存在: %s", backup)
        return 1

    url = make_url(settings.DATABASE_URL)                          # 应用连接串
    source_db = url.database                                       # 源库名
    if not source_db:
        logger.error("DATABASE_URL 未指定库名")
        return 1

    # 1.临时凭证：放纯 ASCII 目录，密码只落这里，退出即删
    with tempfile.TemporaryDirectory(dir=args.cnf_dir) as tmp:
        cnf = Path(tmp) / "restore.cnf"                            # 临时凭证文件
        cnf.write_text(
            "[client]\n"
            f"user={url.username}\n"
            f"password={url.password}\n"
            f"host={url.host or '127.0.0.1'}\n"
            f"port={url.port or 3306}\n"
            "default-character-set=utf8mb4\n",
            encoding="utf-8",
        )

        engine = create_engine(url.set(database=None))             # 服务器级引擎（不选库）
        try:
            # 2.建临时库
            with engine.begin() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{args.temp_db}`"))
                conn.execute(text(
                    f"CREATE DATABASE `{args.temp_db}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                ))
            logger.info("临时库已创建: %s", args.temp_db)

            # 3.导入
            code = import_dump(Path(args.mysql), cnf, backup, args.temp_db)
            if code != 0:
                return 1

            # 4.对比表集合
            with engine.connect() as conn:
                source_tables = table_names(conn, source_db)
                restored_tables = table_names(conn, args.temp_db)
            logger.info("源库表数=%s 恢复库表数=%s", len(source_tables), len(restored_tables))
            missing = source_tables - restored_tables              # 丢失的表
            extra = restored_tables - source_tables                # 多出的表
            if missing or extra:
                logger.error("表集合不一致 缺失=%s 多余=%s", sorted(missing), sorted(extra))
                return 1

            # 5.对比关键表行数
            with engine.connect() as conn:
                for table in KEY_TABLES:
                    src = conn.execute(text(f"SELECT COUNT(*) FROM `{source_db}`.`{table}`")).scalar()
                    dst = conn.execute(text(f"SELECT COUNT(*) FROM `{args.temp_db}`.`{table}`")).scalar()
                    if src != dst:
                        logger.error("行数不一致 table=%s 源库=%s 恢复库=%s", table, src, dst)
                        return 1
                    logger.info("行数一致 table=%s rows=%s", table, src)
            logger.info("恢复验证通过：表集合与关键表行数完全一致")
            return 0
        finally:
            # 6.清理临时库（--keep 时保留）
            if not args.keep:
                with engine.begin() as conn:
                    conn.execute(text(f"DROP DATABASE IF EXISTS `{args.temp_db}`"))
                logger.info("临时库已删除: %s", args.temp_db)
            engine.dispose()



if __name__ == "__main__":
    raise SystemExit(main())
