-- ============================================================
-- 0005：凭证识别记录表（Phase C）
-- 背景：
--   报销凭证不止增值税发票，还包括行程单、火车票、打车行程、
--   支付截图、收据等。AI 预审需要知道"这张附件是什么、金额/日期/对方是谁、
--   证据强度如何、识别是否成功"，并且每次识别都要可追溯。
-- 变更：
--   新增 document_extractions：公共字段单独建列，类型专属字段放 JSON。
-- 说明：
--   - 每次识别追加一行，不覆盖历史；附件/明细删除时级联清理；
--   - amount/document_date/identifier 建索引，供比对与重复凭证查询；
--   - 执行后请同步 app/sql/schema.sql 快照，保证测试库回放一致。
-- 执行：python scripts/migrate.py upgrade
-- ============================================================

CREATE TABLE IF NOT EXISTS `document_extractions` (
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
