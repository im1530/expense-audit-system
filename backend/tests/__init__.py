"""
测试包引导（tests/__init__.py）
    职责：在导入任何 app 模块之前，把 DATABASE_URL 指向独立测试库。
    为什么必须放在 __init__.py：
        app.dependencies 在导入时就会创建数据库引擎，一旦那时指向正式库，测试清表就会清掉真实数据。
        Python 保证"导入 tests.xxx 时先执行 tests/__init__.py"，因此这里是唯一可靠的时机。
    运行方式（在 backend 目录下）：
        python -m unittest discover -s tests -v
"""
import os
import pathlib



# 测试库名（与开发库 expense_audit 隔离）
TEST_DATABASE_NAME = "expense_audit_test"



# 从 .env 读取正式库连接串（仅用于推导主机/账号，不会真的连它）
def _production_url() -> str:
    """
    读取 .env 里的 DATABASE_URL，作为拼测试库连接串的基准
        :return: 正式库连接串；没有 .env 时回落到本机默认值
    """
    env_file = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return "mysql+pymysql://root:root@127.0.0.1:3306/expense_audit?charset=utf8mb4"



# 拼出测试库连接串
def _test_url() -> str:
    """
    把正式库连接串里的库名替换成测试库名
        :return: 形如 mysql+pymysql://user:pwd@host:3306/expense_audit_test?charset=utf8mb4
    """
    base = _production_url().split("?")[0].rsplit("/", 1)[0]
    return f"{base}/{TEST_DATABASE_NAME}?charset=utf8mb4"



# 覆盖环境变量：pydantic-settings 的优先级是 环境变量 > .env 文件
os.environ["DATABASE_URL"] = _test_url()
# 测试时日志只留 WARNING，避免刷屏
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DEBUG", "True")
# 测试不连真实 Redis；队列/自动触发由用例单独开启并 mock
os.environ.setdefault("AI_REVIEW_QUEUE_ENABLED", "false")
os.environ.setdefault("AI_REVIEW_AUTO_TRIGGER", "false")
# 测试默认关闭影像外发授权门槛；授权逻辑由专门用例单独开启验证
os.environ.setdefault("DOCUMENT_AI_CONSENT_REQUIRED", "false")
os.environ.setdefault("AI_REVIEW_RAW_RESPONSE_ENABLED", "false")
os.environ.setdefault("DOCUMENT_RAW_RESPONSE_ENABLED", "false")
# 单测默认用 BM25：不依赖 95MB 向量模型，保证离线可跑；Chroma/Hybrid 由专门用例显式构造
os.environ.setdefault("POLICY_RETRIEVER", "bm25")
# 限流默认走进程内：单测不连真实 Redis，Redis 存储由用例注入假客户端单独验证
os.environ.setdefault("RATE_LIMIT_BACKEND", "memory")
