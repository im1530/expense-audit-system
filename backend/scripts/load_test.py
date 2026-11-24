"""
并发压测脚本（手动执行，不进 CI）
    目的：给"AI 预审 1.3~1.9 秒"之外的接口补一个可重复的并发基线，
          重点覆盖审批待办列表这类"一次加载一批单据"的接口。

用法（后端已启动）：
    cd backend
    .venv/Scripts/python.exe scripts/load_test.py --requests 200 --concurrency 10

常用参数：
    --base-url       默认 http://127.0.0.1:8000
    --username       默认 manager1
    --password       默认 Demo@123456
    --endpoint       默认 /api/v1/approval/pending?page_size=20
    --requests       总请求数
    --concurrency    并发协程数
    --max-p95-ms     可选：p95 超过该值以退出码 1 失败
    --insecure       自签 HTTPS 时跳过证书校验

说明：
    - 只读 GET，不写业务数据；
    - 压测前先登录拿 token，token 只取一次；
    - 统计的是客户端端到端耗时，包含网络与 JSON 解析；
    - 生产环境请用独立账号与只读接口，不要在业务高峰压测。
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import httpx


def percentile(values: list[float], p: float) -> float:
    """线性插值计算百分位（p 取 0~100）"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    """登录并返回 Bearer token"""
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("登录响应缺少 access_token")
    return token


async def worker(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict,
    count: int,
    latencies: list[float],
    statuses: list[int],
    errors: list[str],
) -> None:
    """单个并发协程：连续发 count 次 GET"""
    for _ in range(count):
        started = time.perf_counter()
        try:
            response = await client.get(endpoint, headers=headers)
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            statuses.append(response.status_code)
            if response.status_code >= 400:
                errors.append(f"HTTP {response.status_code}: {response.text[:200]}")
        except Exception as exc:                       # 网络超时/连接失败也计入错误
            elapsed_ms = (time.perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            errors.append(f"{type(exc).__name__}: {exc}")


async def run(args) -> dict:
    """执行一轮压测并返回统计结果"""
    base_url = args.base_url.rstrip("/")
    headers = {"Accept": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    else:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=args.timeout,
            verify=not args.insecure,
        ) as client:
            token = await login(client, args.username, args.password)
            headers["Authorization"] = f"Bearer {token}"

    # 预热：避免把 TLS 握手/连接池初始化算进主样本
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=args.timeout,
        verify=not args.insecure,
        limits=httpx.Limits(
            max_connections=max(20, args.concurrency * 2),
            max_keepalive_connections=max(20, args.concurrency),
        ),
    ) as client:
        for _ in range(max(0, args.warmup)):
            await client.get(args.endpoint, headers=headers)

        per_worker = args.requests // args.concurrency
        remainder = args.requests % args.concurrency
        counts = [per_worker + (1 if index < remainder else 0) for index in range(args.concurrency)]

        latencies: list[float] = []
        statuses: list[int] = []
        errors: list[str] = []

        started = time.perf_counter()
        await asyncio.gather(*[
            worker(client, args.endpoint, headers, count, latencies, statuses, errors)
            for count in counts
            if count > 0
        ])
        elapsed = time.perf_counter() - started

    success = sum(1 for status in statuses if 200 <= status < 400)
    total = len(latencies)
    return {
        "base_url": base_url,
        "endpoint": args.endpoint,
        "requests": total,
        "concurrency": args.concurrency,
        "success": success,
        "errors": len(errors),
        "error_rate": round(len(errors) / total, 4) if total else 1.0,
        "rps": round(total / elapsed, 2) if elapsed > 0 else 0.0,
        "latency_ms": {
            "min": round(min(latencies), 2) if latencies else 0.0,
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        },
        "error_samples": errors[:5],
    }


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="审批接口并发压测（只读）")
    parser.add_argument("--base-url", default=os.getenv("LOAD_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--username", default=os.getenv("LOAD_USERNAME", "manager1"))
    parser.add_argument("--password", default=os.getenv("LOAD_PASSWORD", "Demo@123456"))
    parser.add_argument(
        "--endpoint",
        default=os.getenv("LOAD_ENDPOINT", "/api/v1/approval/pending?page_size=20"),
    )
    parser.add_argument("--token", default=os.getenv("LOAD_TOKEN", ""), help="已有 token，跳过登录")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-p95-ms", type=float, default=0.0, help="0 表示不设阈值")
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    args = parser.parse_args(argv)
    if args.requests <= 0:
        parser.error("--requests 必须大于 0")
    if args.concurrency <= 0:
        parser.error("--concurrency 必须大于 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["error_rate"] > args.max_error_rate:
        return 1
    if args.max_p95_ms > 0 and result["latency_ms"]["p95"] > args.max_p95_ms:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
