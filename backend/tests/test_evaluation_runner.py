"""
评测运行器/金标准集/版本对比测试（Phase F）
    覆盖：
        - 100 条金标准集结构完整、编号唯一；
        - mock 评测可用；
        - 真实结果按关键词匹配 finding 代码；
        - 录制 → 回放 → 评测；
        - 两份报告对比。
    约束：不调用真实 LLM。
"""
import unittest
from decimal import Decimal

import tests  # noqa: F401
from app.agents.risk_agent import ReviewOutcome
from evaluation.compare import compare_reports, render_comparison_markdown
from evaluation.harness import GOLDEN_CASES, REAL_CASES, evaluate_cases, evaluate_outcome, load_cases
from evaluation.runner import (
    outcome_to_record,
    outcomes_from_recording,
    record_cases,
    run_case,
)



# 假风险 Agent
class FakeAgent:
    """返回固定低风险结果"""

    async def review(self, context, *, policy=None) -> ReviewOutcome:
        """
        返回固定结果
            :param context: 上下文
            :param policy: 制度结果
            :return: ReviewOutcome
        """
        return ReviewOutcome(
            risk_score=Decimal("20"),
            risk_level="low",
            summary="fake",
            findings=[],
            policy_citations=[],
            is_fallback=False,
            model="fake",
            prompt_version="fake_v1",
            latency_ms=5,
            prompt_tokens=10,
            completion_tokens=5,
        )



class GoldenSetTest(unittest.TestCase):
    """100 条金标准集"""

    def test_golden_set_structure(self):
        """案例数、编号、context、expected 完整"""
        cases = load_cases(GOLDEN_CASES)
        self.assertEqual(len(cases), 100)
        self.assertEqual(len({case["case_id"] for case in cases}), 100)
        for case in cases:
            self.assertIn("context", case)
            self.assertIn("expected", case)
            self.assertEqual(case["label_status"], "reviewed")
            self.assertTrue(case.get("frozen"))
            self.assertIn("expense", case["context"])
            self.assertTrue(case["context"]["items"])

    def test_mock_golden_metrics(self):
        """mock 模式下草稿标签与模拟结果一致"""
        report = evaluate_cases(load_cases(GOLDEN_CASES))
        self.assertEqual(report["total"], 100)
        self.assertEqual(report["decision_accuracy"], 1.0)
        self.assertEqual(report["risk_level_accuracy"], 1.0)
        self.assertEqual(report["must_not_violations"], 0)

    def test_real96_structure(self):
        """真实评测集 96 条，剔除 ai_fallback"""
        cases = load_cases(REAL_CASES)
        ids = {case["case_id"] for case in cases}
        self.assertEqual(len(cases), 96)
        self.assertFalse(ids & {"G073", "G074", "G075", "G076"})
        self.assertTrue(all(case.get("eval_scope") == "real" for case in cases))

    def test_real_message_keyword_match(self):
        """真实输出没有 code，按关键词识别 finding"""
        case = next(item for item in load_cases(GOLDEN_CASES) if item["case_id"] == "G025")
        outcome = ReviewOutcome(
            risk_score=Decimal("80"),
            risk_level="high",
            summary="金额不足",
            findings=[{
                "type": "invoice",
                "level": "high",
                "message": "凭证金额低于明细金额 720 元",
                "evidence": "发票 1280 元",
            }],
            policy_citations=[],
            is_fallback=False,
            model="fake",
            prompt_version="fake_v1",
            latency_ms=10,
            prompt_tokens=10,
            completion_tokens=5,
        )
        result = evaluate_outcome(case, outcome, mode="real")
        self.assertTrue(result["decision_match"])
        self.assertTrue(result["risk_match"])
        self.assertEqual(result["missing_findings"], [])



class RunnerTest(unittest.TestCase):
    """录制与回放"""

    def test_run_case_requires_context(self):
        """缺少 context 的冒烟案例不能直接跑真实模式"""
        smoke = load_cases()[0]
        with self.assertRaises(ValueError):
            run_case(smoke)

    def test_record_and_replay(self):
        """录制结果可还原并评测"""
        cases = load_cases(GOLDEN_CASES)[:2]
        recording = record_cases(cases, agent=FakeAgent())
        self.assertEqual(recording["meta"]["cases"], 2)
        outcomes = outcomes_from_recording(recording)
        report = evaluate_cases(cases, outcomes, mode="replay")
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["mode"], "replay")
        self.assertEqual(report["avg_tokens"], 15.0)

    def test_outcome_to_record_serializable(self):
        """录制字段不包含 raw_content"""
        outcome = ReviewOutcome(
            risk_score=Decimal("20"),
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
            raw_content='{"secret": "x"}',
        )
        record = outcome_to_record(outcome)
        self.assertNotIn("raw_content", record)
        self.assertEqual(record["prompt_tokens"], 1)



class CompareTest(unittest.TestCase):
    """版本对比"""

    def make_report(self, decision: str, matched: bool) -> dict:
        """
        构造最小报告
            :param decision: 实际决策
            :param matched: 是否正确
            :return: 报告
        """
        return {
            "mode": "mock",
            "decision_accuracy": 1.0 if matched else 0.0,
            "risk_level_accuracy": 1.0 if matched else 0.0,
            "high_risk_miss_rate": 0.0,
            "finding_recall": 1.0 if matched else 0.0,
            "fallback_rate": 0.0,
            "avg_latency_ms": 10,
            "avg_tokens": 20,
            "results": [{
                "case_id": "G001",
                "title": "测试案例",
                "actual_decision": decision,
                "actual_risk_level": "low" if matched else "mid",
                "decision_match": matched,
                "risk_match": matched,
                "missing_findings": [] if matched else ["amount_short"],
            }],
        }

    def test_compare_improved(self):
        """基线错误、候选正确：记为改进"""
        comparison = compare_reports(self.make_report("fast_track", False), self.make_report("manual_review", True))
        self.assertEqual(comparison["improved_cases"], 1)
        self.assertEqual(comparison["regressed_cases"], 0)
        self.assertIn("AI 预审版本对比", render_comparison_markdown(comparison))
