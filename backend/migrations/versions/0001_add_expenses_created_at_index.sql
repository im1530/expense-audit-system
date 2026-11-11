-- ============================================================
-- 0001：为 expenses.created_at 增加索引
-- 背景：
--   报表（按月/按部门/按类别）与各类列表都会按创建时间过滤和排序，
--   数据量上来后全表扫描会明显变慢，这里补一个普通索引。
-- 执行：python scripts/migrate.py upgrade
-- ============================================================

ALTER TABLE `expenses` ADD INDEX `idx_expenses_created_at` (`created_at`);
