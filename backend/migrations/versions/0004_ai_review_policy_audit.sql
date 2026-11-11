-- ============================================================
-- 0004：AI 预审制度 RAG 审计字段（Phase B）
-- 背景：
--   预审结果需要回答"本次引用的是哪一版制度、检索到了哪些条款、
--   哪些引用被丢弃以及为什么"，只存 policy_citations 无法复盘。
-- 变更：
--   1) policy_version：本次使用的制度版本；
--   2) retrieval_meta：检索与引用校验的审计 JSON
--      （检索条款编号、丢弃原因、未验证的 policy finding 数）。
-- 说明：
--   - 两列都允许为空/空串，兼容 Phase A 的历史记录；
--   - 执行后请同步 app/sql/schema.sql 快照，保证测试库回放一致。
-- 执行：python scripts/migrate.py upgrade
-- ============================================================

ALTER TABLE `ai_review_logs`
  ADD COLUMN `policy_version` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '' COMMENT '制度版本号' AFTER `prompt_version`,
  ADD COLUMN `retrieval_meta` json DEFAULT NULL COMMENT '制度检索审计信息' AFTER `policy_citations`;
