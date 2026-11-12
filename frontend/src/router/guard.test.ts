/**
 * 路由守卫单测
 * 覆盖：公开页放行、未登录跳登录、刷新时自动拉资料、角色权限拦截、强制改密不拦
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  getMe: vi.fn(),
  changePassword: vi.fn(),
}))

import { getMe } from '@/api/auth'
import { authGuard } from '@/router'
import { useUserStore } from '@/stores/user'
import { setToken } from '@/utils/request'

/**
 * 构造一个最小的路由对象
 * @param path 路径
 * @param meta 路由元信息
 */
function route(path: string, meta: Record<string, any> = {}) {
  return { path, fullPath: path, meta }
}

describe('authGuard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setToken('')
    vi.mocked(getMe).mockReset()
  })

  it('公开页面直接放行', async () => {
    expect(await authGuard(route('/login', { public: true }))).toBe(true)
  })

  it('未登录访问受保护页面：跳登录并带 redirect', async () => {
    const result: any = await authGuard(route('/expenses'))
    expect(result.path).toBe('/login')
    expect(result.query.redirect).toBe('/expenses')
  })

  it('已登录但资料未加载：先拉资料再放行', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 1, username: 'emp', role: 'employee' } as any)

    const store = useUserStore()
    store.token = 'tk'

    expect(await authGuard(route('/dashboard'))).toBe(true)
    expect(getMe).toHaveBeenCalledTimes(1)
    expect(store.user?.role).toBe('employee')
  })

  it('拉资料失败：清状态并跳登录', async () => {
    vi.mocked(getMe).mockRejectedValue(new Error('401'))
    const store = useUserStore()
    store.token = 'tk'

    const result: any = await authGuard(route('/dashboard'))
    expect(result.path).toBe('/login')
    expect(store.token).toBe('')
  })

  it('员工访问审批中心被拦回工作台', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 1, username: 'emp', role: 'employee' } as any)
    const store = useUserStore()
    store.token = 'tk'
    await store.fetchMe()

    const result: any = await authGuard(route('/approvals', { requiresApprove: true }))
    expect(result.path).toBe('/dashboard')
  })

  it('财务访问审批中心放行', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 2, username: 'fin', role: 'finance' } as any)
    const store = useUserStore()
    store.token = 'tk'
    await store.fetchMe()

    expect(await authGuard(route('/approvals', { requiresApprove: true }))).toBe(true)
  })

  it('非管理员访问系统管理被拦回工作台', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 2, username: 'fin', role: 'finance' } as any)
    const store = useUserStore()
    store.token = 'tk'
    await store.fetchMe()

    const result: any = await authGuard(route('/admin/users', { requiresAdmin: true }))
    expect(result.path).toBe('/dashboard')
  })

  it('管理员访问系统管理放行', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 3, username: 'admin', role: 'admin' } as any)
    const store = useUserStore()
    store.token = 'tk'
    await store.fetchMe()

    expect(await authGuard(route('/admin/users', { requiresAdmin: true }))).toBe(true)
  })

  it('强制改密状态不跳登录页（由布局弹窗处理）', async () => {
    setToken('tk')
    vi.mocked(getMe).mockResolvedValue({ id: 1, username: 'emp', role: 'employee' } as any)
    const store = useUserStore()
    store.token = 'tk'
    store.mustChangePassword = true
    await store.fetchMe()

    expect(await authGuard(route('/dashboard'))).toBe(true)
  })
})
