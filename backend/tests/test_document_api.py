"""
凭证识别接口测试（离线，不联网；直接调用路由函数）
    覆盖：触发识别返回落库记录、查询最新识别结果、附件不存在。
"""
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1.attachment import extract_invoice, get_invoice_extraction
from app.models import Attachment, DocumentExtraction, DocumentExtractionStatus, ExpenseItem
from app.models.expense import ExpenseStatus
from app.schemas.document import DocumentExtractionResponse
from app.services.document_service import DocumentRunResult
from tests.support import BaseTestCase, make_expense



class DocumentApiTest(BaseTestCase, unittest.TestCase):
    """凭证识别接口"""

    def make_attachment(self):
        """
        造一张待审批单据、明细与附件
            :return: (expense, item, attachment)
        """
        employee = self.fx["users"]["employee"]
        expense = make_expense(
            self.db,
            employee,
            status=ExpenseStatus.PENDING,
            flow_id=self.fx["flows"]["mgr"].id,
            amount=Decimal("100.00"),
            category_id=self.fx["categories"]["rail"].id,
        )
        item = self.db.query(ExpenseItem).filter_by(expense_id=expense.id).first()
        attachment = Attachment(
            expense_item_id=item.id,
            file_name="park.jpg",
            file_path="invoices/test/park.jpg",
            content_type="image/jpeg",
            file_size=16,
            uploaded_by=employee.id,
            is_deleted=False,
        )
        self.db.add(attachment)
        self.db.commit()
        self.db.refresh(attachment)
        return expense, item, attachment

    def make_record(self, attachment, item) -> DocumentExtraction:
        """
        构造一条识别记录（不落库）
            :param attachment: 附件
            :param item: 明细
            :return: DocumentExtraction
        """
        return DocumentExtraction(
            id=1,
            attachment_id=attachment.id,
            expense_item_id=item.id,
            status=DocumentExtractionStatus.SUCCESS,
            document_type="payment_screenshot",
            evidence_level="weak",
            amount=Decimal("18.00"),
            document_date=date(2026, 11, 2),
            identifier="ORDER-1",
            extracted_fields={"payment_method": "微信支付"},
            confidence=Decimal("0.990"),
            needs_manual_review=False,
            provider="deepseek",
            model="deepseek-flash",
            prompt_version="document_extract_v1",
            created_at=datetime.now(),
        )

    def test_extract_returns_record(self):
        """触发识别：返回识别记录"""
        _expense, item, attachment = self.make_attachment()
        record = self.make_record(attachment, item)
        with patch(
            "app.api.v1.attachment.extract_attachment",
            return_value=DocumentRunResult(extraction=record, created=True),
        ):
            result = extract_invoice(
                attachment_id=attachment.id,
                force=False,
                db=self.db,
                current_user=self.fx["users"]["employee"],
            )
        self.assertEqual(result.document_type, "payment_screenshot")
        self.assertEqual(result.amount, Decimal("18.00"))

    def test_get_latest_returns_record(self):
        """查询最新识别结果"""
        _expense, item, attachment = self.make_attachment()
        record = self.make_record(attachment, item)
        with patch(
            "app.api.v1.attachment.get_latest_extraction",
            return_value=record,
        ):
            result = get_invoice_extraction(
                attachment_id=attachment.id,
                db=self.db,
                current_user=self.fx["users"]["employee"],
            )
        self.assertEqual(result.identifier, "ORDER-1")
        self.assertEqual(result.evidence_level, "weak")

    def test_response_model_handles_null_fields(self):
        """降级记录的 extracted_fields 为 NULL 时响应归一成空字典"""
        _expense, item, attachment = self.make_attachment()
        record = self.make_record(attachment, item)
        record.extracted_fields = None
        response = DocumentExtractionResponse.model_validate(record)
        self.assertEqual(response.extracted_fields, {})

    def test_missing_attachment_returns_404(self):
        """附件不存在：404"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            get_invoice_extraction(
                attachment_id=999999,
                db=self.db,
                current_user=self.fx["users"]["employee"],
            )
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
