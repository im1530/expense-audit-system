/**
 * 展示层格式化工具：状态/等级的中文映射、金额与时间格式化
 * 说明：后端返回的是枚举字符串（如 pending），中文只在展示层维护，避免前后端各写一份
 */

// 报销单状态
export const EXPENSE_STATUS: Record<string, { label: string; type: string }> = {
  draft: { label: '草稿', type: 'info' },
  pending: { label: '待审批', type: 'warning' },
  approved: { label: '已通过', type: 'success' },
  registered: { label: '待领取', type: 'warning' },
  rejected: { label: '已驳回', type: 'danger' },
  paid: { label: '已报销', type: 'success' },
  closed: { label: '已关闭', type: 'info' },
  cancelled: { label: '已撤销', type: 'info' },
}

// AI 风险等级
export const RISK_LEVEL: Record<string, { label: string; type: string }> = {
  low: { label: '低风险', type: 'success' },
  mid: { label: '中风险', type: 'warning' },
  high: { label: '高风险', type: 'danger' },
}

// AI 建议决策
export const DECISION: Record<string, { label: string; type: string }> = {
  fast_track: { label: '建议快速审批', type: 'success' },
  manual_review: { label: '建议人工复核', type: 'warning' },
  suggest_reject: { label: '建议退回补正', type: 'danger' },
}

// 审批记录状态
export const APPROVAL_STATUS: Record<string, { label: string; type: string }> = {
  pending: { label: '待审批', type: 'warning' },
  approved: { label: '已通过', type: 'success' },
  rejected: { label: '已驳回', type: 'danger' },
  recalled: { label: '已撤回', type: 'info' },
  cancelled: { label: '已作废', type: 'info' },
  obsoleted: { label: '已失效', type: 'info' },
}

// 费用大类
export const EXPENSE_TYPE: Record<string, string> = {
  meal: '餐饮',
  transportation: '交通',
  accommodation: '住宿',
  office: '办公',
  other: '其他',
}

// 规则命中级别
export const RULE_LEVEL: Record<string, { label: string; type: string }> = {
  ok: { label: '正常', type: 'success' },
  warn: { label: '提示', type: 'warning' },
  review: { label: '需复核', type: 'warning' },
  block: { label: '阻止提交', type: 'danger' },
}

/**
 * 金额格式化（后端用字符串返回 Decimal，避免精度丢失）
 * @param value 金额
 * @returns 形如 ¥1,234.00
 */
export function formatMoney(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (Number.isNaN(num)) return String(value)
  return `¥${num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/**
 * 时间格式化（ISO 字符串 → 本地可读）
 * @param value ISO 时间
 * @returns 形如 2026-11-12 09:30
 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * 日期格式化（只到天）
 * @param value ISO 时间/日期
 * @returns 形如 2026-11-12
 */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  return String(value).slice(0, 10)
}
