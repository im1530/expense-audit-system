"""
预审决策策略（Phase D：确定性决策节点）
    原则：
        - 决策不调用 LLM，完全由风险等级、findings、降级状态推导；
        - 只输出"建议"，不自动通过、不自动驳回、不自动付款；
        - 任何 high / 降级 / 中风险 finding 都不允许 fast_track；
        - 仅有 low finding（提示性信息）不阻断 fast_track，由审批人快速确认。
"""
from app.agents.risk_agent import ReviewOutcome



# 决策标签
DECISION_FAST_TRACK = "fast_track"             # 建议快速审批
DECISION_MANUAL_REVIEW = "manual_review"       # 建议人工复核
DECISION_SUGGEST_REJECT = "suggest_reject"     # 建议退回/补正



# 生成建议决策
def decide_review(
    outcome: ReviewOutcome,
    *,
    fallback_reason: str | None = None,
) -> dict:
    """
    根据风险结果生成确定性建议
        :param outcome: 风险评审结果
        :param fallback_reason: 降级原因；非空时一律人工复核
        :return: {"decision": ..., "reason": ...}
    """
    # 1.降级/兜底：不允许快速通过
    if fallback_reason or outcome.is_fallback:
        return {
            "decision": DECISION_MANUAL_REVIEW,
            "reason": f"AI 预审降级为规则兜底（{fallback_reason or '未知原因'}），需人工复核",
        }

    # 2.高风险：建议退回/补正（等级由 risk_agent 按分数统一换算，决策不再重复阈值）
    high_findings = [item for item in outcome.findings if item.get("level") == "high"]
    if high_findings or outcome.risk_level == "high":
        first = str(high_findings[0].get("message") or "")[:200] if high_findings else ""
        reason = f"存在高风险项 {len(high_findings)} 条" if high_findings else "风险等级为高风险"
        if first:
            reason = f"{reason}：{first}"
        return {"decision": DECISION_SUGGEST_REJECT, "reason": reason[:500]}

    # 3.中风险/中高风险 finding：人工复核；仅有 low finding 不在此列
    mid_findings = [item for item in outcome.findings if item.get("level") in ("mid", "high")]
    if mid_findings or outcome.risk_level == "mid":
        reason = "存在需要人工核对的疑点"
        if mid_findings:
            reason = f"存在 {len(mid_findings)} 条中高风险提示，需人工复核"
        return {"decision": DECISION_MANUAL_REVIEW, "reason": reason}

    # 4.低风险：允许 fast_track；low finding 仅作为提示，不阻断
    low_findings = [item for item in outcome.findings if item.get("level") == "low"]
    if low_findings:
        return {
            "decision": DECISION_FAST_TRACK,
            "reason": f"低风险，仅有 {len(low_findings)} 条提示性信息，可由审批人快速确认",
        }
    return {
        "decision": DECISION_FAST_TRACK,
        "reason": "低风险、无风险提示，可由审批人快速确认",
    }
