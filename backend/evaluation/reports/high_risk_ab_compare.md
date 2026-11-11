# 高危 24 条 A/B 复核对比

> 生成日期：2026-11-10；数据来源：AI 预填 + 复核人确认（非独立双盲）

| 指标 | 结果 |
|---|---|
| 案例数 | 24 |
| A/B 风险等级一致率 | 100.00% |
| A/B 决策一致率 | 100.00% |
| Cohen's kappa（风险） | 不适用 |
| Cohen's kappa（决策） | 不适用 |
| 与金标准不一致 | 0 条 |

## 重要说明

- 两份表均由同一份 AI 预填生成，复核人在此基础上确认；**A/B 一致率 100% 不代表两人独立判断一致**；
- 风险等级与决策均为单一取值（全部 high / suggest_reject），P_e = 1，Cohen's kappa 数学上不可计算；
- **不要为了“让 kappa 可计算”修改复核标签**：人为制造分歧会破坏与金标准的一致性，而且 24 条里只有 2 个类别时 kappa 仍会退化为 0，同样没有解释力；
- 如需可审计的独立双标证据，应由两名复核人在互不可见条件下重新独立填写，再运行本脚本对比。

## 逐案结论

| 案例 | 分类 | 风险 | 决策 | 财务复核人 | 老板复核人 | 与金标准 |
|---|---|---|---|---|---|---|
| G025 | 金额不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G026 | 金额不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G027 | 金额不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G028 | 金额不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G033 | 抬头错误 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G034 | 抬头错误 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G035 | 抬头错误 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G036 | 抬头错误 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G041 | 发票号不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G042 | 发票号不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G043 | 发票号不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G044 | 发票号不符 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G045 | 重复报销 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G046 | 重复报销 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G047 | 重复报销 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G048 | 重复报销 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G085 | 个人消费 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G086 | 个人消费 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G087 | 个人消费 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G088 | 个人消费 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G089 | 测试单据 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G090 | 测试单据 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G091 | 测试单据 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
| G092 | 测试单据 | high | suggest_reject | 王晓丽（财务） | 刘振华（老板） | 一致 |
