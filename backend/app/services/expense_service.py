"""
报销业务服务（跨接口复用的业务逻辑，与 HTTP 无关）
    把"多个接口都要用、又不属于某个具体接口"的报销逻辑集中在这里，供路由层直接调用：
        - apply_business_filters：给"报销单"查询追加通用筛选条件。
          费用大类/小类用 exists 子查询判断"是否存在符合条件的明细"，避免直接 join 明细表造成主单重复行；
        - apply_item_business_filters：给"报销明细"查询追加通用筛选条件。
          明细列表本身已 join 好分类表，直接按列过滤即可，不能再套 exists；
        - match_approval_flow：按「提交人角色 + 报销金额」匹配审批流，未命中回落默认审批流，
          boss 返回 0 表示"不走线上审批"；首次提交与重新提交都复用它；
        - recalculate_expense_total_amount：从数据库重新汇总明细金额并回写主单，保证金额不受 ORM 缓存影响。
    约定：本模块只改内存中的对象、不提交事务（commit 由调用方负责），便于把多次写操作放进同一事务保证原子性。
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Query, Session
from app.models.expense import Expense,ExpenseItem,Category
from app.schemas.common import BusinessFilter
from fastapi import HTTPException
from app.models.user import User
from app.models.rule import Rule
from app.models.approval import ApprovalFlow
from sqlalchemy import func,exists





# 查询报销单时的通用筛选条件
def apply_business_filters(query: Query, filters: BusinessFilter) -> Query:
    """
    给"报销单"查询对象追加通用业务筛选条件，返回新的查询对象（链式调用，不修改入参）
        为什么费用条件用 exists 而不是 join：一张单可能有多条明细，直接 join 会让同一张单在结果里
        重复出现、并让 count() 统计失真；exists 只判断"是否存在符合条件的明细"，主单始终一行。
        各条件相互独立、同时生效（AND 关系），未传值的条件直接跳过。
        日期口径：created_at 是 datetime，而筛选参数是 date，因此区间按"整天"处理——
        start_date 含当天 00:00 起，end_date 用"次日 0 点之前"的半开区间，不会漏掉结束日白天的单据。
        :param query: 已追加权限范围条件的报销单查询对象（如已限定 user_id == 当前用户）
        :param filters: 通用筛选参数实例（全字段可选）
        :return: 追加完筛选条件的新的查询对象
    """
    # 1.按明细所属费用大类筛选：关联明细表+分类表，判断是否存在对应大类的明细
    if filters.expense_type is not None:
        subquery = exists().where(                  # 存在性子查询
            ExpenseItem.expense_id == Expense.id
        ).where(
            ExpenseItem.category_id == Category.id
        ).where(
            Category.expense_type == filters.expense_type
        )
        query = query.filter(subquery)              # 追加存在性条件

    # 2.按明细所属费用分类(小类)筛选：关联明细表，判断是否存在对应分类ID的明细
    if filters.category_id is not None:
        subquery = exists().where(                  # 存在性子查询
            ExpenseItem.expense_id == Expense.id
        ).where(
            ExpenseItem.category_id == filters.category_id
        )
        query = query.filter(subquery)              # 追加存在性条件

    # 3.按创建日期区间筛选(created_at 是 datetime,日期参数是 date,按"整天"口径处理)
    # start_date:从当天 00:00:00 起算(>= 日期值,MySQL 会自动补 00:00:00)
    # end_date:用"次日 0 点之前"的半开区间(< 次日),这样结束日 00:00 之后创建的单据也能被查到
    if filters.start_date is not None:
        query = query.filter(Expense.created_at >= filters.start_date)   # 起始日
    if filters.end_date is not None:
        query = query.filter(Expense.created_at < filters.end_date + timedelta(days=1))   # 结束日

    # 4.按金额区间筛选
    if filters.min_amount is not None:
        query = query.filter(Expense.total_amount >= filters.min_amount)   # 金额下限
    if filters.max_amount is not None:
        query = query.filter(Expense.total_amount <= filters.max_amount)   # 金额上限

    return query



# 查询报销明细时的通用筛选条件
def apply_item_business_filters(query: Query, filters: BusinessFilter) -> Query:
    """
    给"报销明细"查询对象追加通用业务筛选条件，返回新的查询对象
        与 apply_business_filters 的区别：明细查询本身已经 join 了费用类别表，
        因此可以直接按 ExpenseItem.amount、Category.expense_type 等列过滤，无需 exists；
        "日期"指费用发生日期、"金额"指单条明细金额，与单据维度的口径不同。
        依赖约定：调用前查询必须已 join Category，否则按大类筛选会报列找不到。
        :param query: 已 join 报销单表与费用类别表的明细查询对象
        :param filters: 通用筛选参数实例（全字段可选）
        :return: 追加完筛选条件的新的查询对象
    """
    # 1.按所属费用大类筛选：直接通过关联的费用类别表过滤
    if filters.expense_type is not None:
        query = query.filter(Category.expense_type == filters.expense_type)   # 费用大类

    # 2.按费用分类(小类)筛选:直接按明细外键过滤
    if filters.category_id is not None:
        query = query.filter(ExpenseItem.category_id == filters.category_id)   # 费用小类

    # 3.按费用发生日期区间筛选
    if filters.start_date is not None:
        query = query.filter(ExpenseItem.expense_date >= filters.start_date)   # 发生日期起
    if filters.end_date is not None:
        query = query.filter(ExpenseItem.expense_date <= filters.end_date)     # 发生日期止

    # 4.按单条明细金额区间筛选
    if filters.min_amount is not None:
        query = query.filter(ExpenseItem.amount >= filters.min_amount)   # 明细金额下限
    if filters.max_amount is not None:
        query = query.filter(ExpenseItem.amount <= filters.max_amount)   # 明细金额上限

    return query



# 金额统一转 Decimal（规则配置来自 JSON，可能是 int/float/str）
def _as_decimal(value, *, label: str) -> Decimal | None:
    """
    把金额转成 Decimal，避免用浮点数做金额比较
        :param value: 原始金额；None 表示"不限"
        :param label: 出错时的定位说明（哪条规则的哪个字段）
        :return: Decimal；None 原样返回
        :raises HTTPException: 500，配置里的金额不是合法数字（属于配置错误，不是用户输入问题）
    """
    # 1.不限额的语义用 None 表示，直接透传
    if value is None:
        return None
    # 2.已经是 Decimal 无需转换
    if isinstance(value, Decimal):
        return value
    # 3.其余类型统一先转字符串再转 Decimal：str(0.1) 得到 "0.1"，
    #   而 Decimal(0.1) 会带出二进制误差 0.1000000000000000055511151231257827
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"审批规则配置的金额不合法：{label}") from exc



# 提交报销单时匹配审批流
def match_approval_flow(db: Session, submitter_user: User, amount: float | Decimal | str) -> int:
    """
    按「提交人角色 + 报销金额」匹配审批流，返回应绑定的审批流ID
    纯计算逻辑：只读库、不写库、不提交事务，供"首次提交"与"驳回后重新提交"复用。
    匹配规则（按顺序执行）:
        1. 特殊角色前置判断：boss 的报销单不在线上走审批，直接返回 0 作为免审批标识；
        2. 取出所有启用中的金额类规则（rule_type="amount" 且 is_active=True）；
        3. 逐条规则按「先角色、后金额区间」校验：config.submitter_roles 含本人角色，
           且金额落在 [min_amount, max_amount] 内（max_amount 缺省视为无上限）；
        4. 采用"第一条命中即生效"，遍历顺序固定为 rules.id 升序（已显式 order_by，保证结果可复现）；
           若业务需要可调优先级，应在 rules 表增加 priority 字段并改为按它排序；
        5. 命中规则后，还要校验该规则 config.flow_id 指向的审批流"存在且已启用"：
           规则与流程是两处独立配置，流程被停用/删掉后规则仍在，若不校验就会把单据挂到无效流程上，
           因此这里宁可 500 阻断，也不静默放行或改走默认流（避免大额单据被降级到简易流程）；
        6. 全部规则都不命中时，回落到"is_default=True 且 is_active=True"的默认审批流；
           默认流被停用视同没有可用默认流；
        7. 前面都拿不到流程时抛 500 阻断提交（属于初始化数据缺失，不能静默放过）。
        :param db: 数据库会话
        :param submitter_user: 报销单提交人，用于取角色
        :param amount: 报销单总金额（Decimal 优先；兼容 float/str 入参，内部统一转 Decimal 比较）
        :return: 匹配到的审批流ID；boss 角色返回 0，表示不走线上审批
        :raises HTTPException: 500，规则命中的审批流不存在/已停用，或系统没有可用的默认审批流
    """
    # 1. 统一处理提交人角色格式，枚举类型转字符串
    submitter_role = submitter_user.role.value if hasattr(submitter_user.role, "value") else submitter_user.role   # 提交人角色
    matched_rule = None     # 命中的规则

    # 2.boss角色前置特殊处理:boss的报销单不在线上系统走审批流程,仅做单据留存
    if submitter_role == "boss":
        return 0

    # 2.1 金额统一转 Decimal：金额比较不能用 float，否则边界值（如 999.99 / 19999.99）
    #     可能因二进制浮点表示误差被多算或少算进一档，导致审批层级错配
    amount_value = _as_decimal(amount, label="报销金额")
    if amount_value is None:
        raise HTTPException(status_code=500, detail="报销单总金额缺失，无法匹配审批流")

    # 3. 查询所有启用中的金额类审批规则
    #    显式按 id 升序：'第一条命中即生效'要求遍历顺序确定，
    #    否则同一笔金额在规则区间重叠时可能今天走 flow A、明天走 flow B（取决于数据库返回顺序）。
    #    当前用 id 作为"写入顺序"的替身；若业务需要可调优先级，应在 rules 表加 priority 字段并改这里。
    rules = db.query(Rule).filter(                  # 启用金额规则
        Rule.rule_type == "amount",                 # 金额类规则
        Rule.is_active == True                      # 仅启用
    ).order_by(Rule.id.asc()).all()

    # 4. 遍历规则，按「角色 -> 金额区间」依次匹配
    for rule in rules:
        # 先取出当前规则的配置参数(JSON字典),为空则用空字典兜底
        rule_config = rule.config or {}             # 规则配置
        # 拿到适用于当前规则的申请人角色列表,比如["employee","admin"]
        allowed_roles = rule_config.get("submitter_roles", [])   # 适用角色

        # 角色校验:如果当前用户的角色不在适用列表中，则跳过该规则
        if submitter_role not in allowed_roles:
            continue

        # 取出金额区间，缺省值兜底（统一转 Decimal，NULL 表示无上限）
        min_amount = _as_decimal(
            rule_config.get("min_amount", 0), label=f"规则 {rule.id} 的 min_amount"
        )                                            # 金额下限
        if min_amount is None:
            min_amount = Decimal("0")                # 未配置下限视为 0
        max_amount = _as_decimal(
            rule_config.get("max_amount"), label=f"规则 {rule.id} 的 max_amount"
        )                                            # 金额上限（None=不限）

        # 低于金额下限，不命中
        if amount_value < min_amount:
            continue
        # 设有金额上限且超出上限，不命中
        if max_amount is not None and amount_value > max_amount:
            continue

        # 角色与金额均匹配，记录命中规则并终止遍历
        matched_rule = rule                        # 命中规则
        break

    # 5. 命中自定义规则，先校验规则里配置的审批流"存在且已启用"，再返回
    # 规则和流程是两处独立配置:流程被停用或删掉后,规则里的 flow_id 会变成悬空指向
    if matched_rule and matched_rule.config and "flow_id" in matched_rule.config:
        # 取出规则配置里指定的审批流ID
        rule_flow_id = matched_rule.config["flow_id"]   # 目标审批流ID
        # 按ID + 启用状态查一次,查不到说明流程不存在或已停用
        rule_flow = db.query(ApprovalFlow).filter(   # 规则指向的流程
            ApprovalFlow.id == rule_flow_id,        # 规则指定的审批流ID
            ApprovalFlow.is_active == True          # 且该流程处于启用状态
        ).first()
        # 配置有误直接阻断:既不放行,也不偷偷改走默认流(否则大额单据可能被降级到简易流程)
        if not rule_flow:
            raise HTTPException(status_code=500, detail="规则命中的审批流不存在或已停用")
        return rule_flow.id

    # 6. 未命中任何规则，使用默认审批流兜底
    # 必须同时满足"是默认流"和"已启用":停用的默认流不能被绑定,否则单据会挂到一个已下线的流程上
    default_flow = db.query(ApprovalFlow).filter(   # 默认审批流
        ApprovalFlow.is_default == True,        # 是默认审批流
        ApprovalFlow.is_active == True          # 且流程处于启用状态
    ).first()
    # 默认流不存在、或唯一那条默认流已被停用,都按"没有可用默认流"处理,直接阻断提交
    if not default_flow:
        raise HTTPException(status_code=500, detail="未配置默认审批流")
    return default_flow.id



# 计算报销单总金额
def recalculate_expense_total_amount(expense: Expense, db: Session) -> None:
    """
    明细变动后重算报销单总金额，并把结果写回传入对象的 total_amount 字段
        为什么直接查库而不 sum(expense.items)：ORM 的 items 可能是旧快照（尤其是刚 flush/deleted 的场景），
        走一次 SUM 聚合能拿到数据库中的真实结果，保证主表金额与明细表一致。
        - 无副作用承诺：只改内存对象字段，不 commit、不 flush，由调用方决定事务边界；
        - 空值处理：明细被全部删除时 SUM 返回 None，这里统一落成 0，避免 NOT NULL 冲突。
        :param expense: 报销单 ORM 对象（必须已有 id）
        :param db: 数据库会话
        :return: 无返回值，直接修改 expense.total_amount
    """
    # 1.直接从数据库汇总该单据所有明细的金额，避开内存缓存
    # func.:把 Python 风格的代码，自动转换成对应数据库的原生 SQL 函数
    # 调用数据库的 SUM() 聚合函数，对 expense_items 表的 amount 字段求和
    # .scalar():把查询结果提取成单个数值。因为聚合查询返回的是结果集对象，直接取出第一行第一列的数值，也就是最终的总金额数字。
    total = db.query(func.sum(ExpenseItem.amount)).filter(   # 明细金额合计
        ExpenseItem.expense_id == expense.id
    ).scalar()

    # 2.处理空值：所有明细都删完时，总金额为0
    expense.total_amount = total if total is not None else 0   # 回写总金额
