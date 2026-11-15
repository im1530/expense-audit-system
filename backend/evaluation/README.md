# Phase D/F 评测（evaluation/）

## 当前内容

- `cases/smoke_cases.json`：30 条冒烟案例，工程侧起草，`label_status=draft`；
- `cases/golden_v1.json`：100 条金标准（25 个场景 × 4 个变体），已确认 `reviewed/frozen`，带 `context`，可直接跑真实 LLM；
- `cases/golden_v1_real96.json`：真实评测集 96 条，剔除 4 条 `ai_fallback` 模拟降级场景；
- `cases/rag_queries.json`：16 条制度检索查询（结构化表述，基线）；
- `cases/rag_queries_colloquial.json`：20 条口语化压测查询（员工说法 vs 制度用词，用于判断 BM25 缺口）；
- `cases/citation_labels_v1.json` / `.md`：96 条案例的 clause_id 引用标注（全部 reviewed），含逐案理由、类别映射、条款原文与指标对比；
- `generate_golden.py`：重新生成 `golden_v1.json`（改场景后运行）；
- `harness.py`：决策/风险一致率、高危漏放率、降级率、finding 召回率、平均耗时/token；
- `runner.py`：真实 LLM 运行、结果录制、回放；
- `compare.py`：Prompt/模型版本的报告对比；
- `rag_harness.py`：检索评测（Recall@1/3/5、MRR），支持 `--backend bm25|chroma|hybrid`；
- `reports/latest.*`：30 条冒烟 mock 报告；
- `reports/golden_mock.*`：100 条金标准 mock 报告；
- `reports/rag_latest.*` / `reports/rag_colloquial.*`：BM25 结构化基线 / 口语化压测报告；
- `reports/rag_chroma_base.*` / `reports/rag_colloquial_chroma.*`：Chroma + BGE-zh 的检索报告；
- `reports/rag_hybrid_*.*`：Hybrid 后端对比报告；
- `recordings/preview_5.json`：5 条真实 LLM 预览录制（deepseek-flash）；
- `recordings/real_100.json`：100 条真实 LLM 录制（deepseek-flash）；
- `reports/preview_5.*`、`reports/preview_5_replay.*`：真实/回放预览报告；
- `reports/real_100.*`、`reports/real_100_replay.*`：100 条真实/回放报告；
- `recordings/v4_100.json`：Prompt v4 全量录制（制度 v1.1）；
- `recordings/v4_100_v12.json`：Prompt v4 全量重录（制度 v1.2，2026-11-10）；
- `recordings/v4_1_100.json`：Prompt v4.1（引用校准）全量重录（制度 v1.2，2026-11-10）；
- `reports/v4_1_100.*`：Prompt v4.1 完整报告（18 条标注子集，引用准确率 100%）；
- `reports/v4_1_100_full96.*`：Prompt v4.1 全量 96 条标注报告（准确率 98.91%、召回 87.50%）；
- `reports/v4_100.*`、`reports/v4_100_final.*`：Prompt v4 真实/最终决策重评分报告；
- `reports/v4_100_citation.*`：制度 v1.1 的引用指标基线（按当时“G041 制度空白”的标注口径生成）；
- `reports/v4_100_citation_v12.*`：制度 v1.2 的引用指标报告（引用准确率 92%、召回 83.33%）；
- `reports/v3_vs_v4_100_final.*`：v3 与 v4 全量最终对比；
- `reports/mock_vs_real.*`：mock 与真实结果对比；
- `reports/golden_review.xlsx`：复核工作簿（5 条真实对照 + 100 条双人复核 + 100 条真实结果 + v4对比）；
- `reports/high_risk_review_finance.xlsx`：高危 24 条盲表（复核人 A（财务））；
- `reports/high_risk_review_boss.xlsx`：高危 24 条盲表（复核人 B（老板））；
- `cases/high_risk_final_v1.json` / `reports/high_risk_ab_compare.md`：高危 24 条最终标签与 A/B 对比（AI 预填 + 人工确认口径）；
- `finalize_high_risk_review.py`：复核表收口脚本（更新备注、对比 A/B、写最终标签与报告）；
- `build_review_workbook.py`、`update_review_workbook.py`、`add_real_results_sheet.py`、`add_v4_sheet.py`、`build_high_risk_blind.py`：生成/更新复核文件；
- `scripts/evaluate.py`、`scripts/evaluate_rag.py`、`scripts/compare_evaluations.py`：命令行入口。

## 运行

在 `backend/` 下：

```bash
# 冒烟集 mock
python scripts/evaluate.py

# 100 条金标准 mock
python scripts/evaluate.py --cases evaluation/cases/golden_v1.json \
  --output evaluation/reports/golden_mock.json \
  --markdown evaluation/reports/golden_mock.md

# 真实 LLM（默认 v4；会产生费用，执行前必须确认授权）
python scripts/evaluate.py --cases evaluation/cases/golden_v1_real96.json \
  --mode real --limit 5 \
  --recording evaluation/recordings/preview.json

# 回放历史录制
python scripts/evaluate.py --cases evaluation/cases/golden_v1_real96.json \
  --mode replay --recording evaluation/recordings/preview.json

# v3 历史版本 A/B（显式指定 prompt）
python scripts/evaluate.py --cases evaluation/cases/golden_v1_real96.json \
  --mode real --prompt v3 --limit 10 \
  --recording evaluation/recordings/v3_preview.json

# 两个版本对比
python scripts/compare_evaluations.py \
  --baseline evaluation/reports/golden_mock.json \
  --candidate evaluation/reports/candidate.json

# 检索评测
python scripts/evaluate_rag.py
```

## 模式说明

| 模式 | 是否调用模型 | 数据来源 | 用途 |
|---|---|---|---|
| `mock` | 否 | 案例里的 `simulated` | 验证决策层、指标计算、快速回归 |
| `real` | 是 | 真实 RiskAgent 输出 | 真实准确率、耗时、token、引用准确率 |
| `replay` | 否 | `--recording` 录制文件 | Prompt/模型版本对比、复盘，不重复花钱 |

## 重要说明

1. `golden_v1.json` 的标签已于 2026-11-09 由用户确认，状态为 `reviewed/frozen`，作为冻结 test 集使用；后续修改需记录版本；
2. `real` 模式会调用真实模型并产生费用，执行前需确认授权；
3. 录制文件只保存结构化结论、token、耗时与费用，不保存模型原始返回；
4. 30 条冒烟集保留，不与 100 条金标准集混用；
5. 当前真实模式依赖案例自带 `context`，不查业务库；冒烟集没有 `context`，只能跑 mock。

## 当前真实结果（2026-11-09）

- 100 条真实：决策一致率 **82%**、风险等级 **85%**、高危漏放 **0**、禁止决策违规 **1**、finding 召回 **98.68%**；
- 平均耗时 **1922 ms**、平均 token **1461**；
- 主要差异：低风险案例偏保守（manual_review）、Prompt 注入案例被误判为中风险、拆票案例被判高风险、`ai_fallback` 场景在真实模式无法强制降级（G073 给出 fast_track）；
- 详见 `reports/real_100.md` 与 `reports/mock_vs_real.md`。

## Prompt v4 + 决策策略调整（2026-11-09）

- 决策策略已调整：低风险且仅有 `low` finding 时允许 `fast_track`，low finding 只作提示；中/高风险 finding 仍强制人工复核；
- v4 全量 100 条：决策一致率 **96%**、风险等级 **96%**、高危漏放 **0**、finding 召回 **96.05%**、禁止决策违规 **3**；
- 平均耗时 **1326 ms**、平均 token **1619**；
- 对比 v3（同一决策策略）：决策 85%→96%、风险 85%→96%、耗时 -596 ms；改进 11 条、退化 2 条；
- 2 条退化和 3 条违规全部来自 `ai_fallback` 场景（真实模型无法强制降级）；
- 剔除 4 条 `ai_fallback` 后：96 条决策 **100%**、风险 **100%**、finding 召回 **100%**、违规 **0**；
- **v4 已切换为线上默认 Prompt**；真实评测请使用 `cases/golden_v1_real96.json`；
- 高危 24 条已生成两份独立盲表：`high_risk_review_finance.xlsx` / `high_risk_review_boss.xlsx`；
- 详见 `reports/v3_vs_v4_100_final.md`、`reports/v4_100_final.md`、`reports/v3_100_final.md`。

## 引用准确率（citation_v1，2026-11-10）

补齐 P0 #2 最后一个缺口：引用准确率单独统计。标注本身不调用模型；指标可用录制回放复现。

- 标注：`cases/citation_labels_v1.json`（覆盖 96/96 案例，全部 `reviewed`；68 条要求引用、28 条核心为空），评审表见 `cases/citation_labels_v1.md`；
- 口径：
    - **条款级准确率** = 被引用条款中属于 `core ∪ optional` 的比例（越界引用会拉低）；
    - **案例级召回率** = 应引用核心条款的案例中，至少引用到一条核心条款的比例；
    - **检索覆盖率** = 核心条款至少被检索后端（BM25 / Chroma）召回一条的案例比例（检索缺口，不计入模型责任）；
    - **条件召回率** = 核心条款已被检索到的案例中成功引用的比例（衡量模型是否漏引）；
- 结果（制度 v1.2 + Prompt v4.1，2026-11-10 真实重录 `v4_1_100`）：
    - **全量 96 条**：准确率 **98.91%**（91/92）、召回 **92.65%**（63/68）、检索覆盖 98.53%（67/68）、条件召回 94.03%（63/67）；
    - **18 条抽检子集**：准确率 100%（19/19）、召回 83.33%（15/18）、覆盖 100%、条件召回 83.33%；
    - 全量漏引 5 条：金额超额 G030/G032、重复订单号 G049/G052（只引 4.3）、弱凭证超限 G021；
    - 全量越界 1 条：G090（测试单据）引 2.4；
    - 识别失败 G069–G072 按“系统能力问题、不要求引用”收口（引用 2.2 可接受，不引用不扣召回）；
- 对比（同一制度 v1.2，仅 Prompt 不同；18 条子集）：

| 版本 | 准确率 | 召回率 | 被引条款数 | 平均 token |
|---|---|---|---|---|
| v4（`v4_100_v12`） | 92.00%（23/25） | 83.33% | 25 | 1627.7 |
| v4.1（`v4_1_100`） | **100%（19/19）** | 83.33% | 19 | 1891.1 |
- v1.1 基线（旧录制回放）：准确率 90.91%、召回 70.59%、检索覆盖 82.35%、条件召回 85.71%；
- 结论：制度侧补全（1.2 关键词、2.4 发票号）解决检索缺口，Prompt v4.1 引用校准解决“方向/主题不匹配”的越界引用；全量口径下准确率 98.91%、召回 92.65%，18 条抽检没有高估引用质量；决策/风险指标无回退（决策 100%、风险 100%、高危漏放 0、违规 0）；
- 报告：`reports/v4_1_100_full96.json` / `.md`（全量）；`reports/v4_1_100.json` / `.md`（18 条子集）；`reports/v4_100_citation_v12.*`（v4）；`reports/v4_100_citation.*`（v1.1 基线）；
- 待办（可选）：重复订单号 2 条漏引可强化“1.2 为禁止性依据”；金额超额 G032 属检索缺口，可给 2.4 补“金额”关键词；两项都需重录验证。

```bash
# 回放 v4.1 录制，产出完整报告（不调用模型）
python scripts/evaluate.py --cases evaluation/cases/golden_v1_real96.json \
  --mode replay --recording evaluation/recordings/v4_1_100.json \
  --output evaluation/reports/v4_1_100.json \
  --markdown evaluation/reports/v4_1_100.md
```

## 检索后端选型（BM25 / Chroma / Hybrid，2026-11-11）

背景：口语化查询下 BM25 只有 Recall@3 60%，低于 85% 门槛，因此引入 Chroma + 本地中文 embedding。

| 查询集 | BM25 | Chroma（BGE-zh） | Hybrid（BM25 优先+向量补位） |
|---|---|---|---|
| 结构化 16 条 | R@1 100%、R@3 100%、MRR 1.000 | R@1 93.75%、R@3 100%、MRR 0.969 | R@1 100%、R@3 100%、MRR 1.000 |
| 口语化 20 条 | R@1 35%、R@3 60%、MRR 0.475 | **R@1 65%、R@3 95%、MRR 0.767** | R@1 40%、R@3 80%、MRR 0.592 |

- 结论：**Chroma + BGE-zh 在两边都过门槛**（结构化 100%、口语化 95%），已设为默认后端 `POLICY_RETRIEVER=chroma`；
- 纯 BM25 保留为 `bm25` 回退（缺依赖/缺模型时工厂自动降级）；Hybrid 保留为 `hybrid` 实验后端（结构化最稳，但口语化不如纯向量）；
- 英文默认模型 all-MiniLM-L6-v2 实测中文只有 62.5%，已弃用；
- 模型文件不打进仓库：默认读取 `~/.cache/expense_audit/embeddings/bge-small-zh-v1.5`，也可用 `POLICY_EMBEDDING_DIR` 指定；
  预置来源（ModelScope，含 model.onnx 与 tokenizer.json）：`https://modelscope.cn/models/Xenova/bge-small-zh-v1.5`；
- 复现命令：

```bash
# 结构化基线 / 口语化压测（chroma）
python scripts/evaluate_rag.py --backend chroma \
  --queries evaluation/cases/rag_queries.json \
  --output evaluation/reports/rag_chroma_base.json --markdown evaluation/reports/rag_chroma_base.md
python scripts/evaluate_rag.py --backend chroma \
  --queries evaluation/cases/rag_queries_colloquial.json \
  --output evaluation/reports/rag_colloquial_chroma.json --markdown evaluation/reports/rag_colloquial_chroma.md
```

- 报告：`reports/rag_latest.*`（BM25 结构化）、`reports/rag_colloquial.*`（BM25 口语化）、`reports/rag_chroma_base.*`、`reports/rag_colloquial_chroma.*`、`reports/rag_hybrid_*.*`。

## 独立双标 blind_v2（2026-11-14）

背景：v1 的高危 24 条是「AI 预填 + 人工确认」，同源，κ 不可计算。blind_v2 改为两人互不可见、不看 AI 的独立标注：

- **样本**：24 高危全取 + low 18 + mid 18 = 60 条，冻结在 `cases/blind_v2.json`（预登记种子、来源 SHA-256、指南版本）；分层过采样，κ 只代表两人一致性、不外推总体；
- **盲包**：A（财务）/ B（老板）两份 xlsx，不含 AI 结论、规则命中、场景类别、原始编号，顺序各自独立打乱；发放哈希在 `cases/blind_v2_distribution.json`；标注指南 `docs/annotate_guide_v2.0.md`；
- **结果**：58/60 一致，percent agreement **96.67%**，Cohen's κ=**0.9495**（bootstrap 95%CI **0.8705–1.0000**），线性/二次加权 κ 0.9611/0.9733，PABAK 0.95；2 条分歧（完全无凭证判 high 还是 mid）裁决为 **mid/manual_review**；
- **终版**：`cases/high_risk_final_v2.json`（60 条，含 A/B 原标、`adjudicated` 标记与裁决说明）；旧 24 条保留为抽样框，不再作双标证据；
- **口径差异**：终版与既有标签 13/60 条不一致——金额/发票号不符（系统 high、人工 mid）、订单号重复（系统 mid、人工 high）、注入文本（系统 low、人工 high）。原因是 `annotate_guide_v2.0` 与 `document_rules` 口径不一致；详见 `reports/blind_v2_final.md` 第 4 节；
- **使用定位（2026-11-14 决定，方案 C）**：本批定位为**人工标准基准**，不直接作为系统评测金标准；改规则（A）或出 guide_v2.1 重标（B）属于下一轮独立决策，完成前不得用本批标签判模型“过严/过松”；
- **复现**：`build_blind_v2.py --verify` → `build_blind_v2_packets.py --verify` → `analyze_blind_v2.py` → `finalize_blind_v2.py`。

## 真实自采集 real_self_v1（已冻结，2026-11-15）

- 来源：本人 10 份真实消费凭证（6 张电子发票、3 张微信支付凭证、1 张支付截图），原始件在仓库外 `D:\expense_real_self\raw`；
- 口径：**真实票据 + 构造事由 + 模拟公司抬头**（示例科技有限公司），不测抬头不符；6 条 low（票证齐全、限额内）+ 4 条 mid（无发票/弱凭证），无 high；
- 文件：`cases/real_self_v1.json`、`cases/real_self_v1_manifest.json`（哈希/口径/限制）、`reports/real_self_v1_review.md`；已冻结并设只读，哈希在 `cases/real_self_v1.sha256`；
- OCR 试点 10 条（`reports/real_self_v1_ocr.md`）：类型/金额/日期/商户 **40/40**（Prompt v2 修复了交通票日期口径），输入约 13.2k token；
- 限制：n=10 只能冒烟、单标注未做 κ；个人抬头不能用于测抬头不符；OCR 仅 4 条、单次运行。

## 指标口径

- 决策一致率：`decision` 与人工标签一致比例；
- 风险等级一致率：`low/mid/high` 一致比例；
- 高危漏放率：人工 high 的案例中，被判为 low 或 `fast_track` 的比例；
- 降级率：模拟/真实 fallback 的比例；
- finding 召回率：必须命中的风险点被命中的比例；
- 禁止决策违规：期望禁止 `fast_track` 但实际给出 `fast_track` 的数量；
- 平均耗时 / 平均 token：真实或回放结果的平均值；
- 引用准确率 / 引用召回率 / 检索覆盖率 / 条件召回率：口径见"引用准确率（citation_v1）"一节。

## 后续

- `citation_labels_v1.json` 已覆盖 96 条并冻结；后续调整口径需记录 change_log 并升版本；
- 独立双标 blind_v2 已完成（2026-11-14）：60 条、κ=0.9495（95%CI 0.8705–1.0000）、2 条分歧已裁决，见上文；
- 旧 24 条（v1）保留为抽样框，仍属 AI 预填 + 人工确认，**不能当作独立双标证据**；
- blind_v2 与既有标签体系有 13/60 条口径差异；2026-11-14 已决定先定位为人工标准基准（方案 C），改规则或重标另立决策；
- 增加置信区间与分类别指标；
- 把 4 条 `ai_fallback` 改成“强制 LLM 失败”的专项降级测试；
- 系统上线后补 20~30 条脱敏真实历史单据；
- 根据检索评测结论决定是否引入 Chroma + Embedding。
