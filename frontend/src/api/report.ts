import request from '@/utils/request'

/**
 * 报表总览
 * @param params 日期区间/部门
 */
export function listOverview(params: { start_date?: string; end_date?: string; department_id?: number }) {
  return request.get('/report/overview', { params })
}

/**
 * 按月统计
 * @param params 日期区间/部门
 */
export function listMonthly(params: { start_date?: string; end_date?: string; department_id?: number }) {
  return request.get('/report/monthly', { params })
}

/**
 * 按部门统计
 * @param params 日期区间
 */
export function listDepartments(params: { start_date?: string; end_date?: string }) {
  return request.get('/report/department', { params })
}

/**
 * 按费用类别统计
 * @param params 日期区间/部门
 */
export function listCategories(params: { start_date?: string; end_date?: string; department_id?: number }) {
  return request.get('/report/category', { params })
}

/**
 * 导出 Excel（返回二进制）
 * @param params 日期区间/部门
 */
export function exportReport(params: { start_date?: string; end_date?: string; department_id?: number }) {
  return request.get('/report/export', { params, responseType: 'blob' })
}
