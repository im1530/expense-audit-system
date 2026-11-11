"""
数据库连接与依赖注入（全局唯一数据库入口）
    用 SQLAlchemy 搭建「引擎 → 会话工厂 → 请求级会话」三层结构，供全项目复用：
        - engine：连接池持有者，进程内只创建一份，真正负责和 MySQL 打交道；
        - SessionLocal：会话工厂，每次调用产出独立的 Session（一次请求一个工作单元）；
        - get_db：FastAPI 依赖，用 yield 把会话交给接口，请求结束（无论成功失败）一定关闭，防止连接泄漏。
    使用方式：接口函数声明 `db: Session = Depends(get_db)` 即可拿到会话，无需自己开关连接。
    注意：本模块不提交事务，事务的 commit/rollback 由接口或服务层按业务流程显式控制。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker,Session
from app.config import settings


# ===== 引擎：管理连接池，真正连 MySQL（全局一份）=====
engine = create_engine(
    settings.DATABASE_URL,     # 从 .env 读连接串
    echo=False,                # True 打印 SQL，调试用，平时关
    pool_pre_ping=True         # 取连接前先探活，避免拿到断线连接
)



# ===== 会话工厂：每次 SessionLocal() 开一个独立会话 =====
# **造会话的机器**，本身不出产会话
SessionLocal = sessionmaker(
    bind=engine,            # 把这个会话工厂 "绑定" 到指定的引擎engine上
    autocommit=False,       # 不自动刷写，由代码显式控制
    autoflush=False         # 不自动提交，避免误写库
)



# ===== 数据库会话依赖：给每个接口注入一个会话，用完自动关闭 =====
# **服务员**，帮你从机器取一个会话、递给接口，用完自动收回关闭。
def get_db():
    """
    数据库会话依赖：为每个请求注入一个独立的 Session，请求结束后自动关闭
        执行流程：FastAPI 收到请求时调用本生成器 → 执行到 yield 把会话交给接口 → 接口处理完（或抛异常）回到 finally 关闭。
        - 生成器 yield：yield 之前的代码在请求开始时执行，yield 之后的代码在请求结束时执行；
        - try/finally：哪怕接口中途报错，close() 也一定会执行，避免连接一直被占住；
        - 不用 commit：查询无需提交，写操作由业务代码显式 commit，保证事务边界清晰。
    用法：`db: Session = Depends(get_db)`，接口里直接拿 db 用，不用自己开关会话。
        :yield: 一个可用的数据库会话对象
    """
    # 1.从会话工厂开一个独立会话
    db = SessionLocal()        # 从会话工厂开一个新会话
    try:
        # 2.把会话交给接口使用（请求处理期间一直有效）
        yield db               # 把会话交给接口使用
    finally:
        # 3.无论成功还是抛异常，最后都关闭会话，防连接泄漏
        db.close()             # 无论成功失败，最后都关闭会话，防连接泄漏
