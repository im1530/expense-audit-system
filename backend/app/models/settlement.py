"""
结算流水模型（expense_settlements）

    记录“登记 / 撤销登记 / 提交人确认 / 管理员强制关闭”四个动作，只增不改，作为支付环节的审计轨迹。
"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, func

from app.models.base import Base


# 结算动作
ACTION_REGISTER = "register"            # 财务登记
ACTION_CANCEL = "cancel"                # 财务撤销登记
ACTION_CONFIRM = "confirm"              # 提交人确认领取
ACTION_FORCE_CLOSE = "force_close"      # 管理员强制关闭


class ExpenseSettlement(Base):
    """报销单结算流水"""

    __tablename__ = "expense_settlements"

    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="结算流水ID")
    expense_id = Column(BigInteger, ForeignKey("expenses.id"), nullable=False, comment="报销单ID")
    action = Column(String(20), nullable=False, comment="register/cancel/confirm/force_close")
    operator_id = Column(BigInteger, comment="操作人ID")
    note = Column(String(500), comment="备注/原因")
    channel = Column(String(20), comment="bank/alipay/wechat/cash")
    transaction_no = Column(String(64), unique=True, comment="流水号（可选，唯一）")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="操作时间")
