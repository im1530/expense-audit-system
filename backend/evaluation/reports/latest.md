# AI 预审评测报告（mock 模式）

> 说明：当前使用案例中的 simulated 结果验证决策层与指标计算，不代表真实 LLM 准确率。

| 指标 | 结果 |
|---|---|
| 案例数 | 30 |
| 决策一致率 | 90.00% |
| 风险等级一致率 | 90.00% |
| 高危案例数 | 5 |
| 高危漏放数 | 1 |
| 高危漏放率 | 20.00% |
| 降级率 | 3.33% |
| finding 召回率 | 90.00% |
| 禁止决策违规 | 2 |
| 平均耗时（ms） | 0.0 |
| 平均 token | 0.0 |

## 案例差异

| 案例 | 期望风险 | 实际风险 | 期望决策 | 实际决策 | 缺失 finding |
|---|---|---|---|---|---|
| C011 同一发票号在其他单据出现 | high | mid | suggest_reject | manual_review | - |
| C014 单条明细超过类别限额 | mid | low | manual_review | fast_track | category_limit |
| C029 金额明显不符但风险分被低估 | high | low | suggest_reject | fast_track | amount_short |
