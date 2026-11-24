"""
凭证一致性规则测试（离线，不查库）
    覆盖：金额不足/超额、发票号、抬头、日期、弱/中等凭证阈值、重复凭证号、
          无法识别、threshold 配置校验。
"""
import unittest
from datetime import date, timedelta
from decimal import Decimal

import tests  # noqa: F401  先设置测试环境变量
from app.services.document_rules import (
    DocumentFacts,
    EvidencePolicy,
    build_document_findings,
    normalize_identifier,
)
from app.services.rule_service import normalize_config



# 默认公司抬头
COMPANY_IDENTITIES = [
    {"name": "示例科技有限公司", "tax_id": "91440101MA5TEST001"},
]



# 构造明细上下文
def make_item(
    amount: str = "100.00",
    *,
    invoice_no: str = "",
    expense_date: date | None = None,
    category_type: str = "transportation",
) -> dict:
    """
    构造比对用明细
        :param amount: 明细金额
        :param invoice_no: 手填发票号
        :param expense_date: 费用日期
        :param category_type: 费用大类
        :return: 字典
    """
    return {
        "amount": Decimal(amount),
        "invoice_no": invoice_no,
        "expense_date": expense_date or date(2026, 11, 1),
        "category_type": category_type,
        "description": "测试明细",
    }



# 构造凭证事实
def make_facts(
    *,
    document_type: str = "vat_invoice",
    evidence_level: str = "strong",
    amount: str | None = "100.00",
    document_date: date | None = None,
    identifier: str = "",
    extra: dict | None = None,
    confidence: float = 0.95,
    needs_manual_review: bool = False,
) -> DocumentFacts:
    """
    构造凭证事实
        :param document_type: 类型
        :param evidence_level: 证据强度
        :param amount: 金额
        :param document_date: 日期
        :param identifier: 凭证号
        :param extra: 扩展字段
        :param confidence: 置信度
        :param needs_manual_review: 是否人工
        :return: DocumentFacts
    """
    return DocumentFacts(
        document_type=document_type,
        evidence_level=evidence_level,
        amount=Decimal(amount) if amount is not None else None,
        document_date=document_date or date(2026, 11, 1),
        identifier=identifier,
        extra=extra or {},
        confidence=confidence,
        needs_manual_review=needs_manual_review,
    )



class DocumentRulesTest(unittest.TestCase):
    """凭证一致性"""

    def setUp(self):
        """默认阈值策略"""
        self.policy = EvidencePolicy(
            weak_max_amount=Decimal("200"),
            medium_max_amount=Decimal("1000"),
            category_overrides={"transportation": Decimal("300")},
        )

    def test_amount_short_is_high(self):
        """凭证金额低于明细金额 → high"""
        findings = build_document_findings(
            make_facts(amount="80.00"),
            make_item("100.00"),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "high" and "低于" in item["message"] for item in findings))

    def test_amount_over_is_mid(self):
        """凭证金额高于明细金额 → mid"""
        findings = build_document_findings(
            make_facts(amount="120.00"),
            make_item("100.00"),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "mid" and "高于" in item["message"] for item in findings))

    def test_multi_invoice_total_amount_covers_item(self):
        """同一明细两张 500 发票：按合计 1000 比对，不误报金额不足"""
        findings = build_document_findings(
            make_facts(amount="500.00"),
            make_item("1000.00"),
            policy=self.policy,
            total_amount=Decimal("1000.00"),
        )
        self.assertFalse(any(item["level"] == "high" and "低于" in item["message"] for item in findings))

    def test_invoice_no_mismatch_is_high(self):
        """发票号不一致 → high"""
        findings = build_document_findings(
            make_facts(identifier="INV-002"),
            make_item("100.00", invoice_no="INV-001"),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "high" and "发票号" in item["message"] for item in findings))

    def test_buyer_mismatch_is_high(self):
        """购买方不是公司抬头 → high"""
        findings = build_document_findings(
            make_facts(extra={"buyer_name": "其他公司", "buyer_tax_id": "OTHER"}),
            make_item("100.00"),
            policy=self.policy,
            company_identities=COMPANY_IDENTITIES,
        )
        self.assertTrue(any(item["level"] == "high" and "购买方" in item["message"] for item in findings))

    def test_buyer_missing_is_mid(self):
        """配置了公司抬头但票面没有购买方 → mid"""
        findings = build_document_findings(
            make_facts(),
            make_item("100.00"),
            policy=self.policy,
            company_identities=COMPANY_IDENTITIES,
        )
        self.assertTrue(any(item["level"] == "mid" and "购买方" in item["message"] for item in findings))

    def test_no_company_identities_skips_buyer(self):
        """未配置公司抬头时跳过购买方校验"""
        findings = build_document_findings(
            make_facts(),
            make_item("100.00"),
            policy=self.policy,
            company_identities=[],
        )
        self.assertFalse(any("购买方" in item["message"] for item in findings))

    def test_date_deviation_is_mid(self):
        """票面日期与费用日期偏差超过 7 天 → mid"""
        findings = build_document_findings(
            make_facts(document_date=date(2026, 11, 20)),
            make_item("100.00", expense_date=date(2026, 11, 1)),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "mid" and "日期" in item["message"] for item in findings))

    def test_weak_evidence_threshold_with_category_override(self):
        """弱凭证 350 元，交通类阈值 300 → mid"""
        facts = make_facts(
            document_type="payment_screenshot",
            evidence_level="weak",
            amount="350.00",
        )
        findings = build_document_findings(
            facts,
            make_item("350.00", category_type="transportation"),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "mid" and "弱凭证" in item["message"] for item in findings))

    def test_weak_evidence_below_threshold_no_finding(self):
        """弱凭证 150 元，低于阈值 → 不提示"""
        facts = make_facts(
            document_type="payment_screenshot",
            evidence_level="weak",
            amount="150.00",
        )
        findings = build_document_findings(
            facts,
            make_item("150.00"),
            policy=self.policy,
        )
        self.assertEqual(findings, [])

    def test_medium_evidence_threshold(self):
        """中等凭证 1500 元，超过 1000 → mid"""
        facts = make_facts(
            document_type="hotel_folio",
            evidence_level="medium",
            amount="1500.00",
        )
        findings = build_document_findings(
            facts,
            make_item("1500.00", category_type="accommodation"),
            policy=self.policy,
        )
        self.assertTrue(any(item["level"] == "mid" and "中等凭证" in item["message"] for item in findings))

    def test_duplicate_invoice_is_high(self):
        """重复发票号 → high"""
        findings = build_document_findings(
            make_facts(identifier="INV-001"),
            make_item("100.00"),
            policy=self.policy,
            duplicate_identifier=True,
        )
        self.assertTrue(any(item["level"] == "high" and "重复" in item["message"] for item in findings))

    def test_unreadable_is_low(self):
        """无法识别 → low，不产生金额结论"""
        findings = build_document_findings(
            make_facts(document_type="unreadable", evidence_level="none", amount=None),
            make_item("100.00"),
            policy=self.policy,
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["level"], "low")

    def test_missing_fields_need_manual_review(self):
        """关键字段缺失 → low"""
        findings = build_document_findings(
            make_facts(amount=None, document_date=None, needs_manual_review=True),
            make_item("100.00"),
            policy=self.policy,
        )
        self.assertTrue(findings)
        self.assertEqual(findings[0]["level"], "low")

    def test_normalize_identifier(self):
        """凭证号忽略空格与连字符"""
        self.assertEqual(normalize_identifier(" INV-001 "), "INV001")

    def test_evidence_rule_config(self):
        """check_evidence_strength 配置校验"""
        config = normalize_config(None, "check_evidence_strength", {
            "weak_max_amount": 200,
            "medium_max_amount": 1000,
            "category_overrides": {"transportation": 300},
        })
        self.assertEqual(config["weak_max_amount"], 200.0)
        self.assertEqual(config["category_overrides"]["transportation"], 300.0)

    def test_evidence_rule_rejects_bad_category(self):
        """非法的类别覆盖键 → 400"""
        with self.assertRaises(Exception):
            normalize_config(None, "check_evidence_strength", {
                "category_overrides": {"unknown": 100},
            })


if __name__ == "__main__":
    unittest.main()
