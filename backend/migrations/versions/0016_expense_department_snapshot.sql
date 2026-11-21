-- 0016：报销单提交时固化部门快照
-- 报表按提交时部门统计，员工调部门后历史数据不再随当前部门漂移。
ALTER TABLE `expenses`
  ADD COLUMN `department_id` bigint(20) DEFAULT NULL COMMENT '提交时部门快照ID' AFTER `user_id`,
  ADD KEY `ix_expenses_department_id` (`department_id`),
  ADD CONSTRAINT `fk_expenses_department` FOREIGN KEY (`department_id`) REFERENCES `departments` (`id`) ON DELETE SET NULL;

-- 历史数据回填：只能按当前部门回填（历史归属已无法精确还原，按现状固化）
UPDATE `expenses` e
JOIN `users` u ON e.`user_id` = u.`id`
SET e.`department_id` = u.`department_id`
WHERE e.`department_id` IS NULL;
