-- 0017：拆分规则 action / severity
-- action 只保留引擎动作：amount -> assign_chain，check_* -> check
-- severity 只保留检查级别：warn / review / block
ALTER TABLE `rules`
  ADD COLUMN `severity` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL
    COMMENT '检查规则风险级别：warn/review/block' AFTER `action`;

-- 历史检查规则：旧 action 就是级别，先搬到 severity，再把 action 统一为 check
UPDATE `rules`
SET `severity` = `action`,
    `action` = 'check'
WHERE `rule_type` LIKE 'check_%'
  AND `action` IN ('warn', 'review', 'block');

-- 兜底：没有合法级别的检查规则按 warn 处理
UPDATE `rules`
SET `severity` = 'warn'
WHERE `rule_type` LIKE 'check_%'
  AND `severity` IS NULL;
