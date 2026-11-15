"""
凭证识别服务测试（离线，不联网；使用测试库）
    覆盖：识别成功落库、缓存复用、force 重跑、预处理失败降级、与明细比对生成风险。
"""
import io
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.agents.document_agent import DocumentExtractionResult
from app.config import settings
from app.models import Attachment, DocumentExtraction, DocumentExtractionStatus, ExpenseItem
from app.models.expense import ExpenseStatus
from app.services.document_preprocess import DocumentPreprocessError, PreparedImage
from app.services.document_service import (
    collect_document_findings,
    extract_attachment,
)
from tests.support import BaseTestCase, make_expense



# 假存储：不读真实磁盘
class FakeStorage:
    """返回固定字节的存储"""

    def open(self, relative_path: str):
        """
        打开文件
            :param relative_path: 相对路径
            :return: 字节流
        """
        return io.BytesIO(b"fake-image-bytes")



# 假 Agent：返回固定识别结果
class FakeDocumentAgent:
    """测试用凭证识别 Agent"""

    calls = 0                                          # 类级调用计数

    def __init__(self, *args, **kwargs):
        """忽略构造参数"""

    async def extract(self, images, item) -> DocumentExtractionResult:
        """
        返回固定结果
            :param images: 图片列表
            :param item: 明细上下文
            :return: DocumentExtractionResult
        """
        FakeDocumentAgent.calls += 1                   # 记录调用
        return DocumentExtractionResult(
            document_type="vat_invoice",
            evidence_level="strong",
            amount=Decimal("100.00"),
            document_date=date(2026, 11, 1),
            counterparty="测试商户",
            identifier="INV-001",
            extra={"buyer_name": "示例科技有限公司", "buyer_tax_id": "91440101MA5TEST001"},
            confidence=0.98,
            needs_manual_review=False,
            summary="测试发票",
            model=settings.DEEPSEEK_MODEL,             # 与缓存查询使用的模型一致
            prompt_version=settings.DOCUMENT_PROMPT_VERSION,  # 与缓存键使用的版本一致
            latency_ms=10,
            prompt_tokens=100,
            completion_tokens=20,
            raw_content='{"ok": true}',
        )



class DocumentServiceTest(BaseTestCase, unittest.TestCase):
    """凭证识别落库与缓存"""

    def setUp(self):
        """每个用例重置调用计数"""
        super().setUp()
        FakeDocumentAgent.calls = 0

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

    def test_extract_success_and_cache(self):
        """首次识别落库，第二次命中缓存"""
        expense, item, attachment = self.make_attachment()
        prepared = [PreparedImage(b"fake", "image/jpeg", 100, 100)]
        with patch("app.services.document_service.get_storage", return_value=FakeStorage()), \
                patch("app.services.document_service.prepare_attachment", return_value=prepared), \
                patch("app.services.document_service.DocumentAgent", FakeDocumentAgent):
            first = extract_attachment(self.db, attachment, item)
            second = extract_attachment(self.db, attachment, item)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(FakeDocumentAgent.calls, 1)
        self.assertEqual(first.extraction.status, DocumentExtractionStatus.SUCCESS)
        self.assertEqual(first.extraction.amount, Decimal("100.00"))
        self.assertEqual(first.extraction.identifier, "INV-001")
        self.assertEqual(first.extraction.evidence_level, "strong")

    def test_force_creates_new_record(self):
        """force=True 忽略缓存新建记录"""
        expense, item, attachment = self.make_attachment()
        prepared = [PreparedImage(b"fake", "image/jpeg", 100, 100)]
        with patch("app.services.document_service.get_storage", return_value=FakeStorage()), \
                patch("app.services.document_service.prepare_attachment", return_value=prepared), \
                patch("app.services.document_service.DocumentAgent", FakeDocumentAgent):
            first = extract_attachment(self.db, attachment, item)
            second = extract_attachment(self.db, attachment, item, force=True)

        self.assertTrue(second.created)
        self.assertNotEqual(first.extraction.id, second.extraction.id)
        self.assertEqual(FakeDocumentAgent.calls, 2)

    def test_preprocess_failure_falls_back(self):
        """预处理失败：记录 fallback，不影响调用方"""
        expense, item, attachment = self.make_attachment()
        with patch("app.services.document_service.get_storage", return_value=FakeStorage()), \
                patch(
                    "app.services.document_service.prepare_attachment",
                    side_effect=DocumentPreprocessError("无法识别", code="unreadable"),
                ):
            result = extract_attachment(self.db, attachment, item)

        self.assertTrue(result.created)
        self.assertEqual(result.extraction.status, DocumentExtractionStatus.FALLBACK)
        self.assertTrue(result.extraction.needs_manual_review)
        self.assertIn("无法识别", result.extraction.error_message or "")

    def test_collect_findings_amount_short(self):
        """识别金额低于明细金额 → high 风险"""
        expense, item, attachment = self.make_attachment()
        record = DocumentExtraction(
            attachment_id=attachment.id,
            expense_item_id=item.id,
            status=DocumentExtractionStatus.SUCCESS,
            document_type="vat_invoice",
            evidence_level="strong",
            amount=Decimal("50.00"),
            document_date=date(2026, 11, 1),
            identifier="INV-001",
            extracted_fields={},
            confidence=Decimal("0.95"),
            needs_manual_review=False,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)

        findings = collect_document_findings(self.db, expense.id, [record])
        self.assertTrue(any(item["level"] == "high" and "低于" in item["message"] for item in findings))


if __name__ == "__main__":
    unittest.main()
