/**
 * 展示层格式化工具单测
 * 覆盖：金额（千分位/空值/非数字）、时间与日期格式化、状态映射完整性
 */
import { describe, expect, it } from 'vitest'
import {
  APPROVAL_STATUS,
  DECISION,
  EXPENSE_STATUS,
  EXPENSE_TYPE,
  RISK_LEVEL,
  RULE_LEVEL,
  formatDate,
  formatMoney,
  formatTime,
} from '@/utils/helpers'

describe('formatMoney', () => {
  it('数字与字符串都能格式化成两位小数并带千分位', () => {
    expect(formatMoney(1234.5)).toBe('¥1,234.50')
    expect(formatMoney('1234.5')).toBe('¥1,234.50')
    expect(formatMoney(0)).toBe('¥0.00')
    expect(formatMoney('0.00')).toBe('¥0.00')
  })

  it('空值与非数字原样兜底', () => {
    expect(formatMoney(null)).toBe('-')
    expect(formatMoney(undefined)).toBe('-')
    expect(formatMoney('')).toBe('-')
    expect(formatMoney('abc')).toBe('abc')
  })
})

describe('formatTime / formatDate', () => {
  it('ISO 时间转成本地可读格式', () => {
    expect(formatTime('2026-11-12T09:30:00')).toBe('2026-11-12 09:30')
  })

  it('空值与非时间字符串兜底', () => {
    expect(formatTime(null)).toBe('-')
    expect(formatTime('')).toBe('-')
    expect(formatTime('not-a-date')).toBe('not-a-date')
  })

  it('日期只取到天', () => {
    expect(formatDate('2026-11-12T09:30:00')).toBe('2026-11-12')
    expect(formatDate(null)).toBe('-')
  })
})

describe('状态映射', () => {
  it('报销单状态覆盖后端全部枚举', () => {
    for (const status of ['draft', 'pending', 'approved', 'rejected', 'paid', 'cancelled']) {
      expect(EXPENSE_STATUS[status]?.label).toBeTruthy()
      expect(EXPENSE_STATUS[status]?.type).toBeTruthy()
    }
  })

  it('风险等级与决策映射完整', () => {
    expect(Object.keys(RISK_LEVEL)).toEqual(['low', 'mid', 'high'])
    expect(DECISION.fast_track.label).toContain('快速')
    expect(DECISION.suggest_reject.type).toBe('danger')
  })

  it('审批记录状态覆盖六种取值', () => {
    for (const status of ['pending', 'approved', 'rejected', 'recalled', 'cancelled', 'obsoleted']) {
      expect(APPROVAL_STATUS[status]?.label).toBeTruthy()
    }
  })

  it('费用大类与规则级别映射存在', () => {
    expect(EXPENSE_TYPE.transportation).toBe('交通')
    expect(RULE_LEVEL.block.type).toBe('danger')
  })
})
