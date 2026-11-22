"""
测试基础设施（独立测试库 + 建表 + 清库 + 种子数据）
    设计要点：
        - 每次测试运行开始时，删掉并重建 `expense_audit_test`，再回放 app/sql/schema.sql，
          保证结构与正式库一致（不依赖 sqlite，因为项目用了 MySQL 的 enum/json/date_format）；
        - 每个用例开始前清空全部表并重新灌入最小种子数据，用例之间互不影响；
        - 所有写操作前都会校验"当前连的确实是测试库"，防止误清开发库。
    运行方式（在 backend 目录下）：
        python -m unittest discover -s tests -v
"""
import atexit
import pathlib
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.config import settings
from app.models import (
    ApprovalFlow,
    Category,
    Department,
    Expense,
    ExpenseItem,
    Rule,
    User,
    UserRole,
)
from app.models.expense import ExpenseStatus
from app.core.security import hash_password
from tests import TEST_DATABASE_NAME



# 建表脚本（与正式库同源）
SCHEMA_FILE = pathlib.Path(__file__).resolve().parents[1] / "app" / "sql" / "schema.sql"

# 测试账号统一密码（bcrypt 较慢，整个会话只哈希一次）
TEST_PASSWORD = "Passw0rd!"

# 需要清空的表（外键检查关闭后顺序不敏感）
ALL_TABLES = [
    "maintenance_runs",
    "notifications",
    "expense_settlements",
    "workflow_checkpoint_writes",
    "workflow_checkpoint_blobs",
    "workflow_checkpoints",
    "workflow_steps",
    "workflow_runs",
    "ai_review_logs",
    "document_extractions",
    "expense_rule_hits",
    "attachments",
    "approvals",
    "expense_items",
    "expenses",
    "rules",
    "approval_flows",
    "categories",
    "users",
    "departments",
]

# 缓存密码哈希，避免每个用例都跑一次 bcrypt
_password_hash: str | None = None

# 是否已经初始化过测试库
_initialized = False



# 测试结束清理：默认删掉测试库，避免残留脏数据（KEEP_TEST_DB=1 可保留现场排查）
def _drop_at_exit() -> None:
    """
    进程退出时删除测试库
        默认删除：测试库是纯临时产物，留着只会积累脏数据；
        调试时可设置环境变量 KEEP_TEST_DB=1 保留，方便进库看现场。
        :return: 无
    """
    import os
    if os.environ.get("KEEP_TEST_DB") == "1":
        return
    try:
        from app.dependencies import engine as app_engine
        app_engine.dispose()
        server = _server_engine()
        with server.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{TEST_DATABASE_NAME}`"))
        server.dispose()
    except Exception:
        # 清理失败不影响测试结果（下一次运行会先 DROP 再 CREATE）
        pass



atexit.register(_drop_at_exit)


# 取测试账号密码哈希
def get_password_hash() -> str:
    """
    获取测试账号的 bcrypt 哈希（会话内只算一次）
        :return: bcrypt 密文
    """
    global _password_hash
    if _password_hash is None:
        _password_hash = hash_password(TEST_PASSWORD)
    return _password_hash


# 构造"服务器级"引擎（不指向具体库，用于建库/删库）
def _server_engine():
    """
    创建连接到 MySQL 服务器（不带库名）的引擎
        :return: SQLAlchemy Engine
    """
    base = settings.DATABASE_URL.split("?")[0].rsplit("/", 1)[0]
    return create_engine(base + "/")


# 把 schema.sql 拆成可执行语句
def _split_statements(sql: str) -> list[str]:
    """
    按分号拆分建表脚本，并去掉纯注释片段
        :param sql: schema.sql 全文
        :return: 可逐条执行的 SQL 列表
    """
    statements = []
    for chunk in sql.split(";"):
        lines = [line for line in chunk.splitlines() if not line.strip().startswith("--")]
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements


# 确认当前连的是测试库
def assert_test_database() -> None:
    """
    安全护栏：确认当前连接指向测试库，防止误操作开发库
        :raises RuntimeError: 连的不是测试库
    """
    database = create_engine(settings.DATABASE_URL).url.database
    if database != TEST_DATABASE_NAME:
        raise RuntimeError(f"当前连接的不是测试库（{database}），已中止以免误删数据")


# 重建测试库并回放建表脚本
def ensure_database() -> None:
    """
    删除并重建测试库，然后执行 schema.sql（整个测试会话只做一次）
        这样每次跑测试都从"干净且最新"的结构开始，不依赖手工同步。
        :return: 无
    """
    global _initialized
    if _initialized:
        return

    assert_test_database()
    server = _server_engine()
    with server.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{TEST_DATABASE_NAME}`"))
        conn.execute(text(
            f"CREATE DATABASE `{TEST_DATABASE_NAME}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        ))
    server.dispose()

    # 回放建表脚本（把库名替换成测试库）
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8").replace("expense_audit", TEST_DATABASE_NAME)
    engine = create_engine(settings.DATABASE_URL)
    with engine.begin() as conn:
        for statement in _split_statements(schema_sql):
            conn.execute(text(statement))
    engine.dispose()
    _initialized = True



# 清空全部表
def truncate_all(session) -> None:
    """
    清空测试库所有业务表（关闭外键检查，重置自增ID）
        :param session: 数据库会话
        :return: 无
    """
    assert_test_database()
    session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    for table in ALL_TABLES:
        session.execute(text(f"TRUNCATE TABLE `{table}`"))
    session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    session.commit()



# 灌入最小种子数据
def seed_base(session) -> dict:
    """
    建立测试所需的最小数据集：部门、用户、审批流、审批规则、费用类别
        覆盖角色：employee / manager×2 / finance×2 / boss / admin
        审批流：经理单人、经理→财务、经理→财务→老板（默认）、财务单人、其他财务、老板
        规则：按角色 + 金额区间映射到对应审批流
        :param session: 数据库会话
        :return: 字典，包含 users / departments / flows / categories 等供断言使用
    """
    password_hash = get_password_hash()

    # 1.部门（研发部/产品部的负责人稍后回填）
    dept_rnd = Department(name="研发部", description="测试-研发部", is_active=True)
    dept_prd = Department(name="产品部", description="测试-产品部", is_active=True)
    dept_fin = Department(name="财务部", description="测试-财务部", is_active=True)
    dept_app = Department(name="应用开发部", description="测试-应用开发部", is_active=True)
    session.add_all([dept_rnd, dept_prd, dept_fin, dept_app])
    session.flush()

    # 2.用户（密码统一 Passw0rd!）
    def make_user(username: str, role: UserRole, dept: Department | None) -> User:
        return User(
            username=username,
            email=f"{username}@test.local",
            password_hash=password_hash,
            full_name=username,
            phone="13800000000",
            position="测试",
            role=role,
            department_id=dept.id if dept else None,
            is_active=True,
            must_change_password=False,
            token_version=0,
        )

    employee = make_user("employee_e1", UserRole.EMPLOYEE, dept_rnd)
    manager = make_user("manager_m1", UserRole.MANAGER, dept_rnd)
    manager2 = make_user("manager_m2", UserRole.MANAGER, dept_prd)
    finance = make_user("finance_f1", UserRole.FINANCE, dept_fin)
    finance2 = make_user("finance_f2", UserRole.FINANCE, dept_fin)
    boss = make_user("boss_b1", UserRole.BOSS, None)
    admin = make_user("admin_a1", UserRole.ADMIN, dept_app)
    session.add_all([employee, manager, manager2, finance, finance2, boss, admin])
    session.flush()

    # 3.部门负责人（以 departments.manager_id 为准）
    dept_rnd.manager_id = manager.id
    dept_prd.manager_id = manager2.id

    # 4.审批流
    flow_mgr = ApprovalFlow(name="经理单人", description="测试", levels=[{"level": 1, "role": "manager"}],
                            is_active=True, is_default=False)
    flow_mgr_fin = ApprovalFlow(name="经理→财务", description="测试",
                                levels=[{"level": 1, "role": "manager"}, {"level": 2, "role": "finance"}],
                                is_active=True, is_default=False)
    flow_mgr_fin_boss = ApprovalFlow(name="经理→财务→老板", description="测试",
                                     levels=[{"level": 1, "role": "manager"}, {"level": 2, "role": "finance"},
                                             {"level": 3, "role": "boss"}],
                                     is_active=True, is_default=True)
    flow_fin = ApprovalFlow(name="财务单人", description="测试", levels=[{"level": 1, "role": "finance"}],
                            is_active=True, is_default=False)
    flow_fin_other = ApprovalFlow(name="其他财务单人", description="测试",
                                  levels=[{"level": 1, "role": "finance_other"}],
                                  is_active=True, is_default=False)
    flow_boss = ApprovalFlow(name="老板单人", description="测试", levels=[{"level": 1, "role": "boss"}],
                             is_active=True, is_default=False)
    session.add_all([flow_mgr, flow_mgr_fin, flow_mgr_fin_boss, flow_fin, flow_fin_other, flow_boss])
    session.flush()

    # 5.审批规则（金额类）
    def make_rule(name: str, roles: list[str], min_amount, max_amount, flow: ApprovalFlow,
                  is_active: bool = True) -> Rule:
        return Rule(
            name=name,
            rule_type="amount",
            condition=f"{roles} 且金额 {min_amount}~{max_amount}",
            action="assign_chain",
            severity=None,
            config={
                "submitter_roles": roles,
                "min_amount": min_amount,
                "max_amount": max_amount,
                "flow_id": flow.id,
            },
            is_active=is_active,
        )

    session.add_all([
        make_rule("员工小额-经理", ["employee", "admin"], 0, 999.99, flow_mgr),
        make_rule("员工中额-经理→财务", ["employee", "admin"], 1000, 19999.99, flow_mgr_fin),
        make_rule("员工大额-经理→财务→老板", ["employee", "admin"], 20000, None, flow_mgr_fin_boss),
        make_rule("经理小额-财务", ["manager"], 0, 29999.99, flow_fin),
        make_rule("财务小额-其他财务", ["finance"], 0, 19999.99, flow_fin_other),
    ])
    session.flush()

    # 5.1 检查型规则（与 init_data.sql 保持一致，均可单独停用）
    session.add_all([
        Rule(name="类别单次限额提醒", rule_type="check_category_limit",
             condition="单条明细金额超过该费用小类的单次报销限额",
             action="check", severity="warn", config={}, is_active=True),
        Rule(name="发票缺失提醒", rule_type="check_invoice_missing",
             condition="明细未上传发票且未填写无票说明",
             action="check", severity="warn", config={}, is_active=True),
        Rule(name="重复报销提醒", rule_type="check_duplicate",
             condition="同一提交人、同一费用日期、同一类别、同一金额在 60 天内重复出现",
             action="check", severity="warn", config={"window_days": 60}, is_active=True),
        Rule(name="跨期报销提醒", rule_type="check_period_late",
             condition="费用发生日期距提交日期超过 60 天",
             action="check", severity="warn", config={"max_days": 60}, is_active=True),
    ])

    # 6.费用类别
    cat_rail = Category(name="高铁票", code="TEST_RAIL", max_amount=Decimal("2000.00"),
                        description="测试类别", expense_type="transportation", is_active=True)
    cat_hotel = Category(name="商务酒店", code="TEST_HOTEL", max_amount=Decimal("3000.00"),
                         description="测试类别", expense_type="accommodation", is_active=True)
    session.add_all([cat_rail, cat_hotel])
    session.commit()

    # 7.刷新对象，返回给用例使用
    for obj in (employee, manager, manager2, finance, finance2, boss, admin,
                flow_mgr, flow_mgr_fin, flow_mgr_fin_boss, flow_fin, flow_fin_other, flow_boss,
                cat_rail, cat_hotel, dept_rnd, dept_prd, dept_fin, dept_app):
        session.refresh(obj)

    return {
        "users": {
            "employee": employee,
            "manager": manager,
            "manager2": manager2,
            "finance": finance,
            "finance2": finance2,
            "boss": boss,
            "admin": admin,
        },
        "departments": {
            "rnd": dept_rnd,
            "prd": dept_prd,
            "finance": dept_fin,
            "app": dept_app,
        },
        "flows": {
            "mgr": flow_mgr,
            "mgr_fin": flow_mgr_fin,
            "mgr_fin_boss": flow_mgr_fin_boss,
            "fin": flow_fin,
            "fin_other": flow_fin_other,
            "boss": flow_boss,
        },
        "categories": {"rail": cat_rail, "hotel": cat_hotel},
    }



# 造一张报销单（含明细）
def make_expense(
    session,
    user: User,
    *,
    status: ExpenseStatus = ExpenseStatus.DRAFT,
    flow_id: int | None = None,
    current_level: int = 1,
    amount: Decimal = Decimal("100.00"),
    item_count: int = 1,
    category_id: int | None = None,
    title: str = "测试报销单",
) -> Expense:
    """
    创建一张测试报销单（可选带明细）
        :param session: 数据库会话
        :param user: 提交人
        :param status: 单据状态
        :param flow_id: 绑定的审批流ID
        :param current_level: 当前审批层级
        :param amount: 每条明细金额
        :param item_count: 明细条数（0 表示空单，用于测试"无明细不可提交"）
        :param category_id: 明细类别ID，不传时用第一个可用类别
        :param title: 标题
        :return: 已持久化的 Expense 对象
    """
    if category_id is None:
        category_id = session.query(Category).first().id

    expense = Expense(
        user_id=user.id,
        expense_no=f"TEST{uuid.uuid4().hex[:16].upper()}",
        title=title,
        total_amount=amount * item_count,
        status=status,
        flow_id=flow_id,
        current_level=current_level,
        submitted_at=None,
    )
    session.add(expense)
    session.flush()

    for index in range(item_count):
        session.add(ExpenseItem(
            expense_id=expense.id,
            category_id=category_id,
            description=f"测试明细{index + 1}",
            amount=amount,
            expense_date=date.today(),
        ))
    session.commit()
    session.refresh(expense)
    return expense



# 造一个请求对象（鉴权依赖需要 request.state 记录用户ID）
def make_request(path: str = "/test", headers: list | None = None, method: str = "GET") -> Request:
    """
    构造一个最小的 Starlette Request，供直接调用带 Request 参数的依赖/接口
        :param path: 请求路径
        :param headers: 原始请求头列表，如 [(b"host", b"testserver")]
        :param method: HTTP 方法（限流等按方法区分的逻辑需要）
        :return: Request 对象
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)



# 测试基类
class BaseTestCase:
    """
    所有测试用例的基类（配合 unittest.TestCase 多继承使用）
        提供：
            - 会话：每个用例一个 Session，结束关闭；
            - 清库 + 种子数据：setUp 里自动执行，用例之间互不影响；
            - self.fx：种子数据字典（users / departments / flows / categories）。
    """

    @classmethod
    def setUpClass(cls):
        """整个测试类开始前，确保测试库结构与最新 schema 一致"""
        ensure_database()
        cls.Session = sessionmaker(bind=create_engine(settings.DATABASE_URL))

    def setUp(self):
        """每个用例前：清空所有表并重新灌入种子数据"""
        self.db = self.Session()
        truncate_all(self.db)
        self.fx = seed_base(self.db)

    def tearDown(self):
        """每个用例后：回滚未提交事务并关闭会话"""
        self.db.rollback()
        self.db.close()
