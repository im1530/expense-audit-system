"""
Prompt v4 校准测试（P0 #2 A/B 准备）
    覆盖：
        - v4 提示词包含校准段；
        - RiskAgent 可注入自定义 System Prompt 与版本号；
        - 结果中的 prompt_version 正确写入 v4。
    约束：不调用真实 LLM。
"""
import asyncio
import unittest
from decimal import Decimal

import tests  # noqa: F401
from app.agents.prompts import PROMPT_VERSION as DEFAULT_VERSION, SYSTEM_PROMPT as DEFAULT_PROMPT
from app.agents.prompts_v4 import PROMPT_VERSION, SYSTEM_PROMPT_V4
from app.agents.risk_agent import RiskAgent
from app.services.llm_client import LLMResult



class FakeLLM:
    """记录 System Prompt 并返回固定 JSON"""

    def __init__(self):
        """初始化记录字段"""
        self.system_prompt = ""

    async def chat_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        """
        返回固定低风险 JSON
            :param system_prompt: 系统提示词
            :param user_prompt: 用户提示词
            :return: LLMResult
        """
        self.system_prompt = system_prompt
        return LLMResult(
            data={
                "risk_score": 10,
                "risk_level": "low",
                "summary": "正常",
                "findings": [],
                "policy_citations": [],
            },
            content="{}",
            model="fake",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1,
            raw={},
        )



# 最小上下文
CONTEXT = {
    "expense": {
        "expense_no": "TEST",
        "title": "正常报销",
        "total_amount": "100.00",
        "status": "pending",
        "submitter_role": "employee",
        "department": "研发部",
        "current_level": 1,
        "submitted_at": "2026-11-09T09:00:00",
    },
    "items": [{
        "item_id": 1,
        "category": "交通",
        "expense_type": "transportation",
        "description": "高铁票",
        "amount": "100.00",
        "expense_date": "2026-11-07",
        "has_invoice": True,
        "no_invoice_reason": "",
    }],
    "rule_hits": [],
    "document_findings": [],
    "truncated": {"items": 0, "rule_hits": 0},
}



class PromptV4Test(unittest.TestCase):
    """v4 注入与版本"""

    def test_default_prompt_is_v4(self):
        """线上默认 Prompt 为 v4.2（v4 校准 + 引用校准）"""
        self.assertEqual(DEFAULT_VERSION, "risk_review_v4_2_citation_guard")
        self.assertIn("【判断尺度（v4 校准）】", DEFAULT_PROMPT)
        self.assertIn("【制度引用校准（v4.2）】", DEFAULT_PROMPT)

    def test_prompt_contains_calibration(self):
        """v4 包含判断尺度与注入处理规则"""
        self.assertIn("【判断尺度（v4 校准）】", SYSTEM_PROMPT_V4)
        self.assertIn("不要把它本身当成风险", SYSTEM_PROMPT_V4)
        self.assertIn("拆票", SYSTEM_PROMPT_V4)

    def test_prompt_contains_citation_guard(self):
        """v4.2 包含引用方向/主题约束与两条必引规则，且位于输出要求之前"""
        self.assertIn("【制度引用校准（v4.2）】", SYSTEM_PROMPT_V4)
        self.assertIn("方向一致", SYSTEM_PROMPT_V4)              # 方向不符不得引用
        self.assertIn("主题一致", SYSTEM_PROMPT_V4)              # 主题不符不得引用
        self.assertIn("宁可少于 5 条", SYSTEM_PROMPT_V4)          # 数量服从质量
        self.assertIn("金额方向必须成对检查", SYSTEM_PROMPT_V4)     # v4.2 规则 6
        self.assertIn("禁止性条款优先", SYSTEM_PROMPT_V4)          # v4.2 规则 7
        guard_index = SYSTEM_PROMPT_V4.index("【制度引用校准（v4.2）】")   # 引用校准位置
        output_index = SYSTEM_PROMPT_V4.index("【输出要求】")              # 输出要求位置
        self.assertLess(guard_index, output_index)

    def test_agent_uses_custom_prompt_and_version(self):
        """自定义 Prompt 生效，结果版本号为 v4"""
        fake = FakeLLM()
        agent = RiskAgent(
            llm_client=fake,
            system_prompt=SYSTEM_PROMPT_V4,
            prompt_version=PROMPT_VERSION,
        )
        outcome = asyncio.run(agent.review(CONTEXT))
        self.assertIn("【判断尺度（v4 校准）】", fake.system_prompt)
        self.assertEqual(outcome.prompt_version, PROMPT_VERSION)
        self.assertEqual(outcome.risk_level, "low")
        self.assertEqual(outcome.risk_score, Decimal("10.00"))
