"""
日志基建（全项目统一入口）
    目标：让"一条请求"在日志里可被完整追踪，同时不引入任何第三方依赖。
    组成：
        - setup_logging()：配置根日志器（级别来自 settings.LOG_LEVEL），输出到控制台，
          可选按天轮转写文件；并接管 uvicorn 的日志器，避免同一行日志打两遍；
        - request_id_var：请求级上下文变量，中间件写入、日志格式器读取，
          同一次请求内的所有日志都会带上同一个 rid；
        - RequestIdFilter：把 rid 注入每条日志记录，格式串里用 %(request_id)s 读取；
        - mask_phone / mask_email / mask_secret：脱敏工具，写日志时避免泄露敏感信息。
    使用约定：
        - 业务代码统一 `logger = logging.getLogger(__name__)`，不要 print；
        - 日志里只放 ID、状态、数量这类可审计信息，不打印密码、token、完整手机号；
        - 关键业务动作（登录、提交、审批、支付、账号变更等）必须打一条 INFO，
          排障日志会被轮转删掉，真正的合规留痕应进业务表。
"""
import logging
import sys
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import settings



# 当前请求的追踪ID：由中间件写入，日志格式器读取；非请求上下文（如启动阶段）默认 "-"
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")  # 请求ID上下文



# 日志格式：时间 | 级别 | 模块 | 请求ID | 消息
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | rid=%(request_id)s | %(message)s"  # 日志格式
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # 日期格式



# 把请求ID注入日志记录
class RequestIdFilter(logging.Filter):
    """
    日志过滤器：给每条日志记录补上 request_id 字段
        格式串里的 %(request_id)s 就来自这里；
        没有请求上下文时取默认值 "-"，保证格式串永远不会因为缺字段而报错。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        为日志记录注入 request_id
            :param record: 日志记录
            :return: 恒为 True，表示放行该记录
        """
        record.request_id = request_id_var.get()  # 请求ID字段
        return True



# 配置全局日志
def setup_logging() -> None:
    """
    初始化全局日志（应用启动时调用一次，重复调用无副作用）
        行为：
            1. 级别取自 settings.LOG_LEVEL，非法值回落到 INFO；
            2. 控制台输出到 stdout（容器/PM2 友好），带 request_id；
            3. 配置了 settings.LOG_FILE 时，额外按天轮转写文件（保留 N 天）；
            4. 接管 uvicorn 的日志器：
               - uvicorn.error 走根日志器（统一格式）；
               - uvicorn.access 关闭，因为请求日志由 RequestContextMiddleware 输出（带 rid/耗时/用户）。
        :return: 无
    """
    level = getattr(logging, str(settings.LOG_LEVEL).upper(), logging.INFO)  # 日志级别

    root = logging.getLogger()  # 根日志器
    # 避免重复初始化（例如测试里多次 import main）
    if getattr(root, "_app_logging_configured", False):
        return
    root.setLevel(level)

    # 清掉已有 handler，避免与 uvicorn 默认 handler 叠加导致重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)  # 日志格式器
    request_filter = RequestIdFilter()  # 请求ID过滤器

    # 1.控制台输出
    console = logging.StreamHandler(sys.stdout)  # 控制台处理器
    console.setFormatter(formatter)
    console.addFilter(request_filter)
    root.addHandler(console)

    # 2.可选的文件输出（按天轮转，保留 LOG_RETENTION_DAYS 天）
    if settings.LOG_FILE:
        log_path = Path(settings.LOG_FILE)  # 日志文件路径
        if not log_path.is_absolute():
            # 相对路径按 backend 目录解析（app/core/logging.py 往上三层）
            log_path = Path(__file__).resolve().parents[2] / log_path  # 绝对路径
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(  # 文件轮转处理器
            str(log_path),                                  # 日志文件路径
            when="midnight",                                # 每天 0 点切分
            backupCount=settings.LOG_RETENTION_DAYS,        # 保留天数
            encoding="utf-8",                               # 中文日志用 UTF-8 写入
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(request_filter)
        root.addHandler(file_handler)

    # 3.接管 uvicorn 日志器
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)  # uvicorn日志器
        uvicorn_logger.handlers = []  # 清空处理器
        uvicorn_logger.propagate = True  # 向上传播

    # 请求日志由自己的中间件输出，关掉 uvicorn 自带的 access log（否则每条请求两行）
    access_logger = logging.getLogger("uvicorn.access")  # 访问日志器
    access_logger.handlers = []  # 清空处理器
    access_logger.propagate = False  # 不向上传播
    access_logger.setLevel(logging.WARNING)

    # 4.降噪：SQLAlchemy 引擎日志默认不输出（需要看 SQL 时临时调成 INFO）
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    root._app_logging_configured = True  # 已配置标记
    logging.getLogger(__name__).info("日志系统已初始化: level=%s file=%s", logging.getLevelName(level), settings.LOG_FILE or "-")



# 手机号脱敏
def mask_phone(phone: str | None) -> str:
    """
    手机号脱敏：保留前 3 位与后 4 位
        :param phone: 原始手机号，可为 None
        :return: 形如 138****0001；None/空串原样返回
    """
    # 1.空值兜底：没有手机号就返回空串
    if not phone:
        return ""
    # 2.号码过短（<=7 位）只保留首位，避免打码后仍能反推
    if len(phone) <= 7:
        return phone[0] + "****"
    # 3.正常号码：保留前 3 位与后 4 位
    return f"{phone[:3]}****{phone[-4:]}"



# 邮箱脱敏
def mask_email(email: str | None) -> str:
    """
    邮箱脱敏：保留首字符与域名
        :param email: 原始邮箱，可为 None
        :return: 形如 t***@example.com；None/无 @ 时原样返回
    """
    # 1.空值或没有 @ 的非法值原样返回（不强行打码，避免误伤）
    if not email or "@" not in email:
        return email or ""
    # 2.拆成"用户名@域名"，域名保留可读
    name, domain = email.split("@", 1)  # 用户名与域名
    # 3.用户名只有 1 位时只打一个 *，否则看不出是邮箱
    if len(name) <= 1:
        return f"*@{domain}"
    # 4.正常情况：首字符 + *** + @域名
    return f"{name[0]}***@{domain}"



# 令牌/密钥脱敏
def mask_secret(value: str | None, keep: int = 8) -> str:
    """
    令牌/密钥脱敏：只保留前若干位
        :param value: 原始字符串，可为 None
        :param keep: 保留的前缀长度
        :return: 形如 abcdef12...；过短的值整体打码
    """
    # 1.空值兜底
    if not value:
        return ""
    # 2.长度不超过保留位：整体打码，避免等于把原文打出来
    if len(value) <= keep:
        return "***"
    # 3.保留前 keep 位，其余用 ... 省略
    return f"{value[:keep]}..."
