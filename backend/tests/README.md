# 后端测试（tests/）

零第三方依赖：只用标准库 `unittest`，不需要安装 pytest / httpx。
测试库与开发库完全隔离：**`expense_audit_test`**，不会动 `expense_audit`。

## 运行

在 `backend/` 目录下：

```bash
python -m unittest discover -s tests -v      # 全部用例（-v 打印每个用例名）
python -m unittest tests.test_approval_flow  # 只跑某个文件
python -m unittest tests.test_expense_state_machine.ExpenseStateMachineTest.test_submit_draft_binds_flow_and_creates_pending_approval  # 单个用例
```

## 工作机制

1. `tests/__init__.py` 在导入任何 `app` 模块前，把环境变量 `DATABASE_URL` 指向 `expense_audit_test`
   （pydantic-settings 的优先级是「环境变量 > .env」，所以 `.env` 里的正式库不会被用到）；
2. 首个测试类开始时：`DROP DATABASE` → `CREATE DATABASE` → 回放 `app/sql/schema.sql`
   （结构与正式库同源，不用维护第二份 DDL）；
3. 每个用例前：清空所有表（保留自增重置）→ 灌入最小种子数据（4 部门 / 7 用户 / 6 审批流 / 5 规则 / 2 类别）；
4. 每个用例后：回滚并关闭会话；
5. 进程退出时删除测试库。想保留现场排查：`KEEP_TEST_DB=1 python -m unittest discover -s tests`。

安全护栏：所有清表/建表操作前都会校验"当前连接确实是 `expense_audit_test`"，否则直接抛错中止。

## 目录

| 文件 | 覆盖内容 |
|---|---|
| `support.py` | 建库/建表/清库/种子数据/测试基类/`make_expense` 工厂 |
| `test_approval_flow.py` | `match_approval_flow`：boss 免审批、金额分级与边界、默认流兜底、流程停用/缺失报错 |
| `test_expense_state_machine.py` | 提交/撤回/重提/撤销/支付/审批执行，合法与非法流转、自批与跨部门拦截 |
| `test_auth_and_permissions.py` | 登录、令牌失效（含改密后旧令牌）、强制改密、admin 与报表权限 |
| `test_rule_engine.py` | 四条检查规则（类别限额/发票缺失/重复报销/跨期报销）的命中、边界与容错；提交落库、block 拦截、预检不落库、管理端规则校验 |
| `test_risk_marks.py` | 风险标记翻译（中文标签/级别/关联明细/排序）、待办与已办列表的 `rule_risk_*` 字段、规则命中率统计与日期区间、`/hit-stats` 路由顺序 |
| `test_llm_client.py` | JSON 提取容错、未配置 Key、超时重试、401 不重试、非法 JSON 重试耗尽、请求体字段 |
| `test_risk_agent.py` | 上下文脱敏/截断/状态校验、Prompt 边界、分数优先、block 下限、规则去重、编造引用丢弃、兜底映射 |
| `test_ai_review_service.py` | 任务创建幂等/缓存/force、执行领取、成功回写、LLM 降级、开关关闭、每日限额、僵尸任务、制度审计字段 |
| `test_agent_api.py` | 触发/查询权限（本人、部门经理、财务、admin）、202/200/409/404、执行后查询结果、制度引用返回 |
| `test_workflow_trace_api.py` | 工作流轨迹接口：节点顺序/决策/token/引用、失败 run、404、权限、凭证识别结论 |
| `test_rate_limit.py` | 滑动窗口限流：上限/窗口恢复/多 key 隔离、登录 429 结构、探活放行、按 IP/用户计数、只限 POST 触发、开关关闭 |
| `test_review_queue.py` | Celery 投递开关、投递失败降级 BackgroundTasks、自动触发开关/缓存/异常兜底、提交接口接入 |
| `test_policy_loader.py` | 制度 Markdown 解析、条款编号唯一、版本一致性、元数据/标签校验 |
| `test_policy_tokenizer.py` | 全半角归一化、中文 bigram、英文/数字切分、同义词、引用比对 |
| `test_policy_retriever.py` | BM25 首条召回、费用大类/角色过滤、无匹配、阈值、Top-K、排序确定性 |
| `test_chroma_retriever.py` | 向量检索（假 embedding + 内存 client）：Top-K/过滤/阈值/集合重建；混合检索补位与单路容错；工厂降级 BM25 |
| `test_policy_health.py` | 检索启动自检：正常初始化、chroma→BM25 降级标记、异常不抛出、开关关闭、自检脚本退出码 |
| `test_policy_citation.py` | 合法引用、伪造条款号、改写原文、过短、重复、超限、无制度降级 |
| `test_policy_prompt.py` | `<policy_context>` 注入、空制度提示、闭合标签转义、用户输入防逃逸 |
| `test_policy_agent.py` | RiskAgent 引用保留/丢弃、policy finding 绑定引用、Prompt 版本与注入 |
| `test_document_preprocess.py` | 图片缩放/编码、PNG 截图、PDF 转图、非法/损坏文件 |
| `test_document_agent.py` | 凭证类型归一化、金额/日期解析、扩展字段清洗、低置信度、LLM 降级 |
| `test_document_rules.py` | 金额/发票号/抬头/日期/重复凭证、弱/中等凭证阈值、规则配置校验 |
| `test_document_service.py` | 凭证识别落库、缓存/force、预处理降级、金额不足风险 |
| `test_document_api.py` | 触发识别、查询最新结果、附件不存在 |
| `test_workflow.py` | 决策策略、无附件跳过 document、run/step 审计、LLM 降级、MySQL 检查点恢复 |
| `test_workflow_checkpoint.py` | MySQL Checkpointer 的写入/读取、通道值、pending writes、list/filter/before、删除线程 |
| `test_workflow_retry.py` | 节点可重试/不可重试异常、重试耗尽、attempt 单调递增、真实工作流重试落库 |
| `test_workflow_recovery.py` | 僵尸 run 收尾：超时 run/日志标记 failed、新鲜与已完成 run 不受影响、dry-run、幂等、limit |
| `test_retention_job.py` | 保留期清理：附件/原文/检查点、dry-run、运维审计记录、互斥锁与过期锁、任务入口与锁占用跳过 |
| `test_privacy.py` | AI 数据处理授权/撤回/版本失效、未授权跳过凭证识别、raw_response 开关、附件与原文清理 |
| `test_storage_encryption.py` | 附件落盘加密/解密、历史明文兼容、未配置密钥保持明文 |
| `test_cost_control.py` | 费用估算、token/费用预算、凭证每日限额、工作流节点与整单成本落库 |
| `test_evaluation_harness.py` | 30 条冒烟集加载、指标计算、高危漏放、Markdown 报告 |
| `test_citation_metrics.py` | 引用标注结构、条款编号合法性、命中/漏引/越界/检索缺口、引用准确率与召回率、Markdown 引用章节 |
| `test_evaluation_runner.py` | 100 条金标准结构、96 条真实集、mock 指标、关键词 finding 匹配、录制/回放、版本对比 |
| `test_prompt_v4.py` | v4 校准段、默认版本、自定义 System Prompt 注入与结果版本号 |

## 约定

- 用例直接调用**路由函数**（传 `db` 与 `current_user`），不启动 HTTP 服务，
  所以既覆盖业务逻辑，又不需要 `httpx`/`TestClient`；
- 需要 `Request` 的依赖（如 `get_current_user`）用 `support.make_request()` 构造；
- 新增用例时优先断言**状态码 + 状态流转结果 + 数据副作用**，不要只断言"没抛异常"。

## 后续可加

- 覆盖率统计（需要 `coverage` 包）；
- 附件上传/下载链路（async，需要 `asyncio.run` 包一层）；
- 报表聚合数值断言（按部门/类别/月份）；
- 管理端用户/部门/规则/流程的权限与校验矩阵。
