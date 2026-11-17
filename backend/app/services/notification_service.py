"""
站内通知服务：列表 / 未读数 / 标记已读
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Notification, User


def list_notifications(
    db: Session, user: User, *, page: int, page_size: int, unread_only: bool = False
) -> tuple[list[Notification], int]:
    """查当前用户的通知，返回 (当前页, 总数)"""
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))
    total = query.count()
    items = (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return items, total


def unread_count(db: Session, user: User) -> int:
    """未读数"""
    return (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
        .count()
    )


def mark_read(db: Session, user: User, notification_id: int) -> Notification:
    """标记单条已读；不存在或不属于当前用户抛 LookupError"""
    notification = (
        db.query(Notification)
        .filter(Notification.id == notification_id, Notification.user_id == user.id)
        .first()
    )
    if not notification:
        raise LookupError("通知不存在")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now()
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_read(db: Session, user: User) -> int:
    """全部标记已读，返回影响条数"""
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.is_read.is_(False))
        .update({Notification.is_read: True, Notification.read_at: datetime.now()}, synchronize_session=False)
    )
    db.commit()
    return int(count)
