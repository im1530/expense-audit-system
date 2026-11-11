import request from '@/utils/request'

/**
 * 触发 AI 预审（后台投递，返回 202 或已有结果）
 * @param expenseId 报销单ID
 * @param force 是否忽略缓存强制重跑
 */
export function triggerReview(expenseId: number, force = false) {
  return request.post(`/agent/review/${expenseId}`, null, { params: force ? { force: true } : {} })
}

/**
 * 查询最新一次预审结果
 * @param expenseId 报销单ID
 */
export function getReview(expenseId: number) {
  return request.get(`/agent/review/${expenseId}`)
}

/**
 * 查询工作流轨迹（节点耗时/token、决策、引用、凭证结论）
 * @param expenseId 报销单ID
 */
export function getTrace(expenseId: number) {
  return request.get(`/agent/review/${expenseId}/trace`)
}
