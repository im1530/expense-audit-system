"""
AI 预审端到端冒烟脚本（直接调用后端 HTTP 接口，不依赖项目内部模块）
    流程：
        1. 登录拿 token；
        2. 取费用类别；
        3. 创建一张待审批报销单（金额故意超过类别限额，确保有规则命中）；
        4. 提交送审；
        5. 触发 AI 预审；
        6. 轮询结果并打印。
    用法（先启动后端）：
        uvicorn app.main:app --reload
        python scripts/smoke_ai_review.py
    可选参数：
        --base-url http://127.0.0.1:8000
        --username 你的登录名
        --expense-id 已存在的待审批单据ID（跳过创建/提交）
        --force 忽略缓存，强制重跑
    说明：
        - 只用标准库，不需要 requests；
        - 密码用 getpass 输入，不写入命令历史；
        - 脚本会真实调用 DeepSeek，产生少量 token 费用。
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime



# 默认后端地址
DEFAULT_BASE_URL = "http://127.0.0.1:8000"



# 接口错误
class ApiError(Exception):
    """HTTP 接口调用失败"""



# 发请求并解析 JSON
def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    form: dict | None = None,
    timeout: int = 90,
) -> tuple[int, dict]:
    """
    调用后端接口
        :param base_url: 服务地址
        :param method: HTTP 方法
        :param path: 接口路径（以 / 开头）
        :param token: Bearer token
        :param body: JSON 请求体
        :param form: 表单请求体（与 body 二选一）
        :param timeout: 超时秒数
        :return: (HTTP 状态码, 响应 JSON)
        :raises ApiError: 网络错误或 4xx/5xx
    """
    url = base_url.rstrip("/") + path
    headers = {"Accept": "application/json"}
    data = None

    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return response.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ApiError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接后端 {url}: {exc.reason}") from exc



# 登录
def login(base_url: str, username: str, password: str) -> str:
    """
    登录并返回 access_token
        :param base_url: 服务地址
        :param username: 用户名/邮箱
        :param password: 密码
        :return: token
    """
    status, data = request_json(
        base_url,
        "POST",
        "/api/v1/auth/login",
        form={"username": username, "password": password},
    )
    if status != 200 or "access_token" not in data:
        raise ApiError(f"登录失败: {data}")
    if data.get("must_change_password"):
        print("警告：该账号处于“必须修改密码”状态，除改密外的接口都会被拒绝")
    return data["access_token"]



# 选一个有限额的类别，并给出超限金额
def pick_category(base_url: str, token: str) -> tuple[dict, float]:
    """
    选择费用类别并计算测试金额
        :param base_url: 服务地址
        :param token: token
        :return: (类别, 金额)
    """
    _status, categories = request_json(base_url, "GET", "/api/v1/expense/category", token=token)
    if not categories:
        raise ApiError("没有可用的费用类别，请先初始化 categories 表")

    # 优先选有限额的类别，确保能触发“类别限额”规则
    chosen = next((item for item in categories if item.get("max_amount") is not None), categories[0])
    max_amount = chosen.get("max_amount")
    amount = float(max_amount) + 500 if max_amount is not None else 2500.0
    return chosen, amount



# 创建一张待审批报销单
def create_expense(base_url: str, token: str, category: dict, amount: float) -> int:
    """
    创建报销单并提交送审
        :param base_url: 服务地址
        :param token: token
        :param category: 费用类别
        :param amount: 明细金额
        :return: 报销单ID
    """
    payload = {
        "title": f"AI预审冒烟-{datetime.now():%m%d-%H%M%S}",          # 标题
        "description": "脚本自动创建，用于验证 AI 预审链路",              # 说明
        "remark": "smoke test",                                        # 备注
        "items": [
            {
                "category_id": category["id"],                         # 类别ID
                "description": "超出类别限额的测试明细",                  # 明细说明
                "amount": amount,                                      # 金额（超过限额）
                "expense_date": date.today().isoformat(),              # 发生日期
            }
        ],
    }
    _status, created = request_json(base_url, "POST", "/api/v1/expense/", token=token, body=payload)
    expense_id = created["id"]

    # 提交送审（draft -> pending）
    request_json(base_url, "POST", f"/api/v1/expense/{expense_id}/submit", token=token)
    return expense_id



# 触发并轮询预审
def run_review(base_url: str, token: str, expense_id: int, *, force: bool = False) -> dict:
    """
    触发 AI 预审并轮询到结束
        :param base_url: 服务地址
        :param token: token
        :param expense_id: 报销单ID
        :param force: 是否忽略缓存强制重跑
        :return: 最终结果
    """
    path = f"/api/v1/agent/review/{expense_id}"
    if force:
        path += "?force=true"
    status, data = request_json(base_url, "POST", path, token=token)
    print(f"触发结果: HTTP {status}, 任务状态={data.get('status')}, 已投递={data.get('scheduled')}")

    # pending/running 时轮询，最多 60 秒
    for _ in range(30):
        if data.get("status") in ("success", "fallback", "failed"):
            break
        time.sleep(2)
        _status, data = request_json(
            base_url, "GET", f"/api/v1/agent/review/{expense_id}", token=token
        )
        print(f"轮询: status={data.get('status')} score={data.get('risk_score')} level={data.get('risk_level')}")
    return data



# 主入口
def main() -> int:
    """
    执行冒烟流程
        :return: 进程退出码（0 成功，1 失败）
    """
    parser = argparse.ArgumentParser(description="AI 预审端到端冒烟")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--username", default=os.environ.get("SMOKE_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("SMOKE_PASSWORD"))
    parser.add_argument("--expense-id", type=int, default=None, help="已存在的待审批单据ID")
    parser.add_argument("--force", action="store_true", help="忽略缓存，强制重跑")
    args = parser.parse_args()

    username = args.username or input("登录用户名/邮箱: ").strip()
    password = args.password or getpass.getpass("登录密码（输入不回显）: ")

    try:
        token = login(args.base_url, username, password)
        print("登录成功")

        if args.expense_id:
            expense_id = args.expense_id
            print(f"使用已有单据: {expense_id}")
        else:
            category, amount = pick_category(args.base_url, token)
            print(f"使用类别: {category['name']}（限额 {category.get('max_amount')}），测试金额 {amount}")
            expense_id = create_expense(args.base_url, token, category, amount)
            print(f"报销单已创建并提交: id={expense_id}")

        result = run_review(args.base_url, token, expense_id, force=args.force)
        print("\n最终结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if result.get("status") == "failed":
            print("\n预审失败，请查看 ai_review_logs.error_message")
            return 1
        return 0
    except ApiError as exc:
        print(f"\n调用失败: {exc}")
        return 1



if __name__ == "__main__":
    sys.exit(main())
