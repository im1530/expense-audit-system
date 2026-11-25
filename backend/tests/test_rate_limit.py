"""
接口限流测试（离线，不联网、不查库）
    覆盖：
        - 滑动窗口：到上限拒绝、窗口滑动后恢复、limit=0 不限；
        - 中间件：登录超限返回 429（统一错误结构 + Retry-After）；
        - 探活/文档路径不限流；
        - 身份区分：按 IP、按登录用户分别计数；
        - 只限 POST 触发，GET 查询不受 AI 触发配额影响。
"""
import asyncio
import math
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

import tests  # noqa: F401  先设置测试环境变量
from app.config import settings
from app.core.rate_limit import (
    RateLimitMiddleware,
    RedisSlidingWindowLimiter,
    SlidingWindowLimiter,
    _resolve_scope,
    build_limiter,
    get_limiter,
    reset_limiter,
)
from app.core.security import create_access_token
from tests.support import make_request



class SlidingWindowLimiterTest(unittest.TestCase):
    """限流器本身"""

    def test_limit_then_recover(self):
        """达到上限拒绝，窗口滑过后恢复"""
        limiter = SlidingWindowLimiter()
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=0)[0])
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=1)[0])

        allowed, retry_after = limiter.hit("k", limit=2, window=10, now=2)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

        self.assertTrue(limiter.hit("k", limit=2, window=10, now=11)[0])

    def test_zero_limit_means_unlimited(self):
        """limit=0 表示不限流"""
        limiter = SlidingWindowLimiter()
        for _ in range(50):
            self.assertTrue(limiter.hit("k", limit=0, window=10)[0])

    def test_keys_are_isolated(self):
        """不同 key 互不影响"""
        limiter = SlidingWindowLimiter()
        self.assertTrue(limiter.hit("a", limit=1, window=10)[0])
        self.assertFalse(limiter.hit("a", limit=1, window=10)[0])
        self.assertTrue(limiter.hit("b", limit=1, window=10)[0])



class RateLimitMiddlewareTest(unittest.TestCase):
    """限流中间件"""

    def setUp(self):
        """每个用例重置计数器（默认进程内后端，见 tests/__init__ 的 RATE_LIMIT_BACKEND）"""
        reset_limiter(SlidingWindowLimiter())                      # 固定用进程内实现，避免读到环境里的 Redis
        self.app = Starlette()

    def dispatch(self, path: str, *, method: str = "POST", headers: list | None = None):
        """
        直接调用中间件 dispatch
            :param path: 请求路径
            :param method: 请求方法
            :param headers: 请求头
            :return: 响应对象
        """
        request = make_request(path, headers=headers, method=method)
        middleware = RateLimitMiddleware(app=self.app)

        async def call_next(_request):
            """模拟下游成功响应"""
            return PlainTextResponse("ok")

        return asyncio.run(middleware.dispatch(request, call_next))

    def test_login_rate_limited(self):
        """登录超过配额：返回 429 与统一错误结构"""
        with patch.object(settings, "RATE_LIMIT_LOGIN_PER_WINDOW", 2):
            self.assertEqual(self.dispatch("/api/v1/auth/login").status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/auth/login").status_code, 200)
            response = self.dispatch("/api/v1/auth/login")

        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        payload = response.body.decode("utf-8")
        self.assertIn("TOO_MANY_REQUESTS", payload)
        self.assertIn("request_id", payload)

    def test_investigate_scope_not_default(self):
        """AI 核查走独立限流，但普通报销 POST 不能被误伤"""
        self.assertEqual(
            _resolve_scope("POST", "/api/v1/expense/12/investigate"),
            "investigate",
        )
        self.assertEqual(
            _resolve_scope("POST", "/api/v1/expense/"),
            "default",
        )

    def test_exempt_path_not_limited(self):
        """探活路径不限流"""
        with patch.object(settings, "RATE_LIMIT_LOGIN_PER_WINDOW", 1):
            for _ in range(5):
                self.assertEqual(self.dispatch("/health", method="GET").status_code, 200)

    def test_ip_identity_separated(self):
        """不同来源 IP 分别计数"""
        headers_a = [(b"host", b"testserver"), (b"x-forwarded-for", b"1.1.1.1")]
        headers_b = [(b"host", b"testserver"), (b"x-forwarded-for", b"2.2.2.2")]
        with patch.object(settings, "RATE_LIMIT_LOGIN_PER_WINDOW", 1):
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_a).status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_b).status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_a).status_code, 429)

    def test_user_identity_separated(self):
        """已登录用户按 sub 计数，不同用户互不影响"""
        token_a = create_access_token({"sub": "101"})
        token_b = create_access_token({"sub": "202"})
        headers_a = [(b"host", b"testserver"), (b"authorization", f"Bearer {token_a}".encode())]
        headers_b = [(b"host", b"testserver"), (b"authorization", f"Bearer {token_b}".encode())]
        with patch.object(settings, "RATE_LIMIT_LOGIN_PER_WINDOW", 1):
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_a).status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_b).status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/auth/login", headers=headers_a).status_code, 429)

    def test_ai_trigger_only_post(self):
        """AI 触发只限 POST；GET 查询走默认配额"""
        with patch.object(settings, "RATE_LIMIT_AI_TRIGGER_PER_WINDOW", 1), \
                patch.object(settings, "RATE_LIMIT_DEFAULT_PER_WINDOW", 100):
            self.assertEqual(self.dispatch("/api/v1/agent/review/1").status_code, 200)
            self.assertEqual(self.dispatch("/api/v1/agent/review/1").status_code, 429)
            for _ in range(3):
                self.assertEqual(
                    self.dispatch("/api/v1/agent/review/1", method="GET").status_code,
                    200,
                )

    def test_disabled_switch_passes_through(self):
        """总开关关闭：完全不限流"""
        with patch.object(settings, "RATE_LIMIT_ENABLED", False), \
                patch.object(settings, "RATE_LIMIT_LOGIN_PER_WINDOW", 1):
            for _ in range(5):
                self.assertEqual(self.dispatch("/api/v1/auth/login").status_code, 200)



class FakeRedis:
    """
    模拟 Redis 的 ZSET + Lua 脚本（离线单测用，不连真实 Redis）
        只实现 SLIDING_WINDOW_SCRIPT 用到的那几个语义：清理过期 → 计数 → 记录。
    """

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, int]] = {}     # key -> {member: score}
        self.fail = False                              # 置 True 模拟 Redis 故障
        self.calls = 0                                 # 脚本调用次数

    def ping(self) -> bool:
        """探活"""
        if self.fail:
            raise ConnectionError("redis down")
        return True

    def register_script(self, _script: str):
        """
        返回可调用的"脚本对象"
            :param _script: Lua 脚本（这里不需要解析，语义用 Python 模拟）
            :return: callable(keys, args)
        """
        def run(*, keys: list[str], args: list) -> list[int]:
            """模拟脚本执行"""
            self.calls += 1
            if self.fail:                              # 故障：让调用方走降级
                raise ConnectionError("redis down")
            key = keys[0]                              # 实际 key
            now_ms, window_ms, limit = int(args[0]), int(args[1]), int(args[2])   # 参数
            member = args[3]                           # 成员（毫秒时间戳-uuid）
            bucket = self.zsets.setdefault(key, {})    # 当前窗口
            for old_member, score in list(bucket.items()):     # 清理过期成员
                if score <= now_ms - window_ms:
                    del bucket[old_member]
            if len(bucket) >= limit:                   # 超限：给出建议重试秒数
                oldest = min(bucket.values())
                retry = max(1, math.ceil((oldest + window_ms - now_ms) / 1000))
                return [0, retry]
            bucket[member] = now_ms                    # 记录本次
            return [1, 0]

        return run



class RedisLimiterTest(unittest.TestCase):
    """Redis 共享计数限流器"""

    def test_counts_across_instances(self):
        """两个实例共享同一 Redis：计数合并（这正是多实例要解决的问题）"""
        client = FakeRedis()
        first = RedisSlidingWindowLimiter(client)      # 实例 A
        second = RedisSlidingWindowLimiter(client)     # 实例 B
        self.assertTrue(first.hit("login:ip:1.1.1.1", limit=2, window=60, now=100.0)[0])
        self.assertTrue(second.hit("login:ip:1.1.1.1", limit=2, window=60, now=101.0)[0])
        allowed, retry_after = first.hit("login:ip:1.1.1.1", limit=2, window=60, now=102.0)
        self.assertFalse(allowed)                      # A 能看到 B 的计数
        self.assertGreaterEqual(retry_after, 1)

    def test_window_slides(self):
        """窗口滑过后恢复"""
        client = FakeRedis()
        limiter = RedisSlidingWindowLimiter(client)
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=0)[0])
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=1)[0])
        self.assertFalse(limiter.hit("k", limit=2, window=10, now=2)[0])
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=11)[0])

    def test_same_millisecond_not_overwritten(self):
        """同一毫秒的多次请求不能被 ZSET 成员覆盖"""
        client = FakeRedis()
        limiter = RedisSlidingWindowLimiter(client)
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=5)[0])
        self.assertTrue(limiter.hit("k", limit=2, window=10, now=5)[0])
        self.assertFalse(limiter.hit("k", limit=2, window=10, now=5)[0])

    def test_zero_limit_skips_redis(self):
        """limit=0 直接放行，不访问 Redis"""
        client = FakeRedis()
        limiter = RedisSlidingWindowLimiter(client)
        self.assertTrue(limiter.hit("k", limit=0, window=10)[0])
        self.assertEqual(client.calls, 0)

    def test_fallback_on_redis_error(self):
        """Redis 故障时回退进程内，限流仍然生效并标记降级"""
        client = FakeRedis()
        client.fail = True
        limiter = RedisSlidingWindowLimiter(client)
        self.assertTrue(limiter.hit("k", limit=1, window=10)[0])   # 首次放行（回退实现计数）
        allowed, _ = limiter.hit("k", limit=1, window=10)          # 第二次应被回退实现拦截
        self.assertFalse(allowed)
        self.assertTrue(limiter.degraded)

    def test_recover_after_redis_back(self):
        """Redis 恢复后自动回到共享计数"""
        client = FakeRedis()
        client.fail = True
        limiter = RedisSlidingWindowLimiter(client)
        limiter.hit("k", limit=5, window=10)           # 触发降级
        client.fail = False
        limiter.hit("k", limit=5, window=10)           # 成功一次
        self.assertFalse(limiter.degraded)



class LimiterFactoryTest(unittest.TestCase):
    """后端选择与回退"""

    def test_memory_backend(self):
        """显式 memory：不创建 Redis 客户端"""
        with patch.object(settings, "RATE_LIMIT_BACKEND", "memory"):
            self.assertIsInstance(build_limiter(), SlidingWindowLimiter)

    def test_redis_backend(self):
        """Redis 可用：使用共享计数实现"""
        client = FakeRedis()
        with patch.object(settings, "RATE_LIMIT_BACKEND", "redis"), \
                patch("app.core.rate_limit._create_redis_client", return_value=client):
            self.assertIsInstance(build_limiter(), RedisSlidingWindowLimiter)

    def test_fallback_when_ping_fails(self):
        """Redis 探活失败：回退进程内"""
        client = FakeRedis()
        client.fail = True
        with patch.object(settings, "RATE_LIMIT_BACKEND", "auto"), \
                patch("app.core.rate_limit._create_redis_client", return_value=client):
            self.assertIsInstance(build_limiter(), SlidingWindowLimiter)

    def test_fallback_when_client_missing(self):
        """未安装/无法创建客户端：回退进程内"""
        with patch.object(settings, "RATE_LIMIT_BACKEND", "auto"), \
                patch("app.core.rate_limit._create_redis_client", return_value=None):
            self.assertIsInstance(build_limiter(), SlidingWindowLimiter)



if __name__ == "__main__":
    unittest.main()
