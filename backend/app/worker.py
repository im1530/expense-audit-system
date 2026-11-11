r"""
Celery 应用入口（Phase F：异步任务队列）
    职责：
        1. 读取 config 里的 Redis / Celery 配置，创建全局唯一的 Celery 实例；
        2. 注册任务模块 app.tasks.review_tasks，供 worker 进程消费；
        3. 统一设置序列化、超时、重试、预取等生产参数。
    启动方式（在 backend 目录下，Windows 必须用线程池）：
        ..\.venv\Scripts\celery.exe -A app.worker.celery_app worker --loglevel=INFO --pool=threads --concurrency=4
    注意：
        - 唯一运行环境是仓库根目录的 .venv（依赖齐全，含 sqlalchemy/chromadb）；
          命令里的 "..\" 不能漏：backend/.venv 只是早期验证 celery 5.6.3 与 python-dateutil 4.x
          能否共存的一次性探测环境，缺 sqlalchemy 等依赖，用它启动 worker 会在导入阶段直接失败；
        - 本文件只做 Celery 装配，不写业务逻辑；
        - Celery 官方不支持 Windows，开发机用 threads / solo 池，生产建议 Linux；
        - 任务结果不落 Redis（task_ignore_result=True），业务状态统一以 MySQL 审计表为准。
"""
import logging

from celery import Celery

from app.config import settings



logger = logging.getLogger(__name__)

# 消息中间站：优先用显式配置，未配置时回落到统一的 REDIS_URL
BROKER_URL = settings.CELERY_BROKER_URL or settings.REDIS_URL

# 结果后端：当前任务忽略结果，保留配置是为了后续接入 Flower / 任务查询
RESULT_BACKEND = settings.CELERY_RESULT_BACKEND or settings.REDIS_URL



# 全局唯一 Celery 实例，worker 与 Web 进程共用
celery_app = Celery(                                # Celery 应用对象
    "expense_audit",                                # 应用名：日志与任务注册用
    broker=BROKER_URL,                              # 消息中间站地址
    backend=RESULT_BACKEND,                         # 结果后端地址
    include=["app.tasks.review_tasks"],             # 显式注册任务模块，避免自动发现漏加载
)



# 生产参数：序列化、超时、重试、预取、时区
celery_app.conf.update(                             # 更新 Celery 全局配置
    task_serializer="json",                         # 任务参数序列化格式
    accept_content=["json"],                        # 只接受 JSON，禁止 pickle
    result_serializer="json",                       # 结果序列化格式
    timezone="Asia/Shanghai",                       # 业务时区：与系统/数据库保持一致
    enable_utc=False,                               # 关闭 UTC，避免日期偏移
    task_track_started=True,                        # 记录任务开始状态，便于排障
    task_acks_late=True,                            # 执行完成后才确认，worker 崩溃可重新投递
    task_reject_on_worker_lost=True,                # worker 丢失时拒绝任务，触发重新入队
    worker_prefetch_multiplier=1,                   # 每次只预取一个任务，避免长任务饿死其他任务
    broker_connection_retry_on_startup=True,        # 启动时 Redis 未就绪自动重连
    broker_connection_timeout=3,                    # 连接 Redis 的超时（秒），失败时快速回退
    broker_transport_options={                      # Redis 传输层超时，避免提交接口被拖住
        "socket_connect_timeout": 3,                # 建连超时（秒）
        "socket_timeout": 3,                        # 读写超时（秒）
    },
    task_publish_retry=True,                        # 投递失败允许重试
    task_publish_retry_policy={                     # 投递重试策略：快速失败，交给上层降级
        "max_retries": 2,                           # 最多重试两次
        "interval_start": 0,                        # 首次立即重试
        "interval_step": 0.2,                       # 每次增加 0.2 秒
        "interval_max": 0.5,                        # 最大间隔 0.5 秒
    },
    task_ignore_result=True,                        # 不把结果写入 Redis，业务结果以 MySQL 为准
    result_expires=3600,                            # 结果保留一小时（预留）
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,                 # 硬超时（秒）
    task_soft_time_limit=max(1, settings.CELERY_TASK_TIME_LIMIT_SECONDS - 30),   # 软超时提前 30 秒
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,                     # 测试/排障时同步执行
    task_eager_propagates=True,                     # eager 模式下异常向上抛，测试能捕获
)



logger.debug("Celery 已装配 broker=%s backend=%s eager=%s", BROKER_URL, RESULT_BACKEND, settings.CELERY_TASK_ALWAYS_EAGER)
