"""
真实 LLM 评测运行器与录制/回放（Phase F：P0 #2）
    模式：
        - real：对每条案例构造上下文，调用真实 RiskAgent，并录制结果；
        - replay：读取录制文件，不再调用模型，直接复用历史结果；
        - mock：由 harness 使用案例里的 simulated 字段。
    设计：
        - 案例自带 context，评测不查数据库、不依赖业务单据；
        - 录制只保存结构化结论、token、耗时与费用，不保存模型原始返回；
        - 真正执行 real 模式前必须得到授权，并确认 API 费用。
"""
import asyncio
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.agents.risk_agent import ReviewOutcome, RiskAgent
from app.services.cost_service import estimate_cost
from app.services.policy_service import retrieve_policy



# 默认录制目录
RECORDING_DIR = Path(__file__).resolve().parent / "recordings"



# 执行单条案例
def run_case(case: dict, *, agent=None) -> ReviewOutcome:
    """
    对一条案例执行真实风险评审
        :param case: 案例（必须包含 context）
        :param agent: 可注入的 Agent（测试用）
        :return: ReviewOutcome
    :raises ValueError: 案例缺少 context
    """
    context = case.get("context")
    if not context:
        raise ValueError(f"案例缺少 context: {case.get('case_id')}")

    policy = retrieve_policy(context)                       # 制度检索（失败自动降级为空）
    return asyncio.run((agent or RiskAgent()).review(context, policy=policy))



# 单条结果转录制格式
def outcome_to_record(outcome: ReviewOutcome) -> dict:
    """
    把 ReviewOutcome 转成可 JSON 序列化的录制结果
        :param outcome: 风险结果
        :return: 录制字典
    """
    return {
        "risk_score": str(outcome.risk_score),
        "risk_level": outcome.risk_level,
        "summary": outcome.summary,
        "findings": outcome.findings,
        "policy_citations": outcome.policy_citations,
        "is_fallback": bool(outcome.is_fallback),
        "model": outcome.model,
        "prompt_version": outcome.prompt_version,
        "policy_version": outcome.policy_version,
        "latency_ms": int(outcome.latency_ms or 0),
        "prompt_tokens": int(outcome.prompt_tokens or 0),
        "completion_tokens": int(outcome.completion_tokens or 0),
        "cost": str(estimate_cost(outcome.prompt_tokens, outcome.completion_tokens)),
        "retrieved_clause_ids": list(outcome.retrieved_clause_ids or []),
        "citation_stats": outcome.citation_stats or {},
        "error": None,
    }



# 录制多条案例
def record_cases(cases: list[dict], *, agent=None, limit: int | None = None) -> dict:
    """
    逐条执行并录制结果
        :param cases: 案例列表
        :param agent: 可注入的 Agent
        :param limit: 最多执行多少条（成本预览用）
        :return: 录制字典
    """
    selected = cases[:limit] if limit else cases
    results: dict[str, dict] = {}
    for case in selected:
        case_id = case.get("case_id")
        try:
            outcome = run_case(case, agent=agent)
            results[case_id] = outcome_to_record(outcome)
        except Exception as exc:
            # 单条失败不影响整体录制；回放时会按降级处理
            results[case_id] = {
                "risk_score": "0",
                "risk_level": "low",
                "summary": "评测执行失败",
                "findings": [],
                "policy_citations": [],
                "is_fallback": True,
                "model": "error",
                "prompt_version": "",
                "latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": "0",
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "meta": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "real",
            "cases": len(results),
            "model": _first_value(results, "model"),
            "prompt_version": _first_value(results, "prompt_version"),
        },
        "results": results,
    }



# 保存录制
def save_recording(recording: dict, path: str | Path) -> Path:
    """
    保存录制 JSON
        :param recording: 录制字典
        :param path: 输出路径
        :return: 实际路径
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(recording, ensure_ascii=False, indent=2), encoding="utf-8")
    return target



# 读取录制
def load_recording(path: str | Path) -> dict:
    """
    读取录制 JSON
        :param path: 文件路径
        :return: 录制字典
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))



# 录制结果转 ReviewOutcome
def outcomes_from_recording(recording: dict) -> dict[str, ReviewOutcome]:
    """
    把录制结果还原成 ReviewOutcome 映射
        :param recording: 录制字典
        :return: {case_id: ReviewOutcome}
    """
    outcomes: dict[str, ReviewOutcome] = {}
    for case_id, item in (recording.get("results") or {}).items():
        outcomes[case_id] = ReviewOutcome(
            risk_score=Decimal(str(item.get("risk_score") or 0)),
            risk_level=str(item.get("risk_level") or "low"),
            summary=str(item.get("summary") or ""),
            findings=list(item.get("findings") or []),
            policy_citations=list(item.get("policy_citations") or []),
            is_fallback=bool(item.get("is_fallback")),
            model=str(item.get("model") or ""),
            prompt_version=str(item.get("prompt_version") or ""),
            latency_ms=int(item.get("latency_ms") or 0),
            prompt_tokens=int(item.get("prompt_tokens") or 0),
            completion_tokens=int(item.get("completion_tokens") or 0),
            policy_version=str(item.get("policy_version") or ""),
            retrieved_clause_ids=list(item.get("retrieved_clause_ids") or []),
            citation_stats=dict(item.get("citation_stats") or {}),
        )
    return outcomes



# 取第一条非空字段
def _first_value(results: dict[str, dict], key: str) -> str:
    """
    取录制结果里第一个非空字段值
        :param results: 录制结果
        :param key: 字段名
        :return: 值或空串
    """
    for item in results.values():
        value = item.get(key)
        if value:
            return str(value)
    return ""
