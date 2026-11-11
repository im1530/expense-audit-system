import { defineStore } from 'pinia'
import { login as apiLogin, getMe, changePassword as apiChangePassword } from '@/api/auth'
import { getToken, setToken } from '@/utils/request'

// 用户状态：令牌 + 当前用户资料（角色决定菜单与按钮权限）
export const useUserStore = defineStore('user', {
  state: () => ({
    token: getToken(),
    user: null as any,
    mustChangePassword: false,
  }),

  getters: {
    /** 是否已登录 */
    isLoggedIn: (state) => !!state.token,
    /** 角色：admin/boss/finance/manager/employee */
    role: (state) => state.user?.role || '',
    /** 是否管理员（可进管理端） */
    isAdmin: (state) => state.user?.role === 'admin',
    /** 是否有审批资格（经理/财务/老板） */
    canApprove: (state) => ['manager', 'finance', 'boss'].includes(state.user?.role || ''),
    /** 是否能看全公司报表（财务/老板/admin 视配置） */
    canViewReports: (state) => ['finance', 'boss', 'admin'].includes(state.user?.role || ''),
    /** 显示名 */
    displayName: (state) => state.user?.full_name || state.user?.username || '',
  },

  actions: {
    /**
     * 登录：拿令牌 → 拉取用户资料
     * @param username 用户名/邮箱
     * @param password 密码
     */
    async login(username: string, password: string) {
      const data: any = await apiLogin(username, password)
      this.token = data.access_token
      setToken(data.access_token)
      this.mustChangePassword = !!data.must_change_password
      await this.fetchMe()
      return data
    },

    /**
     * 拉取当前用户资料（角色/部门/授权状态）
     */
    async fetchMe() {
      const user: any = await getMe()
      this.user = user
      return user
    },

    /**
     * 修改密码（首登强制改密也走这里），成功后换新令牌
     * @param oldPassword 旧密码
     * @param newPassword 新密码
     */
    async changePassword(oldPassword: string, newPassword: string) {
      const data: any = await apiChangePassword(oldPassword, newPassword)
      this.token = data.access_token
      setToken(data.access_token)
      this.mustChangePassword = false
      this.user = data.user
      return data
    },

    /**
     * 退出登录：清空本地状态
     */
    logout() {
      this.token = ''
      this.user = null
      this.mustChangePassword = false
      setToken('')
    },
  },
})
