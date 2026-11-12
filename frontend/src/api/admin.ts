import request from '@/utils/request'

// ===== 用户管理 =====

/**
 * 用户列表（管理端）
 * @param params 分页与筛选
 */
export function listAdminUsers(params: {
  page?: number
  page_size?: number
  role?: string
  department_id?: number
  is_active?: boolean
  keyword?: string
}) {
  return request.get('/admin/user', { params })
}

/**
 * 新建用户
 * @param payload 账号信息（密码为明文，服务端哈希）
 */
export function createAdminUser(payload: any) {
  return request.post('/admin/user', payload)
}

/**
 * 用户详情
 * @param userId 用户ID
 */
export function getAdminUser(userId: number) {
  return request.get(`/admin/user/${userId}`)
}

/**
 * 修改用户（可改角色、部门、启停）
 * @param userId 用户ID
 * @param payload 全量字段
 */
export function updateAdminUser(userId: number, payload: any) {
  return request.put(`/admin/user/${userId}`, payload)
}

/**
 * 重置用户密码（管理员操作，触发 must_change_password）
 * @param userId 用户ID
 * @param newPassword 新密码
 */
export function resetUserPassword(userId: number, newPassword: string) {
  return request.post(`/admin/user/${userId}/reset-password`, { new_password: newPassword })
}

// ===== 部门管理 =====

/**
 * 部门列表（管理端）
 * @param params 分页与筛选
 */
export function listAdminDepartments(params: { page?: number; page_size?: number; is_active?: boolean; keyword?: string }) {
  return request.get('/admin/department', { params })
}

/**
 * 新建部门
 * @param payload 名称 + 备注
 */
export function createAdminDepartment(payload: { name: string; description?: string }) {
  return request.post('/admin/department', payload)
}

/**
 * 修改部门
 * @param departmentId 部门ID
 * @param payload 全量字段
 */
export function updateAdminDepartment(departmentId: number, payload: any) {
  return request.put(`/admin/department/${departmentId}`, payload)
}

// ===== 规则管理 =====

/**
 * 规则列表
 * @param params 分页与筛选
 */
export function listAdminRules(params: {
  page?: number
  page_size?: number
  rule_type?: string
  is_active?: boolean
  keyword?: string
}) {
  return request.get('/admin/rule', { params })
}

/**
 * 新建规则
 * @param payload 规则定义（config 为 JSON 对象）
 */
export function createAdminRule(payload: any) {
  return request.post('/admin/rule', payload)
}

/**
 * 修改规则
 * @param ruleId 规则ID
 * @param payload 全量字段
 */
export function updateAdminRule(ruleId: number, payload: any) {
  return request.put(`/admin/rule/${ruleId}`, payload)
}

/**
 * 删除规则（逻辑删除）
 * @param ruleId 规则ID
 */
export function deleteAdminRule(ruleId: number) {
  return request.delete(`/admin/rule/${ruleId}`)
}

/**
 * 规则命中率统计
 * @param params 日期区间
 */
export function getRuleHitStats(params: { start_date?: string; end_date?: string }) {
  return request.get('/admin/rule/hit-stats', { params })
}

// ===== 审批流管理 =====

/**
 * 审批流列表
 * @param params 分页与筛选
 */
export function listAdminFlows(params: { page?: number; page_size?: number; is_active?: boolean; keyword?: string }) {
  return request.get('/admin/approval-flow', { params })
}

/**
 * 新建审批流
 * @param payload 名称 + 层级数组
 */
export function createAdminFlow(payload: any) {
  return request.post('/admin/approval-flow', payload)
}

/**
 * 修改审批流
 * @param flowId 流程ID
 * @param payload 全量字段
 */
export function updateAdminFlow(flowId: number, payload: any) {
  return request.put(`/admin/approval-flow/${flowId}`, payload)
}

/**
 * 删除审批流
 * @param flowId 流程ID
 */
export function deleteAdminFlow(flowId: number) {
  return request.delete(`/admin/approval-flow/${flowId}`)
}

// ===== 字典 =====

/**
 * 角色字典
 */
export function listRoles() {
  return request.get('/user/roles')
}

/**
 * 部门下拉（公开接口，已启用部门）
 */
export function listDepartments() {
  return request.get('/department/')
}
