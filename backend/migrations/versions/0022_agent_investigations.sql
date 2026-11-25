-- 0022：AI 核查 Agent（ReAct 工具调用）记录表
CREATE TABLE `agent_investigations` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '核查记录ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `created_by` bigint(20) DEFAULT NULL COMMENT '触发人ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT 'running/success/failed',
  `question` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '核查问题',
  `report` text COLLATE utf8mb4_unicode_ci COMMENT '核查结论',
  `trace` json DEFAULT NULL COMMENT '工具调用轨迹',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '模型名',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Prompt版本',
  `latency_ms` int(11) DEFAULT NULL COMMENT '耗时毫秒',
  `prompt_tokens` int(11) DEFAULT NULL COMMENT '输入token',
  `completion_tokens` int(11) DEFAULT NULL COMMENT '输出token',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `ix_agent_investigations_expense_id` (`expense_id`),
  KEY `ix_agent_investigations_created_by` (`created_by`),
  CONSTRAINT `fk_agent_investigations_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_agent_investigations_user` FOREIGN KEY (`created_by`) REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI核查记录';
