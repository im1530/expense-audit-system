-- ============================================================
-- 0007：AI 预审工作流审计（Phase D）
-- 背景：
--   Phase B/C 已把凭证、规则、制度、风险评审串起来，但执行过程仍是线性函数；
--   Phase D 需要节点级审计、失败定位、决策建议与后续断点恢复。
-- 变更：
--   1) 新建 workflow_runs：一次预审的 run 级状态、版本、决策；
--   2) 新建 workflow_steps：节点级状态、耗时、脱敏摘要；
--   3) ai_review_logs 增加 decision / decision_reason / workflow_run_id。
-- 说明：
--   - state_json 只存脱敏摘要，不存原始票据/证件/完整 Prompt；
--   - review_log_id 删除时置空，保留 workflow 审计；
--   - 执行后同步 app/sql/schema.sql。
-- ============================================================

CREATE TABLE IF NOT EXISTS `workflow_runs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '工作流运行ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `review_log_id` bigint(20) DEFAULT NULL COMMENT '最终预审日志ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT '运行状态：running/success/failed/cancelled',
  `current_node` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '当前/最后执行节点',
  `engine` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'langgraph' COMMENT '编排引擎',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT 'Prompt版本',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '模型名',
  `policy_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '制度版本',
  `state_json` json DEFAULT NULL COMMENT '脱敏状态摘要',
  `decision` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '建议决策',
  `decision_reason` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '决策说明',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `started_at` datetime DEFAULT NULL COMMENT '开始时间',
  `finished_at` datetime DEFAULT NULL COMMENT '结束时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_workflow_runs_expense_id` (`expense_id`),
  KEY `ix_workflow_runs_review_log_id` (`review_log_id`),
  KEY `ix_workflow_runs_status` (`status`),
  CONSTRAINT `fk_workflow_runs_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_workflow_runs_review_log` FOREIGN KEY (`review_log_id`) REFERENCES `ai_review_logs` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI预审工作流运行';

CREATE TABLE IF NOT EXISTS `workflow_steps` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '节点记录ID',
  `run_id` bigint(20) NOT NULL COMMENT '工作流运行ID',
  `node` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '节点名',
  `attempt` int(11) NOT NULL DEFAULT '1' COMMENT '第几次尝试',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT '节点状态',
  `latency_ms` int(11) DEFAULT NULL COMMENT '节点耗时（毫秒）',
  `summary` json DEFAULT NULL COMMENT '脱敏输出摘要',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_workflow_steps_run_id` (`run_id`),
  KEY `ix_workflow_steps_node` (`node`),
  KEY `ix_workflow_steps_status` (`status`),
  CONSTRAINT `fk_workflow_steps_run` FOREIGN KEY (`run_id`) REFERENCES `workflow_runs` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI预审工作流节点';

ALTER TABLE `ai_review_logs`
  ADD COLUMN `decision` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '建议决策' AFTER `retrieval_meta`,
  ADD COLUMN `decision_reason` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '建议决策说明' AFTER `decision`,
  ADD COLUMN `workflow_run_id` bigint(20) DEFAULT NULL COMMENT '工作流运行ID' AFTER `decision_reason`,
  ADD KEY `ix_ai_review_logs_workflow_run_id` (`workflow_run_id`);
