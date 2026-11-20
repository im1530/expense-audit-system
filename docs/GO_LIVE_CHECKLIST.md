# 上线前检查清单

> 用法：逐项确认并填写「验证结果 / 负责人」；**P0 未完成不得上线**，P1 可上线后一周内补齐。
> 部署形态：前端 `frontend/dist`（Vue 3）由 Nginx 静态托管，`/api/` 反代到后端；数据库 MySQL，缓存/队列 Redis(Memurai)。
> **具体安装步骤见 [`DEPLOYMENT.md`](DEPLOYMENT.md)**，Nginx 样例见 [`deploy/nginx/expense-audit.conf`](../deploy/nginx/expense-audit.conf)，部署后自检脚本 [`deploy/verify_deploy.ps1`](../deploy/verify_deploy.ps1)。

## 0. 前置信息

| 项 | 内容 |
|---|---|
| 上线版本 / Git commit | |
| 部署环境（域名 / 主机） | |
| 部署人 / 日期 | |
| 回滚方式（上一版本包 / 数据库备份） | |

## 1. 配置与密钥（P0）

- [ ] `SECRET_KEY` 已换成 64 位以上随机串，且不在代码/日志/仓库中
- [ ] `DATABASE_URL` 指向生产库，使用独立账号（非 root）并只授权本库
- [ ] 应用账号只授 `SELECT/INSERT/UPDATE/DELETE`；建表/迁移用另一个有 DDL 权限的账号（见 `DEPLOYMENT.md`）
- [ ] `DEEPSEEK_API_KEY` 已配置，账户余额与并发额度确认
- [ ] `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` 只包含生产域名
- [ ] `ATTACHMENT_ENCRYPTION_KEY` 已生成并配置（历史明文附件仍可读，新文件加密）
- [ ] 该加密密钥已进密码库/离线备份，并知道"启用后不可直接更换（丢失=附件不可读）"
- [ ] `APP_ENV=production`、`DEBUG=False`、`LOG_LEVEL=INFO`
- [ ] `LOG_FILE` 指向持久磁盘（如 `logs/app.log`），`LOG_RETENTION_DAYS` 已确认
- [x] admin 初始密码已重置（admin 仍为 `must_change_password=True` 强制首登改密），演示/冒烟账号已停用 —— 2026-11-15 停用 10 个：testuser、manager1、fin1、manager2、emp_p2、fin2、boss1、smoke_emp、smoke_mgr、smoke_fin（`is_active=0` + `token_version+1`，登录返回 403“账号已被停用”；备份见仓库外 `users_backup_20261115.json`）
- [ ] 启动自检生效：密钥不合格时进程应**起不来**（日志出现"生产环境安全配置不合法"即为护栏生效）

验证：启动后日志无配置告警；`/api/docs` 可访问但仅内网。

## 2. 依赖与基础设施（P0）

- [ ] Python 3.13 + `pip install -r requirements.txt` 成功（含 `chromadb`；Celery 固定 5.6.3）
- [ ] MySQL 已执行 `app/sql/schema.sql` + `init_data.sql`，`python scripts/migrate.py verify` 输出“结构一致”
- [ ] Redis / Memurai 为正式授权版本，`redis-cli ping` 返回 PONG
- [ ] Celery worker 已启动（Linux 建议 `--pool=prefork`；Windows 开发机 `--pool=threads`）
- [ ] `uploads/`、`chroma_policy/`、`logs/` 挂载到数据盘（不要放代码盘）

## 3. 制度检索模型（P0，向量检索必做）

- [ ] 预置 BGE-zh ONNX 模型到 `POLICY_EMBEDDING_DIR`（默认 `~/.cache/expense_audit/embeddings/bge-small-zh-v1.5`）
- [ ] 执行自检脚本，期望输出 `backend=ChromaPolicyRetriever`、`degraded=false`：

```bash
python scripts/check_policy_backend.py
```

- [ ] 服务启动日志出现：`制度检索自检完成 backend=ChromaPolicyRetriever clauses=33 degraded=False`
- [ ] **模型目录对服务账户可读**：以 SYSTEM/服务账户运行时不能放用户目录（`%USERPROFILE%` 会变成 `C:\Windows\system32\config\systemprofile`），应放机器级路径并用 `POLICY_EMBEDDING_DIR` 指定；否则日志显示 `embedding 模型文件缺失` 且 `degraded=True`（静默降级 BM25）
- [ ] **后端单 worker**：Chroma 本地持久化（SQLite + 文件锁）不支持多进程并发写，`--workers 2` 会在启动时并发重建集合（详见 `DEPLOYMENT.md`）
- [ ] 若日志提示“已降级为 BM25”：检查 `chromadb` 是否安装、模型目录 6 个文件是否齐全
- [ ] 首次启动会因签名变化**自动重建向量集合**（`doc=v2_kw_first`）：日志出现"制度向量集合构建完成 clauses=33"，耗时约 1.4 s，属正常；此后启动直接复用
- [ ] 重建依据：参与 embedding 的文本已改为「关键词 + 原文」，核心条款检索覆盖从 82.4% 提升到 100%（68 条评测案例，离线实测）。**口径变化后引用指标需重录才会更新**，旧录制（98.91%/92.65%）不再代表现状

模型下载（ModelScope，约 95MB；model.onnx + tokenizer 相关 6 个文件）：

```powershell
$dir = "$env:USERPROFILE\.cache\expense_audit\embeddings\bge-small-zh-v1.5"
New-Item -ItemType Directory -Force $dir | Out-Null
$base = "https://modelscope.cn/models/Xenova/bge-small-zh-v1.5/resolve/master"
foreach ($f in @("config.json","tokenizer.json","tokenizer_config.json","special_tokens_map.json","vocab.txt")) {
  curl.exe -s -L -o "$dir\$f" "$base/$f"
}
curl.exe -s -L -o "$dir\model.onnx" "$base/onnx/model.onnx"
```

## 4. 数据与合规（P0/P1）

- [ ] 保留期清理已配成每日计划任务：`python scripts/run_retention_job.py`（先 `--dry-run` 观察 3 天）
- [ ] 清理审计可查：`maintenance_runs` 有成功记录，失败时退出码非 0
- [ ] MySQL 每日备份 + `uploads/` 同步备份，恢复演练过一次（见下方「备份与恢复」，已落地并演练通过）
- [ ] 用户 AI 数据处理授权条款版本（`DOCUMENT_AI_CONSENT_VERSION`）与隐私说明一致
- [ ] `AI_REVIEW_RAW_RESPONSE_ENABLED` / `DOCUMENT_RAW_RESPONSE_ENABLED` 保持 false（数据最小化）

### 备份与恢复（已落地，2026-11-11 演练通过）

| 项 | 落地内容 |
|---|---|
| 备份账号 | `backup_ro`@`localhost`，仅授 `SELECT, LOCK TABLES, SHOW VIEW, TRIGGER, EVENT`（本库）+ `RELOAD, PROCESS`；**不使用 root 备份** |
| 凭证存放 | `D:\MySQLBackup\conf\backup.cnf`（`[client]` 段），ACL 已收紧为仅当前用户可读；密码不进命令行/日志/仓库 |
| 备份目录 | `D:\MySQLBackup`，30 天保留期自动清理 |
| 命名规则 | `库名__环境__主机__类型__时间.sql.gz`，如 `expense_audit__production__10-0-0-8__full__20261111_183005.sql.gz`；`--kind` 区分 `full`（日常）/`preupgrade`（变更前）/`manual`（手工），避免互相覆盖 |
| 执行 | `python scripts/backup_mysql.py`；演练用 `--dry-run`；变更前 `--kind preupgrade` |
| 每日任务 | `schtasks` 任务 `ExpenseAudit_DailyBackup`（每天 02:00，调用 `D:\MySQLBackup\run_backup.cmd` → `scripts/run_backup_job.py`），一次跑完数据库备份 + 附件同步 + 过期清理；日志 `D:\MySQLBackup\logs\backup_YYYYMMDD.log` 与 `task_stdout.log` |
| 运行身份 | 任务以 `SYSTEM` 运行（`Logon Mode: Interactive/Background`），**不依赖用户登录**；改身份需管理员权限：`schtasks /change /tn "ExpenseAudit_DailyBackup" /ru "SYSTEM"` |
| 权限配套 | 因凭证文件 ACL 已收紧，必须给 SYSTEM 授权，否则以 SYSTEM 运行时 `mysqldump` 报 `Fatal error in defaults handling`：`icacls D:\MySQLBackup\conf /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(RX)"` + `icacls D:\MySQLBackup\conf\backup.cnf /grant "NT AUTHORITY\SYSTEM:(R)"` |
| 附件备份 | `uploads/` 增量镜像到 `D:\MySQLBackup\uploads\`（新增/变化才复制）；源已删除且超期的副本才清理，避免误删在用的附件 |
| 自校验 | 备份后完整解压一遍 + 统计 `CREATE TABLE`，失败退出码非 0 且删除半成品文件 |
| 注意 | MySQL 5.7 客户端**无法打开含中文的路径**，临时凭证文件必须放纯 ASCII 目录（如 `D:\MySQLBackup\conf`）；`.cmd` 包装脚本必须**纯 ASCII 编码**，含中文注释会被 cmd.exe 按 GBK 误读成命令分隔符 |

恢复演练（把备份导入临时库逐表比对，不碰正式库）：

```bash
# 1. 备份
python scripts/backup_mysql.py --kind full

# 2. 恢复验证：导入临时库 expense_audit_restore_check，对比表集合与关键表行数，结束后自动删除临时库
python scripts/verify_restore.py --backup "D:\MySQLBackup\expense_audit__development__127-0-0-1__full__20261111_183713.sql.gz"
```

演练结论：19 张表（含 `schema_migrations`）与源库完全一致，`users/expenses/expense_items/ai_review_logs/workflow_runs` 行数一致。

### 保留期清理任务（已落地）

| 项 | 落地内容 |
|---|---|
| 任务名 | `ExpenseAudit_DailyRetention`，每天 **03:00**（排在 02:00 备份之后） |
| 执行链 | `D:\MySQLBackup\run_retention.cmd` → `scripts/run_retention_job.py` |
| 互斥 | 脚本自带锁文件（`%TEMP%\expense_audit_retention.lock`，6 小时过期），重叠执行会跳过而不是重复删 |
| 审计 | 每次执行写一条 `maintenance_runs`（`job=retention_purge`，成功/失败、参数、数量），失败退出码非 0 |
| 日志 | `D:\MySQLBackup\logs\retention_stdout.log` |
| 预演 | 上线前先 `cmd /c D:\MySQLBackup\run_retention.cmd --dry-run`，观察 3 天数量与耗时再正式启用 |
| 运行身份 | 需与备份任务一致改为 SYSTEM（改身份要管理员）：`schtasks /change /tn "ExpenseAudit_DailyRetention" /ru "SYSTEM"` |

### 后台服务化（Memurai / Celery worker，2026-11-11 验证通过）

| 组件 | 落地方式 | 验证结果 |
|---|---|---|
| Memurai | Windows 服务（启动类型"自动"），`net start Memurai` | `STATE: 4 RUNNING`、`memurai-cli ping → PONG` |
| Celery worker | 计划任务 `ExpenseAudit_CeleryWorker`：`/sc onstart /ru SYSTEM /rl HIGHEST`，执行 `D:\MySQLBackup\run_worker.cmd`（`--pool=threads --concurrency=4`） | 日志 `worker@GIANTDOG ready`；`celery inspect ping → pong`（1 node online）；`inspect registered → ai_review.execute`；队列积压 `llen celery = 0` |
| 注意 | 运行身份改动需管理员权限，普通权限报 `Access is denied`；SYSTEM 身份的任务普通权限**连查都查不到**（同样报 Access is denied），属正常现象 | — |
| 启动冲突 | 手工跑过 `memurai.exe` 后服务会报"系统错误 183（文件已存在）"——先结束手工进程再 `net start` | — |
| 日志轮转 | `celery_worker.log` 只追加不轮转，需按大小定期清理或归档 | — |

## 5. 安全（P0）

- [ ] 限流参数按生产调整：登录 10/分钟、AI 触发 5/分钟、上传 30/分钟（`RATE_LIMIT_*`）
- [ ] `RATE_LIMIT_BACKEND` 设为 `auto` 或 `redis`（多实例必须，否则各实例独立计数、配额成倍放大）
- [ ] 用错误密码连续请求登录，确认第 11 次返回 429 且带 `Retry-After`
- [ ] admin 初始账号已改密，`must_change_password` 生效
- [ ] Nginx/网关启用 HTTPS，并正确透传 `X-Forwarded-For`（限流按真实 IP）
- [ ] 附件下载签名链接有效期（`ATTACHMENT_URL_EXPIRE_MINUTES`）符合安全要求

### 限流存储后端（已落地，2026-11-12 验证）

| 项 | 内容 |
|---|---|
| 配置 | `RATE_LIMIT_BACKEND=auto`（优先 Redis）/ `redis`（强制）/ `memory`（单实例） |
| Redis 实现 | 每 key 一个 ZSET，清理过期 + 计数 + 记录 + 过期时间由一个 **Lua 脚本原子完成**（无"查完再写"并发窗口） |
| 回退 | Redis 连不上 → 启动时回退进程内；运行中故障 → 逐次回退并只告警一次；恢复后自动切回（日志"Redis 限流已恢复"） |
| key 命名 | `ratelimit:<scope>:<身份>`，如 `ratelimit:login:ip:127.0.0.1`，可用 `--scan --pattern 'ratelimit:*'` 排查 |
| 超时 | 连接/读写均 0.5s，限流不允许把请求拖住 |
| 实测 | 两个实例共享同一 Redis 时计数合并（单测）；真实 Redis 连打登录 10×401 → 第 11 次 429，Redis 里出现对应 key |

## 6. 监控与告警（P1）

- [ ] `/health` 接入探针；`/` 返回版本号
- [ ] `maintenance_runs` 失败、`workflow_runs` 长时间 running 有告警（可用 `scripts/recover_workflows.py`）
- [ ] 每日 token / 费用预算已设置（`AI_REVIEW_DAILY_TOKEN_LIMIT` / `AI_REVIEW_DAILY_COST_LIMIT`）
- [ ] 慢请求阈值 `SLOW_REQUEST_MS` 与日志告警规则确认

### 告警方案（已落地，脚本 + 日志，不接第三方）

| 项 | 内容 |
|---|---|
| 脚本 | `python scripts/check_alerts.py`（`--json` 机器可读、`--base-url` 额外探活、`--state-file` 抑制、`--log-file` 落盘） |
| 检查项 | ①`maintenance_runs` 失败（回看 24h）②`maintenance_runs` 卡在 running（>60min）③`workflow_runs` 僵尸（> `WORKFLOW_STALE_MINUTES`）④`ai_review_logs` 僵尸（> `AI_REVIEW_STALE_MINUTES`）⑤当日 token / 费用 / 调用次数超预算 |
| 级别 | `critical`：运维失败、超预算、探活失败；`warning`：任务/工作流/预审卡死 |
| 退出码 | `0` 无告警（或全被抑制）、`1` 仅 warning、`2` 存在 critical —— 可直接给计划任务/监控平台判断 |
| 抑制 | 同一 `code` 在 `--suppress-minutes`（默认 60）内只报一次；**级别升级（warning→critical）立即报**，不会被静默 |
| 调度 | 计划任务 `ExpenseAudit_AlertCheck`：每 **15 分钟** 执行 `D:\MySQLBackup\run_alerts.cmd` |
| 日志 | `D:\MySQLBackup\logs\alerts.log`（告警流水，每行 `severity code detail`）+ `alerts_stdout.log`（原始输出）+ `alert_state.json`（抑制状态） |
| 探活说明 | `--base-url` 需应用**常驻**后再启用（当前应用未服务化，启用会产生假告警） |
| 运行身份 | 已改为 SYSTEM（`Logon Mode: Interactive/Background`），改身份需管理员：`schtasks /change /tn "ExpenseAudit_AlertCheck" /ru "SYSTEM"` |
| 实测 | 每 15 分钟自动运行，`alerts.log` 连续记录 `OK total=0`，`Last Result: 0` |

## 7. 上线验收（逐条执行并记录）

- [ ] `python -m unittest discover -s tests` 全部通过（当前 482 个，2026-11-20 实测：Ran 482 tests OK）
- [ ] 冒烟链路：登录 → 建草稿 → 加明细 → 上传发票 → 提交 → AI 预审 → 审批 → 支付
  - 2026-11-15 已在 `expense_audit_test` + 8001 临时后端跑通两轮（含两级审批、凭证识别、支付），生产入口（443/8000 + Celery）待上线后复跑；见 `backend/evaluation/reports/e2e_smoke_20261115.md`
- [ ] `GET /api/v1/agent/review/{id}/trace` 返回节点耗时、决策、引用、凭证结论
- [ ] `python scripts/purge_expired_data.py --dry-run` 输出正常
- [ ] `python scripts/recover_workflows.py --dry-run` 输出正常
- [ ] 回滚演练：恢复上一版本包 + 数据库备份可用

## 8. 已知边界（写进交付说明）

- 限流已支持 Redis 共享计数（`RATE_LIMIT_BACKEND`），但 Redis 故障时会**静默降级为进程内计数**：多实例下此时实际配额会成倍放大，需靠告警关注（日志关键字"Redis 限流不可用"）；
- 60 条独立双标已完成（blind_v2，2026-11-14）：risk_level κ=0.9495（95%CI 0.8705–1.0000），58/60 一致，2 条分歧已裁决；分层过采样，κ 只代表两人一致性、不能外推总体；报告见 `backend/evaluation/reports/blind_v2_final.md`；
- 旧高危 24 条（v1）保留，仅作 blind_v2 的抽样框，属“AI 预填 + 人工确认”，**不作为独立双标证据**；
- blind_v2 终版与既有标签口径有 13/60 条差异（金额/发票号不符、订单号重复、注入文本）；2026-11-14 决策：本批定位为**人工标准基准**，不直接作为系统评测金标准，口径统一（改规则 / guide_v2.1 重标）另立决策（详见 blind_v2 终版报告第 4、5 节）；
- 真实历史单据尚未导入（等上线后积累 20~30 条脱敏数据）；
- 前端已提供（Vue 3 管理端：登录/工作台/报销/审批/报表/AI 轨迹/系统管理），静态托管见 `DEPLOYMENT.md`；接口调试仍可用 `/api/docs`（生产限内网）；
- **引用指标存在 ±5pp 模型噪声**：同一 Prompt、`temperature=0` 重跑 11 条，引用结果仅 6/11 一致；单次运行的 88%~93% 差异不构成版本优劣结论，需多次运行取中位数才能比较；
- **引用指标口径已变**：2026-11-11 起检索文本改为「关键词 + 原文」、Prompt 升到 `risk_review_v4_2_citation_guard`，旧录制（引用准确率 98.91% / 召回 92.65%）不再代表现状；当前基线见 `docs/CITATION_RECALL_PLAN.md`；
- 标注口径两处待人工复核（不改标签）：拆票类 G081–G084 引 1.2 被判越界（1.2 原文含“拆分报销”）、审批缺失类 G097–G100 要求引用 3.7 而制度无专项条款。
