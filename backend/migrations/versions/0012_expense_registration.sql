-- 0012：报销单“财务登记 → 提交人确认领取”两阶段结算
-- 1) 状态枚举新增 registered（已登记待领取）
ALTER TABLE `expenses`
  MODIFY COLUMN `status` ENUM('draft','pending','approved','registered','rejected','paid','cancelled')
    COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '报销状态';

-- 2) 登记/确认字段（paid_at 仍表示“报销闭环完成时间”，即提交人确认领取的时间）
ALTER TABLE `expenses`
  ADD COLUMN `registered_at` DATETIME NULL COMMENT '财务登记时间' AFTER `approved_at`,
  ADD COLUMN `registered_by` BIGINT NULL COMMENT '登记财务ID' AFTER `registered_at`,
  ADD COLUMN `register_note` VARCHAR(500) COLLATE utf8mb4_unicode_ci NULL COMMENT '登记备注（发给提交人）' AFTER `registered_by`,
  ADD COLUMN `confirmed_at` DATETIME NULL COMMENT '提交人确认领取时间' AFTER `paid_at`,
  ADD KEY `idx_expenses_registered_at` (`registered_at`),
  ADD CONSTRAINT `fk_expenses_registered_by` FOREIGN KEY (`registered_by`) REFERENCES `users` (`id`) ON DELETE SET NULL;

-- 3) 结算流水：登记 / 撤销 / 确认 / 强制关闭，只增不改
CREATE TABLE `expense_settlements` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '结算流水ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `action` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'register/cancel/confirm/force_close',
  `operator_id` bigint(20) DEFAULT NULL COMMENT '操作人ID',
  `note` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '备注/原因',
  `channel` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'bank/alipay/wechat/cash，可选',
  `transaction_no` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '流水号，可选',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_settlements_expense` (`expense_id`, `created_at`),
  UNIQUE KEY `uk_settlements_transaction_no` (`transaction_no`),
  CONSTRAINT `fk_settlements_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
