"""
LLM 客户端单元测试（不联网、不依赖真实 Key）
    覆盖：
        - JSON 提取：纯 JSON、Markdown 代码块、JSON 前后夹说明文字、非法内容；
        - 配置缺失：没有 Key 时抛 LLMConfigError，且不发起请求；
        - 重试策略：超时重试后成功、401 不重试、非法 JSON 重试耗尽；
        - 结果字段：模型名、token 用量、耗时、解析后的 data。
"""
import unittest
import urllib.request
from unittest.mock import patch

import tests  # noqa: F401  必须先执行 tests/__init__，把 DATABASE_URL 指向测试库
from app.services.llm_client import (
    LLMClient,
    LLMConfigError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
    parse_json_object,
)



class ParseJsonTest(unittest.TestCase):
    """模型输出 → JSON 对象"""

    def test_plain_json(self):
        """纯 JSON 直接解析"""
        value = parse_json_object('{"risk_score": 72, "risk_level": "high"}')
        self.assertEqual(value["risk_level"], "high")

    def test_fenced_json(self):
        """```json 代码块能去掉围栏"""
        text = '```json\n{"risk_score": 10}\n```'
        self.assertEqual(parse_json_object(text)["risk_score"], 10)

    def test_json_with_surrounding_text(self):
        """JSON 前后有解释文字时截取第一个 { 到最后一个 }"""
        text = '结果如下：{"risk_score": 40} 请复核。'
        self.assertEqual(parse_json_object(text)["risk_score"], 40)

    def test_invalid_text_raises(self):
        """完全不是 JSON 时抛可重试异常"""
        with self.assertRaises(LLMResponseError) as ctx:
            parse_json_object("我不知道")
        self.assertTrue(ctx.exception.retryable)

    def test_non_object_json_raises(self):
        """合法 JSON 但不是对象（如数组）也拒绝"""
        with self.assertRaises(LLMResponseError):
            parse_json_object("[1, 2, 3]")



# 构造一条合法响应
def fake_response(content: str) -> dict:
    """
    构造 OpenAI 兼容的响应体
        :param content: message.content 文本
        :return: 响应字典
    """
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }



class LLMClientTest(unittest.IsolatedAsyncioTestCase):
    """客户端重试与结果解析"""

    def make_client(self) -> LLMClient:
        """
        构造测试客户端（Key 填假值，禁止联网时也会走 mock）
            :return: LLMClient
        """
        return LLMClient(api_key="sk-test", max_retries=1, retry_backoff=0)

    async def test_missing_key_raises_config_error(self):
        """未配置 Key：直接抛 LLMConfigError，不发起请求"""
        client = LLMClient(api_key="", max_retries=0)
        with self.assertRaises(LLMConfigError):
            await client.chat_json(system_prompt="s", user_prompt="u")

    async def test_timeout_then_success(self):
        """第一次超时、第二次成功：应重试并返回结果"""
        client = self.make_client()
        calls = []

        def fake_post(payload):
            """
            模拟第一次超时、第二次成功
                :param payload: 请求体
                :return: (响应体, 状态码)
            """
            calls.append(payload)
            if len(calls) == 1:
                raise LLMTimeoutError("模拟超时", retryable=True)
            return fake_response('{"risk_score": 72, "risk_level": "high"}'), 200

        client._post = fake_post      # type: ignore[method-assign]
        result = await client.chat_json(system_prompt="s", user_prompt="u")

        self.assertEqual(len(calls), 2)
        self.assertEqual(result.data["risk_level"], "high")
        self.assertEqual(result.prompt_tokens, 100)
        self.assertEqual(result.completion_tokens, 20)
        self.assertEqual(result.model, client.model)

    async def test_401_not_retried(self):
        """401 不可重试：只调用一次就抛出"""
        client = self.make_client()
        calls = []

        def fake_post(payload):
            """
            模拟鉴权失败
                :param payload: 请求体
            """
            calls.append(payload)
            raise LLMHTTPError("401", retryable=False, status_code=401)

        client._post = fake_post      # type: ignore[method-assign]
        with self.assertRaises(LLMHTTPError):
            await client.chat_json(system_prompt="s", user_prompt="u")
        self.assertEqual(len(calls), 1)

    async def test_invalid_json_retries_then_raises(self):
        """模型返回非 JSON：重试一次后仍失败则抛出"""
        client = self.make_client()
        calls = []

        def fake_post(payload):
            """
            模拟模型返回纯文本
                :param payload: 请求体
                :return: (响应体, 状态码)
            """
            calls.append(payload)
            return fake_response("这不是 JSON"), 200

        client._post = fake_post      # type: ignore[method-assign]
        with self.assertRaises(LLMResponseError):
            await client.chat_json(system_prompt="s", user_prompt="u")
        self.assertEqual(len(calls), 2)

    async def test_payload_contains_expected_fields(self):
        """请求体包含模型、双消息、温度与输出上限"""
        client = self.make_client()
        captured = {}

        def fake_post(payload):
            """
            捕获请求体并返回合法结果
                :param payload: 请求体
                :return: (响应体, 状态码)
            """
            captured.update(payload)
            return fake_response('{"risk_score": 1}'), 200

        client._post = fake_post      # type: ignore[method-assign]
        await client.chat_json(system_prompt="系统", user_prompt="用户")

        self.assertEqual(captured["model"], client.model)
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertEqual(captured["messages"][1]["content"], "用户")
        self.assertFalse(captured["stream"])
        self.assertIn("temperature", captured)
        self.assertIn("max_tokens", captured)

    async def test_image_payload_disables_thinking(self):
        """多模态请求：图片块 + 关闭思考模式"""
        client = self.make_client()
        captured = {}

        def fake_post(payload):
            """
            捕获多模态请求体
                :param payload: 请求体
                :return: (响应体, 状态码)
            """
            captured.update(payload)
            return fake_response('{"document_type": "payment_screenshot"}'), 200

        client._post = fake_post      # type: ignore[method-assign]
        result = await client.chat_image_json(
            system_prompt="系统",
            user_prompt="用户",
            image_data_urls=["data:image/jpeg;base64,abc"],
            max_tokens=100,
        )

        self.assertEqual(result.data["document_type"], "payment_screenshot")
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(captured["max_tokens"], 100)
        self.assertEqual(captured["messages"][1]["content"][0]["type"], "text")
        self.assertEqual(captured["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(captured["messages"][1]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,abc")

    def test_opener_bypasses_proxy_when_enabled(self):
        """disable_system_proxy=True：构造空 ProxyHandler"""
        client = LLMClient(api_key="sk-test", disable_system_proxy=True)
        with patch("urllib.request.build_opener") as mocked:
            client._build_opener()
            args = mocked.call_args[0]
        self.assertIsInstance(args[0], urllib.request.ProxyHandler)

    def test_opener_uses_proxy_when_disabled(self):
        """disable_system_proxy=False：不传 ProxyHandler，走系统代理"""
        client = LLMClient(api_key="sk-test", disable_system_proxy=False)
        with patch("urllib.request.build_opener") as mocked:
            client._build_opener()
            args = mocked.call_args[0]
        self.assertEqual(args, ())


if __name__ == "__main__":
    unittest.main()
