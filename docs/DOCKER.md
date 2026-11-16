# Docker 一键体验

> 目标：让克隆仓库的人不装 Python/MySQL/Redis，也能在 10 分钟内跑起来。
> 这套 compose **只用于本地演示**，不是生产部署；生产见 [`DEPLOYMENT.md`](DEPLOYMENT.md)。

## 1. 启动

前置：安装 Docker Desktop（Windows/macOS）或 Docker Engine（Linux）。

```bash
# 可选：配置 DeepSeek Key；不配也能跑，AI 预审会降级为规则兜底
export DEEPSEEK_API_KEY=sk-xxxx        # Windows PowerShell: $env:DEEPSEEK_API_KEY="sk-xxxx"

docker compose up --build
```

首次构建约 5~10 分钟（Python 依赖 + 前端构建）。启动后访问：

- 前端：<http://localhost:8080>
- 健康检查：<http://localhost:8080/health>
- 接口文档：<http://localhost:8080/api/docs>

## 2. 登录账号

| 账号 | 密码 | 说明 |
|---|---|---|
| `admin` | `Admin@123456` | 管理员，首次登录强制改密 |
| `testuser` | `Demo@123456` | 员工（需先执行下一步） |
| `manager1` | `Demo@123456` | 部门经理 |
| `fin1` | `Demo@123456` | 财务 |

`init_data.sql` 里的演示密码是 bcrypt 哈希，克隆者不知道明文；先执行一次：

```bash
docker compose exec backend python scripts/seed_demo_passwords.py
```

> 这个脚本只用于本地演示；生产环境（`APP_ENV=production`）默认拒绝执行。

## 3. 服务与数据

| 服务 | 说明 |
|---|---|
| `mysql` | MySQL 8.0，首次启动自动回放 `schema.sql` + `init_data.sql` |
| `redis` | 限流共享计数 + Celery broker |
| `backend` | FastAPI（单 worker） |
| `worker` | Celery（线程池） |
| `frontend` | Nginx 静态托管 + `/api` 反代，8080 暴露 |

- 数据卷：`mysql_data`（数据库）、`app_uploads`（附件）；
- 重置全部数据：`docker compose down -v` 再 `up`；
- MySQL root 密码、应用密码都在 `docker-compose.yml` 里，**仅演示**。

## 4. 与生产部署的差异

| 项 | Docker 演示 | 生产 |
|---|---|---|
| 检索 | BM25（不装 chromadb） | Chroma + BGE-zh |
| 环境 | `APP_ENV=development` | `production` + 密钥自检 |
| HTTPS | 无（HTTP 8080） | Nginx + 证书 |
| 数据库 | MySQL 8.0 | MySQL 5.7 |
| 影像授权 | 关闭（方便体验） | 开启 |

要体验向量检索，可把 `POLICY_RETRIEVER` 改为 `chroma`，并在镜像里安装 `chromadb`、预置模型（镜像会明显变大）。

## 5. 常见问题

- **端口 8080 被占用**：改 `docker-compose.yml` 的 `"8080:80"`；
- **构建失败在 pip**：确认网络能访问 PyPI；国内可给 Docker 配镜像源；
- **AI 一直降级**：`DEEPSEEK_API_KEY` 没传进容器，或余额不足；
- **改了代码不生效**：`docker compose up --build` 重新构建；
- **数据库初始化只执行一次**：想重新灌数据用 `down -v`。
