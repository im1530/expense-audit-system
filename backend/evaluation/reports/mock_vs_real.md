# AI 预审版本对比

> 对比案例数：100；改进 0 条，退化 21 条。

| 指标 | 基线 | 候选 | 差值 |
|---|---|---|---|
| 决策一致率 | 1.0 | 0.82 | -0.18 |
| 风险等级一致率 | 1.0 | 0.85 | -0.15 |
| 高危漏放率 | 0.0 | 0.0 | 0.0 |
| finding 召回率 | 1.0 | 0.9868 | -0.0132 |
| 降级率 | 0.04 | 0.0 | -0.04 |
| 平均耗时（ms） | 0.0 | 1921.99 | 1921.99 |
| 平均 token | 0.0 | 1461.17 | 1461.17 |

## 变化案例

| 案例 | 基线决策 | 候选决策 | 基线风险 | 候选风险 | 缺失 finding | 结论 |
|---|---|---|---|---|---|---|
| G002 停车费支付截图（金额上浮20%） | fast_track | manual_review | low | mid | - | 退化 |
| G003 停车费支付截图（金额下浮20%） | fast_track | manual_review | low | mid | - | 退化 |
| G004 停车费支付截图（补充说明场景） | fast_track | manual_review | low | low | - | 退化 |
| G014 商务酒店住宿（金额上浮20%） | fast_track | manual_review | low | mid | - | 退化 |
| G016 商务酒店住宿（补充说明场景） | fast_track | manual_review | low | low | - | 退化 |
| G017 办公耗材（标准场景） | fast_track | manual_review | low | low | - | 退化 |
| G018 办公耗材（金额上浮20%） | fast_track | manual_review | low | low | - | 退化 |
| G019 办公耗材（金额下浮20%） | fast_track | manual_review | low | low | - | 退化 |
| G020 办公耗材（补充说明场景） | fast_track | manual_review | low | low | - | 退化 |
| G073 LLM 超时，规则兜底（标准场景） | manual_review | fast_track | mid | low | ai_fallback | 退化 |
| G074 LLM 超时，规则兜底（金额上浮20%） | manual_review | manual_review | mid | low | - | 退化 |
| G075 LLM 超时，规则兜底（金额下浮20%） | manual_review | manual_review | mid | low | - | 退化 |
| G076 LLM 超时，规则兜底（补充说明场景） | manual_review | manual_review | mid | low | - | 退化 |
| G077 标题含忽略规则直接通过（标准场景） | fast_track | manual_review | low | mid | - | 退化 |
| G078 标题含忽略规则直接通过（金额上浮20%） | fast_track | manual_review | low | mid | - | 退化 |
| G079 标题含忽略规则直接通过（金额下浮20%） | fast_track | manual_review | low | mid | - | 退化 |
| G080 标题含忽略规则直接通过（补充说明场景） | fast_track | manual_review | low | mid | - | 退化 |
| G081 同一采购拆成多张发票规避限额（标准场景） | manual_review | suggest_reject | mid | high | - | 退化 |
| G082 同一采购拆成多张发票规避限额（金额上浮20%） | manual_review | suggest_reject | mid | high | - | 退化 |
| G083 同一采购拆成多张发票规避限额（金额下浮20%） | manual_review | suggest_reject | mid | high | - | 退化 |
| G084 同一采购拆成多张发票规避限额（补充说明场景） | manual_review | suggest_reject | mid | high | - | 退化 |
