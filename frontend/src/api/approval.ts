import request from '@/utils/request'

/**
 * 我的待审批列表
 * @param params 分页与筛选
 */
export function listPending(params: { page?: number; page_size?: number; department_id?: number; user_id?: number }) {
  return request.get('/approval/pending', { params })
}

/**
 * 我的已办列表
 * @param params 分页与筛选
 */
export function listProcessed(params: { page?: number; page_size?: number }) {
  return request.get('/approval/processed', { params })
}

/**
 * 执行审批
 * @param expenseId 报销单ID
 * @param status approved / rejected
 * @param comment 审批意见
 */
export function approve(expenseId: number, status: 'approved' | 'rejected', comment?: string) {
  return request.post(`/approval/${expenseId}`, { status, comment })
}

/**
 * 审批结果字典
 */
export function listApprovalStatuses() {
  return request.get('/approval/statuses')
}
