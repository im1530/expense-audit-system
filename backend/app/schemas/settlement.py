"""
结算相关契约（登记 / 撤销 / 强制关闭 / 结算流水）

    说明：channel、transaction_no 均为可选，登记时只要求状态与权限。
"""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# 支付渠道白名单
SettlementChannel = Literal["bank", "alipay", "wechat", "cash"]


class ExpenseRegisterRequest(BaseModel):
    """财务登记入参"""

    note: Optional[str] = Field(None, max_length=500, description="发给提交人的备注，选填")
    channel: Optional[SettlementChannel] = Field(None, description="支付渠道，选填")
    transaction_no: Optional[str] = Field(None, max_length=64, description="流水号，选填且唯一")


class CancelRegistrationRequest(BaseModel):
    """财务撤销登记入参"""

    reason: Optional[str] = Field(None, max_length=200, description="撤销原因，选填")


class ForceCloseRequest(BaseModel):
    """管理员强制关闭入参"""

    reason: str = Field(..., min_length=1, max_length=200, description="强制关闭原因，必填")


class ExpenseSettlementItem(BaseModel):
    """结算流水出参"""

    id: int
    expense_id: int
    action: str
    operator_id: Optional[int] = None
    note: Optional[str] = None
    channel: Optional[str] = None
    transaction_no: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
