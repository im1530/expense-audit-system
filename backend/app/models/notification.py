"""
站内通知模型（notifications）

    只用于系统内提醒（如“报销单已登记，可领取”），不接邮件/短信。
"""
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, String, func

from app.models.base import Base


# 通知类型
TYPE_EXPENSE_REGISTERED = "expense_registered"              # 财务已登记，通知提交人领取
TYPE_REGISTRATION_CANCELLED = "registration_cancelled"      # 财务撤销登记
TYPE_EXPENSE_CONFIRMED = "expense_confirmed"                # 提交人已确认领取
TYPE_FORCE_CLOSED = "force_closed"                          # 管理员强制关闭


class Notification(Base):
    """站内通知"""

    __tablename__ = "notifications"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="通知ID")
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False, comment="接收人ID")
    type = Column(String(40), nullable=False, comment="通知类型")
    title = Column(String(100), nullable=False, comment="通知标题")
    content = Column(String(500), nullable=False, comment="通知内容")
    related_type = Column(String(20), comment="关联对象类型")
    related_id = Column(BigInteger, comment="关联对象ID")
    is_read = Column(Boolean, nullable=False, default=False, comment="是否已读")
    read_at = Column(DateTime, comment="已读时间")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="创建时间")
