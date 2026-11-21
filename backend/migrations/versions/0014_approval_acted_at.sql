-- 0014：审批记录一条任务一行
-- 1) 新增处理时间：created_at 表示任务创建（提交）时间，acted_at 表示审批/撤回/撤销时间
ALTER TABLE `approvals`
  ADD COLUMN `acted_at` datetime DEFAULT NULL COMMENT '处理时间（审批/撤回/撤销时回填）' AFTER `comment`;

-- 2) 历史数据回填：已经处理过的记录，处理时间近似取原 created_at
UPDATE `approvals`
SET `acted_at` = `created_at`
WHERE `status` <> 'pending' AND `acted_at` IS NULL;

-- 3) 清理历史遗留的“占位 pending”：同单据同层级已经有 approved/rejected 结果时，
--    占位记录不再保留；仍处于待审批的占位记录（没有同层结果）保持原样。
DELETE p FROM `approvals` p
JOIN `approvals` d
  ON d.`expense_id` = p.`expense_id`
 AND d.`level` = p.`level`
 AND d.`id` <> p.`id`
 AND d.`status` IN ('approved', 'rejected')
WHERE p.`approver_id` IS NULL
  AND p.`status` = 'pending';
