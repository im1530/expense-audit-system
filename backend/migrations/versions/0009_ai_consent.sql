-- ============================================================
-- 0009：AI 数据处理授权（Phase F）
-- 背景：
--   凭证识别会把发票影像发送给 DeepSeek，属于个人信息/财务数据处理，
--   需要用户显式授权并记录授权版本，未授权时跳过凭证识别。
-- 变更：
--   users 增加 ai_consent_at / ai_consent_version。
-- 说明：
--   1) 两个字段均可空，NULL 表示未授权；
--   2) 仅记录时间与条款版本，不记录 IP、设备等额外个人信息；
--   3) 执行后同步 app/sql/schema.sql。
-- ============================================================

ALTER TABLE `users`
  ADD COLUMN `ai_consent_at` datetime DEFAULT NULL COMMENT 'AI数据处理授权时间' AFTER `token_version`,
  ADD COLUMN `ai_consent_version` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'AI数据处理授权版本' AFTER `ai_consent_at`;
