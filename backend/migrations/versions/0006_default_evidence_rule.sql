-- ============================================================
-- 0006：默认"弱凭证金额提醒"规则（Phase C）
-- 背景：
--   document_service 从 rules 表读取 check_evidence_strength 配置；
--   没有该规则时回落到 config.py 默认值。为了让管理端能看到并调整阈值，
--   已有环境升级时补一条默认规则。
-- 说明：
--   - 新环境执行 init_data.sql 时已经包含同名规则，本迁移用 NOT EXISTS 跳过；
--   - 阈值可按公司实际在管理端修改，不影响历史识别记录。
-- 执行：python scripts/migrate.py upgrade
-- ============================================================

INSERT INTO `rules` (`name`, `rule_type`, `condition`, `action`, `config`, `is_active`, `created_at`)
SELECT
  '弱凭证金额提醒',
  'check_evidence_strength',
  '支付截图等弱凭证金额超过免复核阈值时提示人工复核',
  'review',
  '{"weak_max_amount": 200, "medium_max_amount": 1000, "category_overrides": {"transportation": 300}}',
  1,
  NOW()
FROM DUAL
WHERE NOT EXISTS (
  SELECT 1 FROM `rules` WHERE `name` = '弱凭证金额提醒'
);
