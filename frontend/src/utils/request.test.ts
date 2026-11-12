/**
 * Axios 实例单测
 * 覆盖：令牌读写、请求头注入、响应解包、rawResponse 透传、401 清令牌并跳登录
 * 说明：用自定义 adapter 拦截请求，不发真实网络请求
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 路由用 mock：只关心是否被调用 replace
// 注意：vi.mock 会被提升到文件顶部，工厂里引用的变量必须用 vi.hoisted 提前创建
const { replace } = vi.hoisted(() => ({ replace: vi.fn() }))
vi.mock('@/router', () => ({
  default: {
    currentRoute: { value: { path: '/expenses', fullPath: '/expenses' } },
    replace,
  },
}))

import request, { getToken, setToken } from '@/utils/request'

/**
 * 安装一个假 adapter
 * 返回体必须带上真实 config，否则拦截器读不到 rawResponse 这类自定义标记
 * @param handler 根据请求配置生成响应（data/headers 等）
 */
function useAdapter(handler: (config: any) => any) {
  const adapter = vi.fn(async (config: any) => ({
    status: 200,
    statusText: 'OK',
    headers: {},
    ...(await handler(config)),
    config,
  }))
  ;(request.defaults as any).adapter = adapter
  return adapter
}

describe('令牌读写', () => {
  beforeEach(() => {
    setToken('')
  })

  it('写入后能读出，清除后为空串', () => {
    setToken('token-123')
    expect(getToken()).toBe('token-123')
    setToken('')
    expect(getToken()).toBe('')
  })
})

describe('请求拦截器', () => {
  it('有令牌时自动加 Authorization 头', async () => {
    setToken('abc')
    const adapter = useAdapter(() => ({ data: { ok: true } }))

    await request.get('/expense/my')
    expect(adapter.mock.calls[0][0].headers.Authorization).toBe('Bearer abc')
  })

  it('没有令牌时不带 Authorization 头', async () => {
    setToken('')
    const adapter = useAdapter(() => ({ data: {} }))

    await request.get('/expense/my')
    expect(adapter.mock.calls[0][0].headers.Authorization).toBeUndefined()
  })
})

describe('响应拦截器', () => {
  it('默认直接返回 data，调用方不用再取一层', async () => {
    useAdapter(() => ({ data: { total: 3 } }))
    const data: any = await request.get('/expense/my')
    expect(data).toEqual({ total: 3 })
  })

  it('rawResponse=true 时返回完整响应（下载要读响应头）', async () => {
    useAdapter(() => ({
      data: 'blob',
      headers: { 'content-disposition': 'attachment; filename="a.xlsx"' },
    }))
    const response: any = await request.get('/report/export', { rawResponse: true } as any)
    expect(response.data).toBe('blob')
    expect(response.headers['content-disposition']).toContain('a.xlsx')
  })

  it('401 时清空令牌并跳转登录页', async () => {
    setToken('expired')
    replace.mockClear()
    ;(request.defaults as any).adapter = async (config: any) => {
      const error: any = new Error('Unauthorized')
      error.config = config
      error.response = { status: 401, data: { code: 'UNAUTHORIZED', message: '登录已过期' }, headers: {}, config }
      throw error
    }

    await expect(request.get('/expense/my')).rejects.toBeTruthy()
    expect(getToken()).toBe('')
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace.mock.calls[0][0].path).toBe('/login')
  })
})
