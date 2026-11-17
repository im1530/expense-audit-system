import request from '@/utils/request'

/** 我的通知分页 */
export function listNotifications(params: { page?: number; page_size?: number; unread_only?: boolean }) {
  return request.get('/notifications', { params })
}

/** 未读数 */
export function getUnreadCount() {
  return request.get('/notifications/unread_count')
}

/** 标记已读 */
export function readNotification(id: number) {
  return request.post(`/notifications/${id}/read`)
}

/** 全部已读 */
export function readAllNotifications() {
  return request.post('/notifications/read-all')
}
