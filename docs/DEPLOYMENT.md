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

### 数据库账号与密钥（最小权限）

应用**不要用 root 连库**。生产建议拆成两个账号：

| 账号 | 权限 | 用途 |
|---|---|---|
| `expense_app` | `expense_audit.*` 的 `SELECT/INSERT/UPDATE/DELETE` | 后端运行时读写业务数据 |
| 运维账号（root 或有 DDL 的账号） | `expense_audit.*` 的 `ALTER/CREATE/INDEX/DROP` | 只用于执行 `schema.sql`、`scripts/migrate.py upgrade` |

```sql
-- 建应用账号（密码用随机串，不要写进仓库；命令历史里也不要留明文）
CREATE USER 'expense_app'@'localhost' IDENTIFIED BY '<随机密码>';
GRANT SELECT, INSERT, UPDATE, DELETE ON `expense_audit`.* TO 'expense_app'@'localhost';
-- 测试库需要 ALL：tests/__init__.py 会 DROP/CREATE expense_audit_test 并回放 schema.sql
GRANT ALL PRIVILEGES ON `expense_audit_test`.* TO 'expense_app'@'localhost';
FLUSH PRIVILEGES;
```

两条密钥的运维红线：

- **`SECRET_KEY` 轮换 = 所有令牌立即失效**：已登录用户会被踢回登录页（附件签名链接也一起失效），换之前先通知；建议在停机窗口做。
- **`ATTACHMENT_ENCRYPTION_KEY` 一旦启用不可直接更换**：库里/磁盘上是旧密钥密文，直接换会导致历史附件全部不可读。
  更换流程 = 用旧密钥解密全部附件 → 用新密钥重新加密 → 才能替换配置；密钥本身要进密码库或离线备份，**丢失等于附件永久不可读**。
- 配置了 `APP_ENV=production` 后启动会做**密钥自检**（`SECRET_KEY` 长度 ≥ 32、加密密钥为合法 Fernet 密钥），
  任一不满足进程直接起不来（见 `app/config.py` 的 `_validate_production_secrets`），不再允许"带空密钥上线"。

## 3. Nginx 配置

### 3.1 内网部署（没有公网域名，自签 HTTPS）

公司内网使用、不打算买域名/做备案时，用自签证书即可，**不需要 OpenSSL**：

```powershell
# 在仓库根目录执行；把 --dns / --ip 换成"同事实际访问用的名字和地址"
.\.venv\Scripts\python.exe deploy\gen_selfsigned_cert.py `
  --dns localhost --dns expense.corp.local --ip 192.168.1.20 `
  --out-dir D:\Nginx\cert --years 3
```

产出 `cert.pem` / `key.pem`，Nginx 这样引用：

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name expense.corp.local 192.168.1.20;
    ssl_certificate     D:/Nginx/cert/cert.pem;
    ssl_certificate_key D:/Nginx/cert/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    # 其余 location 与 deploy/nginx/expense-audit.conf 相同
}
```

三个**必须同时做**的配套动作，少一个就会出问题：

1. **客户端安装根证书**（每台要访问的电脑执行一次，管理员 PowerShell）：
   `Import-Certificate -FilePath 'D:\Nginx\cert\cert.pem' -CertStoreLocation Cert:\LocalMachine\Root`
   否则浏览器会报"证书不受信任"，点"继续访问"只是绕过告警，不是解决。
2. **证书 SAN 必须覆盖访问用的名字**：用 `http://192.168.1.20` 访问，证书里就要有 `192.168.1.20`；
   用主机名访问就要有主机名。换机器/换 IP 后需要重新生成。
3. **`ALLOWED_HOSTS` 必须包含同一个名字**，否则后端会返回 400 `Invalid host header`：
   ```ini
   ALLOWED_HOSTS=["localhost","127.0.0.1","expense.corp.local","192.168.1.20"]
   ALLOWED_ORIGINS=["https://expense.corp.local","https://192.168.1.20"]
   ```

> 自签只解决"传输加密"，不解决"对方是谁"。所以根证书要由你们自己分发（最好走 IT 的域策略/终端管理），
> 不要随代码仓库分发；`certs/`、`*.pem`、`*.key` 已在 `.gitignore` 中。

### 3.2 公网部署（有正式域名与证书）

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

开发机已把 Nginx 装在 **`D:\Nginx`**（便携版，5.5 MB），后端也做成 SYSTEM 计划任务，整条链路不依赖登录状态：

| 项 | 内容 |
|---|---|
| 访问地址 | `http://127.0.0.1`（80 端口；Vite 的 5173 仅用于改代码时的热更新） |
| 主配置 | `D:\Nginx\conf\nginx.conf`（原始文件备份为 `nginx.conf.default`） |
| 站点配置 | `D:\Nginx\conf\expense-audit.conf`（静态目录 + `/api` 反代 + gzip + SPA 回退） |
| 启动脚本 | `D:\Nginx\start_nginx.cmd`（重复触发不会起第二个实例） |
| 自启任务 | `ExpenseAudit_Nginx`：`/sc onstart /ru SYSTEM`，开机自动拉起 |
| 日志 | `D:\Nginx\logs\expense-audit.access.log` / `.error.log` / `task_stdout.log` |
| 后端启动 | `D:\MySQLBackup\run_backend.cmd` → uvicorn **单 worker**，日志 `D:\MySQLBackup\logs\backend_stdout.log` |
| 后端起停 | `D:\MySQLBackup\restart_backend.ps1`（提权执行：杀 8000 端口进程 → 触发计划任务） |

### 为什么后端只跑 1 个 worker

Chroma 用的是本地持久化客户端（SQLite + 文件锁），**多进程并发写不安全**。实测 `--workers 2` 时两个 worker 会在启动阶段同时重建向量集合，日志出现两次"索引构建完成"，属于竞态。单 worker 下 FastAPI 仍是异步并发，处理本系统的并发量（内网十几人）足够；确实要扩并发，应先把向量检索换成独立服务或只读副本。

### 服务账户（SYSTEM）必须注意：模型不能放用户目录

SYSTEM 账户的 `%USERPROFILE%` 是 `C:\Windows\system32\config\systemprofile`，读不到你个人目录下的模型文件，表现为启动日志：

```
FileNotFoundError: embedding 模型文件缺失: C:\WINDOWS\system32\config\systemprofile\.cache\...
制度检索自检完成 backend=BM25PolicyRetriever degraded=True
```

即"静默降级成 BM25"。正确做法是把模型放到机器级可读路径，并用**机器级环境变量**指定（`.env` 里也可以，但环境变量优先级更高、对所有服务账户一致）：

```powershell
# 1. 复制模型到 D 盘
Copy-Item "$env:USERPROFILE\.cache\expense_audit\embeddings\bge-small-zh-v1.5\*" D:\models\bge-small-zh-v1.5 -Recurse -Force

# 2. 设置机器级环境变量（管理员执行）
[Environment]::SetEnvironmentVariable('POLICY_EMBEDDING_DIR','D:\models\bge-small-zh-v1.5','Machine')
```

改完要重启后端任务（SYSTEM 进程**普通权限杀不掉**，必须提权）：

```powershell
Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-File','D:\MySQLBackup\restart_backend.ps1'
```

验证：日志应出现 `加载本地 embedding 模型 dir=D:\models\...` 和 `degraded=False`。

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
| `nginx -s reload` 报 `OpenEvent("Global\ngx_reload_xxx") failed (5: Access is denied)` | nginx 由 SYSTEM 身份启动，它创建的 Global 事件 ACL 不允许交互式管理员打开 | 改为"重启 nginx"：`taskkill /F /T /PID <master>` 后重新 `Start-Process nginx.exe`；或给重载单独建一个 SYSTEM 计划任务并用 `schtasks /run` 触发（每次仍需管理员） |
| 改了 nginx 配置却"部分生效"，`-s reload` 也不报错 | Windows `SO_REUSEADDR` 允许残留的旧 worker 与新实例同时绑 80，请求被旧进程接走 | `Get-Process nginx` 看是否有启动时间很早的进程 → `taskkill /F /T /PID <pid>` 清掉孤儿进程 |
| 浏览器提示"证书不受信任" | 自签证书未装入客户端受信任根 | 每台访问端执行 `Import-Certificate -FilePath cert.pem -CertStoreLocation Cert:\LocalMachine\Root`（当前用户可先装 `CurrentUser\Root` 验证） |
| 证书装上后仍报"名称不匹配" | 证书 SAN 里没有客户端实际输入的名字/IP | 用 `deploy/gen_selfsigned_cert.py` 按实际访问名重新生成并重启 nginx |
| 限流全部按 `127.0.0.1` 计数 | Nginx 没透传 `X-Forwarded-For` | 按样例加上三个 `X-Forwarded-*` 头 |
| 上传 413 | `client_max_body_size` 小于文件大小 | 调大（样例给 12m） |
| 刷新 `/expenses/1` 404 | 缺 SPA 回退 | `try_files $uri $uri/ /index.html;` |
| AI 预审 504 | 默认 60s 超时不够 | `proxy_read_timeout 120s` |
| 附件链接 404 | 用了相对路径但 `PUBLIC_BASE_URL` 配错 | 配成 `https://域名`，或留空走相对路径 |
| 发版后还是旧界面 | `index.html` 被缓存 | 已配 `no-store`，并检查 CDN 缓存策略 |
| Windows 服务起不来 | 工作目录不对，读不到 `.env` | 服务里显式设 `AppDirectory` / `WorkingDirectory` |
