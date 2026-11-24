-- 0021：凭证号查重相关索引
-- document_extractions.identifier 已有索引；这里补 expense_items.invoice_no。
ALTER TABLE `expense_items`
  ADD KEY `ix_expense_items_invoice_no` (`invoice_no`);
