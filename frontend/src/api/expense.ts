import request from '@/utils/request'

// 列表查询参数（与后端 BusinessFilter + PageParams 对齐）
export interface ExpenseQuery {
  page?: number
  page_size?: number
  status?: string
  expense_type?: string
  category_id?: number
  user_id?: number
  department_id?: number
  start_date?: string
  end_date?: string
  min_amount?: number
  max_amount?: number
}

/**
 * 我的报销单（分页）
 * @param params 查询条件
 */
export function listMyExpenses(params: ExpenseQuery) {
  return request.get('/expense/my', { params })
}

/**
 * 本部门报销单（经理可见）
 * @param params 查询条件
 */
export function listDepartmentExpenses(params: ExpenseQuery) {
  return request.get('/expense/department', { params })
}

/**
 * 全公司报销单（财务/老板可见）
 * @param params 查询条件
 */
export function listCompanyExpenses(params: ExpenseQuery) {
  return request.get('/expense/company', { params })
}

/**
 * 报销单详情（含明细、规则命中）
 * @param id 报销单ID
 */
export function getExpense(id: number) {
  return request.get(`/expense/${id}`)
}

/**
 * 新建报销单（草稿）
 * @param payload 主单 + 明细
 */
export function createExpense(payload: any) {
  return request.post('/expense/', payload)
}

/**
 * 修改报销单（仅草稿/已驳回）
 * @param id 报销单ID
 * @param payload 待改字段
 */
export function updateExpense(id: number, payload: any) {
  return request.put(`/expense/${id}`, payload)
}

/**
 * 提交送审（draft → pending，触发规则指派与自动 AI 预审）
 * @param id 报销单ID
 */
export function submitExpense(id: number) {
  return request.post(`/expense/${id}/submit`)
}

/**
 * 撤回提交（pending → draft）
 * @param id 报销单ID
 */
export function recallExpense(id: number) {
  return request.post(`/expense/${id}/recall`)
}

/**
 * 主动撤销单据
 * @param id 报销单ID
 */
export function cancelExpense(id: number) {
  return request.post(`/expense/${id}/cancel`)
}

/**
 * 驳回后重新提交
 * @param id 报销单ID
 */
export function resubmitExpense(id: number) {
  return request.post(`/expense/${id}/resubmit`)
}

/**
 * 财务支付确认（已废弃：改用登记 + 提交人确认）
 * @param id 报销单ID
 */
export function payExpense(id: number) {
  return request.post(`/expense/${id}/pay`)
}

/** 财务登记（审批通过 → 待领取） */
export function registerExpense(id: number, payload: { note?: string; channel?: string; transaction_no?: string }) {
  return request.post(`/expense/${id}/register`, payload)
}

/** 财务撤销登记（待领取 → 已通过） */
export function cancelRegistration(id: number, payload: { reason?: string }) {
  return request.post(`/expense/${id}/cancel-registration`, payload)
}

/** 提交人确认领取（待领取 → 已报销） */
export function confirmReceived(id: number) {
  return request.post(`/expense/${id}/confirm-received`)
}

/** 管理员强制关闭（待领取 → 已报销） */
export function forceCloseExpense(id: number, payload: { reason: string }) {
  return request.post(`/expense/${id}/force-close`, payload)
}

/** 结算流水 */
export function listSettlements(id: number) {
  return request.get(`/expense/${id}/settlements`)
}

/**
 * 提交前规则预检（不改状态，只告诉前端会命中什么）
 * @param id 报销单ID
 */
export function precheckExpense(id: number) {
  return request.post(`/expense/${id}/precheck`)
}

/**
 * 审批轨迹（时间线）
 * @param id 报销单ID
 */
export function getApprovalHistory(id: number) {
  return request.get(`/expense/${id}/approval_history`)
}

/**
 * 给单据追加一条明细
 * @param id 报销单ID
 * @param payload 明细
 */
export function addExpenseItem(id: number, payload: any) {
  return request.post('/expense/items', { expense_id: id, ...payload })
}

/**
 * 修改明细
 * @param expenseId 报销单ID
 * @param itemId 明细ID
 * @param payload 待改字段
 */
export function updateExpenseItem(expenseId: number, itemId: number, payload: any) {
  return request.put(`/expense/${expenseId}/items/${itemId}`, payload)
}

/**
 * 删除明细
 * @param expenseId 报销单ID
 * @param itemId 明细ID
 */
export function deleteExpenseItem(expenseId: number, itemId: number) {
  return request.delete(`/expense/${expenseId}/items/${itemId}`)
}

/**
 * 费用类别字典
 */
export function listCategories() {
  return request.get('/expense/category')
}

/**
 * 单据状态字典
 */
export function listStatuses() {
  return request.get('/expense/statuses')
}

/**
 * 费用大类字典
 */
export function listExpenseTypes() {
  return request.get('/expense/expense-types')
}

/**
 * 为明细上传发票（multipart）
 * @param itemId 明细ID
 * @param file 文件
 */
export function uploadInvoice(itemId: number, file: File) {
  const form = new FormData()
  form.append('file', file)
  return request.post(`/attachment/item/${itemId}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

/**
 * 明细下的发票列表
 * @param itemId 明细ID
 */
export function listInvoices(itemId: number) {
  return request.get(`/attachment/item/${itemId}`)
}

/**
 * 删除发票
 * @param attachmentId 附件ID
 */
export function deleteInvoice(attachmentId: number) {
  return request.delete(`/attachment/${attachmentId}`)
}

/**
 * AI 补充核查（只读 ReAct Agent，不改变审批结果）
 * @param id 报销单ID
 * @param question 可选核查问题
 */
export function investigateExpense(id: number, question?: string) {
  return request.post(`/expense/${id}/investigate`, question ? { question } : {})
}
