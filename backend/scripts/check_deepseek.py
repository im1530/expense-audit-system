"""
DeepSeek API Key 连通性检查脚本（临时排障用，不参与业务）
    用途：
        1. 确认 key 是否被正确读取（长度、前缀，不打印完整 key）；
        2. 请求 /models 接口，打印 HTTP 状态与响应体；
        3. 把网络/TLS/鉴权错误明确打印出来，避免"什么都没返回"。
    用法（在 backend 目录下）：
        python scripts/check_deepseek.py
        或先设置环境变量：
        $env:DEEPSEEK_API_KEY = "sk-..."
        python scripts/check_deepseek.py
    说明：
        - 不依赖第三方库，只用标准库 urllib；
        - 不会把 key 写入文件或日志；
        - /models 若返回 404，说明该接口不可用，可改用 --chat 测对话接口。
"""
import json
import os
import sys
import urllib.error
import urllib.request


# 1.读取配置：默认优先环境变量；带 --prompt 时忽略环境变量，强制手动粘贴
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
FORCE_PROMPT = "--prompt" in sys.argv

API_KEY = ""
KEY_SOURCE = "手动输入"
if not FORCE_PROMPT:
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip().strip('"').strip("'")
    if env_key:
        API_KEY = env_key
        KEY_SOURCE = "环境变量 DEEPSEEK_API_KEY"

if not API_KEY:
    print("请粘贴 API Key（只复制 sk- 开头的那一串），然后按回车：")
    API_KEY = input("Key: ").strip().strip('"').strip("'")

# 2.拦住"把整条命令当 key 粘贴"的常见错误（key 里不会有空格 / curl / Bearer）
if " " in API_KEY or "curl" in API_KEY.lower() or "bearer" in API_KEY.lower():
    print("粘贴的内容不像 API Key：它包含空格或 curl/Bearer 字样。")
    print("请只复制以 sk- 开头的那一串字符，不要带命令、引号或 Bearer。")
    sys.exit(1)

# 2.打印 key 的基本信息（只显示前缀/后缀，不泄露完整 key）
print("=" * 60)
print(f"Base URL : {BASE_URL}")
print(f"Model    : {MODEL}")
print(f"Key 来源 : {KEY_SOURCE}")
print(f"Key 长度 : {len(API_KEY)}")
if API_KEY:
    print(f"Key 前后 : {API_KEY[:3]}...{API_KEY[-4:]}")
if not API_KEY.startswith("sk-"):
    print("警告：key 不是以 sk- 开头，可能粘贴错了")
# 列出常见的代理环境变量（只显示变量名，不打印可能含密码的值）
proxy_names = [name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                                 "http_proxy", "https_proxy", "all_proxy", "no_proxy")
               if os.environ.get(name)]
if proxy_names:
    print(f"检测到代理环境变量: {', '.join(proxy_names)}")
print("=" * 60)


# 3.默认绕过系统代理（Windows 上代理配置错误常导致 10061）
def build_opener():
    """
    构造 urllib 的 opener
        :return: 默认不使用任何代理；带 --use-proxy 参数时才走系统代理
    """
    if "--use-proxy" in sys.argv:
        print("已启用 --use-proxy：本次请求使用系统代理")
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = build_opener()


# 3.发起请求并打印状态码/响应体/异常
def request(url: str, body: dict | None = None) -> None:
    """
    发送一个请求并打印结果
        :param url: 完整请求地址
        :param body: 请求体（None 表示 GET）
        :return: 无；失败时抛异常给调用方
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with OPENER.open(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            print(f"HTTP {resp.status}")
            print(text[:2000])
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code}")
        if exc.code == 401:
            print(">>> key 无效：请确认复制的是完整 key，且没有多余空格")
        elif exc.code == 402:
            print(">>> 账户余额不足：请到 DeepSeek 平台充值")
        elif exc.code == 404:
            print(">>> 接口不存在：可以改用 --chat 测对话接口")
        print(text[:2000])
    except Exception as exc:
        print(f"请求失败: {type(exc).__name__}: {exc}")
        print("排查方向：网络/代理/VPN/防火墙，或换用 Python 请求（本脚本）而不是 curl.exe")
        sys.exit(1)


# 4.先测 /models；带 --chat 参数时再测一次对话接口
print("\n[1] GET /models")
request(f"{BASE_URL}/models")

if "--chat" in sys.argv:
    print("\n[2] POST /chat/completions")
    request(
        f"{BASE_URL}/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": False,
            "max_tokens": 8,
        },
    )

print("\n检查结束。")
