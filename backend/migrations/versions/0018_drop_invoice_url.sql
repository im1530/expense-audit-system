-- 0018：删除已被 attachments 表替代的 expense_items.invoice_url
-- 历史 1:1 URL 不再使用；凭证统一走附件表（支持一明细多票、签名下载、软删）。
ALTER TABLE `expense_items`
  DROP COLUMN `invoice_url`;
