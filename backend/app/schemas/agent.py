"""AI 核查 Agent 的 API 出参"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InvestigationRequest(BaseModel):
    """可选的核查问题；不传使用默认问题"""
    question: str | None = Field(None, max_length=500)


class InvestigationStep(BaseModel):
    """一次工具调用"""
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "called"
    result: str | None = None


class InvestigationResponse(BaseModel):
    """核查结果"""
    id: int
    expense_id: int
    status: str
    question: str | None = None
    report: str | None = None
    trace: list[InvestigationStep] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
