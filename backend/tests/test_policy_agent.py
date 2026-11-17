"""
RiskAgent 制度引用集成测试（离线，不联网、不查库）
    覆盖：
        - 合法引用与 policy finding 保留；
        - 伪造条款号/改写原文被丢弃，policy finding 一并丢弃；
        - 无制度上下文时引用为空；
        - Prompt 注入制度区且版本升级。
"""
import asyncio
import json
import unittest

import tests  # noqa: F401  先设置测试环境变量
from app.agents.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from app.agents.risk_agent import RiskAgent
from app.rag.loader import load_policy_document
from app.rag.schema import PolicyBundle, PolicyHit
from app.services.llm_client import LLMResult



# 测试用 LLM：返回固定 JSON，并记录 Prompt
class FakeLLM:
    """固定返回的 LLM 客户端"""

    def __init__(self, data: dict):
        """
        :param data: chat_json 返回的 JSON
        """
        self.data = data                    # 固定返回
        self.calls: list[tuple] = []        # Prompt 记录

    async def chat_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        """
        返回固定结果
            :param system_prompt: 系统提示词
            :param user_prompt: 用户提示词
            :return: LLMResult
        """
        self.calls.append((system_prompt, user_prompt))   # 记录 Prompt
        return LLMResult(
            data=self.data,
            content=json.dumps(self.data, ensure_ascii=False),
            model="fake-model",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1,
            raw=self.data,
        )



# 构造最小上下文（不依赖数据库）
def make_context() -> dict:
    """
    构造一份预审上下文
        :return: 上下文字典
    """
    return {
        "expense": {
            "expense_no": "TEST-001",
            "title": "差旅费报销",
            "total_amount": "2500.00",
            "status": "pending",
            "submitter_role": "employee",
            "department": "研发部",
            "current_level": 1,
            "submitted_at": None,
        },
        "items": [{
            "category": "高铁/动车票",
            "expense_type": "transportation",
            "description": "广州南-深圳北高铁二等座",
            "amount": "2500.00",
            "expense_date": "2026-11-01",
            "has_invoice": True,
            "no_invoice_reason": "",
        }],
        "rule_hits": [],
        "truncated": {"items": 0, "rule_hits": 0},
    }



# 构造模型输出
def make_payload(*, citations: list, findings: list) -> dict:
    """
    构造模型返回的 JSON
        :param citations: 引用列表
        :param findings: 风险点列表
        :return: 字典
    """
    return {
        "risk_score": 20,
        "risk_level": "low",
        "summary": "测试结论",
        "findings": findings,
        "policy_citations": citations,
    }



class PolicyRiskAgentTest(unittest.TestCase):
    """RiskAgent 引用处理"""

    @classmethod
    def setUpClass(cls):
        """加载真实制度并构造 3.1 的检索结果"""
        cls.document = load_policy_document()                   # 真实制度
        cls.clause = next(                        # 交通费条款
            item for item in cls.document.clauses if item.clause_id == "3.1"
        )
        cls.bundle = PolicyBundle(
            doc_id=cls.document.doc_id,
            doc_title=cls.document.title,
            version=cls.document.version,
            hits=(PolicyHit(clause=cls.clause, score=20.0),),
        )
        cls.valid_quote = "原则上乘坐高铁二等座或飞机经济舱"   # 原文连续片段
        cls.context = make_context()                            # 上下文

    def test_valid_citation_and_policy_finding_kept(self):
        """合法引用保留，policy finding 保留"""
        llm = FakeLLM(make_payload(
            citations=[{"clause_id": "3.1", "quote": self.valid_quote}],
            findings=[{
                "type": "policy",
                "level": "mid",
                "message": "交通标准需人工确认",
                "clause_id": "3.1",
            }],
        ))
        outcome = asyncio.run(RiskAgent(llm).review(self.context, policy=self.bundle))

        self.assertEqual(len(outcome.policy_citations), 1)
        self.assertEqual(outcome.policy_citations[0]["clause_id"], "3.1")
        self.assertTrue(any(item["type"] == "policy" for item in outcome.findings))
        self.assertEqual(outcome.policy_version, "v1.3")
        self.assertEqual(outcome.retrieved_clause_ids, ["3.1"])

    def test_invalid_citation_and_policy_finding_dropped(self):
        """伪造条款号：引用丢弃，policy finding 一并丢弃"""
        llm = FakeLLM(make_payload(
            citations=[{"clause_id": "9.9", "quote": "编造内容"}],
            findings=[{
                "type": "policy",
                "level": "mid",
                "message": "编造的制度要求",
                "clause_id": "9.9",
            }],
        ))
        outcome = asyncio.run(RiskAgent(llm).review(self.context, policy=self.bundle))

        self.assertEqual(outcome.policy_citations, [])
        self.assertEqual(outcome.findings, [])
        self.assertEqual(outcome.citation_stats["unverified_policy_findings"], 1)

    def test_paraphrased_quote_dropped(self):
        """改写原文：引用丢弃"""
        llm = FakeLLM(make_payload(
            citations=[{"clause_id": "3.1", "quote": "员工应该优先乘坐高铁二等座"}],
            findings=[],
        ))
        outcome = asyncio.run(RiskAgent(llm).review(self.context, policy=self.bundle))

        self.assertEqual(outcome.policy_citations, [])
        self.assertEqual(outcome.citation_stats["dropped"][0]["reason"], "quote_not_found")

    def test_no_policy_drops_citations(self):
        """没有制度上下文：引用与 policy finding 都丢弃"""
        llm = FakeLLM(make_payload(
            citations=[{"clause_id": "3.1", "quote": self.valid_quote}],
            findings=[{
                "type": "policy",
                "level": "low",
                "message": "无依据的制度断言",
                "clause_id": "3.1",
            }],
        ))
        outcome = asyncio.run(RiskAgent(llm).review(self.context))

        self.assertEqual(outcome.policy_citations, [])
        self.assertEqual(outcome.policy_version, "")
        self.assertEqual(outcome.findings, [])
        self.assertEqual(outcome.citation_stats["unverified_policy_findings"], 1)

    def test_malformed_citation_items_do_not_break_review(self):
        """脏引用项（字符串/数字）不会拖垮整次预审"""
        llm = FakeLLM(make_payload(
            citations=["bad", {"clause_id": 3.1, "quote": 123}],
            findings=[{"type": "other", "level": "low", "message": "测试风险"}],
        ))
        outcome = asyncio.run(RiskAgent(llm).review(self.context, policy=self.bundle))

        self.assertEqual(outcome.policy_citations, [])
        self.assertTrue(any(item["message"] == "测试风险" for item in outcome.findings))

    def test_prompt_contains_policy_context(self):
        """Prompt 注入制度区，且版本升到 v3"""
        llm = FakeLLM(make_payload(citations=[], findings=[]))
        asyncio.run(RiskAgent(llm).review(self.context, policy=self.bundle))

        system_prompt, user_prompt = llm.calls[0]              # 取出 Prompt
        self.assertIn("制度引用", system_prompt)
        self.assertIn("<policy_context>", user_prompt)
        self.assertIn("[3.1]", user_prompt)
        self.assertIn(self.valid_quote, user_prompt)
        self.assertEqual(PROMPT_VERSION, "risk_review_v4_2_citation_guard")

    def test_prompt_has_v4_2_citation_rules(self):
        """v4.2 两条新规则必须在 System Prompt 里（金额方向成对检查 + 禁止性条款优先）"""
        self.assertIn("制度引用校准（v4.2）", SYSTEM_PROMPT)     # 版本段标题
        self.assertIn("金额方向必须成对检查", SYSTEM_PROMPT)      # 规则 6
        self.assertIn("禁止性条款优先", SYSTEM_PROMPT)            # 规则 7
        self.assertIn("不能单独替代它", SYSTEM_PROMPT)            # 规则 7 的"不许用条件型条款顶替"
        # 规则编号连续，避免后续插入时漏改
        for index in range(1, 9):
            self.assertIn(f"{index}. ", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
