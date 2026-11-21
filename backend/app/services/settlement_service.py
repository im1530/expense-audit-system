"""
结算服务：财务登记 → 提交人确认领取（两阶段）

    状态机：
        approved --register(finance，且非提交人本人）--> registered
        registered --cancel(finance，且非提交人本人）--> approved
        registered --confirm(提交人本人）--> paid
        registered --force_close(admin）--> paid
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Expense, Notification, User, UserRole
from app.models.expense import ExpenseStatus
from app.models.notification import (
    TYPE_EXPENSE_CONFIRMED,
    TYPE_EXPENSE_REGISTERED,
    TYPE_FORCE_CLOSED,
    TYPE_REGISTRATION_CANCELLED,
)
from app.models.settlement import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_FORCE_CLOSE,
    ACTION_REGISTER,
    ExpenseSettlement,
)


class SettlementError(Exception):
    """结算业务错误（由路由转成 HTTP 状态码）"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _get_expense(db: Session, expense_id: int, *, for_update: bool = False) -> Expense:
    """查单据，不存在抛 404；写操作传 for_update=True 行锁，防止并发重复登记/确认"""
    query = db.query(Expense).filter(Expense.id == expense_id)
    if for_update:
        query = query.with_for_update()
    expense = query.first()
    if not expense:
        raise SettlementError("报销单不存在", 404)
    return expense


def _notify(db: Session, user_id: int, type_: str, title: str, content: str, expense_id: int) -> None:
    """写一条站内通知（不提交事务）"""
    db.add(Notification(
        user_id=user_id,
        type=type_,
        title=title,
        content=content[:500],
        related_type="expense",
        related_id=expense_id,
    ))


def register(
    db: Session,
    expense_id: int,
    current_user: User,
    *,
    note: str | None = None,
    channel: str | None = None,
    transaction_no: str | None = None,
) -> Expense:
    """
    财务登记：approved → registered，并通知提交人
    """
    if not current_user.has_permission("pay"):
        raise SettlementError("无支付/登记权限", 403)
    expense = _get_expense(db, expense_id, for_update=True)
    if expense.status != ExpenseStatus.APPROVED:
        raise SettlementError("仅审批通过的报销单可以登记", 409)
    if expense.user_id == current_user.id:
        raise SettlementError("不能登记本人提交的报销单，请由其他财务登记", 403)

    expense.status = ExpenseStatus.REGISTERED
    expense.registered_at = datetime.now()
    expense.registered_by = current_user.id
    expense.register_note = note
    db.add(ExpenseSettlement(
        expense_id=expense.id,
        action=ACTION_REGISTER,
        operator_id=current_user.id,
        note=note,
        channel=channel,
        transaction_no=transaction_no,
    ))
    content = f"您的《{expense.title}》报销单已审核通过，金额 ¥{expense.total_amount:.2f}，可进行费用报销。"
    if note:
        content += f"备注：{note}"
    _notify(db, expense.user_id, TYPE_EXPENSE_REGISTERED, "报销单已登记，待领取", content, expense.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SettlementError("流水号已存在，请勿重复登记", 409) from exc
    db.refresh(expense)
    return expense


def cancel_registration(
    db: Session, expense_id: int, current_user: User, *, reason: str | None = None
) -> Expense:
    """撤销登记：registered → approved"""
    if not current_user.has_permission("pay"):
        raise SettlementError("无支付/登记权限", 403)
    expense = _get_expense(db, expense_id, for_update=True)
    if expense.status != ExpenseStatus.REGISTERED:
        raise SettlementError("仅已登记待领取的报销单可以撤销登记", 409)
    if expense.user_id == current_user.id:
        raise SettlementError("不能撤销本人提交的报销单的登记，请由其他财务处理", 403)

    expense.status = ExpenseStatus.APPROVED
    expense.registered_at = None
    expense.registered_by = None
    expense.register_note = None
    db.add(ExpenseSettlement(
        expense_id=expense.id,
        action=ACTION_CANCEL,
        operator_id=current_user.id,
        note=reason,
    ))
    _notify(
        db, expense.user_id, TYPE_REGISTRATION_CANCELLED, "报销登记已撤销",
        f"您的《{expense.title}》报销登记已撤销，单据回到“已通过”，请等待财务重新登记。", expense.id,
    )
    db.commit()
    db.refresh(expense)
    return expense


def confirm_received(db: Session, expense_id: int, current_user: User) -> Expense:
    """提交人确认领取：registered → paid"""
    expense = _get_expense(db, expense_id, for_update=True)
    if expense.user_id != current_user.id:
        raise SettlementError("只有提交人本人可以确认领取", 403)
    if expense.status != ExpenseStatus.REGISTERED:
        raise SettlementError("仅已登记待领取的报销单可以确认领取", 409)

    now = datetime.now()
    expense.status = ExpenseStatus.PAID
    expense.paid_at = now
    expense.confirmed_at = now
    db.add(ExpenseSettlement(
        expense_id=expense.id,
        action=ACTION_CONFIRM,
        operator_id=current_user.id,
    ))
    if expense.registered_by:
        _notify(
            db, expense.registered_by, TYPE_EXPENSE_CONFIRMED, "报销单已确认领取",
            f"《{expense.title}》的提交人已确认领取，金额 ¥{expense.total_amount:.2f}。", expense.id,
        )
    db.commit()
    db.refresh(expense)
    return expense


def force_close(db: Session, expense_id: int, current_user: User, *, reason: str) -> Expense:
    """管理员强制关闭：registered → closed（不算提交人确认领取）"""
    role = current_user.role.value if hasattr(current_user.role, "value") else current_user.role
    if role != UserRole.ADMIN.value:
        raise SettlementError("只有管理员可以强制关闭", 403)
    expense = _get_expense(db, expense_id, for_update=True)
    if expense.status != ExpenseStatus.REGISTERED:
        raise SettlementError("仅已登记待领取的报销单可以强制关闭", 409)

    now = datetime.now()
    expense.status = ExpenseStatus.CLOSED
    expense.closed_at = now
    expense.paid_at = None
    expense.confirmed_at = None
    db.add(ExpenseSettlement(
        expense_id=expense.id,
        action=ACTION_FORCE_CLOSE,
        operator_id=current_user.id,
        note=reason,
    ))
    _notify(
        db, expense.user_id, TYPE_FORCE_CLOSED, "报销单已由管理员关闭",
        f"您的《{expense.title}》已由管理员确认关闭，原因：{reason}", expense.id,
    )
    if expense.registered_by:
        _notify(
            db, expense.registered_by, TYPE_FORCE_CLOSED, "登记单已由管理员关闭",
            f"《{expense.title}》已由管理员强制关闭，原因：{reason}", expense.id,
        )
    db.commit()
    db.refresh(expense)
    return expense


def list_settlements(db: Session, expense_id: int, current_user: User) -> list[ExpenseSettlement]:
    """查结算流水：提交人本人、审批人、可看全公司者"""
    expense = _get_expense(db, expense_id)
    if (
        expense.user_id != current_user.id
        and not current_user.has_permission("approve")
        and not current_user.has_permission("view_all")
    ):
        raise SettlementError("无权限查看此报销单的结算记录", 403)
    return (
        db.query(ExpenseSettlement)
        .filter(ExpenseSettlement.expense_id == expense_id)
        .order_by(ExpenseSettlement.created_at.asc(), ExpenseSettlement.id.asc())
        .all()
    )
