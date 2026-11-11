"""
应用主包（backend/app）
    FastAPI 后端服务的根包，按"配置 → 数据库 → 模型 → 数据模型 → 接口 → 服务 → 工具"分层组织：
        - config.py        全局配置（Pydantic Settings，从 .env 读取）
        - dependencies.py  数据库引擎、会话工厂、请求级会话依赖
        - core/            基础设施层：密码哈希、JWT 签发与验签
        - models/          SQLAlchemy ORM 表结构（users/departments/expenses/approval/rules 等）
        - schemas/         Pydantic 请求入参与响应出参模型
        - api/             HTTP 接口层（v1 版本路由、鉴权依赖）
        - services/        业务服务层：跨接口复用的业务逻辑
        - utils/           通用工具（分页等）
        - sql/             （暂未纳入整理）建表与初始化数据脚本
    导入约定：统一以 `app` 为根包，例如 `from app.config import settings`；
    启动入口为 `app/main.py` 中的应用对象 `app`，由 uvicorn 加载。
"""
