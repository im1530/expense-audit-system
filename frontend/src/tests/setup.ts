/**
 * 测试环境准备
 *   1. Node 26 内置了实验性的全局 localStorage（未加 --localstorage-file 时为 undefined），
 *      会盖住 jsdom 提供的实现，导致浏览器风格的代码全部报错；这里显式指回 window.localStorage。
 *   2. 补齐 jsdom 缺失的浏览器 API（matchMedia / createObjectURL）。
 *   3. 每个用例前清空本地存储，避免令牌与状态互相污染。
 */
import { beforeEach, vi } from 'vitest'

// ===== 1.把 localStorage / sessionStorage 指向 jsdom 的实现 =====
function memoryStorage(): Storage {
  const data = new Map<string, string>()
  return {
    get length() {
      return data.size
    },
    clear: () => data.clear(),
    getItem: (key: string) => (data.has(key) ? data.get(key)! : null),
    key: (index: number) => Array.from(data.keys())[index] ?? null,
    removeItem: (key: string) => void data.delete(key),
    setItem: (key: string, value: string) => void data.set(key, String(value)),
  } as Storage
}

const storage: Storage = (globalThis as any).window?.localStorage ?? memoryStorage()
const session: Storage = (globalThis as any).window?.sessionStorage ?? memoryStorage()

Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true })
Object.defineProperty(globalThis, 'sessionStorage', { value: session, configurable: true, writable: true })

// ===== 2.补齐浏览器 API =====
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
}

if (typeof URL !== 'undefined' && !URL.createObjectURL) {
  Object.defineProperty(URL, 'createObjectURL', { writable: true, value: vi.fn(() => 'blob:mock') })
  Object.defineProperty(URL, 'revokeObjectURL', { writable: true, value: vi.fn() })
}

// ===== 3.每个用例前清空存储 =====
beforeEach(() => {
  storage.clear()
  session.clear()
})
