-- ============================================================
-- 0002：规则引擎（P1）
-- 1) expense_items 增加"无票说明"：允许无发票，但要求填写原因，规则据此判断是否提示；
-- 2) 新增 expense_rule_hits：每次提交（含重新提交）落一份规则命中结果，供审批页展示与统计。
-- 说明：
--   - rule_id 不建外键：规则被删除后，历史命中记录仍需保留规则名，便于追溯；
--   - expense_id 建外键并级联删除：单据删掉后命中记录一并清理。
-- ============================================================

ALTER TABLE `expense_items`
  ADD COLUMN `no_invoice_reason` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '无票说明(无发票时填写原因)' AFTER `invoice_url`;

CREATE TABLE IF NOT EXISTS `expense_rule_hits` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '命中记录ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `rule_id` bigint(20) DEFAULT NULL COMMENT '规则ID(规则删除后仍保留名称快照)',
  `rule_type` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '规则类型快照',
  `rule_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '规则名称快照',
  `level` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '级别 warn/block/review',
  `message` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '给审批人看的提示',
  `evidence` json DEFAULT NULL COMMENT '命中证据(实际值/阈值/关联单据)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `ix_expense_rule_hits_expense` (`expense_id`),
  CONSTRAINT `fk_expense_rule_hits_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='规则命中记录';
