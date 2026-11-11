"""
隐私与留存测试（Phase F）
    覆盖：
        - AI 数据处理授权/撤回、版本失效；
        - 未授权时跳过凭证识别；
        - raw_response 默认不落库、开关打开才保存；
        - 过期附件文件与识别记录清理、raw_response 置空。
    约束：使用测试库；文件存储用替身，不写真实 uploads。
"""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.api.v1.auth import update_ai_consent
from app.config import settings
from app.models import AIReviewLog, AIReviewStatus, Attachment, DocumentExtraction
from app.models.document import DocumentExtractionStatus
from app.models.expense import ExpenseStatus
from app.schemas.ai_review import AIReviewResponse
from app.schemas.document import DocumentExtractionResponse
from app.schemas.user import AIConsentRequest
from app.services.ai_review_service import _save_outcome
from app.services.document_service import _save_result, extract_for_expense
from app.services.privacy_service import has_ai_consent
from app.agents.risk_agent import ReviewOutcome
from app.services import retention_service as purge
from tests.support import BaseTestCase, make_expense



class ConsentTest(BaseTestCase, unittest.TestCase):
    """AI 数据处理授权"""

    def test_grant_and_revoke_consent(self):
        """授权写入时间/版本，撤回后清空"""
        user = self.fx["users"]["employee"]
        with patch.object(settings, "DOCUMENT_AI_CONSENT_REQUIRED", True):
            self.assertFalse(has_ai_consent(user))

            granted = update_ai_consent(
                AIConsentRequest(consent=True),
                db=self.db,
                current_user=user,
            )
            self.assertIsNotNone(granted.ai_consent_at)
            self.assertEqual(granted.ai_consent_version, settings.DOCUMENT_AI_CONSENT_VERSION)
            self.assertTrue(has_ai_consent(granted))

            revoked = update_ai_consent(
                AIConsentRequest(consent=False),
                db=self.db,
                current_user=user,
            )
            self.assertIsNone(revoked.ai_consent_at)
            self.assertIsNone(revoked.ai_consent_version)
            self.assertFalse(has_ai_consent(revoked))

    def test_old_consent_version_invalid(self):
        """授权版本变化后旧授权失效"""
        user = self.fx["users"]["employee"]
        user.ai_consent_at = datetime.now()
        user.ai_consent_version = "old"
        with patch.object(settings, "DOCUMENT_AI_CONSENT_REQUIRED", True):
            self.assertFalse(has_ai_consent(user))

    def test_extract_skips_without_consent(self):
        """未授权：不读取附件、不调用识别"""
        expense = SimpleNamespace(
            user=SimpleNamespace(ai_consent_at=None, ai_consent_version=None),
            items=[],
        )
        with (
            patch.object(settings, "DOCUMENT_AI_CONSENT_REQUIRED", True),
            patch("app.services.document_service._load_expense", return_value=expense),
            patch("app.services.document_service.extract_attachment") as extract,
        ):
            result = extract_for_expense(MagicMock(), 1)
        self.assertEqual(result, [])
        extract.assert_not_called()



class RawResponseTest(unittest.TestCase):
    """raw_response 保存开关"""

    def make_outcome(self) -> ReviewOutcome:
        """构造带原始返回的风险结果"""
        from decimal import Decimal
        return ReviewOutcome(
            risk_score=Decimal("10"),
            risk_level="low",
            summary="ok",
            findings=[],
            policy_citations=[],
            is_fallback=False,
            model="fake",
            prompt_version="v1",
            latency_ms=1,
            prompt_tokens=1,
            completion_tokens=1,
            raw_content='{"secret": "invoice"}',
        )

    def test_ai_review_raw_response_disabled(self):
        """默认不保存 AI 原始返回"""
        log = AIReviewLog(id=1, expense_id=1, status=AIReviewStatus.RUNNING)
        with patch.object(settings, "AI_REVIEW_RAW_RESPONSE_ENABLED", False):
            _save_outcome(MagicMock(), log, self.make_outcome())
        self.assertIsNone(log.raw_response)

    def test_ai_review_raw_response_enabled(self):
        """开关打开才保存"""
        log = AIReviewLog(id=1, expense_id=1, status=AIReviewStatus.RUNNING)
        with patch.object(settings, "AI_REVIEW_RAW_RESPONSE_ENABLED", True):
            _save_outcome(MagicMock(), log, self.make_outcome())
        self.assertEqual(log.raw_response, '{"secret": "invoice"}')

    def test_document_raw_response_disabled(self):
        """凭证识别默认不保存原始返回"""
        record = DocumentExtraction()
        result = SimpleNamespace(
            document_type="vat_invoice",
            evidence_level="strong",
            amount=None,
            document_date=None,
            counterparty="",
            identifier="",
            extra={},
            confidence=0.9,
            needs_manual_review=False,
            model="fake",
            prompt_version="v1",
            latency_ms=1,
            prompt_tokens=1,
            completion_tokens=1,
            raw_content='{"secret": "image"}',
        )
        with patch.object(settings, "DOCUMENT_RAW_RESPONSE_ENABLED", False):
            _save_result(MagicMock(), record, result)
        self.assertIsNone(record.raw_response)

    def test_raw_response_not_exposed_by_api_models(self):
        """API 响应模型不包含 raw_response，避免原文直接外发"""
        self.assertNotIn("raw_response", AIReviewResponse.model_fields)
        self.assertNotIn("raw_response", DocumentExtractionResponse.model_fields)



class PurgeTest(BaseTestCase, unittest.TestCase):
    """过期数据清理"""

    def make_old_attachment(self) -> tuple[Attachment, DocumentExtraction]:
        """造一个 100 天前的附件与识别记录"""
        user = self.fx["users"]["employee"]
        expense = make_expense(self.db, user, status=ExpenseStatus.PENDING)
        item = expense.items[0]
        attachment = Attachment(
            expense_item_id=item.id,
            file_name="old.pdf",
            file_path="invoices/old.pdf",
            content_type="application/pdf",
            file_size=10,
            uploaded_by=user.id,
            created_at=datetime.now() - timedelta(days=100),
        )
        self.db.add(attachment)
        self.db.commit()
        self.db.refresh(attachment)

        extraction = DocumentExtraction(
            attachment_id=attachment.id,
            expense_item_id=item.id,
            status=DocumentExtractionStatus.SUCCESS,
            trigger_source="manual",
            provider="deepseek",
            model="fake",
            prompt_version="v1",
            raw_response="secret",
            created_at=datetime.now() - timedelta(days=100),
        )
        self.db.add(extraction)
        self.db.commit()
        return attachment, extraction

    def test_purge_attachments_removes_file_and_extraction(self):
        """过期附件：删除文件、删除识别记录、元数据软删"""
        attachment, _ = self.make_old_attachment()
        storage = MagicMock()

        with patch.object(purge, "get_storage", return_value=storage):
            handled = purge.purge_attachments(
                self.db,
                cutoff=datetime.now() - timedelta(days=90),
                limit=10,
                dry_run=False,
            )

        self.assertEqual(handled, 1)
        storage.delete.assert_called_once_with("invoices/old.pdf")
        self.assertEqual(
            self.db.query(Attachment).filter(Attachment.id == attachment.id).count(),
            0,
        )
        self.assertEqual(
            self.db.query(DocumentExtraction).filter(
                DocumentExtraction.attachment_id == attachment.id
            ).count(),
            0,
        )

    def test_purge_raw_response_clears_old_rows(self):
        """过期 raw_response 置空"""
        expense = make_expense(self.db, self.fx["users"]["employee"], status=ExpenseStatus.PENDING)
        log = AIReviewLog(
            expense_id=expense.id,
            status=AIReviewStatus.SUCCESS,
            raw_response="secret",
            created_at=datetime.now() - timedelta(days=60),
        )
        self.db.add(log)
        self.db.commit()

        handled = purge.purge_raw_responses(
            self.db,
            cutoff=datetime.now() - timedelta(days=30),
            limit=10,
            dry_run=False,
        )

        self.assertEqual(handled, 1)
        self.db.refresh(log)
        self.assertIsNone(log.raw_response)
