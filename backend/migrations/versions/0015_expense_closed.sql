-- 0015：区分“提交人确认领取”与“管理员强制关闭”
-- paid 只表示提交人确认领取；强制关闭进入 closed 终态，避免报表把未确认领取的金额算作已报销。
ALTER TABLE `expenses`
  MODIFY COLUMN `status` ENUM('draft','pending','approved','registered','rejected','paid','closed','cancelled')
    COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '报销状态',
  ADD COLUMN `closed_at` DATETIME DEFAULT NULL COMMENT '管理员强制关闭时间' AFTER `confirmed_at`;

-- 历史数据：已有 force_close 结算流水、但仍为 paid 的单据迁移到 closed
UPDATE `expenses` e
SET e.`status` = 'closed',
    e.`closed_at` = COALESCE(e.`paid_at`, e.`updated_at`, e.`created_at`),
    e.`paid_at` = NULL,
    e.`confirmed_at` = NULL
WHERE e.`status` = 'paid'
  AND EXISTS (
    SELECT 1 FROM `expense_settlements` s
    WHERE s.`expense_id` = e.`id` AND s.`action` = 'force_close'
  );
