-- ============================================================
-- 0003：AI 预审审计表（Phase A）
-- 背景：
--   风险评审 Agent 每次预审都要可追溯：用了哪个模型、哪版 Prompt、
--   耗时/ token、是否降级、原始返回是什么。只写 expenses.risk_* 会丢历史。
-- 约定：
--   - status：pending/running/success/fallback/failed；
--   - risk_level：low/mid/high（与规则风险的 warn/review/block 分开）；
--   - raw_response 仅审计/排障使用，接口不直接透出；
--   - 删报销单时级联删除预审记录；删用户时触发人置空，保留审计。
-- 执行：python scripts/migrate.py upgrade
-- ============================================================

CREATE TABLE IF NOT EXISTS `ai_review_logs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '预审记录ID',
  `expense_id` bigint(20) NOT NULL COMMENT '被审报销单ID',
  `triggered_by` bigint(20) DEFAULT NULL COMMENT '触发人ID',
  `trace_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '链路追踪ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/fallback/failed',
  `trigger_source` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'manual' COMMENT '触发来源：manual/submit',
  `provider` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'deepseek' COMMENT 'LLM提供方',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '模型名',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT 'Prompt版本号',
  `risk_score` decimal(5,2) DEFAULT NULL COMMENT 'AI风险分数0~100',
  `risk_level` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'AI风险等级：low/mid/high',
  `summary` text COLLATE utf8mb4_unicode_ci COMMENT 'AI总结',
  `findings` json DEFAULT NULL COMMENT '风险点列表',
  `policy_citations` json DEFAULT NULL COMMENT '制度引用',
  `raw_response` mediumtext COLLATE utf8mb4_unicode_ci COMMENT '模型原始返回（排障用）',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `is_fallback` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否规则兜底',
  `latency_ms` int(11) DEFAULT NULL COMMENT '耗时（毫秒）',
  `prompt_tokens` int(11) DEFAULT NULL COMMENT '输入token数',
  `completion_tokens` int(11) DEFAULT NULL COMMENT '输出token数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_ai_review_logs_expense_id` (`expense_id`),
  KEY `ix_ai_review_logs_triggered_by` (`triggered_by`),
  KEY `ix_ai_review_logs_status` (`status`),
  KEY `ix_ai_review_logs_trace_id` (`trace_id`),
  KEY `idx_ai_review_expense_created` (`expense_id`, `created_at`),
  CONSTRAINT `fk_ai_review_logs_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_ai_review_logs_user` FOREIGN KEY (`triggered_by`) REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI预审审计日志';
