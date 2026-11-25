"""
接口限流（滑动窗口 + 统一 429 响应；支持 Redis 共享计数与进程内回退）
    为什么需要：
        - 登录/注册是最容易被撞库、批量建号打穿的入口；
        - AI 预审触发每调一次都会产生 LLM 费用，必须限制单用户频率；
        - 附件上传会写磁盘，需要防止刷盘。
    实现方式：
        - 滑动窗口计数：每个 key 只保留窗口内的请求时间戳，过期即滑出；
        - 身份优先取 Bearer token 的 sub（按用户限流），解析失败退回客户端 IP；
        - 存储后端可切换（`RATE_LIMIT_BACKEND`）：
            · redis：多进程/多实例共享计数（ZSET + Lua 脚本，单次往返原子完成）；
            · memory：进程内存储，单实例部署或 Redis 不可用时使用；
            · auto（默认）：优先 Redis，连不上自动退回 memory，运行中 Redis 故障也会逐次回退；
        - 回退只降级不阻断：限流仍然生效，只是计数范围退化为单进程。
    响应约定：
        - 429 响应与全局错误结构一致：{"code","message","request_id"}，并带 Retry-After 头；
        - 探活、文档、CORS 预检（OPTIONS）不限流。
"""
import logging
import time
import uuid
from collections import deque
from threading import Lock

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.core.middleware import _client_ip
from app.core.security import decode_access_token



logger = logging.getLogger("app.rate_limit")   # 限流日志器

# 每清理多少次请求做一次过期桶回收（避免字典无限增长）
PURGE_EVERY = 512

# Redis key 前缀：与业务 key 隔离，便于运维批量清理
REDIS_KEY_PREFIX = "ratelimit:"

# Redis 连接/读写超时（秒）：限流不能把请求拖住，连不上必须快速失败
REDIS_TIMEOUT_SECONDS = 0.5

# 滑动窗口的 Lua 脚本：清理过期成员 → 计数 → 未超限则记录，整体原子执行
SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = math.ceil(((tonumber(oldest[2]) + window_ms) - now_ms) / 1000)
  if retry < 1 then retry = 1 end
  return {0, retry}
end

redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms)
return {1, 0}
"""



# 限流规则：(方法, 路径前缀, 作用域)
SCOPE_RULES = (
    ("POST", "/api/v1/auth/login", "login"),            # 登录
    ("POST", "/api/v1/auth/register", "register"),      # 注册
    ("POST", "/api/v1/agent/review/", "ai_trigger"),    # AI 预审触发（只限 POST）
    ("POST", "/api/v1/attachment/", "upload"),          # 附件上传/识别
)



# 作用域 → 每窗口上限
def _limit_for(scope: str) -> int:
    """
    取某个作用域的每窗口请求上限
        :param scope: 作用域名
        :return: 上限；0 表示不限
    """
    return {
        "login": settings.RATE_LIMIT_LOGIN_PER_WINDOW,          # 登录
        "register": settings.RATE_LIMIT_REGISTER_PER_WINDOW,    # 注册
        "ai_trigger": settings.RATE_LIMIT_AI_TRIGGER_PER_WINDOW,   # AI 触发
        "investigate": settings.RATE_LIMIT_INVESTIGATE_PER_WINDOW,  # AI 补充核查
        "upload": settings.RATE_LIMIT_UPLOAD_PER_WINDOW,        # 上传
    }.get(scope, settings.RATE_LIMIT_DEFAULT_PER_WINDOW)



# 解析请求所属作用域
def _resolve_scope(method: str, path: str) -> str:
    """
    按方法与路径前缀判断请求属于哪个限流作用域
        :param method: HTTP 方法
        :param path: 请求路径
        :return: 作用域名；未命中返回 default
    """
    # 1.逐条匹配：方法必须一致，路径按前缀匹配
    for rule_method, prefix, scope in SCOPE_RULES:
        if method == rule_method and path.startswith(prefix):
            return scope
    # 2.AI 补充核查是动态路径 /api/v1/expense/{id}/investigate，
    #   不能用 /api/v1/expense/ 前缀（会把创建/提交等普通接口一起限成 5 次）
    if method == "POST" and path.startswith("/api/v1/expense/") and path.rstrip("/").endswith("/investigate"):
        return "investigate"
    # 3.未命中任何专项规则：走默认限流
    return "default"



# 取请求身份（用户优先，其次 IP）
def _identity(request: Request) -> str:
    """
    生成限流 key 的身份部分
        :param request: 请求对象
        :return: 形如 user:12 或 ip:1.2.3.4
    """
    # 1.尝试从 Authorization: Bearer 解析用户ID
    header = request.headers.get("authorization", "")      # 授权头
    if header.lower().startswith("bearer "):
        payload = decode_access_token(header.split(" ", 1)[1].strip())   # 令牌载荷
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    # 2.解析失败（未登录/令牌过期）：退回客户端 IP
    return f"ip:{_client_ip(request)}"



# 滑动窗口限流器
class SlidingWindowLimiter:
    """
    进程内滑动窗口限流器
        每个 key 保存窗口内的时间戳队列；命中上限后返回建议重试秒数。
        :ivar _buckets: {key: 时间戳队列}
        :ivar _calls: 累计调用次数（用于定期回收过期桶）
        :ivar _lock: 线程锁（FastAPI 同步路由跑在线程池里）
    """

    def __init__(self) -> None:
        """初始化存储与锁"""
        self._buckets: dict[str, deque[float]] = {}      # 各 key 的时间戳队列
        self._calls = 0                                   # 调用计数
        self._lock = Lock()                               # 线程锁

    def hit(self, key: str, *, limit: int, window: int, now: float | None = None) -> tuple[bool, int]:
        """
        记录一次请求并判断是否超限
            :param key: 限流 key（作用域 + 身份）
            :param limit: 窗口内上限；<=0 表示不限
            :param window: 窗口长度（秒）
            :param now: 当前时间（测试注入用）；默认取 monotonic
            :return: (是否允许, 建议重试秒数)；允许时重试秒数为 0
        """
        # 1.不限流：直接放行
        if limit <= 0:
            return True, 0

        # 2.计算窗口边界
        current = time.monotonic() if now is None else now     # 当前时间
        cutoff = current - max(1, int(window))                 # 窗口左边界

        # 3.加锁：清理过期时间戳 → 判断是否超限 → 记录本次
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())     # 当前 key 的队列
            while bucket and bucket[0] <= cutoff:               # 滑出窗口外的记录
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = int(bucket[0] + window - current) + 1   # 最早一条过期后即可再试
                return False, max(1, retry_after)

            bucket.append(current)                              # 记录本次请求
            self._calls += 1                                    # 累计调用数
            if self._calls % PURGE_EVERY == 0:                  # 定期回收
                self._purge(cutoff)
            return True, 0

    def _purge(self, cutoff: float) -> None:
        """
        回收窗口外已经没有记录的 key，避免字典无限增长
            :param cutoff: 窗口左边界
            :return: 无
        """
        # 1.筛出仍有未过期记录的 key
        alive = {key: bucket for key, bucket in self._buckets.items() if bucket and bucket[-1] > cutoff}
        # 2.整体替换（调用方已持锁）
        self._buckets = alive

    def clear(self) -> None:
        """
        清空所有计数（测试用）
            :return: 无
        """
        with self._lock:
            self._buckets.clear()      # 清空队列
            self._calls = 0            # 重置计数



# Redis 共享计数限流器
class RedisSlidingWindowLimiter:
    """
    基于 Redis 的滑动窗口限流器（多实例 / 多 worker 共享计数）
        存储结构：每个 key 一个 ZSET，成员是"毫秒时间戳-uuid"，分数是毫秒时间戳；
        原子性：清理过期成员、计数、记录本次、设置过期，全部在同一个 Lua 脚本里完成，
                避免"查完再写"的并发窗口；
        回退：任何 Redis 异常都退回进程内限流器，只降级不阻断（限流依旧生效）。
        :ivar _client: redis 客户端
        :ivar _script: 已注册的 Lua 脚本
        :ivar _fallback: 进程内回退实现
        :ivar _degraded: 当前是否处于回退状态
    """

    def __init__(self, client, *, prefix: str = REDIS_KEY_PREFIX, fallback=None):
        """
        :param client: redis 客户端（需支持 register_script）
        :param prefix: key 前缀
        :param fallback: 回退用的进程内限流器
        """
        self._client = client                                      # Redis 客户端
        self._prefix = prefix                                      # key 前缀
        self._script = client.register_script(SLIDING_WINDOW_SCRIPT)   # 注册 Lua 脚本
        self._fallback = fallback or SlidingWindowLimiter()         # 回退实现
        self._degraded = False                                     # 是否已降级

    @property
    def degraded(self) -> bool:
        """
        当前是否处于回退状态（运维排障用）
            :return: True 表示已回退到进程内计数
        """
        return self._degraded

    def hit(self, key: str, *, limit: int, window: int, now: float | None = None) -> tuple[bool, int]:
        """
        记录一次请求并判断是否超限
            :param key: 限流 key（作用域 + 身份）
            :param limit: 窗口内上限；<=0 表示不限
            :param window: 窗口长度（秒）
            :param now: 当前时间（测试注入用）
            :return: (是否允许, 建议重试秒数)
        """
        # 1.不限流：直接放行，连 Redis 都不用碰
        if limit <= 0:
            return True, 0

        window_seconds = max(1, int(window))                       # 窗口秒数
        current = time.time() if now is None else now              # 当前时间（秒）
        now_ms = int(current * 1000)                               # 毫秒时间戳
        member = f"{now_ms}-{uuid.uuid4().hex}"                    # 同一毫秒的多次请求不能互相覆盖

        # 2.执行原子脚本；异常时降级到进程内，保证请求仍被限流
        try:
            allowed, retry_after = self._script(
                keys=[self._prefix + key],                         # 实际 Redis key
                args=[now_ms, window_seconds * 1000, int(limit), member],   # 脚本参数
            )
        except Exception as exc:                                   # noqa: BLE001 - 任何 Redis 异常都要降级
            self._mark_degraded(exc)
            return self._fallback.hit(key, limit=limit, window=window_seconds, now=now)

        # 3.成功：若之前处于降级状态，恢复并记一条日志
        if self._degraded:
            logger.info("Redis 限流已恢复")
            self._degraded = False
        return bool(int(allowed)), int(retry_after)

    def _mark_degraded(self, exc: Exception) -> None:
        """
        标记为降级状态（只在首次异常时告警，避免每请求刷日志）
            :param exc: 触发降级的异常
            :return: 无
        """
        if not self._degraded:
            logger.warning("Redis 限流不可用，回退进程内计数：%s: %s", type(exc).__name__, exc)
            self._degraded = True

    def clear(self) -> None:
        """
        清空回退实现的计数（测试用；不清理 Redis 里的 key）
            :return: 无
        """
        self._fallback.clear()



# 创建 Redis 客户端
def _create_redis_client():
    """
    按配置创建 Redis 客户端（延迟导入，未安装时返回 None）
        :return: redis 客户端或 None
    """
    try:
        import redis
    except ImportError:                                            # 未安装 redis 客户端
        logger.warning("未安装 redis 客户端，限流回退进程内计数")
        return None
    try:
        return redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,          # 建连超时，避免拖住请求
            socket_timeout=REDIS_TIMEOUT_SECONDS,                  # 读写超时
        )
    except Exception as exc:                                       # noqa: BLE001 - URL 非法等
        logger.warning("创建 Redis 客户端失败，限流回退进程内计数：%s", exc)
        return None



# 构建限流器
def build_limiter():
    """
    按 RATE_LIMIT_BACKEND 构建限流器
        - memory：强制进程内；
        - redis / auto：尝试 Redis，连不上（或未配置）回退进程内。
        :return: 限流器实例（两者接口一致）
    """
    backend = str(settings.RATE_LIMIT_BACKEND or "auto").lower()    # 配置的后端
    if backend == "memory":                                        # 显式要求进程内
        return SlidingWindowLimiter()

    client = _create_redis_client()                                # Redis 客户端
    if client is None:
        return SlidingWindowLimiter()

    try:
        client.ping()                                              # 探活，连不上就直接回退
    except Exception as exc:                                       # noqa: BLE001 - Redis 不可用
        logger.warning("限流后端连接 Redis 失败，回退进程内计数：%s", exc)
        return SlidingWindowLimiter()

    logger.info("限流后端使用 Redis 共享计数 url=%s", settings.REDIS_URL)
    return RedisSlidingWindowLimiter(client)



# 进程级限流器：首次使用时构建（避免导入期就连接 Redis）
_limiter = None



# 取全局限流器
def get_limiter():
    """
    获取进程级限流器（懒加载，测试可通过 reset_limiter 重建）
        :return: 限流器实例
    """
    global _limiter
    if _limiter is None:                                           # 首次使用才构建
        _limiter = build_limiter()
    return _limiter



# 重置全局限流器（测试用）
def reset_limiter(limiter=None) -> None:
    """
    重置全局限流器
        :param limiter: 指定实例；不传则下次使用重新构建
        :return: 无
    """
    global _limiter
    _limiter = limiter



# 限流中间件
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    接口限流中间件
        - 位于 RequestContextMiddleware 内层：429 也能带上 X-Request-ID 并写入访问日志；
        - 开关关闭、探活/文档路径、OPTIONS 预检一律放行。
    """

    async def dispatch(self, request: Request, call_next):
        """
        处理一次请求：判断作用域 → 计数 → 放行或返回 429
            :param request: 请求对象
            :param call_next: 下游处理链
            :return: 下游响应或 429
        """
        # 1.开关/探活/预检：直接放行
        if (
            not settings.RATE_LIMIT_ENABLED
            or request.method == "OPTIONS"
            or request.url.path in set(settings.RATE_LIMIT_EXEMPT_PATHS)
        ):
            return await call_next(request)

        # 2.解析作用域、上限与身份
        scope = _resolve_scope(request.method, request.url.path)     # 作用域
        limit = _limit_for(scope)                                    # 每窗口上限
        key = f"{scope}:{_identity(request)}"                        # 限流 key

        # 3.计数并判断（Redis 不可用时内部已回退进程内）
        allowed, retry_after = get_limiter().hit(
            key,
            limit=limit,
            window=max(1, int(settings.RATE_LIMIT_WINDOW_SECONDS)),
        )
        if allowed:
            return await call_next(request)

        # 4.超限：返回统一的 429 结构（request_id 由外层中间件提前写入）
        request_id = getattr(request.state, "request_id", "") or ""   # 请求ID
        logger.warning(
            "触发限流 scope=%s key=%s path=%s retry_after=%ss",
            scope, key, request.url.path, retry_after,
        )
        return JSONResponse(
            status_code=429,
            content={
                "code": "TOO_MANY_REQUESTS",
                "message": f"请求过于频繁，请 {retry_after} 秒后再试",
                "request_id": request_id,
            },
            headers={"Retry-After": str(retry_after)},
        )
