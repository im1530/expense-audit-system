# 后端（FastAPI + SQLAlchemy + MySQL + Chroma + LangGraph + Celery）

AI Agent 财务报销审核系统的服务端：报销单全生命周期、逐级审批、规则引擎、制度 RAG、
凭证识别、LangGraph 预审工作流、审计与降级。**79 个 REST 接口，483 个离线单测。**

## 五分钟跑起来

```bash
cd backend
python -m venv .venv                     # Python 3.13
.venv\Scripts\pip install -r requirements.txt

# 1. 建库与初始数据（MySQL 5.7+）
mysql -uroot -p < app/sql/schema.sql
mysql -uroot -p < app/sql/init_data.sql

# 2. 配置：复制 .env.example 为 .env，至少填 DATABASE_URL / SECRET_KEY
#    DEEPSEEK_API_KEY 可以不填 —— AI 预审会自动降级为规则兜底，功能仍可完整体验

# 3. 启动（单 worker：Chroma 本地持久化不支持多进程并发写）
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. 跑测试（用独立库 expense_audit_test，不碰开发库）
..\.venv\Scripts\python.exe -m unittest discover -s tests
```

接口文档：`http://127.0.0.1:8000/api/docs`（生产仅内网）。

## 目录结构（每个文件的职责）

```
backend/
├── app/
│   ├── main.py                  # 应用入口：日志/生命周期/异常/CORS/限流/路由装配
│   ├── config.py                # 全部配置项 + 生产环境密钥自检（不合格拒绝启动）
│   ├── dependencies.py          # 引擎 → 会话工厂 → get_db 依赖（请求级会话）
│   ├── worker.py                # Celery 应用：broker/超时/预取/重试参数
│   │
│   ├── api/
│   │   ├── deps.py              # JWT 解析、当前用户、强制改密拦截、可选登录
│   │   └── v1/
│   │       ├── router.py        # /api/v1 总路由，统一挂载 12 个子路由
│   │       ├── auth.py          # 注册 / 登录 / 改密 / AI 数据授权 / 当前用户
│   │       ├── expense.py       # 报销单：草稿、三级列表、明细、状态流转、字典、轨迹
│   │       ├── approval.py      # 审批执行、我的待办、我的已办、审批状态字典
│   │       ├── attachment.py    # 发票上传、列表、签名下载、凭证识别、软删
│   │       ├── report.py        # 总览/按月/按部门/按类别统计 + xlsx 导出
│   │       ├── agent.py         # AI 预审触发/查询/工作流轨迹
│   │       ├── department.py    # 启用部门下拉（公开接口）
│   │       ├── user.py          # 人员下拉 + 角色字典（仅 admin）
│   │       ├── admin_user.py    # 管理端用户：分页/建号/详情/修改/重置密码
│   │       ├── admin_department.py  # 管理端部门：分页/详情/新建/修改（含停用保护）
│   │       ├── admin_rule.py    # 管理端规则：CRUD + 命中率统计
│   │       └── admin_flow.py    # 管理端审批流：CRUD + 引用/默认流保护
│   │
│   ├── core/
│   │   ├── exceptions.py        # 统一错误结构 code/message/request_id + AppError 体系
│   │   ├── logging.py           # 日志格式、request_id 注入、按天轮转、脱敏工具
│   │   ├── middleware.py        # 请求上下文中间件：rid、访问日志、慢请求告警
│   │   ├── rate_limit.py        # 滑动窗口限流（Redis Lua 原子计数 + 进程内回退）
│   │   └── security.py          # bcrypt 哈希、JWT 签发/验签、附件短期签名令牌
│   │
│   ├── models/                  # SQLAlchemy 模型（全部显式 __tablename__）
│   │   ├── base.py              # 声明基类：created_at / updated_at 通用字段
│   │   ├── user.py              # 用户、角色枚举、角色→权限映射 has_permission
│   │   ├── expense.py           # 报销单、明细、费用类别、状态/类型枚举
│   │   ├── approval.py          # 审批流模板 levels、审批记录、记录状态枚举
│   │   ├── rule.py              # 规则定义、规则命中快照（不建外键，保留历史）
│   │   ├── department.py        # 部门、负责人（manager_id 与 users 互相引用）
│   │   ├── attachment.py        # 附件元数据：相对路径、MIME、软删标记
│   │   ├── ai_review.py         # 预审审计：模型/Prompt/token/费用/降级原因
│   │   ├── document.py          # 凭证识别记录 + 文档类型/证据强度常量表
│   │   ├── workflow.py          # workflow_runs / workflow_steps 节点级审计
│   │   ├── checkpoint.py        # LangGraph MySQL 检查点三表（主体/通道值/写入）
│   │   └── maintenance.py       # 运维任务审计 maintenance_runs
│   │
│   ├── schemas/                 # Pydantic 入参校验与出参契约
│   │   ├── common.py            # 分页参数 + 通用业务筛选（大类/小类/日期/金额）
│   │   ├── user.py              # 注册/登录/改密/管理端建号/详情/列表
│   │   ├── expense.py           # 明细与主单的增改查、待办分页、字典、类别
│   │   ├── approval.py          # 审批动作（只允许通过/驳回）、记录、流程管理
│   │   ├── rule.py              # 规则 CRUD + 命中项 + 风险标记 + 命中率统计
│   │   ├── department.py        # 部门下拉 + 管理端列表/详情/新建/修改
│   │   ├── attachment.py        # 附件出参（只给签名 URL，不暴露磁盘路径）
│   │   ├── report.py            # 总览/月度/部门/类别四类出参
│   │   ├── ai_review.py         # 模型输出严格契约 + 引用草稿 + API 响应
│   │   ├── document.py          # 凭证识别宽松草稿（容忍脏输出）+ 严格响应
│   │   └── workflow_trace.py    # 工作流轨迹响应（节点/引用/凭证结论）
│   │
│   ├── services/                # 业务逻辑（路由只管权限与参数，规则都在这层）
│   │   ├── auth_service.py      # 建号/改号/改密：唯一性、部门规则、自锁与最后管理员保护
│   │   ├── expense_service.py   # 通用筛选、审批流匹配、明细变动后重算总额
│   │   ├── approval_service.py  # 某人待办计数（停用账号前的拦截依据）
│   │   ├── department_service.py# 部门唯一性、保留部门、停用保护、负责人约束
│   │   ├── flow_service.py      # 层级连续/角色白名单、默认流唯一、引用保护
│   │   ├── rule_service.py      # 规则类型/动作/配置校验，金额区间不重叠
│   │   ├── rule_engine.py       # 四类检查规则（限额/缺票/重复/跨期），纯计算
│   │   ├── rule_risk_service.py # 命中 → 审批人可读风险标记 + 命中率统计
│   │   ├── ai_review_service.py # 任务创建/幂等/原子领取/降级落库/回写单据
│   │   ├── llm_client.py        # DeepSeek 客户端（标准库 urllib，异步包装，分类重试）
│   │   ├── review_queue.py      # Celery 投递，失败回退 BackgroundTasks，自动触发
│   │   ├── policy_service.py    # 上下文 → 制度查询；异常降级为空结果
│   │   ├── cost_service.py      # token/费用估算、每日预算、凭证识别限额
│   │   ├── document_service.py  # 凭证识别编排：缓存/限额/落库/与明细比对
│   │   ├── document_preprocess.py # 图片缩放/EXIF 纠正/PDF 渲染成图（去元数据）
│   │   ├── document_rules.py    # 票面事实 vs 明细：金额/票号/抬头/日期/强度/重号
│   │   ├── attachment_service.py# 文件头嗅探、大小限制、落盘、签名 URL、权限判断
│   │   ├── storage_service.py   # 本地存储：按年月分目录、路径越界校验、Fernet 加密
│   │   ├── excel_service.py     # 零依赖（zipfile+OOXML）生成 xlsx
│   │   ├── report_service.py    # 四类聚合查询 + 明细导出行
│   │   ├── alert_service.py     # 五类告警检查 + 同码抑制（级别升级不抑制）
│   │   ├── retention_service.py # 附件/原文/检查点过期清理（支持 dry-run）
│   │   ├── maintenance_service.py # 运维任务 begin_run/finish_run
│   │   ├── privacy_service.py   # AI 影像授权判断（版本变化需重新授权）
│   │   ├── workflow_trace_service.py # 组装 run/steps/引用/凭证结论
│   │   └── workflow_recovery_service.py # 僵尸 run 收尾为 failed（不自动重跑）
│   │
│   ├── agents/                  # LLM Agent（只产出结论，不写库）
│   │   ├── context.py           # 上下文构造：脱敏、控制字符清洗、边界逃逸防护、截断
│   │   ├── risk_agent.py        # Prompt→LLM→Schema→归一化→引用校验→ReviewOutcome
│   │   ├── document_agent.py    # 多模态凭证识别：类型归一、金额/日期解析、置信度
│   │   ├── prompts.py           # v3 基线 + v4 校准 + v4.2 引用守卫（版本号同步）
│   │   └── prompts_v4.py        # 兼容别名，避免维护两份 Prompt
│   │
│   ├── workflow/                # LangGraph 编排
│   │   ├── state.py             # TypedDict 状态：context/policy/outcome/decision/timings
│   │   ├── nodes.py             # 六个节点：load→document→rule→rag→risk→decision
│   │   ├── decision.py          # 确定性决策：fast_track/manual_review/suggest_reject
│   │   ├── engine.py            # 建图、节点记录、重试退避、run 汇总
│   │   ├── service.py           # 新建 run / 从 MySQL 检查点恢复
│   │   └── checkpoint.py        # MySQL Checkpointer（对齐 InMemorySaver 三块结构）
│   │
│   ├── rag/                     # 制度检索
│   │   ├── schema.py            # 条款/文档/查询/命中/结果集（含字符预算裁剪）
│   │   ├── loader.py            # 解析 policy:clause 注释块，结构校验，进程级缓存
│   │   ├── tokenizer.py         # 中文 bigram、同义词扩展、引用比对归一化
│   │   ├── retriever.py         # BM25 + 双阈值 + Top-K；按配置构建后端并降级
│   │   ├── hybrid_retriever.py  # BM25 优先、向量补位（不融合分数）
│   │   ├── chroma_retriever.py  # Chroma 检索、集合签名（含文本口径）自动重建
│   │   ├── embedding.py         # 本地 ONNX 模型向量化（mean/cls 池化 + L2 归一化）
│   │   ├── citation.py          # 引用校验：只认本轮注入条款 + 原文连续片段
│   │   ├── filters.py           # applies_to 硬过滤（大类/角色/规则类型/金额）
│   │   └── health.py            # 启动自检：后端类型、条款数、是否降级
│   │
│   ├── tasks/
│   │   └── review_tasks.py      # Celery 任务：执行预审、指数退避重试、耗尽置 failed
│   │
│   ├── utils/
│   │   └── pagination.py        # SQL 分页 / 内存分页 / 统一分页响应组装
│   │
│   └── sql/
│       ├── schema.sql           # 结构快照（新环境初始化；必须与迁移结果一致）
│       └── init_data.sql        # 初始化数据：部门/用户/类别/审批流/规则/演示单
│
├── migrations/
│   ├── README.md                # 零依赖迁移工具说明（status/upgrade/baseline/verify）
│   └── versions/
│       ├── 0001_add_expenses_created_at_index.sql  # 报表按时间过滤的索引
│       ├── 0002_rule_engine.sql                    # 无票说明字段 + 规则命中表
│       ├── 0003_ai_review_logs.sql                 # AI 预审审计表
│       ├── 0004_ai_review_policy_audit.sql         # 制度版本 + 检索审计字段
│       ├── 0005_document_extractions.sql           # 凭证识别记录表
│       ├── 0006_default_evidence_rule.sql          # 默认弱凭证金额规则
│       ├── 0007_workflow_runs.sql                  # 工作流 run/step + 决策字段
│       ├── 0008_workflow_checkpoints.sql           # LangGraph 检查点三表
│       ├── 0009_ai_consent.sql                     # 用户 AI 授权字段
│       ├── 0010_cost_tracking.sql                  # token/费用统计字段
│       └── 0011_maintenance_runs.sql               # 运维任务审计表
│
├── scripts/
│   ├── migrate.py               # 迁移 CLI：status/upgrade/baseline/verify（sha256 防篡改）
│   ├── backup_mysql.py          # mysqldump 备份：命名规则、自校验、保留期
│   ├── verify_restore.py        # 恢复演练：导入临时库逐表比对后删除
│   ├── run_backup_job.py        # 每日任务：数据库备份 + uploads 增量镜像 + 清理
│   ├── run_retention_job.py     # 保留期清理任务（互斥锁 + maintenance_runs 审计）
│   ├── purge_expired_data.py    # 手动清理入口（支持 --dry-run）
│   ├── check_alerts.py          # 告警巡检：失败/卡死/僵尸/超预算/探活，退出码分级
│   ├── recover_workflows.py     # 僵尸工作流收尾
│   ├── resume_workflow.py       # 从检查点恢复指定 run
│   ├── requeue_reviews.py       # 补投 pending 预审任务（幂等）
│   ├── check_policy_backend.py  # 检索后端自检（backend/degraded/耗时）
│   ├── check_deepseek.py        # LLM 连通性与 Key 有效性检查
│   ├── smoke_ai_review.py       # 预审冒烟：建单→提交→等待→打印结论
│   ├── evaluate.py              # 评测入口：mock/real/replay 三模式
│   ├── evaluate_rag.py          # 检索评测：Recall@k / MRR
│   └── compare_evaluations.py   # 两次评测报告对比
│
├── tests/                       # 全部离线：不联网、不调真实 LLM、用独立测试库
│   ├── __init__.py              # 导入 app 前把 DATABASE_URL 指向 expense_audit_test
│   ├── support.py               # 建库/建表/清表/种子数据/测试基类/make_expense
│   ├── test_approval_flow.py    # 审批流匹配：boss 免审、金额分级、默认流兜底
│   ├── test_expense_state_machine.py # 提交/撤回/重提/撤销/支付/审批的合法与非法流转
│   ├── test_auth_and_permissions.py  # 登录、令牌失效、强制改密、角色权限
│   ├── test_rule_engine.py      # 四类检查规则的命中/边界/容错与管理端校验
│   ├── test_risk_marks.py       # 风险标记翻译、待办字段、命中率统计
│   ├── test_llm_client.py       # JSON 提取、超时重试、401 不重试、请求体
│   ├── test_risk_agent.py       # 上下文脱敏、分数优先、block 下限、编造引用丢弃
│   ├── test_ai_review_service.py# 幂等/缓存/领取/回写/降级/限额/僵尸任务
│   ├── test_agent_api.py        # 触发与查询权限、202/200/409/404
│   ├── test_workflow_trace_api.py # 轨迹接口：节点顺序/决策/token/权限
│   ├── test_rate_limit.py       # 滑动窗口、429 结构、按 IP/用户计数、开关
│   ├── test_review_queue.py     # Celery 投递/失败回退/自动触发
│   ├── test_policy_loader.py    # 制度解析、编号唯一、版本一致、元数据校验
│   ├── test_policy_tokenizer.py # 全半角、bigram、同义词、引用比对
│   ├── test_policy_retriever.py # BM25 首条召回、过滤、阈值、Top-K、确定性
│   ├── test_chroma_retriever.py # 向量检索（假 embedding）、混合补位、工厂降级
│   ├── test_policy_health.py    # 自检正常/降级标记/异常不抛出/开关关闭
│   ├── test_policy_citation.py  # 合法引用、伪造条款、改写原文、过短、超限
│   ├── test_policy_prompt.py    # 制度区注入、空制度提示、标签转义、防逃逸
│   ├── test_policy_agent.py     # 引用保留/丢弃、policy finding 绑定引用
│   ├── test_document_preprocess.py # 图片缩放/编码、PDF 转图、损坏文件
│   ├── test_document_agent.py   # 类型归一、金额/日期解析、扩展字段、低置信度
│   ├── test_document_rules.py   # 金额/票号/抬头/日期/重号、凭证强度阈值
│   ├── test_document_service.py # 识别落库、缓存/force、预处理降级
│   ├── test_document_api.py     # 触发识别、查询最新结果、附件不存在
│   ├── test_workflow.py         # 决策策略、无附件跳过 document、run/step 审计、恢复
│   ├── test_workflow_checkpoint.py # Checkpointer 读写、通道值、pending writes、删除
│   ├── test_workflow_retry.py   # 可重试/不可重试、重试耗尽、attempt 单调递增
│   ├── test_workflow_recovery.py# 僵尸 run 收尾、dry-run、幂等、limit
│   ├── test_retention_job.py    # 清理三类数据、审计记录、互斥锁与过期锁
│   ├── test_privacy.py          # 授权/撤回/版本失效、未授权跳过识别
│   ├── test_storage_encryption.py # 落盘加密/解密、历史明文兼容、未配密钥明文
│   ├── test_cost_control.py     # 费用估算、预算、每日限额、节点成本落库
│   ├── test_config_security.py  # 生产环境密钥自检（短密钥/空/非法 Fernet → 拒绝启动）
│   ├── test_selfsigned_cert.py  # 自签证书生成脚本（SAN/私钥匹配/有效期）
│   ├── test_evaluation_harness.py # 冒烟集加载、指标计算、高危漏放、报告
│   ├── test_citation_metrics.py # 引用标注结构、命中/漏引/越界、准确率与召回
│   ├── test_evaluation_runner.py# 金标准结构、mock 指标、录制/回放、版本对比
│   ├── test_prompt_v4.py        # v4 校准段、默认版本、自定义 Prompt 注入
│   ├── test_blind_v2_freeze.py  # blind_v2 冻结样本：哈希/分层/无泄题/重跑一致
│   ├── test_blind_v2_packets.py # 盲包：A/B 独立乱序、待填空白、无 G 编号、33 条条款
│   ├── test_blind_v2_agreement.py # 一致性：58/60、κ 与 CI 可复现、裁决清单
│   └── test_blind_v2_final.py   # 终版标签：16/23/21、2 条裁决、与 v1 差异计数
│
├── evaluation/                  # 离线评测（详见 evaluation/README.md）
│   ├── README.md                # 指标口径、历史结论、复现命令
│   ├── generate_golden.py       # 重新生成 100 条金标准
│   ├── harness.py               # 决策/风险一致率、漏放率、finding 召回
│   ├── runner.py                # 真实 LLM 运行、录制、回放
│   ├── compare.py               # 两个版本报告对比
│   ├── rag_harness.py           # 检索评测（Recall@k / MRR，三种后端）
│   ├── build_review_workbook.py / update_review_workbook.py  # 复核工作簿生成/更新
│   ├── add_real_results_sheet.py / add_v4_sheet.py          # 往工作簿追加结果页
│   ├── build_high_risk_blind.py / fill_high_risk_blind.py   # 高危盲表生成与填写
│   ├── build_blind_v2.py / build_blind_v2_packets.py        # blind_v2 冻结样本与两份盲包
│   ├── analyze_blind_v2.py / finalize_blind_v2.py           # 一致性统计与终版标签收口
│   ├── fill_reviewer_names.py / finalize_high_risk_review.py # 复核人回填与 A/B 收口
│   ├── confirm_golden.py / freeze_real96.py                 # 金标准确认与冻结
│   ├── cases/                   # 冒烟集/金标准/真实集/检索查询/引用标注/高危标签
│   ├── recordings/              # 真实调用录制（结构化结论，不含原始返回）
│   └── reports/                 # 评测报告（json + md）与复核工作簿
│
├── requirements.txt             # 依赖与版本约束（bcrypt/celery 有明确钉版原因）
└── .env.example                 # 全部配置项示例（不含真实密钥）
```

## 分层与约定

| 层 | 目录 | 规则 |
|---|---|---|
| 路由 | `app/api/v1/` | 只做权限、参数、状态码；业务规则一律下沉到 `services/` |
| 服务 | `app/services/` | 一个领域一个文件；写操作的规则集中在这里，直接 `commit` 或由调用方控制事务 |
| 模型 | `app/models/` | 只描述表结构与关系；枚举用 `native_enum=False`，改取值不用 ALTER |
| 契约 | `app/schemas/` | 入参校验 + 出参裁剪；出参模型不声明 `password_hash` 即为脱敏 |
| Agent | `app/agents/` | 只产出结论，不写库；模型输出一律先过 Pydantic 再归一化 |
| 编排 | `app/workflow/` | 节点只调 service；失败重试与审计由引擎负责 |
| 检索 | `app/rag/` | 业务只依赖 `PolicyRetriever` 协议，换后端零改动 |

三条硬约束：

1. **AI 失败必须降级**：LLM 超时/报错/输出非法 → 规则兜底（`is_fallback=true`），绝不阻断提交与审批；
2. **单 worker**：Chroma 本地持久化（SQLite + 文件锁）不支持多进程并发写，多 worker 会在启动时并发重建集合；
3. **写操作的事务边界**：状态流转与审批记录必须同事务提交，避免"单据待审批但待办里查不到"。

## 测试

```bash
..\.venv\Scripts\python.exe -m unittest discover -s tests          # 全部（当前 483 个）
..\.venv\Scripts\python.exe -m unittest tests.test_rule_engine -v  # 单个文件
```

- 独立测试库 `expense_audit_test`，用例前清表、灌最小种子数据，进程退出删除；
- 不联网、不调真实 LLM：LLM 用假客户端/录制回放，Chroma 用假 embedding + 内存 client；
- 需要真实模型时用 `scripts/evaluate.py --mode real`，会产生费用，执行前确认。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`../README.md`](../README.md) | 项目总览、架构、5 分钟快速开始 |
| [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) | 部署、内网自签 HTTPS、数据库最小权限、常见坑 |
| [`../docs/GO_LIVE_CHECKLIST.md`](../docs/GO_LIVE_CHECKLIST.md) | 上线前逐项检查（P0/P1） |
| [`../docs/CITATION_RECALL_PLAN.md`](../docs/CITATION_RECALL_PLAN.md) | 引用漏引的定位过程与收口方案 |
| [`../docs/policies/expense_policy_v1_3.md`](../docs/policies/expense_policy_v1_3.md) | 制度原文（v1.3，33 条） |
| [`evaluation/README.md`](evaluation/README.md) | 评测集、指标口径、历史结论 |
| [`migrations/README.md`](migrations/README.md) | 零依赖迁移工具用法 |
| [`tests/README.md`](tests/README.md) | 测试机制与逐文件覆盖说明 |
