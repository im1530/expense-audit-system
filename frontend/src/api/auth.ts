import request from '@/utils/request'

/**
 * 登录（后端是 OAuth2 表单入参，不是 JSON）
 * @param username 用户名或邮箱
 * @param password 明文密码
 * @returns access_token / token_type / must_change_password
 */
export function login(username: string, password: string) {
  const form = new URLSearchParams()
  form.append('username', username)
  form.append('password', password)
  form.append('grant_type', 'password')
  return request.post('/auth/login', form, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  })
}

/**
 * 当前登录用户信息（角色、部门、AI 授权状态）
 */
export function getMe() {
  return request.get('/auth/me')
}

/**
 * 修改密码（首登强制改密也走这里）
 * @param oldPassword 旧密码
 * @param newPassword 新密码
 */
export function changePassword(oldPassword: string, newPassword: string) {
  return request.post('/auth/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
}

/**
 * 授权/撤回 AI 数据处理
 * @param consent 是否授权
 */
export function updateAiConsent(consent: boolean) {
  return request.post('/auth/ai-consent', { consent })
}

/**
 * 注册（公开接口，注册出来是 employee 角色）
 * @param payload 注册信息
 */
export function register(payload: {
  username: string
  email: string
  password: string
  full_name?: string
  phone?: string
  department_id?: number
}) {
  return request.post('/auth/register', payload)
}
