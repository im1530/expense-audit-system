/**
 * 用户状态（Pinia store）单测
 * 覆盖：登录写令牌并拉资料、角色判定、强制改密标记、退出清状态
 * 说明：api/auth 全部 mock，不发真实请求
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/auth', () => ({
  login: vi.fn(),
  getMe: vi.fn(),
  changePassword: vi.fn(),
}))

import { getMe, login } from '@/api/auth'
import { useUserStore } from '@/stores/user'
import { getToken } from '@/utils/request'

const employee = { id: 1, username: 'emp', full_name: '员工甲', role: 'employee' }
const finance = { id: 2, username: 'fin', full_name: '财务乙', role: 'finance' }
const admin = { id: 3, username: 'admin', full_name: '管理员', role: 'admin' }

describe('useUserStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(getMe).mockReset()
    vi.mocked(login).mockReset()
  })

  it('登录成功后保存令牌并拉取用户资料', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer', must_change_password: false } as any)
    vi.mocked(getMe).mockResolvedValue(employee as any)

    const store = useUserStore()
    await store.login('emp', 'pwd')

    expect(store.isLoggedIn).toBe(true)
    expect(getToken()).toBe('tk')
    expect(store.user?.username).toBe('emp')
    expect(store.displayName).toBe('员工甲')
  })

  it('首登必须改密时置 mustChangePassword', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer', must_change_password: true } as any)
    vi.mocked(getMe).mockResolvedValue(employee as any)

    const store = useUserStore()
    await store.login('emp', 'pwd')
    expect(store.mustChangePassword).toBe(true)
  })

  it('角色判定：employee 不能审批、不是管理员', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer' } as any)
    vi.mocked(getMe).mockResolvedValue(employee as any)

    const store = useUserStore()
    await store.login('emp', 'pwd')
    expect(store.canApprove).toBe(false)
    expect(store.isAdmin).toBe(false)
    expect(store.canViewReports).toBe(false)
  })

  it('角色判定：finance 可审批可看报表但不是管理员', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer' } as any)
    vi.mocked(getMe).mockResolvedValue(finance as any)

    const store = useUserStore()
    await store.login('fin', 'pwd')
    expect(store.canApprove).toBe(true)
    expect(store.canViewReports).toBe(true)
    expect(store.isAdmin).toBe(false)
  })

  it('角色判定：admin 是管理员但不能审批', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer' } as any)
    vi.mocked(getMe).mockResolvedValue(admin as any)

    const store = useUserStore()
    await store.login('admin', 'pwd')
    expect(store.isAdmin).toBe(true)
    expect(store.canApprove).toBe(false)
  })

  it('退出登录清空令牌、用户与本地存储', async () => {
    vi.mocked(login).mockResolvedValue({ access_token: 'tk', token_type: 'bearer' } as any)
    vi.mocked(getMe).mockResolvedValue(employee as any)

    const store = useUserStore()
    await store.login('emp', 'pwd')
    store.logout()

    expect(store.isLoggedIn).toBe(false)
    expect(store.user).toBeNull()
    expect(getToken()).toBe('')
  })
})
