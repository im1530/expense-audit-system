"""
管理端-部门管理路由（挂载前缀 /api/v1/admin/department）
    与业务侧的 /department 分开的原因：
        - /department 是给"选部门"下拉框用的公开接口，只返回启用部门、字段只有 id/名称/负责人/备注；
        - 管理端要维护部门：看停用部门、看成员数与待审批数、改名、启停、指定负责人，权限必须收紧到 admin。
    当前提供的接口（同一前缀下）：
        - GET  ""                 分页列表（可按启用状态、关键字筛选）
        - GET  "/{department_id}" 详情（编辑页回填 + 停用前风险提示）
        - POST ""                 新建部门
        - PUT  "/{department_id}" 全量修改（名称 / 备注 / 启停 / 负责人）
    不提供 DELETE：部门被 users.department_id 引用，物理删除会让历史账号失去归属，
    正确做法是把部门停用（停用前会校验没有启用成员、没有待审批单据）。
    权限约定：统一用 has_permission("manage_user") 判断，目前只有 admin 拥有。
"""



from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user               # 当前登录用户依赖
from app.models import User, Department                 # 用户/部门模型
from app.schemas.department import (                    # 管理端入参/出参
    AdminDepartmentCreate,
    AdminDepartmentDetail,
    AdminDepartmentItem,
    AdminDepartmentListResponse,
    AdminDepartmentUpdate,
)
from app.schemas.common import PageParams               # 通用分页参数
from app.services.department_service import (           # 部门规则(唯一性/保留部门/停用保护/负责人约束)
    RESERVED_DEPARTMENT_NAMES,
    count_department_pending_expenses,
    count_department_users,
    create_department,
    update_department,
)
from app.utils.pagination import paginate_query, assemble_paginated_result



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 组装管理端部门详情(列表项 + 停用前提示信息)
def _build_department_detail(db: Session, department: Department) -> dict:
    """
    把部门实体组装成管理端详情出参（详情、新建、修改三处共用）
        额外算三项"操作前必须知道"的信息：
            - 成员数（全部 / 启用）：有启用成员时不允许停用部门；
            - 待审批单据数：有未处理单据时不允许停用部门；
            - 是否系统保留部门：应用开发部 / 财务部 不允许改名与停用。
        :param db: 数据库会话
        :param department: 部门实体
        :return: 可直接作为 AdminDepartmentDetail 出参的字典
    """
    # 1.负责人姓名:未配置负责人时为 None
    manager_name = None  # 负责人姓名
    if department.manager_id is not None:
        manager = db.query(User).filter(User.id == department.manager_id).first()  # 负责人实体
        manager_name = manager.full_name if manager is not None else None  # 负责人姓名

    # 2.成员数:一次查询同时拿到"全部"与"启用"两个数
    total_map, active_map = count_department_users(db, [department.id])  # 成员数统计
    user_count = total_map.get(department.id, 0)  # 全部成员数
    active_user_count = active_map.get(department.id, 0)  # 启用成员数

    # 3.待审批单据数:停用部门前的拦截依据
    pending_expense_count = count_department_pending_expenses(db, department.id)  # 待审批数

    return {
        "id": department.id,                                    # 部门ID
        "name": department.name,                                # 部门名称
        "description": department.description,                  # 部门备注(可空)
        "manager_id": department.manager_id,                    # 负责人用户ID(可空)
        "manager_name": manager_name,                           # 负责人姓名(可空)
        "is_active": department.is_active,                      # 部门是否启用
        "user_count": user_count,                               # 全部成员数
        "active_user_count": active_user_count,                 # 启用成员数
        "created_at": department.created_at,                    # 创建时间
        "pending_expense_count": pending_expense_count,         # 待审批单据数
        "is_reserved": department.name in RESERVED_DEPARTMENT_NAMES,  # 是否系统保留部门
    }



# 分页查询部门列表的接口(仅admin可用)
@router.get(
    "",                                         # 接口路径；拼上总前缀后完整为 /api/v1/admin/department
    response_model=AdminDepartmentListResponse, # 返回值按管理端分页响应模型格式化
    tags=["管理端"],                             # 在 /api/docs 文档里归到"管理端"分组显示
    summary="分页查询部门列表(仅admin可用)",        # 接口标题
)
def list_departments(
    page_params: PageParams = Depends(),                                      # 分页参数
    is_active: Optional[bool] = Query(None, description="筛选:部门状态(true启用/false停用/不传则全部)"),  # 部门状态
    keyword: Optional[str] = Query(None, max_length=50, description="筛选:关键字,匹配部门名称/备注"),  # 搜索关键字
    db: Session = Depends(get_db),                                            # 注入数据库会话
    current_user: User = Depends(get_current_user),                           # 当前登录人（验签）
):
    """
    分页查询全部部门（管理端部门列表）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
        - 与公开接口 GET /department 的区别：这里能看到停用部门、成员数与负责人姓名
    筛选能力:
        - is_active：true 只看启用、false 只看停用、不传表示"全部"
        - keyword：模糊匹配部门名称与备注
    查询与组装:
        - 左连接 users 带出负责人姓名（未配置负责人时为空，必须用 outerjoin）
        - 成员数与启用成员数用一次分组查询批量取回，避免逐个部门数一遍
        - 按部门ID升序，顺序稳定，便于分页
        :param page_params: 分页参数
        :param is_active: 可选，按启用状态筛选
        :param keyword: 可选，模糊搜索关键字
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: AdminDepartmentListResponse 实例
        :raises HTTPException: 403 当前用户没有 manage_user 权限
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看部门列表")


    # 2.构建基础查询:左连接负责人,保证没有负责人的部门也能查出来
    query = db.query(  # 部门查询
        Department,                                 # 部门主表
        User.full_name.label("manager_name")        # 负责人姓名
    ).outerjoin(
        User, Department.manager_id == User.id      # 左连接:负责人可能为空
    )


    # 3.追加筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:启用状态
    if is_active is not None:
        query = query.filter(Department.is_active == is_active)  # 状态筛选

    # 筛选:关键字(部门名称或备注)
    if keyword:
        kw = keyword.strip()  # 去空格关键字
        if kw:
            like = f"%{kw}%"                        # 参数化查询,不存在 SQL 注入
            query = query.filter(  # 名称或备注匹配
                or_(
                    Department.name.like(like),         # 部门名称
                    Department.description.like(like),  # 部门备注
                )
            )


    # 4.排序 + 分页:按部门ID升序,保证分页稳定
    query = query.order_by(Department.id.asc())  # ID升序
    rows, total = paginate_query(  # 部门分页
        query=query,                            # 查询对象
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size,        # 每页条数
    )


    # 5.批量统计成员数:一次查回当前页所有部门的人数,避免 N+1
    department_ids = [department.id for department, _manager_name in rows]  # 部门ID列表
    total_map, active_map = count_department_users(db, department_ids)  # 成员数统计


    # 6.组装列表项
    result_items = []  # 部门出参列表
    for department, manager_name in rows:
        result_items.append({
            "id": department.id,                                    # 部门ID
            "name": department.name,                                # 部门名称
            "description": department.description,                  # 部门备注
            "manager_id": department.manager_id,                    # 负责人ID
            "manager_name": manager_name,                           # 负责人姓名
            "is_active": department.is_active,                      # 是否启用
            "user_count": total_map.get(department.id, 0),          # 全部成员数
            "active_user_count": active_map.get(department.id, 0),  # 启用成员数
            "created_at": department.created_at,                    # 创建时间
        })


    # 7.组装完整分页结果并返回
    department_page = assemble_paginated_result(  # 分页结果
        items=result_items,                             # 当前页的部门数据
        total=total,                                    # 符合条件的总条数
        page=page_params.page,                          # 当前页码
        page_size=page_params.page_size,                # 每页条数
        response_model=AdminDepartmentListResponse,     # 管理端分页响应模型
    )
    return department_page



# 查询部门详情的接口(仅admin可用)
@router.get(
    "/{department_id}",                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/department/{department_id}
    response_model=AdminDepartmentDetail,   # 详情比列表多返回"能否停用"的判断字段
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="查询部门详情(仅admin可用)",       # 接口标题
)
def get_department_detail(
    department_id: int,                                                     # 路径参数，要查询的部门ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    查询指定部门的详情（编辑页回填 + 停用前风险提示）
    返回内容:
        - 基础字段：名称、备注、负责人、启停状态
        - 统计字段：成员数、启用成员数、待审批单据数（决定能不能停用）
        - 标记字段：is_reserved 表示是否为系统保留部门（不可改名/停用）
    路由顺序提醒:
        - 带路径参数的接口写在最后；以后加 /options 这类静态子路径必须放在它前面
        :param department_id: 路径参数，目标部门ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 部门详情（含统计与风险提示字段）
        :raises HTTPException: 403 无权限；404 部门不存在
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看部门详情")

    # 2.查询部门
    department = db.query(Department).filter(Department.id == department_id).first()  # 目标部门
    if department is None:
        raise HTTPException(status_code=404, detail="部门不存在")

    # 3.组装详情并返回
    return _build_department_detail(db, department)



# 新建部门的接口(仅admin可用)
@router.post(
    "",                                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/department
    response_model=AdminDepartmentDetail,   # 返回详情,前端可直接渲染列表/表单
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="新建部门(仅admin可用)",           # 接口标题
)
def create_new_department(
    payload: AdminDepartmentCreate,                                         # 入参：名称 + 备注
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    新建一个部门（默认启用、默认没有负责人）
    校验规则:
        - 名称去空格后不能为空；名称全局唯一（409）
        - 新建的部门没有成员，因此不可能在创建时指定负责人：
          正确流程是先建部门，再到用户管理里把 manager 放到该部门（会自动回写 manager_id）
        :param payload: 新建入参（名称、备注）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 新建部门的详情
        :raises HTTPException: 403 无权限；409 名称已存在；400 名称为空
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可新建部门")

    # 2.调用服务层创建(名称校验、唯一性都在服务层)
    department = create_department(  # 新建部门
        db,                                 # 数据库会话
        name=payload.name,                  # 部门名称
        description=payload.description,    # 部门备注(可空)
    )

    # 3.返回详情
    return _build_department_detail(db, department)



# 修改部门的接口(全量,仅admin可用)
@router.put(
    "/{department_id}",                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/department/{department_id}
    response_model=AdminDepartmentDetail,   # 与详情同构,前端改完可直接刷新
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="修改部门(全量,仅admin可用)",       # 接口标题
)
def update_existing_department(
    department_id: int,                                                     # 路径参数，要修改的部门ID
    payload: AdminDepartmentUpdate,                                         # 全量入参：名称/备注/启停/负责人
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    修改指定部门（PUT 全量：名称、备注、启用状态、负责人一次发全）
    为什么用 PUT:
        - 编辑页从详情接口回填整张表单，全量提交可避免"只改备注却把负责人清空"的部分更新事故
    业务规则（详见 services.department_service.update_department）:
        - 系统保留部门（应用开发部/财务部）不允许改名、不允许停用
        - 名称全局唯一（排除自己）
        - 停用前必须：没有启用成员、没有待审批单据
        - 负责人必须是本部门、启用状态、manager 角色的用户；传 null 表示清空负责人
        :param department_id: 路径参数，目标部门ID
        :param payload: 全量修改入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 修改后的部门详情
        :raises HTTPException: 403 无权限；404 部门不存在；409 名称重复；400 各类规则不通过
    """
    # 1.权限校验
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可修改部门")

    # 2.查目标部门
    department = db.query(Department).filter(Department.id == department_id).first()  # 目标部门
    if department is None:
        raise HTTPException(status_code=404, detail="部门不存在")

    # 3.调用服务层修改(唯一性、保留部门、停用保护、负责人约束都在服务层)
    department = update_department(  # 修改后的部门
        db,                                 # 数据库会话
        department,                         # 待修改的部门实体
        name=payload.name,                  # 新名称
        description=payload.description,    # 新备注
        is_active=payload.is_active,        # 新启用状态
        manager_id=payload.manager_id,      # 新负责人(可空)
    )

    # 4.返回详情
    return _build_department_detail(db, department)
