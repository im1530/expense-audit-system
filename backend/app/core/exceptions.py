"""
统一异常与错误响应（全项目错误出口）
    目标：所有错误（业务拒绝、参数校验失败、未捕获异常、路由不存在）都返回同一种结构，
    并且带上 request_id，方便把"用户看到的报错"和"服务端日志"对上。
    错误响应结构：
        {
          "code": "NOT_FOUND",              # 机器可读的错误码（前端按它分支）
          "message": "报销单不存在或无权查看",  # 人可读的中文说明
          "request_id": "a1b2c3d4e5f6"       # 请求追踪ID，与日志里的 rid 一致
        }
    参数校验失败（422）额外带 errors：
        {"field": "body.email", "message": "value is not a valid email address"}
    设计取舍：
        - 只统一"错误"，成功响应仍直接返回模型（不动 47 个接口的 return）；
        - 已有接口里的 HTTPException 不需要改写，由 http_exception_handler 统一包装；
        - 需要更精细分类的新代码，直接 raise AppError 子类（400/401/403/404/409 等）。
    安全约定：
        - 5xx 只返回"服务器内部错误"，堆栈只进日志，绝不返回给前端；
        - 错误信息里不放 SQL、文件路径、密钥等内部细节。
"""
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var



logger = logging.getLogger(__name__)  # 日志器



# HTTP 状态码 → 错误码（前端按 code 做分支，不再解析中文文案）
STATUS_CODE_MAP = {  # 状态码映射
    400: "BAD_REQUEST",  # 参数错误
    401: "UNAUTHORIZED",  # 未认证
    403: "FORBIDDEN",  # 无权限
    404: "NOT_FOUND",  # 资源不存在
    405: "METHOD_NOT_ALLOWED",  # 方法不支持
    409: "CONFLICT",  # 状态冲突
    413: "PAYLOAD_TOO_LARGE",  # 请求体过大
    415: "UNSUPPORTED_MEDIA_TYPE",  # 类型不支持
    422: "VALIDATION_ERROR",  # 参数校验失败
    429: "TOO_MANY_REQUESTS",  # 请求过于频繁
    500: "INTERNAL_ERROR",  # 服务器内部错误
}



# 业务异常基类
class AppError(Exception):
    """
    业务异常基类（新代码推荐用它，而不是裸 HTTPException）
        用法：
            raise NotFoundError("报销单不存在")
            raise ConflictError("部门名称已存在", code="DEPARTMENT_NAME_TAKEN")
        由 app_error_handler 统一转成标准错误响应；
        code 默认按 HTTP 状态码推导，需要更细的业务码时显式传入。
    """
    status_code: int = 500  # 默认状态码
    code: str = "INTERNAL_ERROR"  # 默认错误码

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        """
        :param message: 给用户看的中文说明
        :param code: 机器可读错误码，默认按状态码推导
        :param status_code: 覆盖默认 HTTP 状态码
        """
        # 1.先交给父类保存原始异常信息（str(exc) 能拿到 message）
        super().__init__(message)
        # 2.保存给用户看的中文说明
        self.message = message  # 错误说明
        # 3.调用方显式传了错误码就用它，否则沿用类属性上的默认值
        if code is not None:
            self.code = code  # 错误码
        # 4.调用方显式传了状态码就用它，否则沿用类属性上的默认值
        if status_code is not None:
            self.status_code = status_code  # 状态码



# 400 参数/业务规则不合法
class BadRequestError(AppError):
    """请求参数或业务规则不合法（400）"""
    status_code = 400  # 400 状态码
    code = "BAD_REQUEST"  # 400 错误码



# 401 未认证
class UnauthorizedError(AppError):
    """未登录或令牌失效（401）"""
    status_code = 401  # 401 状态码
    code = "UNAUTHORIZED"  # 401 错误码



# 403 无权限
class ForbiddenError(AppError):
    """已登录但没有操作权限（403）"""
    status_code = 403  # 403 状态码
    code = "FORBIDDEN"  # 403 错误码



# 404 资源不存在
class NotFoundError(AppError):
    """目标资源不存在或无权限查看（404）"""
    status_code = 404  # 404 状态码
    code = "NOT_FOUND"  # 404 错误码



# 409 状态冲突
class ConflictError(AppError):
    """当前状态与操作冲突，如重复提交、重名（409）"""
    status_code = 409  # 409 状态码
    code = "CONFLICT"  # 409 错误码



# 取当前请求的追踪ID
def _current_request_id(request: Request | None) -> str:
    """
    取当前请求的 request_id（优先 request.state，其次上下文变量）
        为什么要两个来源：正常错误在中间件上下文内，直接读 request_id_var 即可；
        但未捕获异常会冒泡到最外层的 ServerErrorMiddleware，此时中间件已清理上下文，
        必须从 request.state 里取，否则 500 响应里的 request_id 会变成 "-"，无法和日志对应。
        :param request: 请求对象，可为 None（直接调用处理器时）
        :return: 请求ID；都取不到时返回 "-"
    """
    # 1.优先从 request.state 取：500 兜底时中间件上下文可能已被清理
    if request is not None:
        state_rid = getattr(request.state, "request_id", None)  # state中的请求ID
        if state_rid:
            return state_rid
    # 2.普通请求直接读上下文变量；都取不到返回 "-"
    return request_id_var.get()



# 构造标准错误响应体
def _build_payload(status_code: int, code: str, message: str, request_id: str, extra: dict | None = None) -> dict:
    """
    组装统一的错误响应体
        :param status_code: HTTP 状态码（仅用于兜底推导 code，不放进 body）
        :param code: 机器可读错误码
        :param message: 给用户看的中文说明
        :param request_id: 请求追踪ID
        :param extra: 额外字段，如 422 的 errors
        :return: 响应体字典
    """
    # 1.组装固定三字段；code 为空时按状态码兜底推导
    payload: dict[str, Any] = {  # 响应体
        "code": code or STATUS_CODE_MAP.get(status_code, f"HTTP_{status_code}"),  # 错误码
        "message": message,  # 中文说明
        "request_id": request_id,  # 请求ID
    }
    # 2.合并额外字段（如 422 的 errors 列表）
    if extra:
        payload.update(extra)
    return payload



# 生成 JSON 错误响应
def _json_error(
    request: Request | None,  # 请求对象
    status_code: int,  # HTTP状态码
    code: str,  # 错误码
    message: str,  # 错误说明
    headers: dict | None = None,  # 响应头
    extra: dict | None = None,  # 额外字段
) -> JSONResponse:
    """
    生成 JSONResponse 形态的错误响应
        :param request: 请求对象（用于取 request_id）
        :param status_code: HTTP 状态码
        :param code: 错误码
        :param message: 中文说明
        :param headers: 需要透传的响应头（如 401 的 WWW-Authenticate）
        :param extra: 额外字段
        :return: JSONResponse
    """
    # 1.取当前请求的追踪ID（state 优先，其次上下文变量）
    request_id = _current_request_id(request)  # 请求ID
    # 2.错误响应也带上 X-Request-ID：500 由最外层兜底生成、不经过请求中间件，
    #   这里统一补上，保证前端任何错误都能拿到同一个请求ID
    merged_headers = dict(headers or {})  # 响应头副本
    merged_headers.setdefault("X-Request-ID", request_id)

    # 3.返回统一结构的 JSON 响应（状态码 + body + headers）
    return JSONResponse(
        status_code=status_code,                                        # HTTP 状态码
        content=_build_payload(status_code, code, message, request_id, extra),  # 统一错误响应体
        headers=merged_headers,                                         # 响应头（含 X-Request-ID）
    )



# 兜底：把"没有中文说明"的标准错误翻成中文
def _normalize_message(status_code: int, detail: str) -> str:
    """
    把框架自带的英文提示换成中文（只处理最常见的几种）
        :param status_code: HTTP 状态码
        :param detail: 原始 detail
        :return: 展示用文案
    """
    # 1.404：路由不存在（框架默认英文 "Not Found"）
    if status_code == 404 and detail in ("Not Found", ""):
        return "请求的接口不存在"
    # 2.405：请求方法不支持
    if status_code == 405 and detail in ("Method Not Allowed", ""):
        return "该接口不支持此请求方法"
    # 3.401：未认证
    if status_code == 401 and detail in ("Not authenticated", ""):
        return "未登录或登录已失效，请重新登录"
    # 4.403：无权限
    if status_code == 403 and detail in ("Forbidden", "Not Authorized"):
        return "没有操作权限"
    # 5.其余：原样返回 detail；没有 detail 时用错误码兜底
    return detail or STATUS_CODE_MAP.get(status_code, "请求失败")



# 处理 HTTPException（覆盖全项目现有 raise 的地方）
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    统一包装 HTTPException（兼容 FastAPI 与 Starlette 两套）
        现有 100 多处 raise HTTPException(...) 不需要改，全部走这里；
        401 的 WWW-Authenticate 等自定义响应头会原样透传。
        :param request: 请求对象（保留参数签名，便于扩展记录路径）
        :param exc: 异常对象
        :return: 统一结构的 JSONResponse
    """
    # 1.状态码 → 错误码（未登记的用 HTTP_xxx 兜底）
    code = STATUS_CODE_MAP.get(exc.status_code, f"HTTP_{exc.status_code}")  # 错误码
    # 2.取 detail 作为中文说明；非字符串（如结构化 detail）统一给"请求失败"
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"  # 错误说明
    # 3.把框架英文提示翻成中文
    message = _normalize_message(exc.status_code, message)  # 规范化文案
    # 4.透传原响应头（如 401 的 WWW-Authenticate）并输出统一结构
    return _json_error(request, exc.status_code, code, message, headers=getattr(exc, "headers", None))



# 处理业务异常
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """
    统一处理 AppError 及其子类
        :param request: 请求对象
        :param exc: 业务异常
        :return: 统一结构的 JSONResponse
    """
    return _json_error(request, exc.status_code, exc.code, exc.message)



# 处理参数校验失败（422）
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    把 Pydantic 的校验错误整理成前端友好的结构
        原始 errors() 里的 loc 是元组，这里拼成 "body.items.0.amount" 这种路径，
        前端可以直接按字段高亮提示。
        :param request: 请求对象
        :param exc: 校验异常
        :return: 422 + 字段错误列表
    """
    # 1.把 Pydantic 的 loc 元组拼成 "body.items.0.amount" 这种字段路径
    errors = []  # 字段错误列表
    for error in exc.errors():
        field = ".".join(str(part) for part in error.get("loc", ()))  # 字段路径
        errors.append({"field": field, "message": error.get("msg", "参数不合法")})
    # 2.统一返回 422 + 字段错误列表（extra 会并入响应体）
    return _json_error(
        request,                            # 请求对象（用于取 request_id）
        422,                                # HTTP 状态码
        "VALIDATION_ERROR",                 # 机器可读错误码
        "请求参数校验失败",                    # 给用户看的中文说明
        extra={"errors": errors},           # 额外字段：字段级错误列表
    )



# 处理未捕获异常（500）
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    兜底所有未捕获异常：只返回通用提示，堆栈只进日志
        为什么不在返回值里放异常信息：SQL、文件路径、内部类名都可能泄露，
        排障请用响应体里的 request_id 去日志里搜对应堆栈。
        说明：异常日志由 RequestContextMiddleware 统一记录（带方法/路径/用户ID），
        这里不再重复打印，避免一次异常出现两条堆栈。
        :param request: 请求对象
        :param exc: 异常对象
        :return: 500 + 通用提示
    """
    return _json_error(request, 500, "INTERNAL_ERROR", "服务器内部错误，请稍后重试")



# 注册所有异常处理器
def register_exception_handlers(app: FastAPI) -> None:
    """
    把统一异常处理器注册到应用上（main.py 创建 app 后调用一次）
        注册顺序无所谓，FastAPI 按异常类型精确匹配：
            - StarletteHTTPException：路由不存在/方法不支持/业务 raise 的 HTTPException；
            - AppError：自定义业务异常；
            - RequestValidationError：422 参数校验；
            - Exception：最后兜底。
        :param app: FastAPI 实例
        :return: 无
    """
    # 1.框架级 HTTPException：路由不存在/方法不支持/业务 raise
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    # 2.自定义业务异常（AppError 及子类）
    app.add_exception_handler(AppError, app_error_handler)
    # 3.请求参数校验失败（422）
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # 4.兜底：所有未捕获异常（500）
    app.add_exception_handler(Exception, unhandled_exception_handler)
    logger.info("统一异常处理器已注册")
