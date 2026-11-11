-- ============================================================
-- 0011：运维任务审计（Phase F）
-- 背景：
--   保留期清理、僵尸 run 收尾都是无人值守的定时任务，出问题没人看到；
--   每次执行需要落一条记录，便于事后确认"跑没跑、处理了多少、失败原因"。
-- 变更：
--   新增 maintenance_runs 表（job / status / 起止时间 / summary / error_message）。
-- 说明：
--   - summary 只放数量与参数，不放文件名、票面内容等敏感信息；
--   - 与业务表分离，避免运维记录混进审批/预审审计；
--   - 执行后同步 app/sql/schema.sql。
-- ============================================================

CREATE TABLE IF NOT EXISTS `maintenance_runs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '运维任务记录ID',
  `job` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '任务名：retention_purge / workflow_recover 等',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT '执行状态：running/success/failed/skipped',
  `started_at` datetime NOT NULL COMMENT '开始时间',
  `finished_at` datetime DEFAULT NULL COMMENT '结束时间',
  `summary` json DEFAULT NULL COMMENT '执行汇总（数量与参数，不含敏感内容）',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_maintenance_runs_job` (`job`),
  KEY `ix_maintenance_runs_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运维任务执行记录';
