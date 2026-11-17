-- 0013：站内通知
CREATE TABLE `notifications` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '通知ID',
  `user_id` bigint(20) NOT NULL COMMENT '接收人ID',
  `type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'expense_registered/registration_cancelled/expense_confirmed/force_closed',
  `title` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知标题',
  `content` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知内容',
  `related_type` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '关联对象类型，如 expense',
  `related_id` bigint(20) DEFAULT NULL COMMENT '关联对象ID',
  `is_read` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否已读',
  `read_at` datetime DEFAULT NULL COMMENT '已读时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_notifications_user_read` (`user_id`, `is_read`, `created_at`),
  CONSTRAINT `fk_notifications_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
