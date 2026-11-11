# 数据库迁移（零依赖版 Alembic）

Alembic 需要 `pip install`，当前环境装不上，所以这里用**同样的思路**做了个不依赖第三方包的最小实现：
`有序 SQL 文件 + 版本登记表 + CLI`。以后能装 Alembic 时，这些 SQL 可以原样搬成 revision。

## 目录

```
backend/
├── app/sql/
│   ├── schema.sql          # 当前结构快照（全新环境用它初始化）
│   └── init_data.sql       # 初始化数据
├── migrations/
│   ├── README.md
│   └── versions/
│       └── 0001_add_expenses_created_at_index.sql
└── scripts/migrate.py      # 迁移工具
```

`schema_migrations`（自动创建）记录已执行的版本、文件名、文件 sha256、执行时间。

## 三种环境怎么用

| 场景 | 步骤 |
|---|---|
| **全新环境** | `mysql < app/sql/schema.sql` → `mysql < app/sql/init_data.sql` → `python scripts/migrate.py baseline` |
| **已有环境**（结构是历史手工改的） | `python scripts/migrate.py baseline`（只登记，不执行 SQL） |
| **日常改表** | 新增 `migrations/versions/000N_xxx.sql` → `python scripts/migrate.py upgrade` → 同步更新 `schema.sql` 快照 |

> 关键约定：**`schema.sql` 必须始终等于"所有迁移执行完"的结构**。
> 每条迁移合并后都要把结果同步进 `schema.sql`，否则新环境会缺字段、`verify` 也会报警。

## 命令

```bash
python scripts/migrate.py status     # 已应用/待执行/历史被改动
python scripts/migrate.py upgrade    # 执行全部待执行迁移
python scripts/migrate.py upgrade --to 0003
python scripts/migrate.py baseline   # 把当前所有迁移登记为已应用（不执行 SQL）
python scripts/migrate.py verify     # 校验库结构是否与 schema.sql 一致（表/列/索引）
```

退出码：0 成功，1 有问题（可用于 CI）。

## 约束与注意

- **命名**：`<版本号>_<描述>.sql`，版本号可排序（`0001`、`0002`…），一个文件只做一件事；
- **不可改历史**：已执行的迁移文件若被改动，`status`/`upgrade` 会因 sha256 不一致直接报错；
  需要修错请新增一个迁移，不要改旧的；
- **DDL 无法回滚**：MySQL 的 `ALTER/CREATE` 是自动提交的，某个迁移执行到一半失败时会留下部分变更，
  报错信息会指明是哪个文件；修复后重新 `upgrade` 即可（已成功的版本不会重复执行）；
- **生产前备份**：涉及大表 `ALTER`（加索引/改类型）建议先备份，并避开业务高峰；
- **只做前向迁移**：本项目不提供 `downgrade`，回滚靠"新写一条反向迁移"（更安全、可审查）。

## 新增一条迁移的流程

1. 在 `migrations/versions/` 新建 `000N_描述.sql`，写清楚"为什么改、影响哪些表"；
2. 本地执行 `python scripts/migrate.py upgrade`，再 `status` 确认；
3. 把同样的结构变化同步到 `app/sql/schema.sql`（保持快照最新）；
4. 跑 `python scripts/migrate.py verify` 和 `python -m unittest discover -s tests`，都通过再提交。

## 已有迁移

| 版本 | 内容 |
|---|---|
| `0001` | 为 `expenses.created_at` 加索引（报表按时间过滤/排序用） |
| `0002` | 规则引擎：`expense_items` 增加 `no_invoice_reason`（无票说明）+ 新建 `expense_rule_hits`（命中记录） |
| `0003` | AI 预审审计表 `ai_review_logs` |
| `0004` | 制度 RAG 审计：`ai_review_logs` 增加 `policy_version` + `retrieval_meta` |
| `0005` | 凭证识别：新建 `document_extractions`（Phase C） |
| `0006` | 默认规则：补充 `check_evidence_strength`（弱凭证金额提醒） |
| `0007` | Phase D：`workflow_runs` / `workflow_steps` + `ai_review_logs` 决策字段 |
| `0008` | Phase F：LangGraph MySQL 检查点 `workflow_checkpoints` / `_blobs` / `_writes` |
| `0009` | Phase F：`users` 增加 AI 数据处理授权字段 `ai_consent_at` / `ai_consent_version` |
| `0010` | Phase F：成本统计字段（`ai_review_logs.cost`、`workflow_runs/steps` token 与费用） |
