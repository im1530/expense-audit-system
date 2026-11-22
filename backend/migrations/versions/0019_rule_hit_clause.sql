-- 0019：规则命中记录增加制度条款ID，补齐规则结论的可追溯性
ALTER TABLE `expense_rule_hits`
  ADD COLUMN `clause_id` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL
    COMMENT '命中的制度条款ID' AFTER `rule_name`;

-- 历史命中按规则类型回填（与当前 rule_engine 的映射保持一致）
UPDATE `expense_rule_hits`
SET `clause_id` = CASE `rule_type`
  WHEN 'check_category_limit' THEN '3.7'
  WHEN 'check_invoice_missing' THEN '2.5'
  WHEN 'check_duplicate' THEN '4.3'
  WHEN 'check_period_late' THEN '4.2'
  WHEN 'check_evidence_strength' THEN '2.2'
  ELSE NULL
END
WHERE `clause_id` IS NULL;
