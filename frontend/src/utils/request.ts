import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

// 统一 Axios 实例：所有请求走 /api/v1，开发期由 Vite 代理到后端
const request = axios.create({
  baseURL: '/api/v1',
  timeout: 60000, // AI 预审可能较慢，给 60 秒
})

// 令牌存放位置（与 user store 保持一致）
const TOKEN_KEY = 'expense_audit_token'

/**
 * 读取本地令牌
 * @returns 令牌字符串；未登录返回空串
 */
export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

/**
 * 写入/清除令牌
 * @param token 令牌；传空表示清除
 */
export function setToken(token: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

// 请求拦截：自动带上 Authorization
request.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：直接返回 data；错误统一提示；401 回登录页
request.interceptors.response.use(
  (response) => {
    // 需要读响应头（如文件下载的 Content-Disposition）时，调用方传 rawResponse: true
    if ((response.config as any).rawResponse) {
      return response
    }
    return response.data
  },
  (error) => {
    const status = error.response?.status
    const data = error.response?.data || {}
    const message = data.message || data.detail || error.message || '请求失败'

    if (status === 401) {
      setToken('')
      // 避免在登录页重复跳转
      if (router.currentRoute.value.path !== '/login') {
        ElMessage.warning('登录已过期，请重新登录')
        router.replace({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
      }
      return Promise.reject(error)
    }

    if (status === 429) {
      ElMessage.warning(message)
    } else {
      ElMessage.error(typeof message === 'string' ? message : '请求失败')
    }
    return Promise.reject(error)
  },
)

export default request
