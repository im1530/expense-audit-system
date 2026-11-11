"""
LLM 客户端（DeepSeek，OpenAI 兼容协议）
    为什么不用 openai / httpx SDK：
        当前运行环境无法稳定安装第三方包，而这里只需要"一个 HTTPS POST + JSON"，
        用标准库 urllib 实现即可，避免为了一个请求引入新依赖和版本冲突。
        异步方面用 asyncio.to_thread 把阻塞 IO 放到线程池，不阻塞 FastAPI 事件循环。
    能力边界：
        - 只负责"发请求、拿文本、解析 JSON、统计耗时/ token"；
        - 不做业务判断、不写数据库、不拼接业务 Prompt（这些在 agent / service 层）；
        - 不记录 API Key，也不把用户单据内容写进日志。
    错误分类（供上层决定是否降级）：
        - LLMConfigError：未配置 Key / 配置非法（不可重试，直接降级）；
        - LLMTimeoutError：超时（可重试）；
        - LLMHTTPError：HTTP 4xx/5xx（429/5xx 可重试，401/400 不可重试）；
        - LLMResponseError：返回体结构不对 / JSON 非法（可重试一次）；
        - LLMError：其余网络错误（可重试）。
"""
import asyncio
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import settings



logger = logging.getLogger(__name__)

# 响应体上限：防止异常返回把内存打爆（正常 JSON 远小于 1MB）
MAX_RESPONSE_BYTES = 1024 * 1024

# 可重试的 HTTP 状态码：限流、超时、服务端错误
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}



# 业务异常基类
class LLMError(Exception):
    """
    LLM 调用异常基类
        :ivar retryable: 是否值得重试
        :ivar status_code: HTTP 状态码（网络异常时为 None）
    """
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        """
        :param message: 错误说明（不包含 Key / 单据内容）
        :param retryable: 是否可重试
        :param status_code: HTTP 状态码
        """
        super().__init__(message)
        self.message = message          # 错误说明
        self.retryable = retryable      # 是否可重试
        self.status_code = status_code  # HTTP 状态码



class LLMConfigError(LLMError):
    """未配置 Key 或配置非法（不可重试）"""



class LLMTimeoutError(LLMError):
    """调用超时（可重试）"""



class LLMHTTPError(LLMError):
    """HTTP 层错误（按状态码决定是否可重试）"""



class LLMResponseError(LLMError):
    """响应结构/JSON 非法（可重试）"""



# 一次成功调用的结果
@dataclass(frozen=True)
class LLMResult:
    """
    LLM 调用结果（只保留业务需要的字段）
        :ivar data: 解析后的 JSON 对象
        :ivar content: 模型返回的原始文本
        :ivar model: 实际使用的模型名
        :ivar prompt_tokens: 输入 token 数（提供方未返回时为 0）
        :ivar completion_tokens: 输出 token 数（提供方未返回时为 0）
        :ivar latency_ms: 本次调用耗时（毫秒）
        :ivar raw: 完整响应体（排障用）
    """
    data: dict[str, Any]       # 解析后的 JSON
    content: str               # 原始文本
    model: str                 # 模型名
    prompt_tokens: int         # 输入 token
    completion_tokens: int     # 输出 token
    latency_ms: int            # 耗时（毫秒）
    raw: dict[str, Any]        # 完整响应体



# 从模型输出中提取 JSON 对象
def parse_json_object(text: str) -> dict:
    """
    把模型返回的文本解析成 JSON 对象（容忍 ```json 代码块与前后说明文字）
        解析顺序：
            1. 原样解析；
            2. 去掉 Markdown 代码围栏再解析；
            3. 截取第一个 { 到最后一个 } 再解析；
            4. 仍失败则抛 LLMResponseError（由上层重试或降级）。
        :param text: 模型返回文本
        :return: 解析后的字典
        :raises LLMResponseError: 不是合法 JSON 对象
    """
    raw = (text or "").strip()                          # 去首尾空白
    if not raw:
        raise LLMResponseError("模型返回为空", retryable=True)

    # 1.先尝试原样解析
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # 2.去掉 ```json ... ``` 围栏
    fenced = raw
    if fenced.startswith("```"):
        lines = fenced.splitlines()
        if lines:
            lines = lines[1:]                            # 去掉首行 ```json
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]                          # 去掉末行 ```
        fenced = "\n".join(lines).strip()
    try:
        value = json.loads(fenced)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    # 3.截取第一个 { 到最后一个 }（模型偶尔会在 JSON 前后加解释）
    start = fenced.find("{")
    end = fenced.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(fenced[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    raise LLMResponseError("模型返回不是合法 JSON 对象", retryable=True)



# DeepSeek 客户端
class LLMClient:
    """
    DeepSeek Chat Completions 客户端（OpenAI 兼容）
        默认从 settings 读取配置，也允许构造函数覆盖（测试用）。
        线程模型：chat_json 是 async，内部用 asyncio.to_thread 调阻塞的 _post，
        避免在事件循环里做网络 IO。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,             # 覆盖 API Key
        base_url: str | None = None,            # 覆盖接口地址
        model: str | None = None,               # 覆盖模型名
        timeout: float | None = None,           # 覆盖超时（秒）
        max_retries: int | None = None,         # 覆盖重试次数
        temperature: float | None = None,       # 覆盖采样温度
        max_tokens: int | None = None,          # 覆盖输出上限
        retry_backoff: float = 0.5,             # 重试退避基数（秒）
        disable_system_proxy: bool | None = None,  # 是否绕过系统代理
    ):
        """
        :param api_key: API Key；不传则读 settings
        :param base_url: 接口地址；不传则读 settings
        :param model: 模型名；不传则读 settings
        :param timeout: 单次请求超时（秒）
        :param max_retries: 失败重试次数（不含首次）
        :param temperature: 采样温度
        :param max_tokens: 输出 token 上限
        :param retry_backoff: 重试退避基数，第 n 次重试等待 backoff * 2^(n-1)
        :param disable_system_proxy: 是否绕过系统代理；None 表示读配置
        """
        self.api_key = (api_key if api_key is not None else settings.DEEPSEEK_API_KEY.get_secret_value()).strip()  # 密钥
        self.base_url = (base_url or settings.DEEPSEEK_BASE_URL).rstrip("/")                                      # 接口地址
        self.model = model or settings.DEEPSEEK_MODEL                                                             # 模型名
        self.timeout = float(timeout if timeout is not None else settings.AI_REVIEW_TIMEOUT_SECONDS)              # 超时
        self.max_retries = int(max_retries if max_retries is not None else settings.AI_REVIEW_MAX_RETRIES)        # 重试次数
        self.temperature = float(temperature if temperature is not None else settings.AI_REVIEW_TEMPERATURE)      # 温度
        self.max_tokens = int(max_tokens if max_tokens is not None else settings.AI_REVIEW_MAX_TOKENS)            # 输出上限
        self.retry_backoff = float(retry_backoff)                                                                 # 退避基数
        self.disable_system_proxy = bool(                                                                         # 是否绕过系统代理
            settings.DEEPSEEK_DISABLE_SYSTEM_PROXY
            if disable_system_proxy is None
            else disable_system_proxy
        )

    # 构造请求器（按需绕过系统代理）
    def _build_opener(self) -> urllib.request.OpenerDirector:
        """
        构造 urllib 请求器
            为什么每次新建：OpenerDirector 的 handler 有状态，跨线程复用不安全；
            单次预审只有一次请求，重建开销可以忽略。
            :return: OpenerDirector
        """
        if self.disable_system_proxy:
            # 空 ProxyHandler：不读取系统/环境变量里的代理
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    # 发起一次 HTTP 请求（阻塞，供线程池调用）
    def _post(self, payload: dict) -> tuple[dict, int]:
        """
        发一次 Chat Completions 请求
            :param payload: 请求体
            :return: (响应 JSON, HTTP 状态码)
            :raises LLMConfigError: 未配置 Key
            :raises LLMTimeoutError: 超时
            :raises LLMHTTPError: HTTP 错误
            :raises LLMResponseError: 响应不是合法 JSON
            :raises LLMError: 其他网络错误
        """
        # 1.没有 Key 直接拒绝，避免发出必然 401 的请求
        if not self.api_key:
            raise LLMConfigError("未配置 DEEPSEEK_API_KEY，无法调用 LLM")

        # 2.组装请求
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )

        # 3.发送并读取（限制响应体大小；按配置绕过系统代理）
        try:
            opener = self._build_opener()
            with opener.open(request, timeout=self.timeout) as response:
                status_code = response.status
                raw_bytes = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise LLMHTTPError(
                f"LLM 返回 HTTP {exc.code}: {detail}",
                retryable=exc.code in RETRYABLE_STATUS,
                status_code=exc.code,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMTimeoutError("LLM 调用超时", retryable=True) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise LLMError(f"LLM 网络请求失败: {reason}", retryable=True) from exc

        # 4.响应体过大直接拒绝
        if len(raw_bytes) > MAX_RESPONSE_BYTES:
            raise LLMResponseError("LLM 响应体过大", retryable=False)

        # 5.解析响应 JSON
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseError("LLM 响应不是合法 JSON", retryable=True) from exc
        if not isinstance(data, dict):
            raise LLMResponseError("LLM 响应结构不是对象", retryable=True)
        return data, status_code

    # 对外方法：要求模型输出 JSON 并解析（网络错误与解析错误共用重试预算）
    async def chat_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        """
        调用纯文本模型并返回解析后的 JSON 结果
            重试策略：网络超时、429/5xx、响应结构/JSON 非法都算可重试；
            401/400 等配置或参数错误不重试，直接抛给上层降级。
            :param system_prompt: 系统提示词（角色、规则、输出约束）
            :param user_prompt: 用户提示词（单据上下文）
            :return: LLMResult
            :raises LLMConfigError: 未配置 Key
            :raises LLMTimeoutError/LLMHTTPError/LLMResponseError: 调用失败
        """
        # 1.组装请求体（不开启 stream，便于一次性拿到 usage）
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        # 结构化 JSON 预审关闭思考模式，避免 reasoning 吃掉输出预算导致 JSON 截断
        if settings.AI_REVIEW_DISABLE_THINKING:
            payload["thinking"] = {"type": "disabled"}

        # 2.交给统一重试执行器
        return await self._request_json(payload)

    # 对外方法：多模态图片 + JSON（Phase C 凭证识别）
    async def chat_image_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_data_urls: list[str],
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMResult:
        """
        调用多模态模型并返回解析后的 JSON 结果
            凭证识别是结构化提取任务，必须关闭思考模式：
            否则模型会把输出预算花在 reasoning_content 上，content 可能为空。
            :param system_prompt: 系统提示词
            :param user_prompt: 用户提示词
            :param image_data_urls: 图片 data URI 列表（1~N 张）
            :param max_tokens: 输出 token 上限；不传用客户端默认值
            :param temperature: 采样温度，识别任务建议 0
            :return: LLMResult
            :raises LLMConfigError: 没有图片或未配置 Key
            :raises LLMTimeoutError/LLMHTTPError/LLMResponseError: 调用失败
        """
        # 1.没有图片直接拒绝，避免发出无意义的请求
        if not image_data_urls:
            raise LLMConfigError("没有可识别的图片")

        # 2.组装多模态消息：文本在前，图片在后
        content: list[dict] = [{"type": "text", "text": user_prompt}]   # 消息内容块
        for data_url in image_data_urls:
            content.append({
                "type": "image_url",                                    # 图片块
                "image_url": {"url": data_url},                         # data URI
            })

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},           # 系统提示
                {"role": "user", "content": content},                   # 文本 + 图片
            ],
            "temperature": float(temperature),                          # 识别任务用 0
            "max_tokens": int(max_tokens if max_tokens is not None else self.max_tokens),   # 输出上限
            "stream": False,
            "thinking": {"type": "disabled"},                           # 关键：关闭思考模式
        }

        # 3.交给统一重试执行器
        return await self._request_json(payload)

    # 统一执行器：发送请求、重试、校验 choices、解析 JSON
    async def _request_json(self, payload: dict) -> LLMResult:
        """
        执行一次带重试的 JSON 请求
            :param payload: 完整请求体
            :return: LLMResult
            :raises LLMError: 调用失败
        """
        last_error: LLMError | None = None

        # 1.统一重试循环：HTTP 错误、超时、JSON 非法都走这里
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                data, _status = await asyncio.to_thread(self._post, payload)

                # 2.取第一条 choice 的文本
                choices = data.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise LLMResponseError("LLM 响应缺少 choices", retryable=True)
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise LLMResponseError("LLM 响应缺少 content", retryable=True)

                # 3.解析 JSON（非法时抛可重试异常，进入下一轮）
                parsed = parse_json_object(content)

                # 4.读取 token 用量（提供方可能不返回，缺省为 0）
                usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                latency_ms = int((time.perf_counter() - started) * 1000)

                # 5.记录日志（只记录模型/耗时/用量，不记录图片或单据内容）
                logger.info(
                    "LLM 调用完成 model=%s latency=%sms prompt_tokens=%s completion_tokens=%s",
                    self.model, latency_ms, prompt_tokens, completion_tokens,
                )

                return LLMResult(
                    data=parsed,
                    content=content,
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                    raw=data,
                )
            except LLMError as exc:
                last_error = exc
                # 不可重试，或已经是最后一次，直接抛出
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                wait_seconds = min(self.retry_backoff * (2 ** attempt), 2.0)
                logger.warning(
                    "LLM 调用失败将重试 attempt=%s/%s error=%s wait=%.1fs",
                    attempt + 1, self.max_retries, exc.message, wait_seconds,
                )
                await asyncio.sleep(wait_seconds)

        # 6.理论上不会到这里，兜底抛出最后一次错误
        raise last_error or LLMError("LLM 调用失败")



# 按配置创建客户端（业务层统一入口，便于测试替换）
def build_llm_client() -> LLMClient:
    """
    按 settings 创建 LLM 客户端
        :return: LLMClient 实例
    """
    return LLMClient()
