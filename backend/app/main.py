"""
FastAPI 应用入口（启动文件）
    把配置、日志、中间件、路由装配成一个可运行的后端服务，uvicorn 通过 `uvicorn app.main:app` 加载本模块的 app 对象。
    装配顺序（也是请求进入后经过的顺序）：
        1. 初始化全局日志（core.logging：级别、格式、request_id、可选文件轮转，并接管 uvicorn 日志）
        2. 创建 FastAPI 实例，把交互式文档统一到 /api/docs 与 /api/redoc；
           lifespan 启动钩子做制度检索自检（模型/向量库是否可用，失败自动降级 BM25）
        3. 注册统一异常处理器（core.exceptions：错误响应统一为 code/message/request_id）
        4. 挂 CORS 跨域中间件，放行 config 中配置的前端白名单
        5. 挂 TrustedHost 可信主机中间件，拒绝伪造 Host 头的请求
        6. 挂接口限流中间件（登录/注册/AI 预审触发/附件上传，超限返回 429）
        7. 挂请求上下文中间件（生成 request_id、记录访问日志、异常堆栈）——最后注册，位于最外层
        8. 挂载 api_router，所有业务接口统一前缀 /api/v1
        9. 另提供 "/" 与 "/health" 两个轻量探活接口（不查库、不做鉴权）
    注意：本文件只做"装配"，不写业务逻辑；业务一律放在 api/ 与 services/ 中。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.config import settings
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.api.v1.router import api_router
from app.core.exceptions import register_exception_handlers  # 统一异常：错误响应结构一致
from app.core.logging import setup_logging          # 日志基建：统一格式 + request_id + 文件轮转
from app.core.middleware import RequestContextMiddleware   # 请求上下文：rid / 访问日志 / 异常堆栈
from app.core.rate_limit import RateLimitMiddleware   # 接口限流：登录/触发/上传



# ========== 日志配置 ==========
# 统一由 core.logging.setup_logging() 处理（级别来自 settings.LOG_LEVEL，可选写文件），
# 并接管 uvicorn 的日志器，避免同一行日志打两遍。
setup_logging()

# 获取日志器：后面各处用它打日志，日志里会带模块名与 request_id
logger = logging.getLogger(__name__)    # 模块日志器




# ========== 生命周期：启动时做制度检索自检 ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期钩子
        启动阶段：初始化检索后端（预热模型/向量库），把实际后端与降级情况写进启动日志；
        关闭阶段：当前无需清理（连接池由各依赖自行管理）。
        :param app: FastAPI 实例
        :return: 异步上下文
    """
    # 1.启动自检：失败不阻断启动，检索层会自动降级 BM25
    if settings.POLICY_STARTUP_CHECK:
        from app.rag.health import check_policy_backend

        check_policy_backend()
    yield




# ========== 创建FastAPI应用实例 ==========
# app 就是整个后端的核心对象，后面挂中间件、路由、异常处理都挂在它身上
app = FastAPI(
    title = "AI Agent 财务报销审核系统",                            # 接口文档页面顶部显示的系统名称
    description = "基于LLM、RAG、LangGraph的智能化报销审核平台",      # 文档里的一段系统简介
    version = "1.0.0",                                            # 当前API版本号，后面大改动可升到 1.1.0 / 2.0.0
    docs_url = '/api/docs',                                       # Swagger 交互式文档地址（默认是 /docs，这里统一到 /api/docs）
    redoc_url = '/api/redoc',                                     # ReDoc 风格的只读文档地址，另一种文档视图
    lifespan = lifespan                                           # 生命周期钩子：启动时做检索自检
)



# ===== 统一异常处理器：所有错误统一成 {code, message, request_id} =====
# 覆盖 HTTPException（现有接口的 raise）、AppError（新代码的业务异常）、422 校验错误、未捕获异常
register_exception_handlers(app)



# ===== CORS 跨域中间件：允许前端(指定白名单域名)访问后端 =====
# 作用：解决前端(不同端口)直接访问后端被浏览器拦截的问题
app.add_middleware(
    CORSMiddleware,              # 这是一个中间件类，后端用来告诉浏览器 "这几个来源我放行" 的中间件
    allow_origins = settings.ALLOWED_ORIGINS,    # 读 config 里配置好的白名单
    allow_credentials= True,                     # 允许带 Cookie/凭证
    allow_methods=["*"],                         # 允许所有 HTTP 方法
    allow_headers=["*"],                         # 允许所有请求头
)



# ===== 可信主机中间件：只放行白名单里的域名/主机，伪造 Host 头的请求直接拒 =====
app.add_middleware(
    TrustedHostMiddleware,                  # TrustedHostMiddleware：进来的每个请求先检查它的 Host 请求头
    allowed_hosts=settings.ALLOWED_HOSTS,   # 允许的主机白名单，读取 config.py 里的 ALLOWED_HOSTS
)



# ===== 接口限流中间件：登录/注册/AI 预审触发/附件上传 =====
# 放在请求上下文中间件之前注册（即位于其内层），429 也能带上 X-Request-ID 并写入访问日志
app.add_middleware(RateLimitMiddleware)


# ===== 请求上下文中间件：生成 request_id、记录访问日志、异常堆栈 =====
# 最后注册 = 位于最外层：连 CORS/TrustedHost 拒绝的请求也能被记录，响应头也会带上 X-Request-ID
app.add_middleware(RequestContextMiddleware)



# ===== 挂载 API 总路由，统一加 /api/v1 前缀 =====
"""
app.include_router() --- FastAPI 的方法："把一组路由挂到应用上"
    api_router: 要挂的那个总路由对象，里面汇总了所有业务子路由
"""
app.include_router(api_router)




# ========== 测试接口 ==========
# @app.get("/")：装饰器，表示用 GET 方法访问根路径 "/" 时执行 root()
@app.get('/')
async def root():
    """
    根路径健康检查接口
        用途：部署后浏览器直接访问根路径，快速确认服务已启动；不查数据库、不做鉴权。
        说明：`async def` 异步函数，遇到 `await` 的耗时等待点会先挂起、让出位置去处理别的请求，等结果好了再回来继续，不阻塞别人。
        :return: 含系统名称、运行状态、版本号的字典，由 FastAPI 自动序列化为 JSON
    """
    return {
        "message": "AI Agent 财务报销审核系统",
        "status": "running",
        "version": "1.0.0",
    }


# @app.get("/health")：健康检查接口，给运维/监控探活用
@app.get("/health")
async def health():
    """
    健康检查端点：给运维与监控探活用（如 K8s 的 liveness/readiness 探针、负载均衡健康探测）
        与根路径的区别：语义只表示"进程活着"，不暴露系统元信息，便于监控系统稳定解析。
        :return: {"status": "healthy"}
    """
    return {"status": "healthy",}
