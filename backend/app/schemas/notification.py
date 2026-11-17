"""
站内通知契约
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class NotificationItem(BaseModel):
    """通知出参"""

    id: int
    type: str
    title: str
    content: str
    related_type: Optional[str] = None
    related_id: Optional[int] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    """通知分页响应"""

    items: list[NotificationItem]
    total: int
    page: int
    page_size: int
    total_pages: int
