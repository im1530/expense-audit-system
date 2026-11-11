# 前端（Vue 3 + Element Plus + Pinia + Vite）

AI Agent 财务报销审核系统的管理端界面，对接 `backend/` 提供的 68 个 REST 接口。

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
        └── TraceView.vue       # AI 工作流轨迹（节点耗时、token、引用、凭证）
```

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

> **本机构建注意**：当前机器的临时目录存在 ACL/占用限制，esbuild 在"降级 + 压缩"阶段
> 会因临时文件无法删除而失败，因此 `vite.config.ts` 里设了 `target: 'esnext'` 且
> `minify: false`。产物功能完整，压缩交给 Nginx 的 gzip 即可。换到正常环境可自行改回。

## 与后端约定的几个点

1. **登录是表单而非 JSON**：后端用 `OAuth2PasswordRequestForm`，前端发 `application/x-www-form-urlencoded`。
2. **登录响应只带令牌**：`access_token / token_type / must_change_password`，用户资料由 `GET /auth/me` 单独拉取（JWT 里只放 `sub`，角色改动无需等令牌过期）。
3. **金额是字符串**：后端用 Decimal 序列化为字符串（避免精度问题），展示层统一走 `formatMoney`。
4. **角色决定菜单**：`admin` 不进审批；`manager/finance/boss` 才有审批中心；报表仅 `finance/boss`（及 `admin`）可见。
5. **429 限流**：登录/上传/AI 触发被限流时后端返回 429 + `Retry-After`，拦截器会弹出提示而非报错堆栈。

## 尚未覆盖（后续）

- 管理端页面：用户/部门/规则/审批流的 CRUD（接口已存在，前端未做）。
- 报表 Excel 导出的前端下载交互（`api/report.ts` 已封装，未接按钮）。
- 附件预览（当前只给签名下载链接，未做内嵌预览）。
