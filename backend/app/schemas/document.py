"""
凭证识别输出契约（模型返回 → Pydantic 校验 → 业务归一化）
    设计取舍：
        - 模型输出是不可信输入，字段先按"宽松草稿"接收，再由 document_agent 归一化；
        - 金额可能是数字/字符串、日期可能是多种格式、document_type 可能写错，
          这些都在业务层处理，不能因为一个字段格式不对就整次失败；
        - API 响应用严格模型，字段类型固定，前端可直接展示。
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.document import DocumentExtractionStatus



# 模型返回的原始识别结果（宽松）
class DocumentExtractionOutput(BaseModel):
    """
    模型返回的凭证识别草稿
        :ivar document_type: 文档类型（业务层会归一化到白名单）
        :ivar amount: 金额（数字/字符串都可能）
        :ivar document_date: 票面日期（多种格式都可能）
        :ivar counterparty: 商户/平台/收款方
        :ivar identifier: 发票号/订单号/流水号
        :ivar confidence: 模型自评置信度 0~1
        :ivar summary: 一句话摘要
        :ivar extra: 类型专属字段（税号/税额/车次/起终点等）
    """
    document_type: str = "other"                                 # 文档类型
    amount: Any = None                                           # 金额（宽松接收）
    document_date: Optional[str] = None                          # 票面日期（宽松接收）
    counterparty: Optional[str] = None                           # 对方/商户
    identifier: Optional[str] = None                             # 凭证号
    confidence: Any = 0.0                                        # 置信度（宽松接收）
    summary: Optional[str] = None                                # 摘要
    extra: dict = Field(default_factory=dict)                    # 类型专属字段
    buyer_name: Optional[str] = None                             # 购买方名称（模型可能放顶层）
    buyer_tax_id: Optional[str] = None                           # 购买方税号（模型可能放顶层）
    seller_name: Optional[str] = None                            # 销售方名称（模型可能放顶层）
    seller_tax_id: Optional[str] = None                          # 销售方税号（模型可能放顶层）
    tax_amount: Any = None                                       # 税额（模型可能放顶层）

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)   # 容忍多余字段/数字转文本

    @field_validator("extra", mode="before")
    @classmethod
    def _extra_to_dict(cls, value):
        """
        把非字典的 extra 归一成空字典
            :param value: 原始值
            :return: 字典
        """
        return value if isinstance(value, dict) else {}

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_to_float(cls, value):
        """
        把置信度转成 0~1 的浮点数，非法值按 0 处理
            :param value: 原始值
            :return: 0~1 的浮点数
        """
        try:
            number = float(value)                               # 转浮点
        except (TypeError, ValueError):
            return 0.0                                          # 非法值兜底
        return min(max(number, 0.0), 1.0)                       # 夹取到 0~1

    @field_validator("document_type", mode="before")
    @classmethod
    def _type_to_str(cls, value):
        """
        把文档类型统一成去空白的小写字符串
            :param value: 原始值
            :return: 字符串
        """
        return str(value or "other").strip().lower()



# API 响应：一次凭证识别结果
class DocumentExtractionResponse(BaseModel):
    """
    凭证识别接口响应
        :ivar id: 识别记录ID
        :ivar attachment_id: 附件ID
        :ivar expense_item_id: 明细ID
        :ivar status: pending/running/success/fallback/failed
        :ivar document_type: 文档类型
        :ivar evidence_level: strong/medium/weak/none
        :ivar amount: 金额
        :ivar document_date: 票面日期
        :ivar counterparty: 商户/平台/收款方
        :ivar identifier: 发票号/订单号/流水号
        :ivar extracted_fields: 类型专属字段
        :ivar confidence: 置信度
        :ivar needs_manual_review: 是否建议人工核对
        :ivar provider/model/prompt_version: 审计信息
        :ivar error_message: 失败/降级原因
        :ivar latency_ms/prompt_tokens/completion_tokens: 成本信息
        :ivar created_at/updated_at: 时间
    """
    id: int                                                         # 识别记录ID
    attachment_id: int                                              # 附件ID
    expense_item_id: int                                            # 明细ID
    status: DocumentExtractionStatus                                # 任务状态
    document_type: Optional[str] = None                             # 文档类型
    evidence_level: Optional[str] = None                            # 证据强度
    amount: Optional[Decimal] = None                                # 金额
    document_date: Optional[date] = None                            # 票面日期
    counterparty: Optional[str] = None                              # 对方/商户
    identifier: Optional[str] = None                                # 凭证号
    extracted_fields: dict = Field(default_factory=dict)            # 类型专属字段
    confidence: Optional[Decimal] = None                            # 置信度
    needs_manual_review: bool = False                               # 是否建议人工核对
    provider: str = ""                                              # 模型提供方
    model: str = ""                                                 # 模型名
    prompt_version: str = ""                                        # Prompt 版本
    error_message: Optional[str] = None                             # 失败/降级原因
    latency_ms: Optional[int] = None                                # 耗时
    prompt_tokens: Optional[int] = None                             # 输入 token
    completion_tokens: Optional[int] = None                         # 输出 token
    created_at: datetime                                            # 创建时间
    updated_at: Optional[datetime] = None                           # 更新时间

    model_config = ConfigDict(from_attributes=True)                 # 允许从 ORM 对象读取

    @field_validator("extracted_fields", mode="before")
    @classmethod
    def _none_fields_to_dict(cls, value):
        """
        数据库里的 extracted_fields 可能为 NULL，响应统一成空字典
            :param value: 原始值
            :return: 字典
        """
        return value if isinstance(value, dict) else {}
