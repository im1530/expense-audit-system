"""
报销模块核心路由（挂载前缀 /api/v1/expenses）
    覆盖报销单从"创建草稿"到"财务支付"的全生命周期，按能力分为七组：
        1. 主单管理：创建草稿、修改草稿、查看详情（内置归属权校验 + 状态前置校验）
        2. 三级列表：我的 / 本部门 / 全公司，各带差异化权限范围、多维筛选与统一排序分页
        3. 明细管理：单条明细的新增、修改、删除，操作后自动重算主单总金额
        4. 三级明细列表：我的 / 本部门 / 全公司，跨单据查询明细并联表带出单号与分类名
        5. 状态流转：提交送审、驳回后重提、撤回提交、主动撤销、财务支付，全部做状态机校验
        6. 基础字典：费用大类、报销状态（公开接口）+ 费用小类（需登录），供前端下拉与标签映射
        7. 审批轨迹：单张单据的完整审批历史（联表带出审批人姓名与角色，按层级与时间排序）
    通用约定：
        - 所有写操作都先做"单据归属 + 当前状态"双重校验，归属条件直接写进查询（查不到即 404），避免越权；
        - 单号、总金额、状态、审批层级等由后端生成/维护，一律不信任前端传值；
        - 路由注册顺序很重要：静态路径（/my、/items/my、/category 等）必须写在 /{expense_id} 之前，
          否则会被路径参数捕获；新增接口时请遵循现有的分组顺序。
"""



from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db                       # 数据库会话依赖
from app.api.deps import get_current_user                 # 当前登录用户依赖
from app.schemas.expense import ExpenseCreate, ExpenseResponse, ExpenseListResponse, ExpenseUpdate, ExpenseItemCreate, \
    ExpenseItemUpdate, CategoryResponse, DictOption, ExpenseItemListResponse
from app.models import User, Expense, ExpenseItem, Rule, ApprovalFlow, Approval, UserRole, Category, ApprovalStatus
from app.models.expense import ExpenseStatus, ExpenseType
import uuid
from datetime import datetime
from typing import List, Optional
from app.schemas.approval import ApprovalRecordItem
from app.schemas.common import PageParams,BusinessFilter
from app.utils.pagination import paginate_query,assemble_paginated_result
from app.services.expense_service import apply_business_filters, match_approval_flow, recalculate_expense_total_amount, \
    apply_item_business_filters
from app.services.rule_engine import run_rule_checks, save_rule_hits, summarize_level
from app.services.review_queue import trigger_auto_review
from app.schemas.rule import RulePrecheckResponse
from sqlalchemy import func, case
import logging



# 创建路由器对象
router = APIRouter()  # 路由对象

# 日志器：报销单状态流转是关键业务事件，每次流转都要留一条可追溯的记录
logger = logging.getLogger(__name__)  # 日志器



# 创建报销单的接口
@router.post(
    "/",                            # 接口路径；拼上总前缀后完整为 /api/v1/expense/ (和总路由拼接)
    response_model=ExpenseResponse,      # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                        # 在 /api/docs 文档里归到"报销"分组显示
    summary="创建报销单草稿",               # 接口标题
)
def create_expense(
        payload: ExpenseCreate,                             # 接收前端请求体，并按 ExpenseCreate 自动校验字段
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    创建报销单草稿（新建的单据状态为 draft，不进入审批流程）
    权限说明:
        - 需登录鉴权；提交人固定为当前登录用户（从令牌取），不接受前端传入，确保数据归属安全
    核心处理:
        - 逐条校验费用分类是否存在且启用，并把"不存在"与"已停用"区分成不同提示，便于前端定位；
        - 后端汇总明细金额得到总金额，前端传了也不采信，避免金额被篡改；
        - 单号规则：EXP + 年月日时分秒 + 6 位随机十六进制（同一秒内也不会重复）；
        - 通过 expense.items.append(...) 一次性级联写入主表与明细表，同一事务提交。
    异常: 400 明细分类不存在/已停用；422 请求体校验失败（如明细为空）
        :param payload: 创建入参（标题、说明、备注 + 至少一条明细）
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 新建的报销单（含明细），经 response_model=ExpenseResponse 输出
    """
    # 1.费用分类有效性校验:费用分类必须存在且已启用
    # 遍历所有前端传入的所有明细项 --- 用 enumerate 包裹后,可以同时拿到「这是第几个元素」
    for index, item_in in enumerate(payload.items, start=1):
        # 查询费用类别表,同时校验存在性和启用状态
        category = db.query(Category).filter(  # 查询费用分类
            Category.id == item_in.category_id,       # 费用分类ID必须匹配
            Category.is_active == True                         # 费用分类必须已启用
        ).first()

        # 如果查询结果为空，再区分不存在和已停用两种场景
        if not category:
            # 再次查询费用类别表,仅校验存在性
            exist_category = db.query(Category).filter(Category.id == item_in.category_id).first()  # 校验类别存在
            # 如果不存在，抛出400错误，提示费用分类不存在
            if not exist_category:
                raise HTTPException(status_code=400, detail=f"第{index}条明细的费用分类不存在")
            # 如果存在则说明已停用，抛出400错误，提示费用分类已停用
            else:
                raise HTTPException(status_code=400, detail=f"第{index}条明细的费用分类已停用,不可使用")


    # 2.汇总明细金额,作为报销单总金额(前端不传入,后端计算)
    total = sum(item.amount for item in payload.items)  # 明细金额合计


    # 3.报销单号：前缀 EXP + 时间戳 + 6位随机，避免重复(前端不传入,后端生成)
    expense_no = (  # 报销单号
            "EXP"                                       # 单号固定前缀，便于识别
            + datetime.now().strftime("%Y%m%d%H%M%S")   # 当前时间戳，保证单号大体按时间有序
            + uuid.uuid4().hex[:6].upper()              # 6位随机十六进制转大写，防止同一秒重复
    )


    # 4.构建报销单 --- user_id 取当前登录人,不让前端传
    expense = Expense(  # 报销单实体
        expense_no = expense_no,             # 报销单号
        title=payload.title,                 # 报销单标题
        description=payload.description,     # 报销说明
        remark=payload.remark,               # 报销单备注
        user_id=current_user.id,             # 单据归属当前登录用户，后端从token获取，不信任前端传入
        total_amount=total,                  # 报销总金额，后端自动汇总明细计算，防止前端篡改
    )


    # 5.把每条明细挂载这张报销单上 (挂上之后 expense_id 自动填充)
    for item_in in payload.items:
        # 先用 item_in.model_dump()：把Pydantic入参对象转为字典
        # 再用 ** 解包字典，作为关键字参数传给 ExpenseItem 数据库模型
        expense.items.append(ExpenseItem(**item_in.model_dump()))


    # 6.写入数据库的三步
    db.add(expense)        # 把对象加入会话(还没真正写入库)
    db.commit()            # 提交事务,真正落库,此时自增id生成
    db.refresh(expense)    # 回读数据库,把 id、默认值等刷新到 expense 对象上


    # 7. 返回；由 response_model=ExpenseResponse 过滤字段
    return expense



# 查询"我的"报销单列表的接口
@router.get(
    "/my",                                  # 接口路径；拼上总前缀后完整为 /api/v1/expense/my (和总路由拼接)
    response_model=ExpenseListResponse,          # 返回值按 ExpenseListResponse 格式化
    tags=["报销"],                                # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询我的报销单列表(所有人可用)",      # 接口标题
)
def list_my_expenses(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:报销单状态"),  # 筛选:报销单状态
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    分页查询"我的"报销单列表（所有登录角色可用）
    权限说明:
        - 需登录鉴权；数据范围强制限定为本人提交的单据（user_id == 当前用户），天然隔离
    筛选能力:
        - 通用筛选：费用大类、费用小类、创建日期区间、报销金额区间（见 apply_business_filters）
        - 专属筛选：按报销单状态
    排序与分页:
        - 草稿（submitted_at 为空）置顶，其余按提交时间倒序，最新提交的优先展示
        - 统一走 SQL 层分页（paginate_query），返回标准化分页结构
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按报销单状态筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: ExpenseListResponse 实例
    """
    # 1.构建基础查询:强制限定为当前用户本人提交的单据
    query = db.query(Expense).filter(Expense.user_id == current_user.id)  # 本人单据查询


    # 2.追加业务筛选条件(均为可选参数, 传值才生效)
    # 筛选:通用业务筛选条件
    query = apply_business_filters(query, common_filter)  # 追加通用筛选

    # 筛选:报销单状态
    if status is not None:
        query = query.filter(Expense.status == status)  # 按单据状态筛选


    # 3.排序：草稿置顶按创建时间倒序，已提交单据按提交时间倒序
    query = query.order_by(  # 排序条件
        # 第一优先级：空值（草稿）排在最前
        func.isnull(Expense.submitted_at).desc(),
        # 第二优先级：草稿按创建时间倒序，已提交按提交时间倒序
        case(
            (Expense.status == ExpenseStatus.DRAFT, Expense.created_at),
            else_=Expense.submitted_at
        ).desc()
    )


    # 4.分页计算:
    items, total = paginate_query(  # 分页查询
        query = query,                         # 查询对象
        page = page_params.page,               # 当前页码
        page_size = page_params.page_size,     # 每页条数
    )


    # 5.构建报销单列表
    my_expenses = assemble_paginated_result(  # 我的单据分页
        items = items,                          # 当前页的报销单列表（已分页）
        total = total,                          # 符合条件的总条数
        page = page_params.page,                # 当前页码
        page_size = page_params.page_size,      # 每页条数
        response_model=ExpenseListResponse,     # 响应模型
    )


    # 6.返回:由 response_model=ExpenseListResponse 过滤字段
    return my_expenses



# 查询"本部门"报销单列表的接口(仅manager可用)
@router.get(
    "/department",                           # 接口路径；拼上总前缀后完整为 /api/v1/expense/department (和总路由拼接)
    response_model=ExpenseListResponse,           # 返回值按 ExpenseListResponse 格式化
    tags=["报销"],                                 # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询本部门报销单列表(仅manager可用)", # 接口标题
)
def list_department_expenses(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:报销单状态"),  # 筛选:报销单状态
        user_id: Optional[int] = Query(None, ge=1, description="筛选:本部门内指定员工提交的报销单"),  # 筛选:本部门内指定员工提交的报销单
        db: Session = Depends(get_db),                                          # 注入数据库会话
        current_user: User = Depends(get_current_user)                          # 当前登录人（验签）
):
    """
    分页查询"本部门"报销单列表（仅 manager 角色可用）
    权限说明:
        - 需登录鉴权，且角色必须是 manager，否则 403
        - 数据范围：join 用户表后按"提交人部门 == 我的部门"强制过滤，实现部门间数据隔离
    关键约束:
        - 部门列表只展示已提交过的单据，统一排除草稿（草稿属于私人未完成内容）；
        - 若显式传 status=draft 直接返回 400，避免用户误以为能看到草稿；
        - user_id 筛选在本部门范围内生效，传其他部门的人查不到结果，天然挡住越权。
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按报销单状态筛选（不允许 draft）
        :param user_id: 可选，只看本部门内指定员工的单据
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 manager）
        :return: ExpenseListResponse 实例
        :raises HTTPException: 403 非 manager；400 试图查询草稿
    """
    # 1.角色校验
    if current_user.role not in (UserRole.MANAGER,):
        raise HTTPException(status_code=403, detail="仅manager可查询本部门报销单")


    # 2.构建基础查询:关联用户表,按部门强制限定取出的是本部门范围
    query = db.query(Expense).join(User, Expense.user_id == User.id).filter(  # 本部门单据查询
        User.department_id == current_user.department_id
    )
    # 强制权限兜底：部门列表仅可见已提交的单据，排除草稿状态
    query = query.filter(Expense.status != ExpenseStatus.DRAFT)  # 排除草稿


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_business_filters(query, common_filter)  # 追加通用筛选

    # 筛选:报销单状态
    if status is not None:
        if status == ExpenseStatus.DRAFT:
            raise HTTPException(status_code=400, detail="仅支持查询本部门已提交过的单据")
        query = query.filter(Expense.status == status)  # 按单据状态筛选

    # 筛选:本部门指定员工(仅在本部门范围内生效,跨部门员工ID自然查不到)
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选


    # 4.排序:默认按照提交时间倒序,最新提交的排在前面
    query = query.order_by(Expense.submitted_at.desc())  # 按提交时间倒序


    # 5.分页计算:
    items, total = paginate_query(  # 分页查询
        query=query,                        # 查询对象
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
    )

    # 6.构建报销单列表
    department_expenses = assemble_paginated_result(  # 本部门单据分页
        items=items,                        # 当前页的报销单列表（已分页）
        total=total,                        # 符合条件的总条数
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
        response_model=ExpenseListResponse, # 响应模型
    )

    # 7.返回:由 response_model=ExpenseListResponse 过滤字段
    return department_expenses



# 查询"全公司"报销单列表的接口(仅finance/boss/admin可用)
@router.get(
    "/company",                              # 接口路径；拼上总前缀后完整为 /api/v1/expense/all (和总路由拼接)
    response_model=ExpenseListResponse,           # 返回值按 ExpenseListResponse 格式化
    tags=["报销"],                                 # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询全公司报销单列表(仅finance/boss/admin可用)", # 接口标题
)
def list_company_expenses(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:报销单状态"),              # 按单据状态筛选
        user_id: Optional[int] = Query(None, ge=1, description="筛选:指定员工提交的报销单"),          # 按提交人筛选
        department_id: Optional[int] = Query(None, ge=1, description="筛选:指定部门的报销单"),       # 按部门筛选
        db: Session = Depends(get_db),                                          # 注入数据库会话
        current_user: User = Depends(get_current_user)                          # 当前登录人（验签）
):
    """
    分页查询"全公司"报销单列表（仅 finance / boss / admin 可用）
    权限说明:
        - 需登录鉴权，角色必须是 finance、boss、admin 之一，否则 403
        - 数据范围覆盖全公司所有部门，不做部门隔离（这是财务与高层的工作视图）
    筛选能力:
        - 通用筛选：费用大类、费用小类、创建日期区间、报销金额区间
        - 专属筛选：单据状态、指定提交人、指定部门（只有传了 department_id 才 join 用户表，省性能）
    排序与分页:
        - 已提交单据（submitted_at 非空）在前，内部再按提交时间倒序；草稿排在后面按创建时间倒序
        - 统一走 SQL 层分页
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按报销单状态筛选
        :param user_id: 可选，只看指定提交人的单据
        :param department_id: 可选，只看指定部门的单据
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 finance/boss/admin）
        :return: ExpenseListResponse 实例
        :raises HTTPException: 403 角色不符合
    """
    # 1.角色校验
    if current_user.role not in (UserRole.FINANCE, UserRole.BOSS, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="仅finance/boss/admin可查询全公司报销单列表")


    # 2.构建基础查询:默认无强制数据范围,返回全公司报销单
    query = db.query(Expense)  # 全公司单据查询


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_business_filters(query, common_filter)  # 追加通用筛选

    # 筛选:报销单状态
    if status is not None:
        query = query.filter(Expense.status == status)  # 按单据状态筛选

    # 筛选:指定提交人
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选

    # 筛选:指定部门
    # 部门信息存储在用户表中，需要关联用户表才能按部门过滤报销单
    if department_id is not None:
        query = query.join(User, Expense.user_id == User.id).filter(  # 关联用户表（按部门筛选）
            User.department_id == department_id  # 按部门筛选
        )


    # 4.排序:默认按照提交时间倒序,最新提交的排在前面
    query = query.order_by(  # 排序条件
        # 第一优先级：非空提交时间（已提交）排在前面
        func.isnull(Expense.submitted_at).asc(),
        # 第二优先级：已提交按提交时间倒序，草稿按创建时间倒序
        case(
            (Expense.status == ExpenseStatus.DRAFT, Expense.created_at),
            else_=Expense.submitted_at
        ).desc()
    )


    # 5.分页计算:
    items, total = paginate_query(  # 分页查询
        query=query,  # 查询对象
        page=page_params.page,  # 当前页码
        page_size=page_params.page_size,  # 每页条数
    )


    # 6.构建报销单列表
    all_expenses = assemble_paginated_result(  # 全公司单据分页
        items=items,                            # 当前页的报销单列表（已分页）
        total=total,                            # 符合条件的总条数
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size,        # 每页条数
        response_model=ExpenseListResponse,     # 响应模型
    )


    # 7.返回:由 response_model=ExpenseListResponse 过滤字段
    return all_expenses



# 分页查询"我的"报销明细列表的接口(所有人可用)
@router.get(
    "/items/my",                              # 接口路径；拼上总前缀后完整为 /api/v1/expense/items/my (和总路由拼接)
    response_model=ExpenseItemListResponse,         # 返回值按 ExpenseItemListResponse 格式化
    tags=["报销"],                                  # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询我的报销明细列表(所有人可用)",      # 接口标题
)
def list_my_expense_items(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:所属报销单状态"),  # 筛选:报销单状态
        db: Session = Depends(get_db),                      # 依赖注入:数据库会话
        current_user: User = Depends(get_current_user)      # 依赖注入:当前登录人（验签）
):
    """
    分页查询"我的"报销明细列表（所有登录角色可用）
    权限说明:
        - 需登录鉴权；数据范围强制限定为本人提交单据下的明细（Expense.user_id == 当前用户）
    查询结构:
        - 以 ExpenseItem 为主表，join 报销单表（用于权限与状态过滤）和费用类别表（用于分类筛选与展示）；
        - 联查结果是 (明细对象, 单号, 分类名, 费用大类) 四元组，循环时手工组装成出参字典。
    筛选能力:
        - 通用筛选：费用大类、费用小类、费用发生日期区间、单条明细金额区间
        - 专属筛选：按所属报销单状态
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按所属报销单状态筛选
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: ExpenseItemListResponse 实例
    """
    # 1.构建基础查询:关联报销单表、费用类别表，补全展示字段，同时支撑后续权限与筛选逻辑
    query = db.query(  # 明细联查
        ExpenseItem,                            # 明细主表:核心业务字段来源
        Expense.expense_no,                     # 关联报销单号:用于标识所属报销单
        Category.name.label("category_name"),   # 关联费用类别名称:前端展示中文
        Category.expense_type                   # 关联费用大类
    ).join(
        Expense, ExpenseItem.expense_id == Expense.id       # 关联报销单表:用于权限和状态过滤
    ).join(
        Category, ExpenseItem.category_id == Category.id    # 关联费用类别表:用于费用大类和费用小类筛选
    )


    # 2.权限过滤:只能查看当前用户的明细
    query = query.filter(Expense.user_id == current_user.id)  # 仅本人单据


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_item_business_filters(query, common_filter)  # 追加明细筛选

    # 筛选:报销单状态
    if status is not None:
        query = query.filter(Expense.status == status)  # 按单据状态筛选


    # 4.排序:默认按照费用发生日期倒序排列,同日期按明细ID倒序
    query = query.order_by(ExpenseItem.expense_date.desc(), ExpenseItem.id.desc())  # 按费用日期倒序


    # 5.分页计算
    item_list, total = paginate_query(  # 明细分页
        query=query,                        # 查询对象
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
    )


    # 6.组装明细数据结构:多表联查结果为元组,需要手动转换响应模型匹配的字典结构
    result_items = []  # 明细结果列表
    for item, expense_no, category_name, expense_type in item_list:
        result_items.append({
            "id": item.id,                                  # 明细ID
            "expense_id": item.expense_id,                  # 所属报销单ID
            "expense_no": expense_no,                       # 所属报销单号
            "category_id": item.category_id,                # 费用类别ID
            "category_name": category_name,                 # 费用类别名称(小类)
            "expense_type": expense_type.value,             # 费用大类(枚举对象取value转字符串，匹配响应模型类型)
            "description": item.description,                # 费用说明
            "amount": item.amount,                          # 费用金额
            "expense_date": item.expense_date,              # 费用发生日期
            "invoice_no": item.invoice_no,                  # 发票编号
            "invoice_url": item.invoice_url,                # 发票文件URL
            "created_at": item.created_at,                  # 创建时间
        })


    # 7.组装完整分页结果
    my_expense_items = assemble_paginated_result(  # 我的明细分页
        items=result_items,                             # 当前页的明细数据列表
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=ExpenseItemListResponse,         # 响应模型
    )


    # 8.返回:由 response_model=ExpenseItemListResponse 过滤字段
    return my_expense_items



# 分页查询"本部门"的报销明细列表的接口(仅manager可用)
@router.get(
    "/items/department",                       # 接口路径；拼上总前缀后完整为 /api/v1/expense/items/department
    response_model=ExpenseItemListResponse,          # 返回值按报销明细分页响应模型格式化
    tags=["报销"],                                    # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询本部门报销明细列表(仅manager可用)",  # 接口标题
)
def list_department_expense_items(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:所属报销单状态"),  # 筛选:报销单状态
        user_id: Optional[int] = Query(None, ge=1, description="筛选:本部门内指定员工的报销明细"),  # 筛选:本部门内指定员工
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    分页查询"本部门"报销明细列表（仅 manager 可用）
    权限说明:
        - 需登录鉴权，角色必须是 manager，否则 403
        - 数据范围：join 用户表后按"提交人部门 == 我的部门"强制过滤，实现部门隔离
    关键约束:
        - 统一排除草稿单据的明细；显式传 status=draft 时返回 400
        - user_id 筛选只在本部门范围内生效
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按所属报销单状态筛选（不允许 draft）
        :param user_id: 可选，只看本部门内指定员工的明细
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 manager）
        :return: ExpenseItemListResponse 实例
        :raises HTTPException: 403 非 manager；400 试图查询草稿
    """
    # 1.角色校验：非经理角色直接拦截
    if current_user.role not in (UserRole.MANAGER,):
        raise HTTPException(status_code=403, detail="仅manager可查询本部门报销明细")


    # 2.构建基础查询:关联报销单表、用户表、费用类别表，按部门强制限定数据范围
    query = db.query(  # 明细联查
        ExpenseItem,                            # 明细主表:核心业务字段来源
        Expense.expense_no,                     # 关联报销单号:用于标识所属报销单
        Category.name.label("category_name"),   # 关联费用类别名称:前端展示中文
        Category.expense_type                   # 关联费用大类
    ).join(
        Expense, ExpenseItem.expense_id == Expense.id       # 关联报销单表:用于权限和状态过滤
    ).join(
        User, Expense.user_id == User.id                    # 关联用户表:用于部门范围过滤
    ).join(
        Category, ExpenseItem.category_id == Category.id    # 关联费用类别表:用于分类筛选
    ).filter(
        User.department_id == current_user.department_id    # 部门隔离:仅本部门数据
    )
    # 强制权限兜底：仅可见已提交单据的明细，排除草稿单据的明细
    query = query.filter(Expense.status != ExpenseStatus.DRAFT)  # 排除草稿


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_item_business_filters(query, common_filter)  # 追加明细筛选

    # 筛选:所属报销单状态
    if status is not None:
        if status == ExpenseStatus.DRAFT:
            raise HTTPException(status_code=400, detail="仅支持查询本部门已提交过的报销单明细")
        query = query.filter(Expense.status == status)  # 按单据状态筛选

    # 筛选:本部门指定员工(仅在本部门范围内生效,跨部门员工ID自然查不到结果)
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选


    # 4.排序:默认按照费用发生日期倒序排列,同日期按明细ID倒序
    query = query.order_by(ExpenseItem.expense_date.desc(), ExpenseItem.id.desc())  # 按费用日期倒序

    # 5.分页计算：复用统一分页工具
    item_list, total = paginate_query(  # 明细分页
        query=query,                        # 查询对象
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
    )

    # 6.组装明细数据结构:多表联查结果为元组,手动映射为响应模型匹配的字典结构
    result_items = []  # 明细结果列表
    for item, expense_no, category_name, expense_type_enum in item_list:
        result_items.append({
            "id": item.id,                                  # 明细ID
            "expense_id": item.expense_id,                  # 所属报销单ID
            "category_id": item.category_id,                # 费用类别ID(继承必填字段)
            "expense_no": expense_no,                       # 所属报销单号
            "category_name": category_name,                 # 费用类别名称(小类)
            "expense_type": expense_type_enum.value,        # 费用大类(枚举转字符串)
            "description": item.description,                # 费用说明
            "amount": item.amount,                          # 费用金额
            "expense_date": item.expense_date,              # 费用发生日期
            "invoice_no": item.invoice_no,                  # 发票编号
            "invoice_url": item.invoice_url,                # 发票文件URL
            "created_at": item.created_at,                  # 创建时间
        })

    # 7.组装完整分页结果
    department_expense_items = assemble_paginated_result(  # 本部门明细分页
        items=result_items,                             # 当前页的明细数据列表
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=ExpenseItemListResponse,         # 分页响应模型
    )

    # 8.返回:由 response_model 统一校验字段格式
    return department_expense_items


# 分页查询"全公司"的报销明细列表的接口(仅finance/boss/admin可用)
# 查询"全公司"报销明细列表的接口(仅finance/boss/admin可用)
@router.get(
    "/items/company",                        # 接口路径；拼上总前缀后完整为 /api/v1/expense/items/company
    response_model=ExpenseItemListResponse,  # 返回值按报销明细分页响应模型格式化
    tags=["报销"],                            # 在 /api/docs 文档里归到"报销"分组显示
    summary="分页查询全公司报销明细列表(仅finance/boss/admin可用)",  # 接口标题
)
def list_company_expense_items(
        page_params: PageParams = Depends(),                # 分页参数
        common_filter: BusinessFilter = Depends(),          # 通用业务筛选参数
        status: Optional[ExpenseStatus] = Query(None, description="筛选:所属报销单状态"),  # 筛选:报销单状态
        user_id: Optional[int] = Query(None, ge=1, description="筛选:指定员工的报销明细"),  # 筛选:指定提交员工
        department_id: Optional[int] = Query(None, ge=1, description="筛选:指定部门的报销明细"),  # 筛选:指定所属部门
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    分页查询"全公司"报销明细列表（仅 finance / boss / admin 可用）
    权限说明:
        - 需登录鉴权，角色必须是 finance、boss、admin 之一，否则 403
        - 数据范围覆盖全公司，不做部门隔离
    筛选能力:
        - 通用筛选：费用大类、费用小类、费用发生日期区间、单条明细金额区间
        - 专属筛选：所属报销单状态、指定提交员工、指定部门（传 department_id 时才 join 用户表）
        :param page_params: 分页参数
        :param common_filter: 通用业务筛选参数
        :param status: 可选，按所属报销单状态筛选
        :param user_id: 可选，只看指定员工的明细
        :param department_id: 可选，只看指定部门的明细
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 finance/boss/admin）
        :return: ExpenseItemListResponse 实例
        :raises HTTPException: 403 角色不符合
    """
    # 1.角色校验：非授权角色直接拦截
    if current_user.role not in (UserRole.FINANCE, UserRole.BOSS, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="仅finance/boss/admin可查询全公司报销明细")


    # 2.构建基础查询:关联报销单表、费用类别表，补全展示与筛选所需字段
    query = db.query(  # 明细联查
        ExpenseItem,                            # 明细主表:核心业务字段来源
        Expense.expense_no,                     # 关联报销单号:用于标识所属报销单
        Category.name.label("category_name"),   # 关联费用类别名称:前端展示中文
        Category.expense_type                   # 关联费用大类
    ).join(
        Expense, ExpenseItem.expense_id == Expense.id       # 关联报销单表:用于状态、提交人过滤
    ).join(
        Category, ExpenseItem.category_id == Category.id    # 关联费用类别表:用于分类筛选
    )


    # 3.追加业务筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:通用业务筛选条件
    query = apply_item_business_filters(query, common_filter)  # 追加明细筛选

    # 筛选:所属报销单状态
    if status is not None:
        query = query.filter(Expense.status == status)  # 按单据状态筛选

    # 筛选:指定提交员工
    if user_id is not None:
        query = query.filter(Expense.user_id == user_id)  # 按提交人筛选

    # 筛选:指定部门
    # 部门信息存储在用户表中，按需关联用户表实现按部门过滤明细
    if department_id is not None:
        query = query.join(User, Expense.user_id == User.id).filter(  # 关联用户表（按部门筛选）
            User.department_id == department_id  # 按部门筛选
        )


    # 4.排序:默认按照费用发生日期倒序排列,同日期按明细ID倒序
    query = query.order_by(ExpenseItem.expense_date.desc(), ExpenseItem.id.desc())  # 按费用日期倒序


    # 5.分页计算：复用统一分页工具
    item_list, total = paginate_query(  # 明细分页
        query=query,                        # 查询对象
        page=page_params.page,              # 当前页码
        page_size=page_params.page_size,    # 每页条数
    )


    # 6.组装明细数据结构:多表联查结果为元组,手动映射为响应模型匹配的字典结构
    result_items = []  # 明细结果列表
    for item, expense_no, category_name, expense_type_enum in item_list:
        result_items.append({
            "id": item.id,                                  # 明细ID
            "expense_id": item.expense_id,                  # 所属报销单ID
            "category_id": item.category_id,                # 费用类别ID(继承必填字段)
            "expense_no": expense_no,                       # 所属报销单号
            "category_name": category_name,                 # 费用类别名称(小类)
            "expense_type": expense_type_enum.value,        # 费用大类(枚举转字符串)
            "description": item.description,                # 费用说明
            "amount": item.amount,                          # 费用金额
            "expense_date": item.expense_date,              # 费用发生日期
            "invoice_no": item.invoice_no,                  # 发票编号
            "invoice_url": item.invoice_url,                # 发票文件URL
            "created_at": item.created_at,                  # 创建时间
        })


    # 7.组装完整分页结果
    company_expense_items = assemble_paginated_result(  # 全公司明细分页
        items=result_items,                             # 当前页的明细数据列表
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=ExpenseItemListResponse,         # 分页响应模型
    )


    # 8.返回:由 response_model 统一校验字段格式
    return company_expense_items



# 获取全部已启用的费用类别(小类)的分类列表的接口
@router.get(
    "/category",                               # 接口路径；拼上总前缀后完整为 /api/v1/expense/category (和总路由拼接)
    response_model=List[CategoryResponse],           # 返回值按 List[CategoryResponse] 格式化
    tags=["报销"],                                   # 在 /api/docs 文档里归到"报销"分组显示
    summary="获取全部已启用的费用类别(小类)的分类列表",    # 接口标题
)
def get_category_list(
        db: Session = Depends(get_db),                      # 依赖注入:数据库会话
        current_user: User = Depends(get_current_user)      # 依赖注入:当前登录人（验签）
):
    """
    获取所有已启用的费用分类（小类）字典数据（需登录）
    业务用途:
        - 报销明细表单的「费用类别」下拉框数据源
        - 报销单/明细列表的分类筛选器数据源
        - 前端可按 expense_type 做"大类 → 小类"级联
    过滤规则:
        - 只返回 is_active = True 的启用分类，避免出现无效可选值
        - 按分类 ID 升序排列，保证下拉顺序稳定
        :param db: 数据库会话
        :param current_user: 当前登录用户（只校验登录态）
        :return: 费用分类列表，由 response_model=List[CategoryResponse] 裁剪字段
    """
    # 1.查询所有已启用的费用分类,并按费用类别ID升序排列
    categories = db.query(Category).filter(  # 启用类别查询
        Category.is_active == True
    ).order_by(Category.id.asc()).all()

    # 2.返回:由 response_model=List[CategoryResponse] 统一转换并过滤字段
    return categories



# 获取费用类别(大类)枚举列表的接口
@router.get(
    path="/expense-types",                    # 接口路径；拼上总前缀后完整为 /api/v1/expenses/expense-types
    response_model=List[DictOption],          # 返回值按 List[DictOption] 格式化
    tags=["报销"],                             # 在 /api/docs 文档里归到"报销"分组显示
    summary="获取费用类别(大类)枚举列表",           # 接口标题
)
def get_expense_type_list():
    """
    获取费用性质大类枚举选项（公开接口，无需登录）
    业务用途:
        - 列表页按大类筛选的筛选器数据源
        - 新建报销时「大类 → 小类」级联选择的一级选项
        - 前端费用类型标签的中文映射
    说明:
        - 枚举项与 ExpenseType 定义完全一致，按枚举定义顺序返回
        - 中文名称集中映射在 type_label_map，新增枚举值时必须同步补充，否则会触发 KeyError
        :return: 字典列表，元素为 {"value": 枚举值, "label": 中文名}
    """
    # 1.枚举值与中文名映射,与枚举定义语义一一对应
    type_label_map = {  # 费用大类中文映射
        ExpenseType.MEAL: "餐饮费",
        ExpenseType.TRANSPORTATION: "交通费",
        ExpenseType.ACCOMMODATION: "住宿费",
        ExpenseType.OFFICE: "办公费",
        ExpenseType.OTHER: "其他",
    }

    # 2.遍历所有枚举成员,组装成标准选项格式
    type_list = [  # 费用大类选项
        {"value": type_item.value, "label": type_label_map[type_item]} for type_item in ExpenseType
    ]

    # 3.返回:由 response_model=List[DictOption] 统一转换并过滤字段
    return type_list



# 获取报销状态枚举列表的接口
@router.get(
    path="/statuses",                           # 接口路径；拼上总前缀后完整为 /api/v1/expense/statuses（和总路由拼接）
    response_model=List[DictOption],            # 返回值按 List[DictOption] 格式化
    tags=["报销"],                               # 在 /api/docs 文档里归到"报销"分组显示
    summary="获取报销状态枚举列表",                 # 接口标题
)
def get_expense_status_list():
    """
    获取报销状态枚举选项（公开接口，无需登录）
    业务用途:
        - 列表页按状态筛选的筛选器数据源
        - 单据列表、详情页的状态标签映射
    说明:
        - 枚举项与 ExpenseStatus 定义完全一致，按枚举定义顺序返回
        - 中文名称集中映射在 status_label_map，新增状态时必须同步补充，否则会触发 KeyError
        :return: 字典列表，元素为 {"value": 枚举值, "label": 中文名}
    """
    # 1.枚举值与中文名映射,与枚举定义注释一一对应
    status_label_map = {  # 状态中文映射
        ExpenseStatus.DRAFT: "草稿",
        ExpenseStatus.PENDING: "待审批",
        ExpenseStatus.APPROVED: "已通过",
        ExpenseStatus.REJECTED: "已驳回",
        ExpenseStatus.PAID: "已支付",
        ExpenseStatus.CANCELLED: "已取消",
    }

    # 2.遍历所有枚举成员,组装成标准选项格式
    status_list = [  # 状态字典列表
        {"value": status.value, "label": status_label_map[status]} for status in ExpenseStatus
    ]

    # 3.返回:由 response_model=List[ExpenseStatusOption] 统一转换并过滤字段
    return status_list



# 修改报销单草稿的接口
@router.put(
    "/{expense_id}",                # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id} (和总路由拼接)
    response_model=ExpenseResponse,      # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                        # 在 /api/docs 文档里归到"报销"分组显示
    summary="修改报销单草稿",               # 接口标题
)
def update_expense(
        expense_id: int,                                # 路径参数，要更新的报销单ID
        payload: ExpenseUpdate,                         # 接收前端请求体，并按 ExpenseUpdate 自动校验字段
        db: Session = Depends(get_db),                  # 注入数据库会话
        current_user: User = Depends(get_current_user)  # 当前登录人（验签）
):
    """
    修改指定报销单的主信息（增量更新，不改明细）
    权限说明:
        - 需登录鉴权，且只能改本人提交的单据——归属条件直接拼进查询，查不到即 404（不区分"不存在"与"无权"）
    校验规则:
        - 只允许草稿与已驳回状态修改；待审批、已通过、已支付、已取消一律 409
        - 用 exclude_unset=True 只取前端真正提交的字段，避免把未传字段覆盖成 None
        - 显式丢弃 items：明细的增删改由独立接口负责，防止两套逻辑互相覆盖
    业务场景:
        - 草稿态：编辑尚未提交的单据；驳回态：按审批意见修改后准备重新提交
        :param expense_id: 路径参数，要修改的报销单ID
        :param payload: 主单更新入参（字段全部可选）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 更新后的报销单完整信息
        :raises HTTPException: 404 单据不存在或无权修改；409 当前状态不允许修改
    """
    # 1.归属校验:查询条件同时带[报销单ID + 当前登录人ID],一步完成归属校验，查不到 -> 404,不存在或无权
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,      # 报销单ID
        Expense.user_id == current_user.id      # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权修改")

    # 2.状态校验:只有草稿和已驳回状态才能修改,其他状态无法修改
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以修改")

    # 3.更新:只取前端实际传入的字段
    # model_dump()：把Pydantic入参对象转为字典; exclude_unset=True：只包含传入的字段，忽略没传的，避免把可选字段改设成 None
    update_data = payload.model_dump(exclude_unset=True)  # 待更新字段

    # 4.不改动报销项目明细(更新报销项目明细在后续单独的接口中完成)
    update_data.pop("items", None)  #, None：兜底参数 —— 如果字典里根本没有 items，不报错、返回 None，而不是抛 KeyError

    # 5.动态的逐字段写回对象(前端这次传了哪几个字段不固定，用循环 + setattr 进行通用处理)
    for key, value in update_data.items():   # .items():字典的内置方法，返回所有 (键, 值)对，用于遍历
        setattr(expense, key, value)         # setattr(对象, "属性名(字符串)", 要赋的值)

    # 6.更新数据库的两步
    db.commit()            # 提交事务,真正落库
    db.refresh(expense)    # 回读数据库,把 更新后的字段值 刷新到 expense 对象上

    # 7.返回；由 response_model=ExpenseResponse 过滤字段
    return expense



# 查询某一张报销单详情的接口
@router.get(
    "/{expense_id}",                # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id} (和总路由拼接)
    response_model=ExpenseResponse,      # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                        # 在 /api/docs 文档里归到"报销"分组显示
    summary="查询报销单的详细信息",          # 接口标题
)
def get_expense(
        expense_id: int,                                # 路径参数，要查看详情的报销单ID
        db: Session = Depends(get_db),                  # 注入数据库会话
        current_user: User = Depends(get_current_user)  # 当前登录人（验签）
):
    """
    查询指定报销单的详情（主信息 + 明细列表）
    权限说明:
        - 按角色动态收窄可见范围：employee 仅本人；manager 本人 + 本部门；finance/boss/admin 全公司
    安全处理:
        - 单据不存在与无权限统一返回 404，不泄露单据是否真实存在
        - 权限条件直接拼进查询，而不是先查出来再判断，避免把无权数据加载进内存
        :param expense_id: 路径参数，要查看的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 报销单详情（含明细），经 response_model=ExpenseResponse 输出
        :raises HTTPException: 404 单据不存在或无权查看
    """
    # 1. 根据角色动态构建查询条件
    query = db.query(Expense).filter(Expense.id == expense_id)  # 按ID查询单据

    if current_user.role == UserRole.EMPLOYEE:
        # 普通员工: 只能看自己的
        query = query.filter(Expense.user_id == current_user.id)  # 仅本人单据

    elif current_user.role == UserRole.MANAGER:
        # 部门经理: 能看自己的 + 本部门员工的
        # 关联用户表,过滤提交人部门与当前用户部门一致
        query = query.join(User, Expense.user_id == User.id).filter(  # 关联用户表（按部门筛选）
            (Expense.user_id == current_user.id) |
            (User.department_id == current_user.department_id)
        )

    # finance / boss / admin 角色: 无额外过滤,可查看所有单据

    # 2. 执行查询
    expense = query.first()  # 单据实体

    # 3. 查不到(不存在或无权限)统一返回404,不泄露单据是否存在
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权查看")

    # 4. 返回单据详情
    return expense



# 新增单条报销单明细的接口
@router.post(
    "/items",                           # 接口路径；拼上总前缀后完整为 /api/v1/expense/items (和总路由拼接)
    response_model=ExpenseResponse,          # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                            # 在 /api/docs 文档里归到"报销"分组显示
    summary="新增单条报销单明细",               # 接口标题
)
def add_expense_item(
        expense_id: int,                                # 查询参数，要新增明细的报销单ID(本接口挂在 POST /items 上，单据ID走 query)
        payload: ExpenseItemCreate,                     # 接收前端请求体，并按 ExpenseItemCreate 自动校验字段
        db: Session = Depends(get_db),                  # 注入数据库会话
        current_user: User = Depends(get_current_user)  # 当前登录人（验签）
):
    """
    为指定报销单追加一条费用明细
    权限说明:
        - 需登录鉴权，且只能操作本人提交的单据（归属条件拼进查询，查不到即 404）
    校验规则:
        - 只允许草稿与已驳回状态新增明细，其余状态 409
        - 本次提交的单条明细，其费用分类必须存在且启用，否则 400（区分"不存在"与"已停用"）
        - 金额为正、费用发生日期不晚于今天等字段规则由 ExpenseItemCreate 统一校验
    业务处理:
        - 明细挂到当前单据后先 flush，确保后续 SUM 能统计到新明细
        - 用 recalculate_expense_total_amount 重算主单总金额，保证主表与明细一致
        - 明细的 expense_id 由关系自动填充，前端无需也不应传入
        :param expense_id: 查询参数（本接口挂在 POST /items 上，单据ID通过 query 传入），目标报销单ID
        :param payload: 单条明细入参（分类、说明、金额、发生日期、发票信息）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 新增明细后的报销单完整信息（含全部明细与最新总金额）
        :raises HTTPException: 404 单据不存在或无权操作；409 状态不允许；400 分类无效
    """
    # 1.归属校验:同时按[报销单ID + 当前登录人ID]过滤,一步完成存在性和权限校验
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,      # 报销单ID
        Expense.user_id == current_user.id      # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")


    # 2.状态校验:只有草稿和已驳回状态才能新增明细,其他状态无法新增
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以新增明细")


    # 3.费用分类有效性校验:本接口一次只加一条明细,直接校验 payload.category_id 对应的分类(不用遍历列表)
    category = db.query(Category).filter(  # 查询费用类别
        Category.id == payload.category_id,   # 费用分类ID必须匹配
        Category.is_active == True            # 费用分类必须已启用
    ).first()

    # 如果查询结果为空，再区分不存在和已停用两种场景
    if not category:
        # 再次查询费用类别表,仅校验存在性
        exist_category = db.query(Category).filter(Category.id == payload.category_id).first()  # 校验类别存在
        # 如果不存在，抛出400错误，提示费用分类不存在
        if not exist_category:
            raise HTTPException(status_code=400, detail="该明细的费用分类不存在")
        # 如果存在则说明已停用，抛出400错误，提示费用分类已停用
        else:
            raise HTTPException(status_code=400, detail="该明细的费用分类已停用,不可使用")


    # 4.创建明细对象,挂载到当前报销单
    # .model_dump():将入参模型转为字典;解包后初始化ORM对象,字段一一对应
    new_item = ExpenseItem(**payload.model_dump())  # 新建明细实体
    expense.items.append(new_item)


    # 5.先把明细变更刷入数据库事务，确保后续重算能拿到最新数据
    db.flush()


    # 6.新增明细后,自动重算报销单总金额,并同步更新主表数据
    recalculate_expense_total_amount(expense,db)


    # 7.更新数据库的两步
    db.commit()             # 提交事务,真正落库
    db.refresh(expense)     # 回读数据库,把 更新后的字段值 刷新到 expense 对象上


    # 8.返回:由 response_model=ExpenseResponse 过滤字段
    return expense



# 修改单条报销单明细的接口
@router.put(
    "/{expense_id}/items/{item_id}",    # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/items/{item_id} (和总路由拼接)
    response_model=ExpenseResponse,           # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                            # 在 /api/docs 文档里归到"报销"分组显示
    summary="修改单条报销单明细",               # 接口标题
)
def update_expense_item(
        expense_id: int,                                # 路径参数，要修改的报销单ID
        item_id: int,                                   # 路径参数，要修改的明细ID
        payload: ExpenseItemUpdate,                     # 接收前端请求体，并按 ExpenseItemUpdate 自动校验字段
        db: Session = Depends(get_db),                  # 注入数据库会话
        current_user: User = Depends(get_current_user)  # 当前登录人（验签）
):
    """
    修改指定报销单中的某一条费用明细
    权限说明:
        - 需登录鉴权，且只能操作本人提交的单据；再叠加"明细必须属于该单据"的第二层校验
    双重校验:
        - 第一层：按 [单据ID + 当前用户ID] 查单，一次完成存在性与归属校验，查不到 404
        - 第二层：按 [明细ID + 单据ID] 查明细，防止拿别的单据的明细ID来越权修改，查不到 404
    校验规则:
        - 只允许草稿与已驳回状态修改明细，其余状态 409
        - 只有本次真正传了 category_id（即改了分类）才校验"该分类存在且启用"，否则 400；不传则沿用原分类
    业务处理:
        - 增量更新（exclude_unset=True），未传字段保持原值
        - 先 flush 再重算总金额，保证 SUM 读到的是修改后的数据
        :param expense_id: 路径参数，所属报销单ID
        :param item_id: 路径参数，要修改的明细ID
        :param payload: 明细更新入参（字段全部可选）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 修改后的报销单完整信息
        :raises HTTPException: 404 单据/明细不存在或无权操作；409 状态不允许；400 分类无效
    """
    # 1.第一层校验:报销单归属和状态校验
    # 1.1 归属校验:按[报销单ID + 当前登录人ID]过滤,一步完成存在性和权限校验
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,  # 报销单ID
        Expense.user_id == current_user.id  # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")

    # 1.2 状态校验:只有草稿、已驳回状态的报销单允许修改明细,其他状态无法修改明细
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以修改明细")


    # 2.第二层校验:明细归属双层校验
    # 查询目标明细，同时校验明细所属报销单ID，防止传入其他报销单的明细ID越权操作
    item = db.query(ExpenseItem).filter(  # 查询明细
        ExpenseItem.id == item_id,    # 明细ID
        ExpenseItem.expense_id == expense_id   # 明细所属报销单ID
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="该条明细不存在或不属于当前报销单")


    # 3.增量更新明细字段:
    # exclude_unset=True: 只更新前端主动传入的字段，未传的字段保持原值
    update_data = payload.model_dump(exclude_unset=True)  # 待更新字段

    # 3.1 若本次改了费用分类，校验新分类有效性:必须存在且已启用
    # 未传 category_id 表示不改分类、沿用原值，这种情况不需要查库校验
    if payload.category_id is not None:
        category = db.query(Category).filter(  # 查询费用分类
            Category.id == payload.category_id,   # 费用分类ID必须匹配
            Category.is_active == True            # 费用分类必须已启用
        ).first()

        # 如果查询结果为空，再区分不存在和已停用两种场景
        if not category:
            # 再次查询费用类别表,仅校验存在性
            exist_category = db.query(Category).filter(Category.id == payload.category_id).first()  # 校验类别存在
            # 如果不存在，抛出400错误，提示费用分类不存在
            if not exist_category:
                raise HTTPException(status_code=400, detail="该明细的费用分类不存在")
            # 如果存在则说明已停用，抛出400错误，提示费用分类已停用
            else:
                raise HTTPException(status_code=400, detail="该明细的费用分类已停用,不可使用")

    # 3.2 动态逐字段更新对象,兼容前端任意字段组合的修改
    for key, value in update_data.items():
        setattr(item, key, value)


    # 4.先把明细变更刷入数据库事务，确保后续重算能拿到最新数据
    db.flush()


    # 5.明细修改后,自动重算报销单总金额,并同步更新主表数据
    recalculate_expense_total_amount(expense,db)


    # 6.更新数据库的两步
    db.commit()             # 提交事务,真正落库
    db.refresh(expense)     # 回读数据库,把 更新后的字段值 刷新到 expense 对象上


    # 7.返回:由 response_model=ExpenseResponse 过滤字段
    return expense



# 删除单条报销单明细的接口
@router.delete(
    "/{expense_id}/items/{item_id}",  # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/items/{item_id} (和总路由拼接)
    response_model=ExpenseResponse,         # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                           # 在 /api/docs 文档里归到"报销"分组显示
    summary="删除单条报销单明细",              # 接口标题
)
def delete_expense_item(
        expense_id: int,                                # 路径参数，要删除的报销单ID
        item_id: int,                                   # 路径参数，要删除的明细ID
        db: Session = Depends(get_db),                  # 注入数据库会话
        current_user: User = Depends(get_current_user)  # 当前登录人（验签）
):
    """
    删除指定报销单中的某一条费用明细
    权限说明:
        - 需登录鉴权，且只能操作本人提交的单据；明细同样要有"属于该单据"的第二层校验
    校验规则:
        - 只允许草稿与已驳回状态删除明细，其余状态 409
        - 明细不存在或不属于当前单据时一律 404
    业务处理:
        - 先 delete + flush，让 SUM 立刻反映删除结果，再重算主单总金额
        - 删到最后一条时总金额会归零（由服务层兜底处理 None），单据仍可保留
        :param expense_id: 路径参数，所属报销单ID
        :param item_id: 路径参数，要删除的明细ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 删除明细后的报销单完整信息（含剩余明细与最新总金额）
        :raises HTTPException: 404 单据/明细不存在或无权操作；409 状态不允许
    """
    # 1.第一层校验:报销单归属和状态校验
    # 1.1 归属校验:按[报销单ID + 当前登录人ID]过滤,一步完成存在性和权限校验
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,  # 报销单ID
        Expense.user_id == current_user.id  # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")

    # 1.2 状态校验:只有草稿、已驳回状态的报销单允许修改明细,其他状态无法修改明细
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿和已驳回状态的报销单可以修改明细")


    # 2.第二层校验:明细归属双层校验
    # 查询目标明细，同时校验明细所属报销单ID，防止传入其他单据的明细ID越权操作
    item = db.query(ExpenseItem).filter(  # 查询明细
        ExpenseItem.id == item_id,  # 明细ID
        ExpenseItem.expense_id == expense_id  # 明细所属报销单ID
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="该条明细不存在或不属于当前报销单")


    # 3.删除目标明细
    db.delete(item)


    # 4.先把明细变更刷入数据库事务，确保后续重算能拿到最新数据
    db.flush()


    # 5.删除明细后,自动重算报销单总金额,并同步更新主表数据
    recalculate_expense_total_amount(expense, db)


    # 6.更新数据库的两步
    db.commit()             # 提交事务,真正落库
    db.refresh(expense)     # 回读数据库,把 更新后的字段值 刷新到 expense 对象上


    # 7.返回:由 response_model=ExpenseResponse 过滤字段
    return expense



# 提交报销单送审的接口
@router.post(
    "/{expense_id}/submit",            # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/submit (和总路由拼接)
    response_model=ExpenseResponse,          # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                            # 在 /api/docs 文档里归到"报销"分组显示
    summary="提交报销单送审",                  # 接口标题
)
def submit_expense(
        expense_id: int,                                    # 路径参数，要提交送审的报销单ID
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    提交报销单送审：草稿 → 待审批，并按规则绑定审批流
    权限说明:
        - 需登录鉴权，且只能提交本人创建的单据（归属条件拼进查询，查不到即 404）
    前置校验:
        - 状态必须是 draft，避免重复提交；明细不能为空，空单没有可审内容
    审批流匹配:
        - 调 match_approval_flow 按「提交人角色 + 总金额」匹配金额类规则，未命中则用默认审批流兜底
        - 返回 0 表示提交人是 boss：不走线上审批，单据直接置 pending，flow_id 置空、层级置 0，不生成审批记录
        - 正常流程：绑定 flow_id、层级从 1 开始、写入 submitted_at，并生成首条 pending 记录作为轨迹起点
    一致性要求:
        - 单据状态、层级与审批记录在同一事务内提交，避免"单据已待审批但待办里查不到"的中间态
        :param expense_id: 路径参数，要提交的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 提交后的报销单完整信息
        :raises HTTPException: 404 不存在或无权；409 状态不允许或无明细；500 未配置默认审批流
    """
    # 1.归属校验:查询条件同时带[报销单ID + 当前登录人ID],一步完成归属校验，查不到 -> 404,不存在或无权
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,  # 报销单ID
        Expense.user_id == current_user.id  # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")


    # 2.状态校验:只有草稿才能提交送审,拒绝待审批/已审批的重复提交
    if expense.status != ExpenseStatus.DRAFT:
        raise HTTPException(status_code=409, detail="仅草稿状态可以提交")


    # 3.明细非空校验:报销单必须至少包含一条明细,不允许提交无明细的报销单
    if len(expense.items) == 0:
        raise HTTPException(status_code=409, detail="该报销单无费用明细,无法提交,请先添加明细")


    # 4.挂载审批流
    # 获取与规则匹配成功的审批流ID
    flow_id = match_approval_flow(  # 匹配审批流
        db=db,                                  # 数据库会话对象
        submitter_user=current_user,            # 提交人
        amount=float(expense.total_amount)      # 报销金额
    )


    # 4.返回值为0代表是boss角色，无线上审批流，仅更新单据状态，不生成审批记录
    if flow_id == 0:
        # 不绑定审批流ID，标记无线上审批层级
        expense.flow_id = None  # 无线上审批流
        expense.current_level = 0  # 无线上审批层级
        # 状态正常更新为待审批，与普通单据提交体验保持一致
        expense.status = ExpenseStatus.PENDING  # 状态：待审批
        expense.submitted_at = datetime.now()  # 记录提交时间


    # 5.非boss角色，走正常审批流绑定逻辑
    else:
        # 将审批流ID挂载到报销单对象上,后续审批都按这个流程走
        expense.flow_id = flow_id  # 绑定审批流
        # 4.审批层级从第一级开始(和 approval_flows.levels 里的 level 的值对应)
        expense.current_level = 1  # 从第一级开始


        # 6.状态改为待审批,并记录提交时间
        expense.status = ExpenseStatus.PENDING  # 状态：待审批  # 状态 draft -> pending
        expense.submitted_at = datetime.now()  # 记录提交时间  # 写入提交时间


        # 7.生成初始待审批记录，作为审批轨迹起始节点
        initial_approval = Approval(  # 初始待办记录
            expense_id=expense_id,  # 关联当前报销单ID，绑定对应单据
            flow_id=expense.flow_id,  # 绑定匹配到的审批流ID，与单据审批流配置保持一致
            level=expense.current_level,  # 当前审批层级，初始为第1级
            status=ApprovalStatus.PENDING,  # 审批记录状态：待审批，等待对应审批人处理
            comment="单据提交，等待审批",  # 系统默认备注，说明该条记录的业务含义
        )
        # 将审批记录加入数据库会话，与报销单状态变更在同一事务中提交，保证原子性
        db.add(initial_approval)


    # 8.规则引擎检查:计算命中项；block 级别直接阻止提交，其余（warn/review）落库供审批人参考
    rule_hits = run_rule_checks(db, expense)  # 执行规则检查
    blocked_hits = [hit for hit in rule_hits if hit["level"] == "block"]  # 阻断级命中项
    if blocked_hits:
        # 抛出 409，依赖注入会在请求结束时回滚，单据保持草稿状态
        raise HTTPException(
            status_code=409,  # HTTP 409：状态冲突
            detail="存在阻止提交的风险项：" + "；".join(hit["message"] for hit in blocked_hits[:3]),  # 风险提示文案
        )
    save_rule_hits(db, expense, rule_hits)


    # 9.写入数据库的三步
    db.commit()                 # 提交事务,真正落库
    db.refresh(expense)         # 回读数据库,把 更新后的字段值 刷新到 expense 对象上


    # 10.提交成功后自动触发 AI 预审；内部失败只记日志，不影响提交结果
    trigger_auto_review(db, expense_id=expense.id, user_id=current_user.id)


    # 11.返回；由 response_model=ExpenseResponse 过滤字段
    logger.info("报销单提交送审 expense_id=%s expense_no=%s user_id=%s flow_id=%s level=%s 规则命中=%s(%s)",  # 日志格式串
                expense.id, expense.expense_no, expense.user_id, expense.flow_id, expense.current_level,  # 单据ID/单号/提交人/流程/层级
                len(rule_hits), summarize_level(rule_hits))  # 命中条数/整体级别
    return expense



# 提交前规则预检的接口
@router.post(
    "/{expense_id}/precheck",           # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/precheck
    response_model=RulePrecheckResponse,     # 返回汇总级别 + 命中明细
    tags=["报销"],                           # 在 /api/docs 文档里归到"报销"分组显示
    summary="提交前规则预检(不改变单据状态)",   # 接口标题
)
def precheck_expense(
    expense_id: int,                                                    # 路径参数，报销单ID
    db: Session = Depends(get_db),                                      # 注入数据库会话
    current_user: User = Depends(get_current_user),                     # 当前登录人（验签）
):
    """
    提交前预检：只跑规则、不改变单据状态
        用途：前端在用户点"提交"之前就能看到会命中哪些提示（比如某条明细超类别限额），
        避免"提交后才看到警告"的割裂感；预检不落库，提交时才会正式写入命中记录。
        权限与状态:
            - 仅单据本人可用（别人的单返回 404，不暴露存在性）；
            - 仅草稿或已驳回可预检，已提交/已通过的单据无需预检（直接看详情里的命中记录）。
        :param expense_id: 报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 汇总级别（ok/warn/review/block）+ 命中条数 + 命中明细
        :raises HTTPException: 404 单据不存在或非本人；409 当前状态不支持预检
    """
    # 1.查本人单据（归属条件直接写进查询，查不到即 404）
    expense = (  # 查询报销单
        db.query(Expense)
        .filter(Expense.id == expense_id, Expense.user_id == current_user.id)
        .first()
    )
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")

    # 2.只有"可提交"的状态才需要预检
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿或已驳回状态的报销单可以预检")

    # 3.跑规则（纯计算，不写库）
    hits = run_rule_checks(db, expense)  # 执行规则检查

    # 4.返回汇总结果
    return {
        "level": summarize_level(hits),
        "total": len(hits),
        "hits": hits,
    }



# 重新提交被驳回的报销单的接口
@router.post(
    "/{expense_id}/resubmit",        # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/resubmit (和总路由拼接)
    response_model=ExpenseResponse,       # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                         # 在 /api/docs 文档里归到"报销"分组显示
    summary="重新提交被驳回的报销单",         # 接口标题
)
def resubmit_expense(
        expense_id: int,                                    # 路径参数，要提交送审的报销单ID
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    重新提交被驳回的报销单：已驳回 → 待审批，并重新匹配审批流
    权限说明:
        - 需登录鉴权，且只能操作本人提交的单据
    前置校验:
        - 状态必须是 rejected（草稿请用提交接口，其余状态不可重提）；明细不能为空
    为什么必须重新匹配审批流:
        - 驳回后金额、费用类型可能已改，沿用旧流程会出现"金额超限却仍走简易流程"的合规风险
    处理步骤:
        - 匹配新流程（boss 仍返回 0，表示免线上审批）
        - 把该单据下所有旧审批记录统一置为 obsoleted，明确区分新旧两轮流程
        - 重置 current_level 为 1、刷新 submitted_at，并生成新一轮的首条 pending 记录
        :param expense_id: 路径参数，要重新提交的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 重新提交后的报销单完整信息
        :raises HTTPException: 404 不存在或无权；409 状态不允许或无明细；500 未配置默认审批流
    """
    # 1.归属校验:查询条件同时带[报销单ID + 当前登录人ID],一步完成归属校验，查不到 -> 404,不存在或无权
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,  # 报销单ID
        Expense.user_id == current_user.id  # 当前登录人ID
    ).first()
    if expense is None:
        raise HTTPException(status_code=404, detail="报销单不存在或无权操作")


    # 2.状态校验:只有被驳回才能提交送审
    if expense.status != ExpenseStatus.REJECTED:
        raise HTTPException(status_code=409, detail="仅已驳回状态的报销单可以重新提交")


    # 3.明细非空校验:报销单必须至少包含一条明细,不允许提交无明细的报销单
    if len(expense.items) == 0:
        raise HTTPException(status_code=409, detail="该报销单无费用明细,无法提交,请先添加明细")


    # 4.挂载审批流
    # 获取与规则匹配成功的审批流ID(被驳回的报销单可能进行修改过,要重新匹配审批流)
    flow_id = match_approval_flow(  # 匹配审批流
        db=db,                              # 数据库会话对象
        submitter_user=current_user,        # 提交人
        amount=float(expense.total_amount)  # 报销金额
    )

    # 4.返回值为0代表是boss角色，无线上审批流
    if flow_id == 0:
        # 先作废该单据下所有历史审批记录，保证历史轨迹标记清晰
        db.query(Approval).filter(
            Approval.expense_id == expense_id
        ).update({
            "status": ApprovalStatus.OBSOLETED
        })
        # 不绑定审批流ID，重置审批层级标记
        expense.flow_id = None  # 无线上审批流
        expense.current_level = 0  # 无线上审批层级
        # 状态更新为待审批，刷新提交时间
        expense.status = ExpenseStatus.PENDING  # 状态：待审批
        expense.submitted_at = datetime.now()  # 记录提交时间

    # 5.非boss角色，走正常重新提交流程
    else:
        # 6.将匹配到的审批流ID挂载到报销单对象上
        expense.flow_id = flow_id  # 绑定审批流

        # 7.审批层级从第一级开始(和 approval_flows.levels 里的 level 的值对应)
        expense.current_level = 1  # 从第一级开始

        # 8.将该报销单下所有历史审批记录统一标记为已作废，明确区分新旧两轮审批流程
        db.query(Approval).filter(
            Approval.expense_id == expense_id  # 限定范围：仅当前报销单的审批记录
        ).update({
            "status": ApprovalStatus.OBSOLETED  # 统一更新为已作废状态，标记上一轮审批流程失效
        })

        # 9.状态改为待审批,并刷新提交时间
        expense.status = ExpenseStatus.PENDING  # 状态：待审批  # 状态 rejected -> pending
        expense.submitted_at = datetime.now()  # 记录提交时间  # 刷新提交时间，以重新提交时间为准

        # 10.创建新一轮审批流程的起始记录，作为新审批轨迹的起点
        new_initial_approval = Approval(  # 新一轮待办记录
            expense_id=expense_id,  # 关联当前报销单ID，绑定对应单据
            flow_id=expense.flow_id,  # 绑定重新匹配的审批流ID，与单据最新配置一致
            level=expense.current_level,  # 当前审批层级，重置为第1级从头流转
            status=ApprovalStatus.PENDING,  # 审批记录状态：待审批，等待对应审批人处理
            comment="重新提交，等待审批",  # 系统默认备注，标识该记录属于重新提交的新一轮流程
        )
        # 将新审批记录加入数据库会话，与单据状态变更在同一事务中提交
        db.add(new_initial_approval)


    # 9.规则引擎检查:重新提交也要重新算一遍（金额/明细/日期都可能变过）
    rule_hits = run_rule_checks(db, expense)  # 执行规则检查
    blocked_hits = [hit for hit in rule_hits if hit["level"] == "block"]  # 阻断级命中项
    if blocked_hits:
        raise HTTPException(
            status_code=409,  # HTTP 409：状态冲突
            detail="存在阻止提交的风险项：" + "；".join(hit["message"] for hit in blocked_hits[:3]),  # 风险提示文案
        )
    save_rule_hits(db, expense, rule_hits)


    # 10.更新数据库的两步
    db.commit()                 # 提交事务,真正落库
    db.refresh(expense)         # 回读数据库,把 更新后的字段值 刷新到 expense 对象上


    # 11.重新提交成功后自动触发 AI 预审；失败不影响提交结果
    trigger_auto_review(db, expense_id=expense.id, user_id=current_user.id)


    # 12.返回；由 response_model=ExpenseResponse 过滤字段
    logger.info("报销单重新提交 expense_id=%s expense_no=%s user_id=%s flow_id=%s level=%s 规则命中=%s(%s)",  # 日志格式串
                expense.id, expense.expense_no, expense.user_id, expense.flow_id, expense.current_level,  # 单据ID/单号/提交人/流程/层级
                len(rule_hits), summarize_level(rule_hits))  # 命中条数/整体级别
    return expense



# 撤回提交报销单送审的接口
@router.post(
    "/{expense_id}/recall",         # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/recall (和总路由拼接)
    response_model=ExpenseResponse,      # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                        # 在 /api/docs 文档里归到"报销"分组显示
    summary="撤回报销单的提交",             # 接口标题
)
def recall_expense(
        expense_id: int,                                    # 路径参数，要撤回的报销单ID
        db: Session = Depends(get_db),                      # 依赖注入:数据库会话
        current_user: User = Depends(get_current_user)      # 依赖注入:当前登录人（验签）
):
    """
    撤回报销单的提交：待审批 → 草稿（把单据收回自己手里重新编辑）
    权限说明:
        - 需登录鉴权，且只能撤回本人提交的单据
    校验规则:
        - 已是草稿的无需撤回（409）；只有 pending 可以撤回，已通过/已驳回/已支付等状态 409
    业务处理:
        - 单据状态回到 draft，并清空 submitted_at，视为"从未提交"
        - 把该单据下所有 pending 的审批记录置为 recalled 并写备注：
          既从待办列表移除，又在审批历史中留下完整的撤回痕迹
        - 注意：撤回不清空 flow_id / current_level，下次提交时会重新匹配并覆盖
        :param expense_id: 路径参数，要撤回的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 撤回后的报销单完整信息
        :raises HTTPException: 404 不存在或无权；409 状态不允许撤回
    """
    # 1.查单 + 归属校验:同时按 id 和当前用户过滤,查不到统一返回 404(放越权看到他人的报销单)
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,  # 报销单ID
        Expense.user_id == current_user.id  # 当前登录人ID
    ).first()
    if not expense:
        raise HTTPException(status_code=404, detail="报销单不存在或无权撤回提交")

    # 2.状态校验:已是草稿状态的报销单无需撤回
    if expense.status == ExpenseStatus.DRAFT:
        raise HTTPException(status_code=409, detail="该报销单已是草稿状态，无需撤回")

    # 3.状态校验:仅 pending 可以撤回
    if expense.status not in (ExpenseStatus.PENDING,):
        raise HTTPException(status_code=409, detail="仅待审批状态的报销单可以撤回")

    # 4.更新报销状态为 draft,并清空提交时间(视为未提交)
    expense.status = ExpenseStatus.DRAFT  # 状态回到草稿
    expense.submitted_at = None  # 清空提交时间

    # 5.将该报销单的审批记录标记为已撤回
    # 撤回后审批记录自动从待办列表消失,同时在审批历史中完整留存撤回操作痕迹
    db.query(Approval).filter(
        Approval.expense_id == expense_id,      # 限定范围:仅当前报销单的审批记录
        Approval.status == ApprovalStatus.PENDING        # 仅待审批状态的记录可撤回
    ).update({
        "status": ApprovalStatus.RECALLED,               # 统一更新为已撤回状态
        "comment": "用户撤回该单据提交"                    # 更新备注，明确标注该条记录的业务操作原因
    })

    # 6.更新数据库的两步
    db.commit()            # 提交事务,真正落库
    db.refresh(expense)    # 回读数据库,把 更新后的字段值 刷新到 expense 对象上

    # 7.返回；由 response_model=ExpenseResponse 过滤字段
    logger.info("报销单撤回提交 expense_id=%s expense_no=%s user_id=%s 状态回到草稿",  # 日志格式串
                expense.id, expense.expense_no, expense.user_id)  # 单据ID/单号/提交人
    return expense



# 主动撤销报销单的接口
@router.post(
    "/{expense_id}/cancel",         # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/cancel (和总路由拼接)
    response_model=ExpenseResponse,       # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                        # 在 /api/docs 文档里归到"报销"分组显示
    summary="主动撤销报销单",               # 接口标题
)
def cancel_expense(
        expense_id: int,                                    # 路径参数，要撤销的报销单ID
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    主动撤销（作废）报销单：不可逆的终态操作
    权限说明:
        - 需登录鉴权，且只能撤销本人提交的单据
    校验规则:
        - 已撤销的禁止重复撤销（409）
        - 仅 draft / pending / rejected 可撤销；approved / paid 属于已落地的业务事实，不可作废
        - 待审批状态还要二次校验：若已有任一级审批通过记录，说明流程已实质推进，禁止撤销
          （多级审批中，中间级通过后单据仍是 pending，这种情况不能"一键撤回"）
    业务处理:
        - 单据置为 cancelled 并记录 cancelled_at
        - 仅把 pending 的审批记录同步置为 cancelled 并写备注；已通过/已驳回的记录保持原状，保留真实审批痕迹
        :param expense_id: 路径参数，要撤销的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为提交人本人）
        :return: 撤销后的报销单完整信息
        :raises HTTPException: 404 不存在或无权；409 状态不允许撤销或已有审批通过记录
    """
    # 1.查单 + 归属校验:同时按 id 和当前用户过滤,查不到统一返回 404(防止越权看到他人的报销单)
    expense = db.query(Expense).filter(  # 查询报销单
        Expense.id == expense_id,      # 报销单ID
        Expense.user_id == current_user.id      # 当前登录人ID
    ).first()
    if not expense:
        raise HTTPException(status_code=404, detail="报销单不存在或无权撤销")

    # 2.状态校验:已撤销的单不能重复撤销
    if expense.status == ExpenseStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="该报销单已撤销，请勿重复撤销")

    # 3.报销单状态校验:仅 draft/pending/rejected 可以撤销
    if expense.status not in (ExpenseStatus.DRAFT, ExpenseStatus.PENDING, ExpenseStatus.REJECTED):
        raise HTTPException(status_code=409, detail="仅草稿、待审批和已驳回状态的报销单可以撤销")

    # 4.待审批状态二次校验:若该报销单处于待审批状态,要校验是否已有审批人进行实质审批
    # 多级审批中,中间级审批通过后单据仍未待审批状态,但已经产生了审批通过记录,此类单据禁止撤销
    if expense.status == ExpenseStatus.PENDING:             # 仅待审批状态的单据需要二次校验
        has_approved_record = db.query(Approval).filter(  # 是否已有通过记录
            Approval.expense_id == expense_id,      # 限定范围:仅当前报销单的审批记录
            Approval.status == ApprovalStatus.APPROVED        # 仅审批通过状态的记录
        ).first()
        if has_approved_record:
            raise HTTPException(status_code=409, detail="该报销单已有审批人进行审批，不允许撤销")

    # 5.更新报销状态为 cancelled,并记录撤销时间
    expense.status = ExpenseStatus.CANCELLED  # 状态：已撤销
    expense.cancelled_at = datetime.now()  # 记录撤销时间

    # 6.将该报销单的审批状态为待审批的审批记录同步更新为已撤销;审批状态为已驳回的的审批记录保持原状,用于标识实际审批动作
    # 撤销后审批记录自动从待办列表消失，同时在审批历史中完整留存撤销操作痕迹
    db.query(Approval).filter(
        Approval.expense_id == expense_id,      # 限定范围:仅当前报销单的审批记录
        Approval.status == ApprovalStatus.PENDING        # 仅修改待审批状态的记录,不触碰历史处理记录
    ).update({
        "status": ApprovalStatus.CANCELLED,             # 统一更新为已撤销状态
        "comment": "用户主动撤销该单据"                    # 更新备注，明确标注该条记录的业务操作原因
    })

    # 7.更新数据库的两步
    db.commit()            # 提交事务,真正落库
    db.refresh(expense)    # 回读数据库,把 更新后的字段值 刷新到 expense 对象上

    # 8.返回；由 response_model=ExpenseResponse 过滤字段
    logger.info("报销单主动撤销 expense_id=%s expense_no=%s user_id=%s",  # 日志格式串
                expense.id, expense.expense_no, expense.user_id)  # 单据ID/单号/提交人
    return expense



# 财务确认支付通过审批的报销单的接口
@router.post(
    "/{expense_id}/pay",             # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/pay (和总路由拼接)
    response_model=ExpenseResponse,        # 返回值按 ExpenseResponse 格式化
    tags=["报销"],                          # 在 /api/docs 文档里归到"报销"分组显示
    summary="财务对通过审批的报销单进行支付确认",    # 接口标题
)
def pay_expense(
        expense_id: int,                                    # 路径参数，要支付的报销单ID
        db: Session = Depends(get_db),                      # 注入数据库会话
        current_user: User = Depends(get_current_user)      # 当前登录人（验签）
):
    """
    财务确认支付：审批通过 → 已支付（报销流程的终点）
    权限说明:
        - 需登录鉴权，且必须拥有 pay 权限（按 User.has_permission 的映射，当前仅 finance 拥有）
        - 不校验单据归属：财务本来就要给别人的单据打款
    校验规则:
        - 单据不存在 404；已支付 409（防重复点击/重复打款）；非 approved 状态 409
    业务处理:
        - 状态置 paid 并记录 paid_at，作为审计与对账依据
        - 注意：本接口只做"状态确认"，不对接真实打款通道
        :param expense_id: 路径参数，要支付的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须有 pay 权限）
        :return: 支付后的报销单完整信息
        :raises HTTPException: 403 无支付权限；404 单据不存在；409 状态不允许支付
    """
    # 1.权限校验:只有 finance 有 pay 权限(admin 刻意不授予),否则 403
    if not current_user.has_permission("pay"):
        raise HTTPException(status_code=403, detail="无支付权限")

    # 2.查询报销单,不存在则404
    expense = db.query(Expense).filter(Expense.id == expense_id).first()  # 按ID查询单据
    if not expense:
        raise HTTPException(status_code=404, detail="报销单不存在")

    # 3.状态校验:已支付的单不能重复支付
    if expense.status == ExpenseStatus.PAID:
        raise HTTPException(status_code=409, detail="该报销单已支付，请勿重复支付")

    # 4.状态校验:只有 approved(审批通过)才能进入支付
    if expense.status not in (ExpenseStatus.APPROVED,):
        raise HTTPException(status_code=409, detail="仅审批通过的报销单可以支付")

    # 5.更新报销状态为 paid,并记录支付时间
    expense.status = ExpenseStatus.PAID  # 状态：已支付
    expense.paid_at = datetime.now()  # 记录支付时间

    # 6.更新数据库的两步
    db.commit()            # 提交事务,真正落库
    db.refresh(expense)    # 回读数据库,把 更新后的字段值 刷新到 expense 对象上

    # 7.返回；由 response_model=ExpenseResponse 过滤字段
    logger.info("报销单支付完成 expense_id=%s expense_no=%s amount=%s operator_id=%s",  # 日志格式串
                expense.id, expense.expense_no, expense.total_amount, current_user.id)  # 单据ID/单号/金额/操作人
    return expense



# 查询某一张报销单审批历史轨迹接口
@router.get(
    "/{expense_id}/approval_history",         # 接口路径；拼上总前缀后完整为 /api/v1/expense/{expense_id}/approval_history (和总路由拼接)
    response_model=List[ApprovalRecordItem],       # 返回值按 List[ApprovalRecordItem] 格式化
    tags=["报销"],                                  # 在 /api/docs 文档里归到"报销"分组显示
    summary="查询报销单审批历史轨迹",                  # 接口标题
)
def get_expense_approval_history(
        expense_id: int,                                    # 路径参数，要查询审批轨迹的报销单ID
        db: Session = Depends(get_db),                      # 依赖注入:数据库会话
        current_user: User = Depends(get_current_user)      # 依赖注入:当前登录人（验签）
):
    """
    查询指定报销单的完整审批历史轨迹（按层级、时间正序，用于详情页时间线）
    权限说明:
        - 提交人本人可看
        - 拥有 approve 权限的角色（manager/finance/boss）可看，便于审批前查阅他人批过的先例
        - 拥有 view_all 权限的角色（finance/boss/admin）可看：admin 不参与审批，但要能查所有单据的审批记录
        - 其余人（如普通员工看他人单据）一律 403，避免窥探他人审批过程
    校验顺序:
        - 先查单据是否存在（404），再判断是否具备查看权限（403），两者刻意区分，便于前端提示
    查询与组装:
        - 审批记录左连接用户表（isouter=True），即使审批人已被删除也能保留记录，不丢历史
        - 按 level 升序、created_at 升序排序，还原真实的审批先后顺序
        - 联查结果是 (审批记录, 审批人姓名, 审批人角色) 三元组，逐条转成字典并注入展示字段
        - 注意：提交时生成的待办记录 approver_id 为空，此时审批人姓名/角色为 None，
          出参模型对应字段已声明为 Optional，前端按"待处理"渲染
        :param expense_id: 路径参数，要查询轨迹的报销单ID
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 审批记录列表，经 response_model=List[ApprovalRecordItem] 输出
        :raises HTTPException: 404 单据不存在；403 无查看权限
    """
    # 1.校验报销单是否存在
    expense = db.query(Expense).filter(Expense.id == expense_id).first()  # 按ID查询单据
    if not expense:
        raise HTTPException(status_code=404, detail="此报销单不存在")


    # 2.权限校验
    # 可见范围:报销单提交本人 + 拥有审批权限的角色(manager/finance/boss) + 能看全公司数据的角色(view_all:finance/boss/admin)
    # 单独判断 view_all 的原因:admin 不参与审批(没有 approve),但需要能查看所有单据的审批轨迹
    is_owner = (expense.user_id == current_user.id)                     # 是否为本单据提交人
    has_approval_permission = current_user.has_permission("approve")    # 是否有审批权限
    has_view_all_permission = current_user.has_permission("view_all")   # 是否能查看全公司数据
    if not is_owner and not has_approval_permission and not has_view_all_permission:
        raise HTTPException(status_code=403, detail="无权限查看此报销单的审批历史")


    # 3.联表查询审批记录 + 审批人信息
    # 构造查询对象,指定要返回的内容
    approval_query = (  # 审批轨迹查询
        db.query(
            Approval,                                   # 返回完整的审批记录ORM对象
            User.full_name.label("approver_name"),      # 联表取出用户真实姓名,起别名approver_name
            User.role.label("approver_role")            # 联表取出用户角色,起别名approver_role
        )
        # 左连接用户表:通过审批人ID关联对应用户
        # isouter=True 表示左连接,即使关联不到用户,审批记录也会保留,避免数据丢失
        .join(User, Approval.approver_id == User.id, isouter=True)
        # 过滤条件:只查询指定报销单ID对应的审批记录
        .filter(Approval.expense_id == expense_id)
        # 排序规则:按审批层级升序排列,同层级按时间升序排列
        .order_by(Approval.level.asc(), Approval.created_at.asc())
    )

    # 执行SQL查询,返回所有符合条件的结果列表 --- [(Approval ORM对象, approver_name字符串, approver_role字符串), ...]
    approval_records = approval_query.all()  # 审批记录列表


    # 4.组装审批历史轨迹数据
    approval_history = []  # 审批轨迹结果
    for record, approver_name, approver_role in approval_records:
        # 将ORM对象转换为字典,注入审批人展示字段
        record_dict = record.__dict__.copy()  # 审批记录字典
        record_dict['approver_name'] = approver_name  # 审批人姓名
        record_dict['approver_role'] = approver_role.value if hasattr(approver_role, "value") else None  # 待处理记录无审批人，返回 null 而不是空串
        approval_history.append(record_dict)


    # 5.返回:由 response_model=List[ApprovalRecordItem] 过滤字段
    return approval_history



