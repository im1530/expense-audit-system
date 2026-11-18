# 前端（Vue 3 + Element Plus + Pinia + Vite）

AI Agent 财务报销审核系统的管理端界面，对接 `backend/` 提供的 79 个 REST 操作。

## 技术栈

| 组件 | 版本 | 用途 |
|---|---|---|
| Vue | 3.5 | 视图框架（Composition API + `<script setup>`） |
| Element Plus | 2.8 | UI 组件库（中文语言包） |
| Pinia | 2.2 | 状态管理（用户/令牌） |
| Vue Router | 4.4 | 路由与登录守卫 |
| Axios | 1.7 | HTTP 客户端（统一拦截器/错误提示） |
| Vite | 5.4 | 开发服务器与构建 |
| TypeScript | 5.6 | 类型（`strict: false`，重实用） |

## 目录结构

```
frontend/
├── index.html
├── vite.config.ts          # /api 代理到后端，避免开发期跨域
├── package.json
└── src/
    ├── main.ts             # 入口：Pinia + Router + Element Plus
    ├── App.vue
    ├── api/                # 按业务域拆分的接口封装
    │   ├── auth.ts         # 登录/当前用户/改密/AI 授权
    │   ├── expense.ts      # 报销单 CRUD、状态流转、明细、发票
    │   ├── approval.ts     # 待办/已办/审批执行
    │   ├── agent.ts        # AI 预审触发/结果/工作流轨迹
    │   └── report.ts       # 报表总览/按月/按部门/按类别/导出
    ├── layouts/
    │   └── MainLayout.vue  # 侧边栏 + 顶栏 + 强制改密弹窗
    ├── router/index.ts     # 路由表 + 登录/角色守卫
    ├── stores/user.ts      # 令牌、当前用户、角色判定
    ├── utils/
    │   ├── request.ts      # Axios 实例、令牌注入、401 跳登录
    │   └── helpers.ts      # 状态/风险/决策中文映射、金额与时间格式化
    ├── styles/main.scss
    └── views/
        ├── Login.vue           # 登录（含首登强制改密）
        ├── Dashboard.vue       # 工作台（统计卡片 + 最近单据 + AI 授权）
        ├── ExpenseList.vue     # 我的报销（筛选/分页/提交/撤回/撤销）
        ├── ExpenseSubmit.vue   # 新建报销（多明细表单）
        ├── ExpenseDetail.vue   # 详情（明细/发票/AI 结论/审批轨迹/支付）
        ├── ApprovalCenter.vue  # 审批中心（待办/已办 + 通过/驳回）
        ├── Reports.vue         # 统计报表（总览/按月/部门/类别）
        ├── TraceView.vue       # AI 工作流轨迹（节点耗时、token、引用、凭证）
        └── admin/              # 管理端（仅 admin 可见）
            ├── UserManage.vue        # 用户：筛选/建号/改角色部门/重置密码
            ├── DepartmentManage.vue  # 部门：建改/启停/设负责人
            ├── RuleManage.vue        # 规则：建改删、JSON 配置校验、启停
            └── FlowManage.vue        # 审批流：多级角色编排、默认流、启停
```

## 管理端（仅 `admin` 角色）

| 页面 | 能力 |
|---|---|
| 用户管理 | 按角色/部门/状态筛选、建号（初始密码强制首登改密）、改角色与部门、启停账号、重置密码 |
| 部门管理 | 新建/编辑、启停、设置部门负责人（从 manager 角色里选） |
| 规则管理 | 建/改/删、规则类型与动作选择、`config` 用 JSON 编辑并做前端合法性校验（提交前解析，非法直接拦下） |
| 审批流管理 | 多级审批链动态增删（manager / finance / finance_other / boss）、设为默认流、启停 |

> 删除规则/审批流前会二次确认并提示影响（规则参与预检、流程被金额规则引用的后果）。

## 本地运行

```bash
cd frontend
npm install --registry=https://registry.npmmirror.com

# 终端 1：先启动后端（默认 8000 端口）
cd ../backend && ..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# 终端 2：启动前端
cd ../frontend && npm run dev
# 打开 http://127.0.0.1:5173
```

后端不在 8000 时，用环境变量指定代理目标：

```powershell
$env:VITE_API_TARGET="http://127.0.0.1:8123"; npm run dev
```

## 构建

```bash
npm run build      # 产物在 dist/，交给 Nginx 托管并把 /api 反代到后端
```

## 单元测试（Vitest + jsdom）

```bash
npm test           # 单次运行
npm run test:watch # 监听模式
```

当前覆盖 **4 个文件 / 30 个用例**，全部离线（不发网络、不连后端）：

| 文件 | 覆盖点 |
|---|---|
| `src/utils/helpers.test.ts` | 金额格式化（千分位/空值/非数字）、时间与日期格式化、状态枚举映射完整性 |
| `src/utils/request.test.ts` | 令牌读写、`Authorization` 自动注入、响应解包、`rawResponse` 透传、401 清令牌并跳登录 |
| `src/stores/user.test.ts` | 登录写令牌并拉资料、首登强制改密标记、角色判定（employee/finance/admin）、退出清状态 |
| `src/router/guard.test.ts` | 公开页放行、未登录跳转并带 redirect、刷新自动拉资料、权限拦截（审批/报表/管理端）、强制改密不跳登录 |

> **Node 26 注意**：Node 26 内置了实验性的全局 `localStorage`（未加 `--localstorage-file` 时为
> `undefined`），会盖掉 jsdom 的实现。`src/tests/setup.ts` 已显式把全局 `localStorage` /
> `sessionStorage` 指回 `window` 的实现，否则所有用到本地存储的用例都会报
> `Cannot read properties of undefined (reading 'clear')`。

> **本机构建注意**：当前机器的临时目录存在 ACL/占用限制，esbuild 在"降级 + 压缩"阶段
> 会因临时文件无法删除而失败，因此 `vite.config.ts` 里设了 `target: 'esnext'` 且
> `minify: false`。产物功能完整，压缩交给 Nginx 的 gzip 即可。换到正常环境可自行改回。

## 与后端约定的几个点

1. **登录是表单而非 JSON**：后端用 `OAuth2PasswordRequestForm`，前端发 `application/x-www-form-urlencoded`。
2. **登录响应只带令牌**：`access_token / token_type / must_change_password`，用户资料由 `GET /auth/me` 单独拉取（JWT 里只放 `sub`，角色改动无需等令牌过期）。
3. **金额是字符串**：后端用 Decimal 序列化为字符串（避免精度问题），展示层统一走 `formatMoney`。
4. **角色决定菜单**：`admin` 不进审批；`manager/finance/boss` 才有审批中心；报表仅 `finance/boss`（及 `admin`）可见。
5. **429 限流**：登录/上传/AI 触发被限流时后端返回 429 + `Retry-After`，拦截器会弹出提示而非报错堆栈。

## 功能覆盖（v1.3）

- 管理端页面：用户 / 部门 / 规则 / 审批流的增删改查；
- 两阶段结算：财务登记、待领取、提交人确认、站内通知；
- 报表：总览 / 按月 / 按部门 / 按类别 + Excel 导出；
- 附件：图片内嵌预览，PDF 新标签打开，支持签名下载与删除。
