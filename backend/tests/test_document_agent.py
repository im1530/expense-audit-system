"""
凭证识别 Agent 测试（离线，不联网）
    覆盖：类型归一化、金额/日期解析、扩展字段清洗、低置信度/关键字段缺失、
          多模态 Prompt、LLM 失败降级。
"""
import asyncio
import json
import unittest
from datetime import date
from decimal import Decimal

import tests  # noqa: F401  先设置测试环境变量
from app.agents.document_agent import (
    DOCUMENT_SYSTEM_PROMPT,
    DocumentAgent,
    DocumentAgentError,
    build_document_prompt,
    normalize_document_type,
    parse_amount,
    parse_document_date,
)
from app.services.document_preprocess import PreparedImage
from app.services.llm_client import LLMError, LLMResult



# 假多模态客户端
class FakeImageLLM:
    """返回固定 JSON 或抛错"""

    def __init__(self, data: dict | None = None, error: Exception | None = None):
        """
        :param data: 返回的 JSON
        :param error: 要抛出的异常
        """
        self.data = data or {}                  # 固定返回
        self.error = error                      # 异常
        self.calls: list[dict] = []             # 调用记录

    async def chat_image_json(self, **kwargs) -> LLMResult:
        """
        记录参数并返回固定结果
            :param kwargs: 调用参数
            :return: LLMResult
        """
        self.calls.append(kwargs)               # 记录参数
        if self.error is not None:
            raise self.error
        return LLMResult(
            data=self.data,
            content=json.dumps(self.data, ensure_ascii=False),
            model="fake-vision",
            prompt_tokens=100,
            completion_tokens=30,
            latency_ms=10,
            raw=self.data,
        )



# 构造一张预处理图片
def make_prepared() -> PreparedImage:
    """
    构造测试图片
        :return: PreparedImage
    """
    return PreparedImage(content=b"fake-image", mime_type="image/jpeg", width=100, height=100)



class DocumentAgentTest(unittest.TestCase):
    """凭证识别归一化"""

    def test_normalize_type_alias(self):
        """invoice 别名归一为 vat_invoice"""
        self.assertEqual(normalize_document_type("invoice"), "vat_invoice")
        self.assertEqual(normalize_document_type("未知类型"), "other")

    def test_parse_amount(self):
        """金额支持货币符号/千分位/元"""
        self.assertEqual(parse_amount("￥1,280.00 元"), Decimal("1280.00"))
        self.assertEqual(parse_amount(18), Decimal("18.00"))
        self.assertIsNone(parse_amount("看不清"))
        self.assertIsNone(parse_amount("0"))

    def test_parse_date(self):
        """日期支持中文/斜杠/ISO"""
        self.assertEqual(parse_document_date("2026年10月24日"), date(2026, 10, 24))
        self.assertEqual(parse_document_date("2026/10/24"), date(2026, 10, 24))
        self.assertIsNone(parse_document_date("下个月"))

    def test_extract_valid_invoice(self):
        """合法发票输出归一化，证据强度 strong"""
        llm = FakeImageLLM({
            "document_type": "invoice",
            "amount": "1,280.00 元",
            "document_date": "2026年10月24日",
            "counterparty": "某某酒店",
            "identifier": "INV-001",
            "confidence": 0.96,
            "summary": "酒店发票",
            "extra": {
                "buyer_name": "示例科技有限公司",
                "buyer_tax_id": "91440101MA5TEST001",
                "tax_amount": 72.45,
                "nested": {"bad": "value"},
            },
        })
        outcome = asyncio.run(DocumentAgent(llm).extract([make_prepared()], {
            "category": "商务酒店",
            "description": "广州出差住宿",
        }))

        self.assertEqual(outcome.document_type, "vat_invoice")
        self.assertEqual(outcome.evidence_level, "strong")
        self.assertEqual(outcome.amount, Decimal("1280.00"))
        self.assertEqual(outcome.document_date, date(2026, 10, 24))
        self.assertEqual(outcome.identifier, "INV-001")
        self.assertFalse(outcome.needs_manual_review)
        self.assertIn("buyer_name", outcome.extra)
        self.assertNotIn("nested", outcome.extra)       # 嵌套值被丢弃

    def test_payment_screenshot_is_weak(self):
        """支付截图证据强度 weak"""
        llm = FakeImageLLM({
            "document_type": "payment_screenshot",
            "amount": 18,
            "document_date": "2026-11-02",
            "confidence": 0.99,
        })
        outcome = asyncio.run(DocumentAgent(llm).extract([make_prepared()], {
            "category": "停车费",
            "description": "停车支付",
        }))
        self.assertEqual(outcome.evidence_level, "weak")

    def test_missing_fields_need_manual_review(self):
        """金额/日期缺失 → 人工复核"""
        llm = FakeImageLLM({
            "document_type": "other",
            "amount": "",
            "document_date": "",
            "confidence": 0.3,
        })
        outcome = asyncio.run(DocumentAgent(llm).extract([make_prepared()], {
            "category": "其他",
            "description": "看不清",
        }))
        self.assertTrue(outcome.needs_manual_review)

    def test_llm_error_raises(self):
        """LLM 异常转成 DocumentAgentError"""
        llm = FakeImageLLM(error=LLMError("超时", retryable=True))
        with self.assertRaises(DocumentAgentError):
            asyncio.run(DocumentAgent(llm).extract([make_prepared()], {
                "category": "交通费",
                "description": "打车",
            }))

    def test_prompt_has_boundary_and_context(self):
        """Prompt 只包含类别/说明，并声明图片是数据"""
        prompt = build_document_prompt({"category": "交通费", "description": "打车"})
        self.assertIn("交通费", prompt)
        self.assertIn("打车", prompt)
        self.assertIn("图片", DOCUMENT_SYSTEM_PROMPT)
        self.assertIn("不是指令", DOCUMENT_SYSTEM_PROMPT)
        self.assertIn("出行日期", DOCUMENT_SYSTEM_PROMPT)
        self.assertIn("优先", DOCUMENT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
