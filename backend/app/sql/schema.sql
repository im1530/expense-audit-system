-- ============================================================
-- 数据库结构：expense_audit（与当前开发库保持一致）
-- 生成时间：2026-11-04
-- 用法：mysql -uroot -p < schema.sql
-- 说明：
--   1) departments.manager_id → users.id 与 users.department_id → departments.id 互相引用，
--      建表阶段先关闭外键检查（SET FOREIGN_KEY_CHECKS = 0），建完再打开；
--   2) 全部表统一 InnoDB + utf8mb4，金额用 decimal(12,2)，状态用枚举或短字符串；
--   3) 枚举列统一 native_enum=False（varchar 落库），改取值只需改代码，不用 ALTER 表。
-- ============================================================

CREATE DATABASE IF NOT EXISTS `expense_audit` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `expense_audit`;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ------------------------------------------------------------
-- 审批流定义表：levels 存 JSON，形如 [{"level":1,"role":"manager"}, ...]
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `approval_flows`;
CREATE TABLE `approval_flows` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '审批流ID',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '审批流名称',
  `description` text COLLATE utf8mb4_unicode_ci COMMENT '审批流说明',
  `levels` json DEFAULT NULL COMMENT '各级审批配置',
  `is_active` tinyint(1) DEFAULT NULL COMMENT '是否启用',
  `is_default` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否默认流',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_approval_flows_id` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 审批记录表：既充当待办（status=pending），也充当审批轨迹
-- approver_id 可空：提交时生成的待办还没有处理人
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `approvals`;
CREATE TABLE `approvals` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '审批记录ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `approver_id` bigint(20) DEFAULT NULL COMMENT '审批人ID，待审批/已撤回/已作废等未处理状态可为空，审批操作后回填',
  `flow_id` bigint(20) NOT NULL COMMENT '审批流ID',
  `status` enum('pending','approved','rejected','recalled','cancelled','obsoleted') COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '审批状态：待审批/已通过/已驳回/已撤回/已撤销/已作废',
  `level` int(11) NOT NULL COMMENT '所属审批层级',
  `comment` text COLLATE utf8mb4_unicode_ci COMMENT '审批意见',
  `acted_at` datetime DEFAULT NULL COMMENT '处理时间（审批/撤回/撤销时回填）',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_approvals_flow_id` (`flow_id`),
  KEY `ix_approvals_status` (`status`),
  KEY `ix_approvals_id` (`id`),
  KEY `ix_approvals_expense_id` (`expense_id`),
  KEY `ix_approvals_approver_id` (`approver_id`),
  CONSTRAINT `approvals_ibfk_1` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`),
  CONSTRAINT `approvals_ibfk_2` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`),
  CONSTRAINT `approvals_ibfk_3` FOREIGN KEY (`flow_id`) REFERENCES `approval_flows` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 附件表：报销明细下的发票/凭证（一张明细可多张票）
-- 文件本身存磁盘/对象存储，这里只存相对路径；is_deleted 为软删标记
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `attachments`;
CREATE TABLE `attachments` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '附件ID',
  `expense_item_id` bigint(20) NOT NULL COMMENT '所属报销明细ID',
  `file_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '原始文件名(展示用)',
  `file_path` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '相对存储路径',
  `content_type` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'MIME类型',
  `file_size` bigint(20) NOT NULL COMMENT '文件字节数',
  `uploaded_by` bigint(20) NOT NULL COMMENT '上传人ID',
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否已删除(软删)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_attachments_item` (`expense_item_id`),
  KEY `ix_attachments_uploader` (`uploaded_by`),
  CONSTRAINT `fk_attachments_item` FOREIGN KEY (`expense_item_id`) REFERENCES `expense_items` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_attachments_user` FOREIGN KEY (`uploaded_by`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='报销发票/附件表';

-- ------------------------------------------------------------
-- 规则命中记录表：每次提交/重新提交先清空再写入，供审批页展示与命中率统计。
-- rule_id 不建外键（规则删除后仍保留名称快照）；单据删除时级联清理。
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `expense_rule_hits`;
CREATE TABLE `expense_rule_hits` (
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

-- ------------------------------------------------------------
-- 费用类别表（小类）：expense_type 对应 ExpenseType 大类
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `categories`;
CREATE TABLE `categories` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '类别ID',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '类别名称',
  `code` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '类别代码',
  `max_amount` decimal(12,2) DEFAULT NULL COMMENT '单次最高限额',
  `description` text COLLATE utf8mb4_unicode_ci COMMENT '类别说明',
  `expense_type` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '所属费用性质大类',
  `is_active` tinyint(1) DEFAULT NULL COMMENT '是否启用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`),
  KEY `ix_categories_id` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 部门表：manager_id 指向本部门负责人（以该字段为准，users.role=manager 只是身份标签）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `departments`;
CREATE TABLE `departments` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `name` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '部门名称',
  `description` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '部门备注',
  `manager_id` bigint(20) DEFAULT NULL COMMENT '部门负责人(经理)ID',
  `is_active` tinyint(1) DEFAULT '1' COMMENT '是否启用',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_departments_name` (`name`),
  KEY `idx_manager` (`manager_id`),
  CONSTRAINT `departments_ibfk_1` FOREIGN KEY (`manager_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 报销明细表：总金额由明细汇总而来，明细变动后必须重算主单 total_amount
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `expense_items`;
CREATE TABLE `expense_items` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '报销项目ID',
  `expense_id` bigint(20) NOT NULL COMMENT '所属报销单ID',
  `category_id` bigint(20) DEFAULT NULL COMMENT '费用类别ID',
  `description` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '费用说明',
  `amount` decimal(12,2) NOT NULL COMMENT '金额',
  `expense_date` date NOT NULL COMMENT '费用发生日期',
  `invoice_no` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '发票编号',
  `invoice_url` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '发票文件URL',
  `no_invoice_reason` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '无票说明(无发票时填写原因)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_expense_items_id` (`id`),
  KEY `ix_expense_items_expense_id` (`expense_id`),
  KEY `ix_expense_items_category_id` (`category_id`),
  CONSTRAINT `expense_items_ibfk_1` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`),
  CONSTRAINT `expense_items_ibfk_2` FOREIGN KEY (`category_id`) REFERENCES `categories` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 报销单主表：status 走状态机，current_level/approval_chain 记录审批进度
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `expenses`;
CREATE TABLE `expenses` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '报销单ID',
  `user_id` bigint(20) NOT NULL COMMENT '提交人ID',
  `expense_no` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '报销单号',
  `title` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '报销标题',
  `total_amount` decimal(12,2) NOT NULL COMMENT '总金额',
  `status` enum('draft','pending','approved','registered','rejected','paid','closed','cancelled') COLLATE utf8mb4_unicode_ci NOT NULL,
  `flow_id` bigint(20) DEFAULT NULL COMMENT '审批流ID',
  `approval_chain` json DEFAULT NULL COMMENT '匹配到的审批角色链',
  `current_level` int(11) NOT NULL DEFAULT '1' COMMENT '当前审批层级',
  `description` text COLLATE utf8mb4_unicode_ci COMMENT '报销说明',
  `remark` text COLLATE utf8mb4_unicode_ci COMMENT '备注',
  `submitted_at` datetime DEFAULT NULL COMMENT '提交时间',
  `approved_at` datetime DEFAULT NULL COMMENT '审批时间',
  `registered_at` datetime DEFAULT NULL COMMENT '财务登记时间',
  `registered_by` bigint(20) DEFAULT NULL COMMENT '登记财务ID',
  `register_note` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '登记备注（发给提交人）',
  `paid_at` datetime DEFAULT NULL COMMENT '支付时间',
  `confirmed_at` datetime DEFAULT NULL COMMENT '提交人确认领取时间',
  `closed_at` datetime DEFAULT NULL COMMENT '管理员强制关闭时间',
  `cancelled_at` datetime DEFAULT NULL COMMENT '撤销时间',
  `risk_score` decimal(5,2) DEFAULT NULL COMMENT '风险分数',
  `risk_level` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '风险等级',
  `ai_reviewed_result` text COLLATE utf8mb4_unicode_ci COMMENT 'AI审核结果',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_expenses_expense_no` (`expense_no`),
  KEY `ix_expenses_id` (`id`),
  KEY `ix_expenses_user_id` (`user_id`),
  KEY `ix_expenses_status` (`status`),
  KEY `idx_flow` (`flow_id`),
  KEY `idx_expenses_created_at` (`created_at`),
  KEY `idx_expenses_registered_at` (`registered_at`),
  CONSTRAINT `expenses_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`),
  CONSTRAINT `expenses_ibfk_2` FOREIGN KEY (`flow_id`) REFERENCES `approval_flows` (`id`),
  CONSTRAINT `fk_expenses_registered_by` FOREIGN KEY (`registered_by`) REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 校验规则表：当前引擎只消费 rule_type='amount' 的规则
-- config 形如 {"submitter_roles":["employee"],"min_amount":0,"max_amount":999.99,"flow_id":2}
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `rules`;
CREATE TABLE `rules` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '校验规则ID',
  `name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '规则名称',
  `rule_type` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '规则类型',
  `condition` text COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '触发条件',
  `action` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '触发动作',
  `config` json DEFAULT NULL COMMENT '规则配置',
  `is_active` tinyint(1) DEFAULT NULL COMMENT '是否启用',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_rules_rule_type` (`rule_type`),
  KEY `ix_rules_id` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 用户表：role 决定功能权限，department_id 决定数据范围
-- must_change_password：管理员建号/重置密码后置 1，用户必须先改密
-- token_version：写进 JWT，改密时 +1，使旧令牌立即失效
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  `username` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录用户名',
  `email` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '登录邮箱',
  `password_hash` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '密码哈希(不存明文)',
  `full_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '真实姓名',
  `phone` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '手机号码',
  `position` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '职位',
  `role` enum('admin','boss','finance','manager','employee') COLLATE utf8mb4_unicode_ci NOT NULL,
  `department_id` bigint(20) DEFAULT NULL COMMENT '所属部门ID',
  `is_active` tinyint(1) NOT NULL COMMENT '账号是否激活',
  `is_superuser` tinyint(1) NOT NULL COMMENT '是否为超级用户',
  `must_change_password` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否必须修改密码',
  `token_version` int(11) NOT NULL DEFAULT '0' COMMENT '令牌版本号(改密+1,旧令牌失效)',
  `ai_consent_at` datetime DEFAULT NULL COMMENT 'AI数据处理授权时间',
  `ai_consent_version` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'AI数据处理授权版本',
  `last_login_at` datetime DEFAULT NULL COMMENT '最后登录时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_users_email` (`email`),
  UNIQUE KEY `ix_users_username` (`username`),
  KEY `ix_users_id` (`id`),
  KEY `idx_dept` (`department_id`),
  CONSTRAINT `users_ibfk_1` FOREIGN KEY (`department_id`) REFERENCES `departments` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- AI 预审审计表：一次预审一行，记录模型、Prompt 版本、耗时、降级与原始返回
-- status：pending/running/success/fallback/failed
-- risk_level：low/mid/high（与规则风险的 warn/review/block 是两套体系）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `ai_review_logs`;
CREATE TABLE `ai_review_logs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '预审记录ID',
  `expense_id` bigint(20) NOT NULL COMMENT '被审报销单ID',
  `triggered_by` bigint(20) DEFAULT NULL COMMENT '触发人ID',
  `trace_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '链路追踪ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/fallback/failed',
  `trigger_source` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'manual' COMMENT '触发来源：manual/submit',
  `provider` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'deepseek' COMMENT 'LLM提供方',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '模型名',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT 'Prompt版本号',
  `policy_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '制度版本号',
  `risk_score` decimal(5,2) DEFAULT NULL COMMENT 'AI风险分数0~100',
  `risk_level` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'AI风险等级：low/mid/high',
  `summary` text COLLATE utf8mb4_unicode_ci COMMENT 'AI总结',
  `findings` json DEFAULT NULL COMMENT '风险点列表',
  `policy_citations` json DEFAULT NULL COMMENT '制度引用',
  `retrieval_meta` json DEFAULT NULL COMMENT '制度检索审计信息',
  `decision` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '建议决策：fast_track/manual_review/suggest_reject',
  `decision_reason` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '建议决策说明',
  `workflow_run_id` bigint(20) DEFAULT NULL COMMENT '工作流运行ID',
  `raw_response` mediumtext COLLATE utf8mb4_unicode_ci COMMENT '模型原始返回（排障用）',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败原因',
  `is_fallback` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否规则兜底',
  `latency_ms` int(11) DEFAULT NULL COMMENT '耗时（毫秒）',
  `prompt_tokens` int(11) DEFAULT NULL COMMENT '输入token数',
  `completion_tokens` int(11) DEFAULT NULL COMMENT '输出token数',
  `cost` decimal(12,6) DEFAULT NULL COMMENT '本次预审估算费用（元）',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_ai_review_logs_expense_id` (`expense_id`),
  KEY `ix_ai_review_logs_triggered_by` (`triggered_by`),
  KEY `ix_ai_review_logs_status` (`status`),
  KEY `ix_ai_review_logs_trace_id` (`trace_id`),
  KEY `ix_ai_review_logs_workflow_run_id` (`workflow_run_id`),
  KEY `idx_ai_review_expense_created` (`expense_id`, `created_at`),
  CONSTRAINT `fk_ai_review_logs_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_ai_review_logs_user` FOREIGN KEY (`triggered_by`) REFERENCES `users` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI预审审计日志';

-- ------------------------------------------------------------
-- 凭证识别记录表：一张附件每次识别一行，保留历史，供 AI 预审与审计复盘
-- status：pending/running/success/fallback/failed
-- evidence_level：strong/medium/weak/none（由 document_type 推导）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `document_extractions`;
CREATE TABLE `document_extractions` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '凭证识别记录ID',
  `attachment_id` bigint(20) NOT NULL COMMENT '附件ID',
  `expense_item_id` bigint(20) NOT NULL COMMENT '所属报销明细ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending' COMMENT '状态：pending/running/success/fallback/failed',
  `trigger_source` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'review' COMMENT '触发来源：review/manual',
  `provider` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'deepseek' COMMENT '模型提供方',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '模型名',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT 'Prompt版本号',
  `document_type` varchar(30) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '文档类型',
  `evidence_level` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '证据强度：strong/medium/weak/none',
  `amount` decimal(12,2) DEFAULT NULL COMMENT '票面/支付金额',
  `document_date` date DEFAULT NULL COMMENT '票面/交易日期',
  `counterparty` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '商户/平台/收款方',
  `identifier` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '发票号/订单号/流水号',
  `extracted_fields` json DEFAULT NULL COMMENT '类型专属字段',
  `confidence` decimal(4,3) DEFAULT NULL COMMENT '模型置信度0~1',
  `needs_manual_review` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否建议人工核对',
  `raw_response` mediumtext COLLATE utf8mb4_unicode_ci COMMENT '模型原始返回（审计用）',
  `error_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '失败/降级原因',
  `latency_ms` int(11) DEFAULT NULL COMMENT '调用耗时（毫秒）',
  `prompt_tokens` int(11) DEFAULT NULL COMMENT '输入token数',
  `completion_tokens` int(11) DEFAULT NULL COMMENT '输出token数',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `ix_document_extractions_attachment_id` (`attachment_id`),
  KEY `ix_document_extractions_expense_item_id` (`expense_item_id`),
  KEY `ix_document_extractions_status` (`status`),
  KEY `ix_document_extractions_identifier` (`identifier`),
  CONSTRAINT `fk_document_extractions_attachment` FOREIGN KEY (`attachment_id`) REFERENCES `attachments` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_document_extractions_item` FOREIGN KEY (`expense_item_id`) REFERENCES `expense_items` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='凭证识别记录';

-- ------------------------------------------------------------
-- AI 预审工作流（Phase D）：run 级状态 + 节点级审计
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `workflow_steps`;
DROP TABLE IF EXISTS `workflow_runs`;
CREATE TABLE `workflow_runs` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '工作流运行ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `review_log_id` bigint(20) DEFAULT NULL COMMENT '最终预审日志ID',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT '运行状态',
  `current_node` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '当前/最后执行节点',
  `engine` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'langgraph' COMMENT '编排引擎',
  `prompt_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT 'Prompt版本',
  `model` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '模型名',
  `policy_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '制度版本',
  `prompt_tokens` int(11) DEFAULT 0 COMMENT '全流程输入token合计',
  `completion_tokens` int(11) DEFAULT 0 COMMENT '全流程输出token合计',
  `cost` decimal(12,6) DEFAULT 0 COMMENT '全流程估算费用（元）',
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

CREATE TABLE `workflow_steps` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '节点记录ID',
  `run_id` bigint(20) NOT NULL COMMENT '工作流运行ID',
  `node` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '节点名',
  `attempt` int(11) NOT NULL DEFAULT '1' COMMENT '第几次尝试',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'running' COMMENT '节点状态',
  `latency_ms` int(11) DEFAULT NULL COMMENT '节点耗时（毫秒）',
  `prompt_tokens` int(11) DEFAULT 0 COMMENT '本节点输入token',
  `completion_tokens` int(11) DEFAULT 0 COMMENT '本节点输出token',
  `cost` decimal(12,6) DEFAULT 0 COMMENT '本节点估算费用（元）',
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

-- ------------------------------------------------------------
-- LangGraph 检查点（Phase F）：MySQL Checkpointer
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `workflow_checkpoint_writes`;
DROP TABLE IF EXISTS `workflow_checkpoint_blobs`;
DROP TABLE IF EXISTS `workflow_checkpoints`;
CREATE TABLE `workflow_checkpoints` (
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

CREATE TABLE `workflow_checkpoint_blobs` (
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

CREATE TABLE `workflow_checkpoint_writes` (
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

-- ------------------------------------------------------------
-- 运维任务审计（Phase F）：保留期清理等定时任务执行记录
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `maintenance_runs`;
CREATE TABLE `maintenance_runs` (
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

-- ------------------------------------------------------------
-- 结算流水（P1）：财务登记 / 撤销 / 提交人确认 / 管理员强制关闭
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `expense_settlements`;
CREATE TABLE `expense_settlements` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '结算流水ID',
  `expense_id` bigint(20) NOT NULL COMMENT '报销单ID',
  `action` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'register/cancel/confirm/force_close',
  `operator_id` bigint(20) DEFAULT NULL COMMENT '操作人ID',
  `note` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '备注/原因',
  `channel` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'bank/alipay/wechat/cash，可选',
  `transaction_no` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '流水号，可选',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_settlements_expense` (`expense_id`, `created_at`),
  UNIQUE KEY `uk_settlements_transaction_no` (`transaction_no`),
  CONSTRAINT `fk_settlements_expense` FOREIGN KEY (`expense_id`) REFERENCES `expenses` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- 站内通知：登记/撤销/确认/强制关闭
-- ------------------------------------------------------------
DROP TABLE IF EXISTS `notifications`;
CREATE TABLE `notifications` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT COMMENT '通知ID',
  `user_id` bigint(20) NOT NULL COMMENT '接收人ID',
  `type` varchar(40) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'expense_registered/registration_cancelled/expense_confirmed/force_closed',
  `title` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知标题',
  `content` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '通知内容',
  `related_type` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '关联对象类型，如 expense',
  `related_id` bigint(20) DEFAULT NULL COMMENT '关联对象ID',
  `is_read` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否已读',
  `read_at` datetime DEFAULT NULL COMMENT '已读时间',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_notifications_user_read` (`user_id`, `is_read`, `created_at`),
  CONSTRAINT `fk_notifications_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET FOREIGN_KEY_CHECKS = 1;
