-- ============================================================
-- 0008：LangGraph 检查点持久化（Phase F）
-- 背景：
--   Phase D 使用 MemorySaver，进程重启/worker 重建后检查点丢失，工作流无法续跑；
--   本迁移把 InMemorySaver 的 storage / blobs / writes 三块结构落到 MySQL。
-- 变更：
--   1) workflow_checkpoints：检查点主体与元数据；
--   2) workflow_checkpoint_blobs：按通道+版本保存状态值；
--   3) workflow_checkpoint_writes：节点 pending writes，用于故障恢复。
-- 说明：
--   - 三张表不建外键，避免与 workflow_runs 删除顺序耦合；
--   - blob 使用 LONGBLOB，禁止存放原始发票图片；
--   - 执行后同步 app/sql/schema.sql。
-- ============================================================

CREATE TABLE IF NOT EXISTS `workflow_checkpoints` (
  `thread_id` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'LangGraph线程ID',
  `checkpoint_ns` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '检查点命名空间',
  `checkpoint_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '检查点ID',
  `parent_checkpoint_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '父检查点ID',
  `checkpoint_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '检查点序列化类型',
  `checkpoint_blob` longblob NOT NULL COMMENT '检查点序列化数据',
  `metadata_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '元数据序列化类型',
  `metadata_blob` longblob NOT NULL COMMENT '元数据序列化数据',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`thread_id`,`checkpoint_ns`,`checkpoint_id`),
  KEY `ix_workflow_checkpoints_parent` (`parent_checkpoint_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='LangGraph检查点';

CREATE TABLE IF NOT EXISTS `workflow_checkpoint_blobs` (
  `thread_id` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'LangGraph线程ID',
  `checkpoint_ns` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '检查点命名空间',
  `channel` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通道名',
  `version` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通道版本号',
  `type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '序列化类型',
  `blob` longblob NOT NULL COMMENT '序列化后的通道值',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`thread_id`,`checkpoint_ns`,`channel`,`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='LangGraph检查点通道值';

CREATE TABLE IF NOT EXISTS `workflow_checkpoint_writes` (
  `thread_id` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'LangGraph线程ID',
  `checkpoint_ns` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '检查点命名空间',
  `checkpoint_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '检查点ID',
  `task_id` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '任务ID',
  `idx` int(11) NOT NULL COMMENT '写入序号',
  `channel` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '目标通道',
  `type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '序列化类型',
  `blob` longblob NOT NULL COMMENT '序列化后的写入值',
  `task_path` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '任务路径',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`thread_id`,`checkpoint_ns`,`checkpoint_id`,`task_id`,`idx`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='LangGraph检查点写入';
