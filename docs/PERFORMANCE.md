# 并发压测说明

## 为什么单独放脚本，不进 CI

压测需要**固定数据规模、独立环境、预热和稳定网络**。直接塞进普通单元测试会出现：

- 数据量不同导致耗时不可比；
- CI 共享 runner 抖动导致 p95 假超标；
- 一次并发压测可能触发限流，污染其他测试。

因此压测是**上线前手动执行**的检查项，不进入每次 CI 的硬门禁。

## 用法

后端已启动、演示数据已就绪后：

```bash
cd backend
.venv/Scripts/python.exe scripts/load_test.py --requests 200 --concurrency 10
```

默认压：

```text
GET /api/v1/approval/pending?page_size=20
```

用 `manager1 / Demo@123456` 登录（仅本地演示；生产换成只读压测账号）。

常用参数：

```bash
# 指定环境与账号
.venv/Scripts/python.exe scripts/load_test.py \
  --base-url http://127.0.0.1:8000 \
  --username manager1 --password Demo@123456 \
  --requests 500 --concurrency 20

# 压某个具体接口
.venv/Scripts/python.exe scripts/load_test.py \
  --endpoint "/api/v1/expense/my?page=1&page_size=20" \
  --requests 200 --concurrency 10

# 自签 HTTPS
.venv/Scripts/python.exe scripts/load_test.py --base-url https://127.0.0.1 --insecure

# 设阈值：p95 超过 800ms 或错误率超过 1% 时退出码 1
.venv/Scripts/python.exe scripts/load_test.py \
  --requests 300 --concurrency 15 --max-p95-ms 800 --max-error-rate 0.01

# 已有 token，跳过登录
.venv/Scripts/python.exe scripts/load_test.py --token "<ACCESS_TOKEN>"
```

## 输出怎么读

```json
{
  "requests": 200,
  "concurrency": 10,
  "success": 200,
  "errors": 0,
  "error_rate": 0.0,
  "rps": 42.31,
  "latency_ms": {"min": 180.2, "p50": 220.4, "p95": 410.8, "p99": 520.1, "max": 610.0}
}
```

- **p50**：一半请求快于该值，代表典型体验；
- **p95**：尾部延迟，比平均值更能暴露“待办全量加载”这类问题；
- **error_rate**：HTTP 4xx/5xx 与网络异常占比，默认超过 1% 退出码 1；
- **rps**：客户端侧吞吐，受并发数和接口耗时共同影响。

## 压测前检查

- 用**独立测试库/独立环境**，不要在业务高峰压生产；
- 先小并发（5~10）跑一轮，确认登录和接口正常，再加压；
- 关注 p95 和错误率，不要只看平均值；
- 若触发 429，说明限流生效，应降低并发或换独立账号，而不是把 429 当接口故障；
- 压测结果不要和不同数据量、不同机器、不同并发数的结果直接比较。

## 已知边界

当前仓库没有保存固定的性能基线报告；首次压测后建议把环境、数据量、并发数、p50/p95 记录到 `docs/GO_LIVE_CHECKLIST.md`，后续版本才能做同口径对比。
