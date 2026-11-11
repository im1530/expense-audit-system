"""
数据库迁移工具（零依赖版 Alembic）
    为什么不是 Alembic：当前环境无法安装第三方包（离线/受限），而"结构变更要有版本、可审查、可追溯"
    这件事本身不依赖 Alembic。这里用"有序 SQL 文件 + 版本登记表"实现同等能力，
    以后能装 Alembic 时，这些 SQL 可以直接改写成 revision，迁移记录也能平移。

    工作方式：
        - 迁移文件：migrations/versions/0001_xxx.sql、0002_xxx.sql……按版本号升序执行；
        - 登记表：schema_migrations(version, name, checksum, applied_at)，记录"哪些已经跑过"；
        - checksum：文件内容的 sha256，已执行的迁移被改动过会在 status 里报错（禁止篡改历史）；
        - schema.sql 是"当前结构快照"，每次发布都要与迁移结果保持一致：
              新环境：执行 schema.sql + init_data.sql，然后 migrate.py baseline
              老环境：migrate.py baseline（把现有版本登记为已应用）
              后续变更：新增 000N_xxx.sql → migrate.py upgrade

    命令：
        python scripts/migrate.py status           # 查看已应用/待执行
        python scripts/migrate.py upgrade          # 执行全部待执行迁移
        python scripts/migrate.py upgrade --to 0003
        python scripts/migrate.py baseline         # 把当前所有迁移标记为已应用（不执行 SQL）
        python scripts/migrate.py verify           # 校验数据库结构是否与 schema.sql 一致
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

# 允许在 backend 目录下直接执行 `python scripts/migrate.py`
BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402  （必须在 sys.path 调整之后再导入）



# 迁移目录与结构快照
VERSIONS_DIR = BASE_DIR / "migrations" / "versions"
SCHEMA_FILE = BASE_DIR / "app" / "sql" / "schema.sql"

# 版本登记表
LEDGER_TABLE = "schema_migrations"



# 创建数据库引擎
def build_engine():
    """
    按 settings.DATABASE_URL 创建引擎
        :return: SQLAlchemy Engine
    """
    return create_engine(settings.DATABASE_URL)



# 拆分 SQL 文件为可执行语句
def split_statements(sql: str) -> list[str]:
    """
    按分号拆分 SQL，并丢弃纯注释片段
        :param sql: SQL 文件全文
        :return: 可逐条执行的语句列表
    """
    statements = []
    for chunk in sql.split(";"):
        lines = [line for line in chunk.splitlines() if not line.strip().startswith("--")]
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements



# 计算文件校验和
def file_checksum(path: Path) -> str:
    """
    计算迁移文件的 sha256，用于检测"已执行的历史被改动"
        :param path: 迁移文件路径
        :return: 十六进制摘要
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()



# 发现全部迁移文件
def discover_migrations() -> list[dict]:
    """
    扫描 migrations/versions 下的 SQL 文件，按版本号升序返回
        文件名约定：<版本号>_<描述>.sql，版本号可比较（0001、0002……）。
        :return: 列表，元素含 version / name / path / checksum
    """
    if not VERSIONS_DIR.exists():
        return []

    migrations = []
    for path in sorted(VERSIONS_DIR.glob("*.sql")):
        version = path.stem.split("_", 1)[0]
        migrations.append({
            "version": version,
            "name": path.stem,
            "path": path,
            "checksum": file_checksum(path),
        })
    migrations.sort(key=lambda item: item["version"])
    return migrations



# 确保登记表存在
def ensure_ledger(conn) -> None:
    """
    创建版本登记表（不存在时）
        :param conn: 数据库连接
        :return: 无
    """
    conn.execute(text(
        f"CREATE TABLE IF NOT EXISTS `{LEDGER_TABLE}` ("
        "  `version` varchar(50) NOT NULL COMMENT '迁移版本号',"
        "  `name` varchar(200) NOT NULL COMMENT '迁移文件名',"
        "  `checksum` char(64) NOT NULL COMMENT '文件内容sha256',"
        "  `applied_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '执行时间',"
        "  PRIMARY KEY (`version`)"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据库迁移记录'"
    ))



# 读取已应用版本
def load_applied(conn) -> dict:
    """
    读取登记表内容
        :param conn: 数据库连接
        :return: {版本号: 记录字典}
    """
    rows = conn.execute(text(
        f"SELECT `version`, `name`, `checksum`, `applied_at` FROM `{LEDGER_TABLE}`"
    )).mappings().all()
    return {row["version"]: dict(row) for row in rows}



# 检查历史是否被篡改
def check_history(applied: dict, migrations: list[dict]) -> list[str]:
    """
    对比已应用记录与当前文件，找出被改动过的历史迁移
        :param applied: 已应用记录
        :param migrations: 当前迁移文件列表
        :return: 问题描述列表（空表示一致）
    """
    problems = []
    for migration in migrations:
        record = applied.get(migration["version"])
        if record and record["checksum"] != migration["checksum"]:
            problems.append(
                f"迁移 {migration['version']} 已执行过，但文件被修改（checksum 不一致）"
            )
    return problems



# 命令：status
def cmd_status() -> int:
    """
    打印当前迁移状态（已应用 / 待执行 / 历史被改动）
        :return: 进程退出码（0 正常，1 有问题）
    """
    engine = build_engine()
    migrations = discover_migrations()

    with engine.begin() as conn:
        ensure_ledger(conn)
        applied = load_applied(conn)

    database = engine.url.database
    print(f"数据库: {database}")
    print(f"迁移目录: {VERSIONS_DIR}")
    print(f"已应用: {len(applied)} 个，待执行: {len([m for m in migrations if m['version'] not in applied])} 个")

    if applied:
        print("\n已应用:")
        for version in sorted(applied):
            record = applied[version]
            print(f"  {version}  {record['name']}  ({record['applied_at']})")

    pending = [m for m in migrations if m["version"] not in applied]
    if pending:
        print("\n待执行:")
        for migration in pending:
            print(f"  {migration['version']}  {migration['name']}")

    problems = check_history(applied, migrations)
    if problems:
        print("\n[警告] 历史被改动:")
        for problem in problems:
            print(f"  - {problem}")
        engine.dispose()
        return 1

    engine.dispose()
    return 0



# 命令：upgrade
def cmd_upgrade(target: str | None = None) -> int:
    """
    按顺序执行待执行的迁移
        注意：MySQL 的 DDL 是自动提交的，单条语句失败无法整体回滚；
        所以失败时会明确告知"已执行到哪一条"，便于人工修复后继续。
        :param target: 只执行到指定版本（含），None 表示全部
        :return: 进程退出码
    """
    engine = build_engine()
    migrations = discover_migrations()

    with engine.begin() as conn:
        ensure_ledger(conn)
        applied = load_applied(conn)

    problems = check_history(applied, migrations)
    if problems:
        print("[错误] 已执行的历史被改动，拒绝继续。请恢复文件或手工处理：")
        for problem in problems:
            print(f"  - {problem}")
        engine.dispose()
        return 1

    pending = [m for m in migrations if m["version"] not in applied]
    if target:
        pending = [m for m in pending if m["version"] <= target]

    if not pending:
        print("没有待执行的迁移，数据库已是最新。")
        engine.dispose()
        return 0

    for migration in pending:
        print(f"执行 {migration['version']}  {migration['name']} ...", end=" ", flush=True)
        statements = split_statements(migration["path"].read_text(encoding="utf-8"))
        try:
            with engine.begin() as conn:
                for index, statement in enumerate(statements, start=1):
                    conn.execute(text(statement))
                conn.execute(text(
                    f"INSERT INTO `{LEDGER_TABLE}` (`version`, `name`, `checksum`) "
                    f"VALUES (:version, :name, :checksum)"
                ), {
                    "version": migration["version"],
                    "name": migration["name"],
                    "checksum": migration["checksum"],
                })
        except Exception as exc:
            print("失败")
            print(f"[错误] 迁移 {migration['version']} 执行失败：{exc}")
            print("        该文件中的语句可能已部分生效（MySQL DDL 不支持事务回滚），请人工确认后再重试。")
            engine.dispose()
            return 1
        print("完成")

    engine.dispose()
    print(f"共执行 {len(pending)} 个迁移。")
    return 0



# 命令：baseline
def cmd_baseline() -> int:
    """
    把当前所有迁移标记为"已应用"（不执行 SQL）
        用途：
            - 已有数据库：结构本来就是最新的（可能是手工改的），先登记，避免重复执行；
            - 全新环境：执行完 schema.sql + init_data.sql 后登记，后续只跑增量。
        :return: 进程退出码
    """
    engine = build_engine()
    migrations = discover_migrations()

    with engine.begin() as conn:
        ensure_ledger(conn)
        applied = load_applied(conn)
        for migration in migrations:
            if migration["version"] in applied:
                continue
            conn.execute(text(
                f"INSERT INTO `{LEDGER_TABLE}` (`version`, `name`, `checksum`) "
                f"VALUES (:version, :name, :checksum)"
            ), {
                "version": migration["version"],
                "name": migration["name"],
                "checksum": migration["checksum"],
            })
            print(f"登记 {migration['version']}  {migration['name']}")

    engine.dispose()
    print("基线登记完成（未执行任何 SQL）。")
    return 0



# 命令：verify
def cmd_verify() -> int:
    """
    校验数据库结构是否与 schema.sql 快照一致（表、列、索引）
        用于发现"改了 schema.sql/迁移文件但忘记执行 upgrade"的情况。
        :return: 进程退出码（0 一致，1 有差异）
    """
    if not SCHEMA_FILE.exists():
        print(f"[错误] 找不到结构快照：{SCHEMA_FILE}")
        return 1

    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    expected = parse_schema(schema_sql)

    engine = build_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    problems = []

    for table, spec in expected.items():
        if table not in existing_tables:
            problems.append(f"缺少表 `{table}`")
            continue

        actual_columns = {column["name"] for column in inspector.get_columns(table)}
        for column in spec["columns"]:
            if column not in actual_columns:
                problems.append(f"`{table}` 缺少列 `{column}`")

        actual_indexes = {index["name"] for index in inspector.get_indexes(table)}
        for index in spec["indexes"]:
            if index not in actual_indexes:
                problems.append(f"`{table}` 缺少索引 `{index}`")

    engine.dispose()

    if problems:
        print(f"[不一致] 共 {len(problems)} 处差异：")
        for problem in problems:
            print(f"  - {problem}")
        print("\n如果这些差异来自新的迁移文件，请执行：python scripts/migrate.py upgrade")
        return 1

    print(f"结构一致：{len(expected)} 张表与 schema.sql 匹配。")
    return 0



# 解析 schema.sql 中的表/列/索引
def parse_schema(schema_sql: str) -> dict:
    """
    从 schema.sql 里解析每张表的列名与索引名
        :param schema_sql: schema.sql 全文
        :return: {表名: {"columns": [...], "indexes": [...]}}
    """
    pattern = re.compile(r"CREATE TABLE `(?P<table>\w+)` \((?P<body>.*?)\n\) ENGINE", re.S)
    result = {}
    for match in pattern.finditer(schema_sql):
        table = match.group("table")
        columns, indexes = [], []
        for line in match.group("body").splitlines():
            stripped = line.strip()
            if stripped.startswith("`"):
                # 列定义：`column` 类型 ...
                columns.append(stripped.split("`")[1])
            elif stripped.startswith("KEY `") or stripped.startswith("UNIQUE KEY `"):
                indexes.append(stripped.split("`")[1])
        result[table] = {"columns": columns, "indexes": indexes}
    return result



# 命令行入口
def main() -> int:
    """
    解析命令行参数并分发到具体命令
        :return: 进程退出码
    """
    parser = argparse.ArgumentParser(description="数据库迁移工具（零依赖版 Alembic）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="查看已应用/待执行的迁移")
    upgrade_parser = subparsers.add_parser("upgrade", help="执行待执行的迁移")
    upgrade_parser.add_argument("--to", help="只执行到指定版本（含）")
    subparsers.add_parser("baseline", help="把当前所有迁移登记为已应用（不执行 SQL）")
    subparsers.add_parser("verify", help="校验数据库结构是否与 schema.sql 一致")

    args = parser.parse_args()

    if args.command == "status":
        return cmd_status()
    if args.command == "upgrade":
        return cmd_upgrade(args.to)
    if args.command == "baseline":
        return cmd_baseline()
    if args.command == "verify":
        return cmd_verify()
    parser.print_help()
    return 2



if __name__ == "__main__":
    sys.exit(main())
