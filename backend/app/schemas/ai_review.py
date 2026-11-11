"""
AI 预审输出契约（模型返回 → Pydantic 校验 → 业务归一化）
    为什么单独定义 Schema：
        - 模型输出是不可信输入，必须先过结构校验，不能直接写库；
        - 字段范围（0~100、low/mid/high）在 Schema 层就锁死，减少脏数据；
        - API 层可以直接复用这些模型做响应，不用再拼字典。
    设计取舍：
        - 允许模型多返回字段（extra="ignore"），避免因为多一个字段就判失败；
        - findings / policy_citations 允许 null（归一化成空列表）；
        - risk_level 仍然保留，但业务层会根据分数重算，不直接信任模型。
"""
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import AIReviewStatus



# 单条风险点
class AIFinding(BaseModel):
    """
    一条 AI 风险点
        :ivar type: 类型：rule 规则 / policy 制度 / invoice 发票 / duplicate 重复 / other 其他
        :ivar level: 级别：low / mid / high
        :ivar message: 说明（给审批人看）
        :ivar evidence: 证据（明细、金额、阈值等）
        :ivar suggestion: 建议动作（如"补充超标审批"）
        :ivar clause_id: 制度条款号；type=policy 时必须填写且必须能对应有效引用
    """
    type: Literal["rule", "policy", "invoice", "duplicate", "other"] = "other"  # 风险类型
    level: Literal["low", "mid", "high"]                                        # 风险级别
    message: str = Field(..., min_length=1, max_length=500)                     # 说明
    evidence: Optional[str] = Field(None, max_length=500)                        # 证据
    suggestion: Optional[str] = Field(None, max_length=300)                      # 建议
    clause_id: Optional[str] = Field(None, max_length=50)                        # 制度条款号

    model_config = ConfigDict(extra="ignore")                                   # 容忍多余字段

    @field_validator("clause_id", mode="before")
    @classmethod
    def _coerce_clause_id(cls, value):
        """
        把模型偶尔返回的数字条款号转成字符串
            :param value: 原始值
            :return: 字符串或原值
        """
        return str(value) if isinstance(value, (int, float)) else value



# 制度引用（对外响应/落库形态）
class PolicyCitation(BaseModel):
    """
    通过校验的制度引用
        :ivar clause_id: 条款编号
        :ivar doc: 制度名称（服务端按条款回填）
        :ivar chapter: 章节（服务端按条款回填）
        :ivar quote: 原文摘录（必须是条款原文的连续片段）
    """
    clause_id: Optional[str] = Field(None, max_length=50)        # 条款编号（兼容历史数据可为空）
    doc: str = Field(..., min_length=1, max_length=200)          # 制度名称
    chapter: Optional[str] = Field(None, max_length=100)          # 章节
    quote: str = Field(..., min_length=1, max_length=500)         # 原文摘录

    model_config = ConfigDict(extra="ignore")                    # 容忍多余字段



# 模型输出的引用草稿（宽松校验，非法项在业务层丢弃）
class PolicyCitationDraft(BaseModel):
    """
    模型返回的制度引用草稿
        为什么不直接用 PolicyCitation 严格校验：
            任何一条引用格式非法都会让整次预审 Schema 校验失败并降级，
            代价过大；这里只做"类型可解析"，逐条合法性交给 citation.py。
        :ivar clause_id: 条款编号（可为空，校验时丢弃）
        :ivar quote: 原文摘录（可为空，校验时丢弃）
        :ivar doc: 模型自报的制度名（忽略，服务端回填）
        :ivar chapter: 模型自报的章节（忽略，服务端回填）
    """
    clause_id: Optional[str] = Field(None, max_length=50)        # 条款编号
    quote: Optional[str] = Field(None, max_length=500)           # 原文摘录
    doc: Optional[str] = Field(None, max_length=200)             # 模型自报制度名（不采用）
    chapter: Optional[str] = Field(None, max_length=100)         # 模型自报章节（不采用）

    model_config = ConfigDict(extra="ignore")                    # 容忍多余字段

    @field_validator("clause_id", "quote", mode="before")
    @classmethod
    def _coerce_text_fields(cls, value):
        """
        把模型偶尔返回的数字转成字符串，避免整次预审被一条脏引用拖垮
            :param value: 原始值
            :return: 字符串或原值
        """
        return str(value) if isinstance(value, (int, float)) else value



# 模型输出的完整结构
class AIReviewOutput(BaseModel):
    """
    LLM 返回的预审结果（严格结构）
        :ivar risk_score: 风险分数 0~100
        :ivar risk_level: 模型自评等级（业务层会按分数重算）
        :ivar summary: 总结
        :ivar findings: 风险点列表（最多 20 条）
        :ivar policy_citations: 制度引用草稿（业务层逐条校验后决定保留或丢弃）
    """
    risk_score: float = Field(..., ge=0, le=100)                          # 分数
    risk_level: Literal["low", "mid", "high"]                             # 模型自评等级
    summary: str = Field(..., min_length=1, max_length=1000)              # 总结
    findings: list[AIFinding] = Field(default_factory=list, max_length=50)        # 风险点（业务层再截断到 20）
    policy_citations: list[PolicyCitationDraft] = Field(default_factory=list, max_length=20)  # 引用草稿

    model_config = ConfigDict(extra="ignore")                             # 容忍多余字段

    @field_validator("findings", mode="before")
    @classmethod
    def _none_to_empty_list(cls, value):
        """
        把模型返回的 null 归一成空列表
            :param value: 原始值
            :return: 列表或原值
        """
        return value or []

    @field_validator("policy_citations", mode="before")
    @classmethod
    def _clean_citation_drafts(cls, value):
        """
        过滤掉非字典的引用项，避免一条脏数据拖垮整次预审
            :param value: 模型返回的引用列表
            :return: 仅保留字典项，最多 20 条
        """
        if not isinstance(value, list):
            return []                                       # 非列表一律视为空
        return [item for item in value if isinstance(item, dict)][:20]   # 只保留字典项



# API 响应：一次预审的最新状态
class AIReviewResponse(BaseModel):
    """
    AI 预审接口响应（触发与查询共用）
        :ivar review_id: 预审记录ID
        :ivar expense_id: 报销单ID
        :ivar status: pending/running/success/fallback/failed
        :ivar risk_score: 风险分数（未完成时为空）
        :ivar risk_level: low/mid/high（未完成时为空）
        :ivar summary: 总结
        :ivar findings: 风险点
        :ivar policy_citations: 通过校验的制度引用
        :ivar is_fallback: 是否规则兜底
        :ivar model: 模型名（兜底为 rules）
        :ivar prompt_version: Prompt 版本
        :ivar policy_version: 本次使用的制度版本
        :ivar decision: 建议决策（fast_track/manual_review/suggest_reject）
        :ivar decision_reason: 建议决策说明
        :ivar workflow_run_id: 工作流运行ID
        :ivar latency_ms: 耗时
        :ivar error_message: 失败/降级原因（仅 failed/fallback 时有值）
        :ivar scheduled: 本次请求是否投递了后台任务
        :ivar created_at: 创建时间
        :ivar updated_at: 更新时间
    """
    review_id: int                                                     # 预审记录ID
    expense_id: int                                                    # 报销单ID
    status: AIReviewStatus                                             # 任务状态
    risk_score: Optional[Decimal] = None                               # 风险分数
    risk_level: Optional[str] = None                                   # 风险等级
    summary: Optional[str] = None                                      # 总结
    findings: list[AIFinding] = Field(default_factory=list)            # 风险点
    policy_citations: list[PolicyCitation] = Field(default_factory=list)  # 制度引用
    is_fallback: bool = False                                          # 是否兜底
    model: str = ""                                                    # 模型名
    prompt_version: str = ""                                           # Prompt 版本
    policy_version: str = ""                                           # 制度版本
    decision: str = ""                                                 # 建议决策
    decision_reason: str = ""                                          # 决策说明
    workflow_run_id: Optional[int] = None                              # 工作流运行ID
    latency_ms: Optional[int] = None                                   # 耗时
    prompt_tokens: Optional[int] = None                                # 输入 token
    completion_tokens: Optional[int] = None                            # 输出 token
    cost: Optional[Decimal] = None                                     # 估算费用（元）
    error_message: Optional[str] = None                                # 失败/降级原因
    scheduled: bool = False                                            # 是否已投递后台任务
    created_at: datetime                                               # 创建时间
    updated_at: Optional[datetime] = None                              # 更新时间

    model_config = ConfigDict(from_attributes=True)                    # 允许从 ORM 对象读取
