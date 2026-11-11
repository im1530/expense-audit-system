"""
MySQL 备份脚本的离线单测（不联网、不连数据库、不调用 mysqldump）
    覆盖：
        - 备份命名的可区分性（库名/环境/主机/类型/时间）与类型白名单；
        - 连接串解析只取库名与主机；
        - 过期备份筛选：只认本脚本命名格式、保留期关闭时不清理；
        - 备份完整性校验：正常 gzip 通过、空文件/非 gzip 失败；
        - 清理逻辑 dry-run 不删文件。
"""
import gzip
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path



from scripts.backup_mysql import (
    ALLOWED_KINDS,
    build_backup_name,
    parse_db_target,
    prune_backups,
    safe_host,
    select_expired_backups,
    verify_backup,
)
from scripts.run_backup_job import plan_upload_prune, plan_upload_sync
from scripts.verify_restore import should_skip_line



class BackupNamingTest(unittest.TestCase):
    """备份命名"""

    def test_name_contains_all_segments(self):
        """文件名同时体现库名/环境/主机/类型/时间"""
        when = datetime(2026, 11, 11, 18, 30, 5)                    # 固定时间便于断言
        name = build_backup_name("expense_audit", "production", "10.0.0.8", "full", when)
        self.assertEqual(name, "expense_audit__production__10-0-0-8__full__20261111_183005.sql.gz")

    def test_kind_is_distinguishable(self):
        """不同类型生成不同文件名，避免互相覆盖"""
        when = datetime(2026, 11, 11, 18, 30, 5)                    # 同一时间
        full = build_backup_name("expense_audit", "dev", "127.0.0.1", "full", when)
        pre = build_backup_name("expense_audit", "dev", "127.0.0.1", "preupgrade", when)
        self.assertNotEqual(full, pre)
        self.assertIn("__preupgrade__", pre)

    def test_unknown_kind_rejected(self):
        """未知类型直接拒绝，防止文件名被任意拼接"""
        with self.assertRaises(ValueError):
            build_backup_name("db", "dev", "localhost", "../evil", datetime.now())

    def test_host_sanitized(self):
        """主机名里的点号替换为短横线"""
        self.assertEqual(safe_host("127.0.0.1"), "127-0-0-1")
        self.assertEqual(safe_host(""), "localhost")

    def test_allowed_kinds(self):
        """白名单覆盖日常/变更前/手工三类"""
        self.assertEqual(ALLOWED_KINDS, ("full", "preupgrade", "manual"))



class DbTargetTest(unittest.TestCase):
    """连接串解析"""

    def test_parse_db_and_host(self):
        """从连接串取出库名与主机，不涉及密码"""
        db, host = parse_db_target("mysql+pymysql://u:p@db.internal:3306/expense_audit?charset=utf8mb4")
        self.assertEqual(db, "expense_audit")
        self.assertEqual(host, "db.internal")

    def test_missing_database_rejected(self):
        """没有库名的连接串直接报错"""
        with self.assertRaises(ValueError):
            parse_db_target("mysql+pymysql://u:p@127.0.0.1:3306/")



class RetentionTest(unittest.TestCase):
    """过期筛选"""

    def _touch(self, directory: Path, name: str, *, days_old: int) -> Path:
        """在目录里造一个指定修改时间的文件"""
        path = directory / name                                  # 目标路径
        path.write_bytes(b"x")                                   # 占位内容
        stamp = (datetime.now() - timedelta(days=days_old)).timestamp()   # 修改时间
        os.utime(path, (stamp, stamp))                           # 回拨 mtime
        return path

    def test_only_matching_names_and_old_files(self):
        """只挑出本脚本命名格式且超期的文件"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)                                # 临时目录
            old_ok = self._touch(directory, "expense_audit__dev__127-0-0-1__full__20260101_010101.sql.gz", days_old=40)
            fresh_ok = self._touch(directory, "expense_audit__dev__127-0-0-1__full__20261101_010101.sql.gz", days_old=1)
            foreign = self._touch(directory, "manual-copy.sql.gz", days_old=40)   # 非本脚本命名
            expired = select_expired_backups(list(directory.iterdir()), datetime.now(), 30)
            self.assertEqual(expired, [old_ok])
            self.assertNotIn(fresh_ok, expired)
            self.assertNotIn(foreign, expired)

    def test_zero_retention_disables_pruning(self):
        """保留天数 0 表示不清理"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)                                # 临时目录
            self._touch(directory, "db__dev__host__full__20200101_010101.sql.gz", days_old=999)
            self.assertEqual(select_expired_backups(list(directory.iterdir()), datetime.now(), 0), [])

    def test_prune_dry_run_keeps_files(self):
        """dry-run 只列不删"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)                                # 临时目录
            old = self._touch(directory, "db__dev__host__full__20200101_010101.sql.gz", days_old=999)
            removed = prune_backups(directory, datetime.now(), 30, dry_run=True)
            self.assertEqual(removed, [old])
            self.assertTrue(old.exists())

    def test_prune_deletes_expired(self):
        """正式模式删除过期备份"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)                                # 临时目录
            old = self._touch(directory, "db__dev__host__full__20200101_010101.sql.gz", days_old=999)
            prune_backups(directory, datetime.now(), 30, dry_run=False)
            self.assertFalse(old.exists())



class VerifyTest(unittest.TestCase):
    """备份完整性校验"""

    def _write_gz(self, directory: Path, text: str, name: str = "b.sql.gz") -> Path:
        """写一个 gzip 文本文件"""
        path = directory / name                                  # 目标路径
        with gzip.open(path, "wt", encoding="utf-8") as handle:   # 压缩写入
            handle.write(text)
        return path

    def test_valid_dump_passes(self):
        """合法 mysqldump 输出：能读通并统计到建表语句"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_gz(
                Path(tmp),
                "-- MySQL dump 10.13\nCREATE TABLE `expenses` (\n  id int\n);\nCREATE TABLE `users` (\n  id int\n);\n",
            )
            info = verify_backup(path)
            self.assertTrue(info["head_ok"])
            self.assertEqual(info["tables"], 2)
            self.assertGreater(info["bytes"], 0)

    def test_empty_file_rejected(self):
        """空文件直接失败"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.sql.gz"                    # 空文件
            path.write_bytes(b"")
            with self.assertRaises(ValueError):
                verify_backup(path)

    def test_not_gzip_rejected(self):
        """非 gzip 内容读取时报错"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.sql.gz"                      # 假压缩文件
            path.write_bytes(b"not a gzip stream")
            with self.assertRaises((OSError, ValueError, EOFError)):
                verify_backup(path)



class RestoreFilterTest(unittest.TestCase):
    """恢复验证的语句过滤"""

    def test_create_database_skipped(self):
        """建库语句必须过滤，否则会覆盖正式库"""
        self.assertTrue(should_skip_line("CREATE DATABASE /*!32312 IF NOT EXISTS*/ `expense_audit`;"))
        self.assertTrue(should_skip_line("  CREATE DATABASE `x`;"))

    def test_use_skipped(self):
        """USE 语句必须过滤"""
        self.assertTrue(should_skip_line("USE `expense_audit`;"))

    def test_data_lines_kept(self):
        """业务数据与建表语句必须保留"""
        for line in (
            "CREATE TABLE `expenses` (",
            "INSERT INTO `users` VALUES (1);",
            "-- MySQL dump 10.13",
            "/*!40101 SET NAMES utf8mb4 */;",
        ):
            self.assertFalse(should_skip_line(line), line)



class UploadSyncTest(unittest.TestCase):
    """附件目录同步与清理（纯文件系统，不连库）"""

    def _make(self, root: Path, relative: str, *, content: bytes = b"data", days_old: int = 0) -> Path:
        """在根目录下造一个文件，可指定内容与修改时间"""
        path = root / relative                                     # 目标路径
        path.parent.mkdir(parents=True, exist_ok=True)             # 建父目录
        path.write_bytes(content)                                  # 写内容
        if days_old:                                               # 回拨修改时间
            stamp = (datetime.now() - timedelta(days=days_old)).timestamp()
            os.utime(path, (stamp, stamp))
        return path

    def test_new_files_are_copied(self):
        """目标缺失的文件进入 copy 计划"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "uploads"                              # 源目录
            target = root / "backup"                               # 备份目录
            self._make(source, "2026/11/a.inv")                    # 造源文件
            plan = plan_upload_sync(source, target)
            self.assertEqual([(p[0].name, p[2]) for p in plan], [("a.inv", "copy")])

    def test_changed_files_are_updated(self):
        """同名但大小不同的文件进入 update 计划"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "uploads"                              # 源目录
            target = root / "backup"                               # 备份目录
            self._make(source, "a.inv", content=b"new-content")    # 源文件较大
            self._make(target, "a.inv", content=b"old")            # 备份文件较小
            plan = plan_upload_sync(source, target)
            self.assertEqual([(p[0].name, p[2]) for p in plan], [("a.inv", "update")])

    def test_identical_files_skipped(self):
        """大小与修改时间一致的文件不再复制"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "uploads"                              # 源目录
            target = root / "backup"                               # 备份目录
            self._make(source, "a.inv")                             # 源文件
            shutil.copy2(source / "a.inv", self._make(target, "a.inv", content=b"data"))   # 完全一致
            self.assertEqual(plan_upload_sync(source, target), [])

    def test_missing_source_dir_is_noop(self):
        """源目录不存在时返回空计划"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(plan_upload_sync(Path(tmp) / "nope", Path(tmp) / "backup"), [])

    def test_prune_only_orphan_and_expired(self):
        """只清理源已删除且过期的副本"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "uploads"                              # 源目录
            target = root / "backup"                               # 备份目录
            self._make(source, "keep.inv")                          # 仍在用的附件
            self._make(target, "keep.inv")                          # 其副本
            orphan_old = self._make(target, "gone-old.inv", days_old=60)   # 源已删除且过期
            orphan_new = self._make(target, "gone-new.inv", days_old=1)    # 源已删除但未过期
            doomed = plan_upload_prune(source, target, datetime.now(), 30)
            self.assertEqual(doomed, [orphan_old])
            self.assertNotIn(orphan_new, doomed)

    def test_prune_disabled_when_retention_zero(self):
        """保留天数为 0 时不清理"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "backup"                               # 备份目录
            self._make(target, "gone.inv", days_old=999)            # 很老的孤儿副本
            self.assertEqual(plan_upload_prune(root / "uploads", target, datetime.now(), 0), [])



if __name__ == "__main__":
    unittest.main()
