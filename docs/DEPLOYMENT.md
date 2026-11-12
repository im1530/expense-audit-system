# 生产部署指南

> 适用：Windows / Linux，Nginx 反代 + MySQL + Redis(Memurai)，前后端分离构建。
> 部署物 = `backend/`（Python）+ `frontend/dist`（静态产物）+ `docs/policies/`（制度文件）。
> 上线前的逐项检查见 [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md)；本文只讲"怎么装起来"。

## 0. 架构与端口

```
浏览器
  │ 443 (HTTPS)
  ▼
Nginx  ── 静态：frontend/dist（SPA 回退 index.html）
  │  └── /api/ 反代
  ▼
uvicorn 127.0.0.1:8000（FastAPI，多 worker）
  │      ├── MySQL 127.0.0.1:3306
  │      └── Redis 127.0.0.1:6379（限流共享计数 + Celery broker）
  ▼
Celery worker（消费 AI 预审任务）
```

**只暴露 443**；8000 / 3306 / 6379 一律绑 127.0.0.1，不要对公网开放。

## 1. 构建前端产物

```bash
cd frontend
npm ci --registry=https://registry.npmmirror.com
npm run build          # 产物：frontend/dist
npm test               # 可选：30 个单测应全绿
```

把 `frontend/dist` 整个目录复制到服务器，例如：

- Linux：`/var/www/expense-audit/dist`
- Windows：`D:\sites\expense-audit\dist`

> 每次发版只需替换 dist 目录；`index.html` 在 Nginx 里配置了 `no-store`，用户刷新即拿到新版本。

## 2. 后端准备

```bash
cd backend
python -m venv .venv                       # Python 3.13
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux
```

数据库初始化（首次）：

```bash
mysql -u root -p < app/sql/schema.sql
mysql -u root -p < app/sql/init_data.sql
python scripts/migrate.py verify           # 期望输出"结构一致"
```

初始化检索后端（首次）：

```bash
python scripts/check_policy_backend.py     # 期望 backend=ChromaPolicyRetriever、degraded=false
```

### 生产 `.env` 必须项

| 配置 | 生产值 | 说明 |
|---|---|---|
| `APP_ENV` | `production` | 关闭调试行为 |
| `DEBUG` | `False` | |
| `SECRET_KEY` | 64 位以上随机串 | 泄露=可伪造任意用户令牌 |
| `DATABASE_URL` | 独立库账号（非 root） | 只授权本库 |
| `ALLOWED_HOSTS` | `["expense.example.com"]` | **必须与 Nginx `server_name` 完全一致** |
| `ALLOWED_ORIGINS` | `["https://expense.example.com"]` | 前后端同域时也必须写，浏览器仍会校验 Origin |
| `PUBLIC_BASE_URL` | `https://expense.example.com` | 附件签名链接的对外前缀 |
| `LOG_FILE` / `LOG_RETENTION_DAYS` | `logs/app.log` / 14 | 放持久盘 |
| `RATE_LIMIT_BACKEND` | `auto` 或 `redis` | 多实例必须用 Redis，否则各实例独立计数 |
| `AI_REVIEW_DAILY_TOKEN_LIMIT` / `..._COST_LIMIT` | 按预算填 | 0 表示不限，生产建议设值 |
| `AI_REVIEW_RAW_RESPONSE_ENABLED` | `False` | 数据最小化 |
| `DOCUMENT_RAW_RESPONSE_ENABLED` | `False` | 同上 |
| `ATTACHMENT_ENCRYPTION_KEY` | Fernet 密钥 | 启用附件落盘加密 |

## 3. Nginx 配置

复制 [`deploy/nginx/expense-audit.conf`](../deploy/nginx/expense-audit.conf)，改 4 处 TODO 后：

```bash
# Linux
sudo nginx -t && sudo systemctl reload nginx

# Windows
D:\nginx\nginx.exe -t
D:\nginx\nginx.exe -s reload
```

该配置已包含：HTTPS 跳转、gzip、`/assets/` 长缓存、`index.html` 不缓存、SPA 回退、
`/api/` 反代（含 `X-Forwarded-For`）、`client_max_body_size 12m`、`/health` 免日志、
`/api/docs` 仅内网可访问。

## 4. 后端与 worker 常驻

### Linux（systemd，推荐）

```bash
sudo cp deploy/systemd/expense-audit-backend.service /etc/systemd/system/
sudo cp deploy/systemd/expense-audit-worker.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now expense-audit-backend expense-audit-worker
systemctl status expense-audit-backend --no-pager
```

### Windows（NSSM 或计划任务）

```powershell
# 方式一：NSSM 注册成服务
nssm install ExpenseAuditBackend "C:\Python\Project\expense-audit-system\.venv\Scripts\python.exe" `
  "-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers --forwarded-allow-ips 127.0.0.1"
nssm set ExpenseAuditBackend AppDirectory "C:\Python\Project\expense-audit-system\backend"
nssm set ExpenseAuditBackend Start SERVICE_AUTO_START
nssm start ExpenseAuditBackend

# 方式二：与 Memurai / Celery 一致，用 SYSTEM 身份的计划任务（无需存密码）
#   schtasks /create /tn "ExpenseAudit_Backend" /tr "D:\MySQLBackup\run_backend.cmd" /sc onstart /ru "SYSTEM" /f
```

> Windows 上 `--workers` 建议 ≤ 2（线程/进程与 SQLAlchemy 连接池一起算），
> 并且限流必须走 Redis（`RATE_LIMIT_BACKEND=redis`），否则多 worker 各自计数。

## 4.5 本机试跑（开发机上的"准生产"形态）

开发机已把 Nginx 装在 **`D:\Nginx`**（便携版，5.5 MB），用真实 `dist` + 真实后端跑通了与生产一致的链路：

| 项 | 内容 |
|---|---|
| 访问地址 | `http://127.0.0.1`（80 端口；Vite 的 5173 仅用于改代码时的热更新） |
| 主配置 | `D:\Nginx\conf\nginx.conf`（原始文件备份为 `nginx.conf.default`） |
| 站点配置 | `D:\Nginx\conf\expense-audit.conf`（静态目录 + `/api` 反代 + gzip + SPA 回退） |
| 启动脚本 | `D:\Nginx\start_nginx.cmd`（给计划任务用，重复触发时不会起第二个实例） |
| 自启任务 | 计划任务 `ExpenseAudit_Nginx`：`/sc onstart /ru SYSTEM`，开机自动拉起 |
| 日志 | `D:\Nginx\logs\expense-audit.access.log` / `.error.log` / `task_stdout.log` |

常用命令（都在 `D:\Nginx` 下执行）：

```powershell
.\nginx.exe -t                 # 校验配置（普通权限即可）
.\nginx.exe -s reload          # 改完配置热加载 ← 需要管理员
.\nginx.exe -s stop            # 停止 ← 需要管理员
```

> **权限提示**：Nginx 由 SYSTEM 身份的计划任务启动，普通权限执行 `-s reload` / `-s stop`
> 会报 `OpenEvent("Global\ngx_stop_xxx") failed (5: Access is denied)`。改配置后请在
> **管理员 PowerShell** 里执行，或临时提权：
>
> ```powershell
> Start-Process D:\Nginx\nginx.exe -ArgumentList '-s','reload' -Verb RunAs
> ```
>
> 只是想验证服务是否正常，普通权限跑 `deploy/verify_deploy.ps1` 即可（只读 HTTP）。

> 本机配置是 HTTP 的简化版；生产用 `deploy/nginx/expense-audit.conf`（HTTPS + 证书 + `/api/docs` 仅内网）。
> 每次 `npm run build` 后无需重启 Nginx，刷新浏览器即可（`index.html` 已配 `no-store`）。

## 5. 部署后自检

```powershell
# Windows PowerShell 5.1（系统自带）
powershell -NoProfile -ExecutionPolicy Bypass -File deploy\verify_deploy.ps1 -BaseUrl "https://expense.example.com"

# 已装 PowerShell 7 时
pwsh -File deploy\verify_deploy.ps1 -BaseUrl "https://expense.example.com"
```

> 脚本保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 会把无 BOM 的 `.ps1` 按 ANSI 读取，
> 中文注释会变乱码并直接解析失败。改动该文件后请保持 BOM。

已在本地用 Nginx 1.27 实测通过（`frontend/dist` 静态托管 + `/api` 反代到 8000）：

```
[PASS] 首页               HTTP 200
[PASS] 健康检查             HTTP 200
[PASS] 接口反代             HTTP 401
[PASS] SPA 路由回退         HTTP 200
[PASS] gzip 压缩          Content-Encoding=gzip
```

期望结果：

| 检查 | 期望 |
|---|---|
| `GET /` | 200，`text/html` |
| `GET /health` | 200，`{"status":"healthy"}` |
| `GET /api/v1/expense/category`（无令牌） | 401，返回统一错误结构 |
| `GET /assets/*.js` | `Content-Encoding: gzip`（带 `Accept-Encoding: gzip`） |
| `GET /expenses`（前端路由） | 200，返回 index.html（SPA 回退） |
| `GET /api/docs`（公网 IP） | 403（仅内网放行） |

再按 `GO_LIVE_CHECKLIST.md` 第 7 节跑一遍冒烟链路（登录 → 建单 → 上传 → 提交 → 预审 → 审批 → 支付）。

## 6. 发版与回滚

| 操作 | 步骤 |
|---|---|
| 发版 | 构建新 dist → 覆盖服务器 dist → 重启后端（`systemctl restart` / `nssm restart`）→ 跑自检 |
| 回滚 | 恢复上一版 dist 目录 + `git checkout <上一个 tag>` 还原后端 → 重启 → 需要时用备份库恢复 |
| 数据库变更 | 只走 `migrations/versions/000N_*.sql`，变更前 `python scripts/backup_mysql.py --kind preupgrade` |

回滚演练记录见 `GO_LIVE_CHECKLIST.md`「备份与恢复」一节。

## 7. 常见坑（都是本项目实际踩过的）

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有请求 400 `Invalid host header` | Nginx `server_name` 与 `ALLOWED_HOSTS` 不一致 | 两边改成同一个域名 |
| 限流全部按 `127.0.0.1` 计数 | Nginx 没透传 `X-Forwarded-For` | 按样例加上三个 `X-Forwarded-*` 头 |
| 上传 413 | `client_max_body_size` 小于文件大小 | 调大（样例给 12m） |
| 刷新 `/expenses/1` 404 | 缺 SPA 回退 | `try_files $uri $uri/ /index.html;` |
| AI 预审 504 | 默认 60s 超时不够 | `proxy_read_timeout 120s` |
| 附件链接 404 | 用了相对路径但 `PUBLIC_BASE_URL` 配错 | 配成 `https://域名`，或留空走相对路径 |
| 发版后还是旧界面 | `index.html` 被缓存 | 已配 `no-store`，并检查 CDN 缓存策略 |
| Windows 服务起不来 | 工作目录不对，读不到 `.env` | 服务里显式设 `AppDirectory` / `WorkingDirectory` |
