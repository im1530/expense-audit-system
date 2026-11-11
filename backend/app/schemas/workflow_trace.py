"""
AI 预审工作流轨迹响应契约（只读展示）
    为什么单独定义：
        - 联调/排障需要看到“一次预审跑了哪些节点、每步耗时与 token、最终决策与制度引用”；
        - 页面只读展示，不能把 raw_response、票面原文等敏感内容透出；
        - 字段与 workflow_runs / workflow_steps / ai_review_logs / document_extractions 对齐，
          前端无需再拼多张表。
    边界：
        - 本模块只定义数据结构，不查库、不写库；
        - 金额/置信度用 Decimal，避免浮点误差；时间允许为空（run 未开始时）。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ai_review import PolicyCitation



# 单个节点执行记录
class WorkflowTraceStep(BaseModel):
    """
    工作流节点（一次尝试一条）
        :ivar node: 节点名：load_context/document/rule/rag/risk/decision
        :ivar attempt: 第几次尝试（从 1 开始）
        :ivar status: running/success/fallback/failed
        :ivar latency_ms: 节点耗时（毫秒）
        :ivar prompt_tokens: 本节点输入 token
        :ivar completion_tokens: 本节点输出 token
        :ivar cost: 本节点估算费用（元）
        :ivar summary: 节点脱敏摘要（不含票面原文）
        :ivar error_message: 失败原因（仅 failed 时有值）
    """
    node: str                                                      # 节点名
    attempt: int = 1                                               # 尝试序号
    status: str                                                    # 节点状态
    latency_ms: Optional[int] = None                               # 耗时（毫秒）
    prompt_tokens: int = 0                                         # 输入 token
    completion_tokens: int = 0                                     # 输出 token
    cost: Optional[Decimal] = None                                 # 估算费用
    summary: dict = Field(default_factory=dict)                    # 脱敏摘要
    error_message: Optional[str] = None                            # 失败原因

    model_config = ConfigDict(extra="ignore")                      # 容忍多余字段



# 一张附件的凭证识别结论
class WorkflowTraceDocument(BaseModel):
    """
    凭证识别结论（每条附件取最近一次识别）
        :ivar attachment_id: 附件ID
        :ivar expense_item_id: 所属明细ID
        :ivar status: pending/running/success/fallback/failed
        :ivar document_type: vat_invoice/train_ticket/payment_screenshot 等
        :ivar evidence_level: strong/medium/weak/none
        :ivar amount: 票面金额
        :ivar document_date: 票面日期
        :ivar counterparty: 商户/收款方
        :ivar identifier: 发票号/订单号/流水号
        :ivar confidence: 模型自评置信度
        :ivar needs_manual_review: 是否建议人工核对
        :ivar error_message: 失败/降级原因
    """
    attachment_id: int                                             # 附件ID
    expense_item_id: int                                           # 明细ID
    status: str                                                    # 识别状态
    document_type: Optional[str] = None                            # 凭证类型
    evidence_level: Optional[str] = None                           # 证据强度
    amount: Optional[Decimal] = None                               # 票面金额
    document_date: Optional[date] = None                           # 票面日期
    counterparty: Optional[str] = None                             # 商户/收款方
    identifier: Optional[str] = None                               # 发票号/订单号
    confidence: Optional[Decimal] = None                           # 置信度
    needs_manual_review: bool = False                              # 是否建议人工核对
    error_message: Optional[str] = None                            # 失败原因



# 工作流轨迹响应
class WorkflowTraceResponse(BaseModel):
    """
    一张报销单最近一次 AI 预审的完整轨迹
        :ivar expense_id: 报销单ID
        :ivar run_id: 工作流运行ID
        :ivar review_id: 预审日志ID（可为空）
        :ivar status: run 状态：running/success/failed/cancelled
        :ivar engine: 编排引擎
        :ivar current_node: 当前/最后执行节点
        :ivar model: 模型名
        :ivar prompt_version: Prompt 版本
        :ivar policy_version: 制度版本
        :ivar decision: 建议决策
        :ivar decision_reason: 决策说明
        :ivar risk_level: 风险等级
        :ivar risk_score: 风险分数
        :ivar is_fallback: 是否规则兜底
        :ivar prompt_tokens: 全流程输入 token
        :ivar completion_tokens: 全流程输出 token
        :ivar cost: 全流程估算费用
        :ivar started_at: 开始时间
        :ivar finished_at: 结束时间
        :ivar error_message: run 失败原因（仅 failed 时有值）
        :ivar state: run 的脱敏状态摘要
        :ivar steps: 节点列表（按执行顺序）
        :ivar citations: 通过校验的制度引用
        :ivar documents: 凭证识别结论
    """
    expense_id: int                                                # 报销单ID
    run_id: int                                                    # 工作流运行ID
    review_id: Optional[int] = None                                # 预审日志ID
    status: str                                                    # run 状态
    engine: str = ""                                               # 编排引擎
    current_node: Optional[str] = None                             # 当前节点
    model: str = ""                                                # 模型名
    prompt_version: str = ""                                       # Prompt 版本
    policy_version: str = ""                                       # 制度版本
    decision: str = ""                                             # 建议决策
    decision_reason: str = ""                                      # 决策说明
    risk_level: Optional[str] = None                               # 风险等级
    risk_score: Optional[Decimal] = None                           # 风险分数
    is_fallback: bool = False                                      # 是否兜底
    prompt_tokens: int = 0                                         # 全流程输入 token
    completion_tokens: int = 0                                     # 全流程输出 token
    cost: Optional[Decimal] = None                                 # 全流程估算费用
    started_at: Optional[datetime] = None                          # 开始时间
    finished_at: Optional[datetime] = None                         # 结束时间
    error_message: Optional[str] = None                            # 失败原因
    state: dict = Field(default_factory=dict)                      # 脱敏状态摘要
    steps: list[WorkflowTraceStep] = Field(default_factory=list)   # 节点列表
    citations: list[PolicyCitation] = Field(default_factory=list)  # 制度引用
    documents: list[WorkflowTraceDocument] = Field(default_factory=list)  # 凭证结论
