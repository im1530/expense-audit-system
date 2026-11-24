-- 0020：单据风险结论增加来源，区分 AI 判断与规则兜底
ALTER TABLE `expenses`
  ADD COLUMN `risk_source` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL
    COMMENT '风险结论来源：ai/rules' AFTER `risk_level`;

-- 历史数据：按该单最新一条非 failed 预审日志回填
UPDATE `expenses` e
SET e.`risk_source` = (
  SELECT CASE WHEN l.`is_fallback` = 1 THEN 'rules' ELSE 'ai' END
  FROM `ai_review_logs` l
  WHERE l.`expense_id` = e.`id`
    AND l.`status` <> 'failed'
  ORDER BY l.`id` DESC
  LIMIT 1
)
WHERE e.`risk_source` IS NULL
  AND e.`risk_level` IS NOT NULL;
