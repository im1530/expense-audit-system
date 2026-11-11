"""
工作流状态定义（LangGraph StateGraph 使用）
    设计：
        - 状态只保存节点间传递的数据与版本信息；
        - 落库日志只保存脱敏摘要，不保存原始图片、完整 Prompt、证件号；
        - context/policy/outcome 是运行时对象，不直接写审计 JSON。
"""
from typing import Any, TypedDict



class WorkflowState(TypedDict, total=False):
    """
    一次 AI 预审工作流的运行时状态
        :ivar expense_id: 报销单ID
        :ivar review_log_id: 预审日志ID
        :ivar context: 预审上下文（已脱敏）
        :ivar document_findings: 凭证风险点
        :ivar policy: PolicyBundle（制度检索结果）
        :ivar outcome: ReviewOutcome（风险评审结果）
        :ivar decision: 建议决策 {"decision","reason"}
        :ivar fallback_reason: 降级原因
        :ivar rule_summary: 规则节点摘要
        :ivar errors: 节点错误列表
        :ivar timings: 节点耗时
    """
    expense_id: int                         # 报销单ID
    review_log_id: int | None               # 预审日志ID
    context: dict                           # 预审上下文
    document_findings: list[dict]           # 凭证风险点
    document_usage: dict                    # 凭证节点本次新增 token 用量
    policy: Any                             # 制度检索结果
    outcome: Any                            # 风险评审结果
    decision: dict                          # 建议决策
    fallback_reason: str | None             # 降级原因
    rule_summary: dict                      # 规则节点摘要
    errors: list[str]                       # 错误列表
    timings: dict                           # 节点耗时
