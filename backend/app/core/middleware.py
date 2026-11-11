"""
请求上下文中间件（每个请求一个追踪ID + 一条访问日志）
    作用：
        1. 为每个请求生成 request_id（优先复用前端传来的 X-Request-ID），
           写入上下文字段并回写到响应头，前端报错截图时能直接给出 ID 让后端定位；
        2. 记录"方法 + 路径 + 状态码 + 耗时 + 用户ID + 客户端IP"，
           耗时超过 settings.SLOW_REQUEST_MS 升级为 WARNING，方便发现慢接口；
        3. 未捕获异常时打一条带堆栈的 ERROR 日志（仍交给后续异常处理返回 500），
           解决"只有 uvicorn 一行堆栈、不知道是谁调的哪个接口"的问题。
    说明：本中间件是最外层（最后注册），因此连 CORS 之前的 4xx/异常也能被记录。
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.core.logging import request_id_var



# 请求追踪ID的请求头/响应头名
REQUEST_ID_HEADER = "X-Request-ID"  # 请求ID头

# 不记录访问日志的路径（探活接口太频繁，避免刷屏）
QUIET_PATHS = {"/health", "/api/docs", "/api/redoc", "/openapi.json"}  # 静默路径

logger = logging.getLogger("app.request")  # 请求日志器



# 取客户端IP
def _client_ip(request: Request) -> str:
    """
    取客户端 IP（优先取反向代理透传的 X-Forwarded-For 第一个地址）
        :param request: 请求对象
        :return: IP 字符串；取不到时返回 "-"
    """
    # 1.优先取反向代理透传的 X-Forwarded-For（第一段是真实客户端IP）
    forwarded = request.headers.get("x-forwarded-for")  # 代理转发头
    if forwarded:
        return forwarded.split(",")[0].strip()
    # 2.没有代理头时取直连地址；拿不到就给 "-"
    return request.client.host if request.client else "-"



# 请求上下文中间件
class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    请求级上下文中间件：生成 rid、记录访问日志、回写响应头
        继承 BaseHTTPMiddleware 而不是纯 ASGI 中间件，是因为这里只需要在"请求前后"各做一次记录，
        不涉及对响应体流的改写，写法更直观。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        处理一次请求：设置 rid → 调用下游 → 记录日志 → 回写 rid
            :param request: 请求对象
            :param call_next: 下游处理链
            :return: 下游返回的响应（附带 X-Request-ID 响应头）
        """
        # 1.生成/复用请求ID，并写入上下文变量（同一请求内的日志都会带上它）
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]  # 请求ID
        token = request_id_var.set(request_id)  # 上下文令牌
        # 同时挂到 request.state：异常处理器在 500 兜底时上下文可能已被清理，从这里取更可靠
        request.state.request_id = request_id  # 记录请求ID
        start = time.perf_counter()  # 开始计时

        try:
            response = await call_next(request)  # 下游响应
        except Exception:
            # 2.未捕获异常：记录堆栈后原样抛出，交给 FastAPI 的异常处理返回 500
            duration_ms = (time.perf_counter() - start) * 1000  # 耗时毫秒
            logger.exception(
                "请求异常 %s %s 耗时=%.1fms user=%s ip=%s",  # 日志模板
                request.method,  # 请求方法
                request.url.path,  # 请求路径
                duration_ms,  # 耗时毫秒
                getattr(request.state, "user_id", "-"),  # 用户ID
                _client_ip(request),  # 客户端IP
            )
            request_id_var.reset(token)
            raise

        # 3.正常返回：按耗时决定日志级别
        duration_ms = (time.perf_counter() - start) * 1000  # 耗时毫秒
        if request.url.path not in QUIET_PATHS:
            level = logging.WARNING if duration_ms >= settings.SLOW_REQUEST_MS else logging.INFO  # 日志级别
            logger.log(
                level,  # 日志级别
                "请求完成 %s %s -> %s 耗时=%.1fms user=%s ip=%s",  # 日志模板
                request.method,  # 请求方法
                request.url.path,  # 请求路径
                response.status_code,  # 响应状态码
                duration_ms,  # 耗时毫秒
                getattr(request.state, "user_id", "-"),  # 用户ID
                _client_ip(request),  # 客户端IP
            )

        # 4.把 rid 回写给前端，并清理上下文，避免线程复用串号
        response.headers[REQUEST_ID_HEADER] = request_id  # 回写请求ID
        request_id_var.reset(token)
        return response
