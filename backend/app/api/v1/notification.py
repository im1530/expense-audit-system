"""
站内通知接口（挂载前缀 /api/v1/notifications）
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.dependencies import get_db
from app.models import User
from app.schemas.notification import NotificationItem, NotificationListResponse
from app.services import notification_service

router = APIRouter()


@router.get("", response_model=NotificationListResponse, summary="我的通知")
def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    unread_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """分页查询当前用户的通知"""
    items, total = notification_service.list_notifications(
        db, current_user, page=page, page_size=page_size, unread_only=unread_only
    )
    total_pages = (total + page_size - 1) // page_size
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/unread_count", summary="未读通知数")
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """铃铛红点数量"""
    return {"count": notification_service.unread_count(db, current_user)}


@router.post("/{notification_id}/read", response_model=NotificationItem, summary="标记已读")
def read_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记单条通知已读（只能操作自己的）"""
    try:
        return notification_service.mark_read(db, current_user, notification_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/read-all", summary="全部已读")
def read_all(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """把当前用户所有未读通知标记为已读"""
    return {"updated": notification_service.mark_all_read(db, current_user)}
