-- ============================================================
-- 0010：成本统计（Phase F）
-- 背景：
--   P0 要求按节点统计 token/费用并设置预算上限；原有表只有整单 token，
--   没有费用字段，也没有节点级用量。
-- 变更：
--   1) ai_review_logs 增加 cost；
--   2) workflow_runs 增加 prompt_tokens / completion_tokens / cost；
--   3) workflow_steps 增加 prompt_tokens / completion_tokens / cost。
-- 说明：
--   - 费用按配置单价估算（元），单价未配置时为 0；
--   - token 与费用只用于成本控制与审计，不参与风险判定；
--   - 执行后同步 app/sql/schema.sql。
-- ============================================================

ALTER TABLE `ai_review_logs`
  ADD COLUMN `cost` decimal(12,6) DEFAULT NULL COMMENT '本次预审估算费用（元）' AFTER `completion_tokens`;

ALTER TABLE `workflow_runs`
  ADD COLUMN `prompt_tokens` int(11) DEFAULT 0 COMMENT '全流程输入token合计' AFTER `policy_version`,
  ADD COLUMN `completion_tokens` int(11) DEFAULT 0 COMMENT '全流程输出token合计' AFTER `prompt_tokens`,
  ADD COLUMN `cost` decimal(12,6) DEFAULT 0 COMMENT '全流程估算费用（元）' AFTER `completion_tokens`;

ALTER TABLE `workflow_steps`
  ADD COLUMN `prompt_tokens` int(11) DEFAULT 0 COMMENT '本节点输入token' AFTER `latency_ms`,
  ADD COLUMN `completion_tokens` int(11) DEFAULT 0 COMMENT '本节点输出token' AFTER `prompt_tokens`,
  ADD COLUMN `cost` decimal(12,6) DEFAULT 0 COMMENT '本节点估算费用（元）' AFTER `completion_tokens`;
