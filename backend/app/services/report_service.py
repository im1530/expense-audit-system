"""
报表服务（管理端统计与导出）
    提供四类聚合视图 + 一份明细导出数据，全部只读：
        - overview：总览（单据数、申报总额/有效金额、各状态分布、待审批数量与金额）
        - monthly：按月趋势（单据数、申报总额/有效金额、已报销金额）
        - by_department：按部门分布（单据数、申报总额/有效金额、已报销、待审批）
        - by_category：按费用类别分布（明细条数、金额）
        - export_rows：明细级导出数据（供 Excel 使用）
    口径说明：
        - 日期区间按报销单 created_at 过滤，且用"次日 0 点之前"的半开区间，避免漏掉结束日白天的单据；
        - 部门维度按"报销单提交时固化的 department_id 快照"统计，人员调部门后历史报表不漂移；
        - 金额一律用 Decimal，避免浮点误差；数据库函数用 MySQL 的 date_format（项目只跑 MySQL）。
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import Category, Department, Expense, ExpenseItem, User
from app.models.expense import ExpenseStatus, ExpenseType



# 状态中文名：与字典接口保持一致
STATUS_LABELS = {                               # 状态→中文名
    ExpenseStatus.DRAFT.value: "草稿",
    ExpenseStatus.PENDING.value: "待审批",
    ExpenseStatus.APPROVED.value: "已通过",
    ExpenseStatus.REGISTERED.value: "待领取",
    ExpenseStatus.REJECTED.value: "已驳回",
    ExpenseStatus.PAID.value: "已报销",
    ExpenseStatus.CLOSED.value: "已关闭",
    ExpenseStatus.CANCELLED.value: "已取消",
}


# 有效金额口径：已通过 + 待领取 + 已报销（不含草稿/驳回/关闭）
NET_AMOUNT_STATUSES = (
    ExpenseStatus.APPROVED,
    ExpenseStatus.REGISTERED,
    ExpenseStatus.PAID,
)



# 费用大类中文名：与字典接口保持一致
EXPENSE_TYPE_LABELS = {                         # 大类→中文名
    ExpenseType.MEAL.value: "餐饮费",
    ExpenseType.TRANSPORTATION.value: "交通费",
    ExpenseType.ACCOMMODATION.value: "住宿费",
    ExpenseType.OFFICE.value: "办公费",
    ExpenseType.OTHER.value: "其他",
}



# 追加日期区间与部门筛选（报销单维度）
def _apply_expense_filters(
    query,                                          # 报销单查询对象
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
    department_id: int | None = None,               # 部门ID
):
    """
    给报销单维度的查询追加日期区间与部门筛选
        日期用半开区间：created_at >= start_date 且 created_at < end_date + 1 天，
        这样结束日 00:00 之后创建的单据也能被统计到。
        :param query: 已 join 好 User 的查询对象（部门筛选使用 Expense.department_id 快照）
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按提交人所属部门过滤
        :return: 追加完条件的查询对象
    """
    # 1.起始日：含当天 00:00 起（DateTime >= date 会自动补 00:00:00）
    if start_date is not None:
        query = query.filter(Expense.created_at >= start_date)   # 起始日条件
    # 2.结束日：用"次日 0 点之前"的半开区间，避免漏掉结束日白天的单据
    if end_date is not None:
        query = query.filter(Expense.created_at < end_date + timedelta(days=1))   # 结束日条件
    # 3.部门：按提交时固化的部门快照过滤，避免人员调部门后历史报表漂移
    if department_id is not None:
        query = query.filter(Expense.department_id == department_id)   # 部门快照条件
    # 4.返回追加条件后的查询（链式调用，不改入参）
    return query



# 总览统计
def get_overview(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
    department_id: int | None = None,               # 部门ID
) -> dict:
    """
    统计总览数据（单据数、总金额、各状态分布、待审批情况）
        :param db: 数据库会话
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按部门过滤
        :return: 总览字典，含 total_count / total_amount / pending_count / pending_amount / status_items
    """
    # 1.按状态分组统计数量与金额
    query = db.query(                                # 状态统计查询
        Expense.status,                                     # 状态
        func.count(Expense.id),                             # 单据数
        func.coalesce(func.sum(Expense.total_amount), 0),   # 金额合计
    ).join(User, Expense.user_id == User.id)
    query = _apply_expense_filters(query, start_date=start_date, end_date=end_date, department_id=department_id)   # 追加筛选
    rows = query.group_by(Expense.status).all()             # 状态统计行

    # 2.组装状态明细（按固定顺序输出，前端展示稳定）
    status_map = {row[0].value if hasattr(row[0], "value") else row[0]: row for row in rows}   # 状态→统计行
    status_items = []                                       # 状态明细
    for status in ExpenseStatus:
        row = status_map.get(status.value)                  # 统计行
        status_items.append({
            "status": status.value,
            "label": STATUS_LABELS.get(status.value, status.value),
            "count": int(row[1]) if row else 0,
            "amount": Decimal(str(row[2])) if row else Decimal("0"),
        })

    # 3.总览汇总
    total_count = sum(item["count"] for item in status_items)   # 单据总数
    gross_amount = sum(
        (item["amount"] for item in status_items if item["status"] != ExpenseStatus.DRAFT.value),
        Decimal("0"),
    )                                                           # 申报总额：排除草稿
    net_statuses = {status.value for status in NET_AMOUNT_STATUSES}
    net_amount = sum(
        (item["amount"] for item in status_items if item["status"] in net_statuses),
        Decimal("0"),
    )                                                           # 有效金额：已通过/待领取/已报销
    pending = next((item for item in status_items if item["status"] == ExpenseStatus.PENDING.value), None)   # 待审批行

    return {
        "total_count": total_count,
        "gross_amount": gross_amount,
        "net_amount": net_amount,
        "total_amount": gross_amount,   # 兼容旧客户端
        "pending_count": pending["count"] if pending else 0,
        "pending_amount": pending["amount"] if pending else Decimal("0"),
        "status_items": status_items,
    }



# 按月趋势
def get_monthly_trend(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
    department_id: int | None = None,               # 部门ID
) -> list[dict]:
    """
    按月统计单据数、总金额、已支付金额
        :param db: 数据库会话
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按部门过滤
        :return: 列表，每项含 month / expense_count / total_amount / paid_amount
    """
    # 1.把创建时间截成 YYYY-MM，作为分组维度
    # MySQL 的 date_format：把 created_at 截成 YYYY-MM
    month_expr = func.date_format(Expense.created_at, "%Y-%m")   # 月份表达式

    # 2.按月聚合：单据数、申报总额（非草稿）、有效金额、已报销金额
    query = db.query(                                # 按月统计查询
        month_expr.label("month"),                                  # 月份
        func.count(Expense.id),                                     # 单据数
        func.coalesce(func.sum(case(
            (Expense.status != ExpenseStatus.DRAFT, Expense.total_amount), else_=0
        )), 0),                                                     # 申报总额（排除草稿）
        func.coalesce(func.sum(case(
            (Expense.status.in_(NET_AMOUNT_STATUSES), Expense.total_amount), else_=0
        )), 0),                                                     # 有效金额
        func.coalesce(func.sum(case(
            (Expense.status == ExpenseStatus.PAID, Expense.total_amount), else_=0
        )), 0),                                                     # 已报销金额
    ).join(User, Expense.user_id == User.id)
    # 3.追加日期区间/部门筛选
    query = _apply_expense_filters(query, start_date=start_date, end_date=end_date, department_id=department_id)   # 追加筛选
    # 4.按月份分组并升序排序（趋势图按时间从左到右）
    rows = query.group_by(month_expr).order_by(month_expr).all()   # 月度统计行

    # 5.转成前端直接可用的字典列表
    return [
        {
            "month": row[0],
            "expense_count": int(row[1]),
            "gross_amount": Decimal(str(row[2])),
            "net_amount": Decimal(str(row[3])),
            "total_amount": Decimal(str(row[2])),   # 兼容旧客户端
            "paid_amount": Decimal(str(row[4])),
        }
        for row in rows
    ]



# 按部门统计
def get_department_report(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
) -> list[dict]:
    """
    按部门统计单据数、总金额、已支付、待审批数量
        没挂部门的用户（如 boss）会归到 department_id=null 的一组，前端显示为"未分配"。
        :param db: 数据库会话
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :return: 列表，按金额倒序
    """
    # 1.定义聚合表达式：申报总额（非草稿）、有效金额、已报销金额、待审批单据数
    gross_amount = func.coalesce(func.sum(case(      # 申报总额（排除草稿）
        (Expense.status != ExpenseStatus.DRAFT, Expense.total_amount), else_=0
    )), 0)
    net_amount = func.coalesce(func.sum(case(       # 有效金额
        (Expense.status.in_(NET_AMOUNT_STATUSES), Expense.total_amount), else_=0
    )), 0)
    paid_amount = func.coalesce(func.sum(case(      # 已报销金额
        (Expense.status == ExpenseStatus.PAID, Expense.total_amount), else_=0
    )), 0)
    pending_count = func.coalesce(func.sum(case(    # 待审批数
        (Expense.status == ExpenseStatus.PENDING, 1), else_=0
    )), 0)

    # 2.按部门聚合：没有部门的用户用 outerjoin 归到"未分配"
    query = db.query(                                # 部门统计查询
        Department.id,                  # 部门ID
        Department.name,                # 部门名称
        func.count(Expense.id),         # 单据数
        gross_amount,                   # 申报总额
        net_amount,                     # 有效金额
        paid_amount,                    # 已报销金额
        pending_count,                  # 待审批数
    ).join(User, Expense.user_id == User.id).outerjoin(
        Department, Expense.department_id == Department.id
    )
    # 3.追加日期区间筛选
    query = _apply_expense_filters(query, start_date=start_date, end_date=end_date)   # 追加筛选
    # 4.按部门分组，金额高的排前面
    rows = query.group_by(Department.id, Department.name).order_by(gross_amount.desc()).all()   # 部门统计行

    # 5.转成前端直接可用的字典列表
    return [
        {
            "department_id": row[0],
            "department_name": row[1] or "未分配",
            "expense_count": int(row[2]),
            "gross_amount": Decimal(str(row[3])),
            "net_amount": Decimal(str(row[4])),
            "total_amount": Decimal(str(row[3])),   # 兼容旧客户端
            "paid_amount": Decimal(str(row[5])),
            "pending_count": int(row[6]),
        }
        for row in rows
    ]



# 按费用类别统计
def get_category_report(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
    department_id: int | None = None,               # 部门ID
) -> list[dict]:
    """
    按费用类别（小类）统计明细条数与金额
        :param db: 数据库会话
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按部门过滤
        :return: 列表，按金额倒序
    """
    # 1.金额聚合表达式：按明细金额求和
    total_amount = func.coalesce(func.sum(ExpenseItem.amount), 0)   # 金额合计

    # 2.类别 → 明细 → 单据 → 提交人 四表关联（多一个 join 用于部门筛选）
    query = db.query(                                # 类别统计查询
        Category.id,                # 类别ID
        Category.name,              # 类别名称
        Category.expense_type,      # 所属大类
        func.count(ExpenseItem.id), # 明细条数
        total_amount,               # 金额合计
    ).join(
        ExpenseItem, ExpenseItem.category_id == Category.id     # 类别 → 明细
    ).join(
        Expense, ExpenseItem.expense_id == Expense.id           # 明细 → 单据
    ).join(
        User, Expense.user_id == User.id                        # 单据 → 提交人（部门筛选）
    )
    # 3.追加日期区间/部门筛选
    query = _apply_expense_filters(query, start_date=start_date, end_date=end_date, department_id=department_id)   # 追加筛选
    # 4.按类别分组，金额高的排前面
    rows = query.group_by(Category.id, Category.name, Category.expense_type).order_by(total_amount.desc()).all()   # 类别统计行

    # 5.转成前端直接可用的字典列表（带费用大类中文名）
    return [
        {
            "category_id": row[0],
            "category_name": row[1],
            "expense_type": row[2],
            "expense_type_label": EXPENSE_TYPE_LABELS.get(
                row[2].value if hasattr(row[2], "value") else row[2], str(row[2])
            ),
            "item_count": int(row[3]),
            "total_amount": Decimal(str(row[4])),
        }
        for row in rows
    ]



# 明细导出数据
def get_export_rows(
    db: Session,                                    # 数据库会话
    *,
    start_date: date | None = None,                 # 起始日期
    end_date: date | None = None,                   # 结束日期
    department_id: int | None = None,               # 部门ID
    status: ExpenseStatus | None = None,            # 单据状态
    limit: int = 20000,                             # 最大导出行数
) -> list[list]:
    """
    取明细级导出数据（一行 = 一条报销明细）
        列顺序与导出表头严格对应：
        单号 / 标题 / 提交人 / 部门 / 单据状态 / 单据金额 / 费用说明 / 类别 / 费用大类 / 明细金额 / 发生日期 / 发票号 / 创建时间
        :param db: 数据库会话
        :param start_date: 可选，起始日期
        :param end_date: 可选，结束日期（含当天）
        :param department_id: 可选，按部门过滤
        :param status: 可选，按单据状态过滤
        :param limit: 最大导出行数，防止误导出百万行把内存打爆
        :return: 二维列表，元素已转成适合写 Excel 的形式
    """
    # 1.选定导出列（顺序必须与 Excel 表头严格一致）
    query = db.query(                                # 导出数据查询
        Expense.expense_no,             # 单号
        Expense.title,                  # 标题
        User.full_name,                 # 提交人
        Department.name,                # 部门
        Expense.status,                 # 单据状态
        Expense.total_amount,           # 单据金额
        ExpenseItem.description,        # 费用说明
        Category.name,                  # 类别
        Category.expense_type,          # 费用大类
        ExpenseItem.amount,             # 明细金额
        ExpenseItem.expense_date,       # 发生日期
        ExpenseItem.invoice_no,         # 发票号
        Expense.created_at,             # 创建时间
    ).join(
        User, Expense.user_id == User.id
    ).outerjoin(
        Department, Expense.department_id == Department.id
    ).join(
        ExpenseItem, ExpenseItem.expense_id == Expense.id
    ).outerjoin(
        Category, ExpenseItem.category_id == Category.id
    )

    # 2.追加日期区间/部门筛选与单据状态筛选
    query = _apply_expense_filters(query, start_date=start_date, end_date=end_date, department_id=department_id)   # 追加筛选
    if status is not None:
        query = query.filter(Expense.status == status)   # 状态条件

    # 3.排序并限制最大导出行数（防止一次导出把内存打爆）
    query = query.order_by(Expense.created_at.desc(), Expense.id.desc(), ExpenseItem.id.asc()).limit(limit)   # 排序与限流

    # 4.逐行转换：枚举转中文、空部门显示"未分配"
    result = []                                     # 导出行列表
    for row in query.all():
        status_value = row[4].value if hasattr(row[4], "value") else row[4]   # 状态值
        type_value = row[8].value if hasattr(row[8], "value") else row[8]     # 大类值
        # 5.按表头顺序组装一行
        result.append([
            row[0],                                     # 单号
            row[1],                                     # 标题
            row[2],                                     # 提交人
            row[3] or "未分配",                          # 部门
            STATUS_LABELS.get(status_value, status_value),  # 单据状态
            row[5],                                     # 单据金额
            row[6],                                     # 费用说明
            row[7],                                     # 类别
            EXPENSE_TYPE_LABELS.get(type_value, type_value) if type_value else None,  # 费用大类
            row[9],                                     # 明细金额
            row[10],                                    # 发生日期
            row[11],                                    # 发票号
            row[12],                                    # 创建时间
        ])

    # 6.返回二维列表，交给 excel_service 写表
    return result
