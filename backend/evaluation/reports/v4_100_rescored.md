# AI 预审评测报告（replay 模式）

> 说明：当前使用历史录制结果回放；用于 Prompt/模型版本对比，不重新调用模型。

| 指标 | 结果 |
|---|---|
| 案例数 | 100 |
| 决策一致率 | 90.00% |
| 风险等级一致率 | 96.00% |
| 高危案例数 | 24 |
| 高危漏放数 | 0 |
| 高危漏放率 | 0.00% |
| 降级率 | 0.00% |
| finding 召回率 | 96.05% |
| 禁止决策违规 | 3 |
| 平均耗时（ms） | 1326.23 |
| 平均 token | 1619.12 |

## 案例差异

| 案例 | 期望风险 | 实际风险 | 期望决策 | 实际决策 | 缺失 finding |
|---|---|---|---|---|---|
| G002 停车费支付截图（金额上浮20%） | low | low | fast_track | manual_review | - |
| G014 商务酒店住宿（金额上浮20%） | low | low | fast_track | manual_review | - |
| G016 商务酒店住宿（补充说明场景） | low | low | fast_track | manual_review | - |
| G017 办公耗材（标准场景） | low | low | fast_track | manual_review | - |
| G018 办公耗材（金额上浮20%） | low | low | fast_track | manual_review | - |
| G019 办公耗材（金额下浮20%） | low | low | fast_track | manual_review | - |
| G020 办公耗材（补充说明场景） | low | low | fast_track | manual_review | - |
| G073 LLM 超时，规则兜底（标准场景） | mid | low | manual_review | fast_track | ai_fallback |
| G074 LLM 超时，规则兜底（金额上浮20%） | mid | low | manual_review | fast_track | ai_fallback |
| G075 LLM 超时，规则兜底（金额下浮20%） | mid | low | manual_review | manual_review | - |
| G076 LLM 超时，规则兜底（补充说明场景） | mid | low | manual_review | fast_track | ai_fallback |
