"""
管理端-用户管理路由（挂载前缀 /api/v1/admin/user）
    为什么单独开一个管理端命名空间，而不是往后端的 /user 上继续加接口：
        - /user 是"选人下拉框"的数据源：所有登录用户可查、只返回启用账号、字段只有 id/姓名/角色；
        - 管理端要的是"账号全貌"：包含停用账号、邮箱手机、部门名称、最后登录时间，还要分页和多维筛选；
        - 两者的权限、数据范围、出参语义都不同，混在一个路径下只能靠一堆可选参数分叉，容易越权。
    当前提供的接口（同一前缀下）：
        - GET  ""            分页列表（多条件筛选）
        - POST ""            建号（可指定角色，规则见 services.auth_service）
        - GET  "/{user_id}"  详情（编辑页回填 + 停用/降级前的风险提示）
        - PUT  "/{user_id}"  全量修改（资料 / 角色 / 部门 / 启停）
    权限约定：统一用 has_permission("manage_user") 判断，目前只有 admin 拥有，
    与 admin 的定位一致——能管账号，但不参与审批（approve）与付款（pay）。
"""



from typing import Optional
import logging

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.dependencies import get_db                     # 数据库会话依赖
from app.api.deps import get_current_user               # 当前登录用户依赖
from app.models import User, Department, UserRole       # 用户/部门模型与角色枚举
from app.schemas.user import (                          # 管理端入参/出参
    AdminPasswordReset,
    AdminUserCreate,
    AdminUserDetail,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdate,
)
from app.schemas.common import PageParams               # 通用分页参数
from app.services.approval_service import count_pending_approvals_for_user   # 待办统计(停用前拦截)
from app.services.auth_service import (                  # 建号/改号/改密逻辑(与注册、改密接口共用)
    create_user,
    reset_password,
    update_user,
)
from app.utils.pagination import paginate_query, assemble_paginated_result



# 创建路由器对象
router = APIRouter()  # 路由器对象

# 日志器：账号类操作属于安全敏感动作，必须留痕（谁、对谁、做了什么）
logger = logging.getLogger(__name__)  # 日志器



# 分页查询用户列表的接口(仅admin可用)
@router.get(
    "",                                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/user
    response_model=AdminUserListResponse,   # 返回值按管理端分页响应模型格式化
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="分页查询用户列表(仅admin可用)",    # 接口标题
)
def list_users(
    page_params: PageParams = Depends(),                                    # 分页参数
    role: Optional[UserRole] = Query(None, description="筛选:用户角色"),      # 筛选:角色
    department_id: Optional[int] = Query(None, ge=1, description="筛选:所属部门ID"),  # 部门筛选
    is_active: Optional[bool] = Query(None, description="筛选:账号状态(true启用/false停用/不传则全部)"),  # 账号状态
    keyword: Optional[str] = Query(None, max_length=50, description="筛选:关键字,匹配用户名/姓名/手机号/邮箱"),  # 搜索关键字
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    分页查询全部用户（管理端账号列表）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
        - 与业务接口 GET /api/v1/user 的区别：这里能看到停用账号与联系方式，所以权限必须收紧
    筛选能力:
        - role：按角色筛选
        - department_id：按所属部门筛选（boss/admin 可能没有部门，天然查不到）
        - is_active：true 只看启用、false 只看停用、不传表示"全部"（管理端默认要看全，而不是只看启用）
        - keyword：模糊匹配用户名/姓名/手机号/邮箱，用于搜索框
    查询与组装:
        - 左连接 departments 带出部门名称：必须用 outerjoin，否则没挂部门的 boss/admin 会被内连接直接过滤掉
        - 按注册时间倒序排列，新账号排最前，ID 作为兜底排序保证分页稳定
        - 返回结构与其他列表接口同构（items/total/page/page_size/total_pages），前端可复用分页组件
        :param page_params: 分页参数（页码、每页条数）
        :param role: 可选，按角色筛选
        :param department_id: 可选，按部门筛选
        :param is_active: 可选，按启用状态筛选；不传则返回启用+停用
        :param keyword: 可选，模糊搜索关键字
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: AdminUserListResponse 实例，包含当前页用户与分页元信息
        :raises HTTPException: 403 当前用户没有 manage_user 权限
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断,不在接口里散写 role == admin
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看用户列表")


    # 2.构建基础查询:左连接部门表带出部门名称
    # outerjoin(左连接):boss/admin 可能不挂部门,用内连接会被过滤掉;department_id 为 NULL 时部门名称为空
    query = db.query(  # 用户查询
        User,                                   # 用户主表:账号全部字段来源
        Department.name.label("department_name")  # 关联部门名称:前端直接展示中文,不用再查一次
    ).outerjoin(
        Department, User.department_id == Department.id   # 关联条件:用户的 department_id 指向部门主键
    )


    # 3.追加筛选条件(均为可选参数,有传入值时才生效)
    # 筛选:角色
    if role is not None:
        query = query.filter(User.role == role)  # 角色筛选

    # 筛选:所属部门
    if department_id is not None:
        query = query.filter(User.department_id == department_id)  # 部门筛选

    # 筛选:账号状态(True=只看启用 / False=只看停用 / None=全部)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)  # 状态筛选

    # 筛选:关键字模糊搜索(用户名/姓名/手机号/邮箱任一命中即可)
    if keyword:
        kw = keyword.strip()                    # 去掉首尾空格,避免用户误输入空格导致搜不到
        if kw:
            like = f"%{kw}%"                    # 拼成 LIKE 模式;参数化查询,不存在 SQL 注入
            query = query.filter(  # 关键字匹配
                or_(
                    User.username.like(like),   # 用户名
                    User.full_name.like(like),  # 真实姓名
                    User.phone.like(like),      # 手机号
                    User.email.like(like),      # 邮箱
                )
            )


    # 4.排序 + 分页
    # 按注册时间倒序(新账号在前),同一秒创建的多条用 ID 倒序兜底,保证分页结果稳定不重复
    query = query.order_by(User.created_at.desc(), User.id.desc())  # 排序规则
    rows, total = paginate_query(  # 用户分页
        query=query,                            # 查询对象
        page=page_params.page,                  # 当前页码
        page_size=page_params.page_size,        # 每页条数
    )


    # 5.组装明细结构:多表联查结果是 (用户对象, 部门名称) 元组,手动映射成出参需要的字典
    result_items = []  # 用户出参列表
    for user, department_name in rows:
        result_items.append({
            "id": user.id,                      # 用户ID
            "username": user.username,          # 登录用户名
            "full_name": user.full_name,        # 真实姓名
            "email": user.email,                # 邮箱
            "phone": user.phone,                # 手机号
            "position": user.position,          # 职位(可空)
            "role": user.role,                  # 角色枚举
            "department_id": user.department_id,        # 所属部门ID(可空)
            "department_name": department_name,         # 所属部门名称(可空)
            "is_active": user.is_active,                # 账号是否启用
            "created_at": user.created_at,              # 注册时间
            "last_login_at": user.last_login_at,        # 最后登录时间(可空)
        })


    # 6.组装完整分页结果
    user_page = assemble_paginated_result(  # 分页结果
        items=result_items,                         # 当前页的用户数据
        total=total,                                # 符合条件的总条数
        page=page_params.page,                      # 当前页码
        page_size=page_params.page_size,            # 每页条数
        response_model=AdminUserListResponse,       # 管理端分页响应模型
    )


    # 7.返回:由 response_model 统一过滤字段(不返回 password_hash)
    return user_page



# 组装管理端用户详情(含风险提示信息)
def _build_user_detail(db: Session, user: User) -> dict:
    """
    把用户实体组装成管理端详情出参（详情接口、建号、修改、重置密码共用）
        除基础字段外，额外算三项"操作前必须知道"的判断：
            - 是否为部门负责人 / 负责哪个部门：改角色、换部门、停用都要先处理负责人身份；
            - 待他审批的单据数：停用前要拦"有审批待办"的账号；
            - 是否为最后一个启用的管理员：是的话禁止停用/降级，否则没人能进管理端。
        这三项放列表里代价太大（每行都要算待办），只在详情/修改后计算一次。
        :param db: 数据库会话
        :param user: 用户实体
        :return: 可直接作为 AdminUserDetail 出参的字典
    """
    # 1.所属部门名称:没有部门时为 None
    department = None  # 所属部门
    if user.department_id is not None:
        department = db.query(Department).filter(Department.id == user.department_id).first()  # 部门实体

    # 2.是否为部门负责人:以 departments.manager_id 为准
    managed_department = db.query(Department).filter(Department.manager_id == user.id).first()  # 负责部门

    # 3.待他审批的单据数:与待办列表同一套资格规则
    pending_approval_count = count_pending_approvals_for_user(db, user)  # 待办数

    # 4.是否为最后一个启用的管理员
    is_last_active_admin = False  # 最后管理员标记
    if user.role == UserRole.ADMIN and user.is_active:
        active_admin_count = db.query(User).filter(  # 启用管理员数
            User.role == UserRole.ADMIN,  # 管理员角色
            User.is_active == True  # 启用状态
        ).count()
        is_last_active_admin = active_admin_count <= 1  # 是否最后管理员

    return {
        "id": user.id,                                              # 用户ID
        "username": user.username,                                  # 登录用户名
        "full_name": user.full_name,                                # 真实姓名
        "email": user.email,                                        # 邮箱
        "phone": user.phone,                                        # 手机号
        "position": user.position,                                  # 职位(可空)
        "role": user.role,                                          # 角色枚举
        "department_id": user.department_id,                        # 所属部门ID(可空)
        "department_name": department.name if department else None,  # 所属部门名称(可空)
        "is_active": user.is_active,                                # 账号是否启用
        "must_change_password": user.must_change_password,          # 是否必须修改密码
        "created_at": user.created_at,                              # 注册时间
        "last_login_at": user.last_login_at,                        # 最后登录时间(可空)
        "is_department_manager": managed_department is not None,    # 是否为部门负责人
        "managed_department_id": managed_department.id if managed_department else None,        # 负责的部门ID
        "managed_department_name": managed_department.name if managed_department else None,    # 负责的部门名称
        "pending_approval_count": pending_approval_count,           # 待他审批的单据数
        "is_last_active_admin": is_last_active_admin,               # 是否最后一个启用的管理员
    }



# 查询用户详情的接口(仅admin可用)
@router.get(
    "/{user_id}",                           # 接口路径；拼上总前缀后完整为 /api/v1/admin/user/{user_id}
    response_model=AdminUserDetail,         # 详情比列表多返回"操作前置判断"字段
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="查询用户详情(仅admin可用)",       # 接口标题
)
def get_user_detail(
    user_id: int,                                                           # 路径参数，要查询的用户ID
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    查询指定用户的详情（编辑页回填 + 风险操作前置提示）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
    为什么还要详情接口（列表已经能筛到）:
        - 深链接：编辑页地址只带 user_id，刷新时前端没有"第几页、什么筛选条件"，需要一个按ID精确取数的接口；
        - 承载判断信息：is_department_manager / pending_approval_count / is_last_active_admin
          这些字段算一次就够了，塞进列表会让每行都多算一次待办，得不偿失；
        - 契约分离：列表项以后为性能可能裁剪字段，详情保持完整。
    查询说明:
        - 不筛 is_active，停用账号同样能查到（否则停用后编辑页打不开、无法重新启用）
    路由顺序提醒:
        - 本接口带路径参数 /{user_id}，如果以后要加 /roles 这类静态子路径，
          必须写在它前面，否则会被 "user_id" 当整数解析而返回 422
        :param user_id: 路径参数，目标用户ID
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 用户详情（含部门名称与风险提示字段），不含密码
        :raises HTTPException: 403 无权限；404 用户不存在
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可查看用户详情")


    # 2.查询目标用户
    user = db.query(User).filter(User.id == user_id).first()  # 目标用户

    # 3.查不到就是 404:管理端查询不需要像业务接口那样隐藏"是否存在"
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")


    # 4.组装详情(部门名称、负责人身份、待办数、最后管理员标记)
    return _build_user_detail(db, user)



# 新建用户账号的接口(仅admin可用)
@router.post(
    "",                                     # 接口路径；拼上总前缀后完整为 /api/v1/admin/user
    response_model=AdminUserDetail,         # 与详情同构,建号后前端可直接渲染同一套信息
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="新建用户账号(仅admin可用)",       # 接口标题
)
def create_user_account(
    payload: AdminUserCreate,                                               # 接收前端请求体，并按 AdminUserCreate 自动校验字段
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    新建一个用户账号（管理端建号，与公开注册的区别是"可以指定角色"）
    权限说明:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
        - 与 /auth/register 的分工：注册只能建 employee 且公开可调，管理端建号可以建任意角色
    角色与部门规则（详见 services.auth_service.resolve_department）:
        - admin   ：不传部门则自动挂「应用开发部」，传了则用传入的部门
        - boss    ：不挂部门，传了会返回 400
        - finance ：固定「财务部」，传别的部门会返回 400
        - manager ：必须指定部门，且该部门不能已有负责人；建好后自动回写 departments.manager_id
        - employee：必须指定部门，部门需存在且启用
    校验与写入:
        - 用户名/邮箱交叉查重（409）、真实姓名与手机号必填（400）
        - 密码经 bcrypt 哈希后入库，响应中不含密码字段
        :param payload: 建号入参（账号、密码、姓名、手机、角色、部门）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 新建用户的详情信息（含部门名称、负责人身份、待办数等提示字段）
        :raises HTTPException: 403 无权限；409 用户名或邮箱已存在；400 参数/部门规则不合法
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可新建用户")


    # 2.调用服务层建号:唯一性、角色部门规则、密码哈希、经理与部门互指全部在服务层完成
    user = create_user(  # 新建用户
        db,                                 # 数据库会话
        username=payload.username,          # 登录用户名
        email=payload.email,                # 邮箱
        password=payload.password,          # 明文密码(服务层负责哈希)
        full_name=payload.full_name,        # 真实姓名
        phone=payload.phone,                # 手机号
        position=payload.position,          # 职位(可空)
        role=payload.role,                  # 角色(管理端可指定)
        department_id=payload.department_id,  # 部门ID(按角色规则校验)
    )


    # 3.返回:统一用详情组装函数,新建后前端也能立刻拿到"是否负责人/是否有待办"等提示
    logger.info("管理员建号成功 operator_id=%s user_id=%s username=%s role=%s department_id=%s",  # 日志模板
                current_user.id, user.id, user.username,  # 操作人/用户ID/用户名
                user.role.value if hasattr(user.role, "value") else user.role, user.department_id)  # 角色/部门ID
    return _build_user_detail(db, user)



# 修改用户账号的接口(全量,仅admin可用)
@router.put(
    "/{user_id}",                           # 接口路径；拼上总前缀后完整为 /api/v1/admin/user/{user_id}
    response_model=AdminUserDetail,         # 与详情同构,前端改完可直接刷新表单
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="修改用户账号(全量,仅admin可用)",   # 接口标题
)
def update_user_account(
    user_id: int,                                                           # 路径参数，要修改的用户ID
    payload: AdminUserUpdate,                                               # 全量入参：可编辑字段必须一次发全
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    修改指定用户账号（PUT 全量替换：资料 + 角色 + 部门 + 启用状态）
    为什么用 PUT 而不是 PATCH:
        - 编辑页本来就从详情接口回填了整张表单，PUT 要求"字段发全"，可以避免
          只传 is_active 就把姓名/手机清空的经典事故；
        - 因此 email / full_name / phone / role / is_active 都是必填，少传直接 422。
    不可修改的字段:
        - username：登录凭据兼审计线索，改名会让历史单据、审批记录对不上人；
        - password：走单独的重置密码接口，不混在资料更新里。
    业务规则（详见 services.auth_service.update_user）:
        - 邮箱不能与任何其他账号的邮箱/用户名重复（409）
        - 部门按新角色校验：admin 不传则保留原部门；boss 必须不传；finance 固定财务部；manager/employee 必填
        - 改角色会联动部门负责人：升为 manager 要求目标部门没有别的负责人；降级/换部门会自动解除原部门负责人
        - 不允许停用自己、不允许修改自己的角色
        - 不允许停用/降级"最后一个启用状态的管理员"
        - 停用前校验待办：他若还有待审批单据，或他是某部门负责人，都会被拒绝
        :param user_id: 路径参数，目标用户ID
        :param payload: 全量修改入参
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 修改后的用户详情（含部门名称与风险提示字段）
        :raises HTTPException: 403 无权限；404 用户不存在；409 邮箱重复；400 各类业务规则不通过
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可修改用户")


    # 2.查目标用户,不存在直接 404
    user = db.query(User).filter(User.id == user_id).first()  # 目标用户
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")


    # 3.调用服务层修改:自锁保护、最后管理员保护、待办拦截、部门负责人同步都在服务层完成
    user = update_user(  # 修改后的用户
        db,                                 # 数据库会话
        user,                               # 待修改的用户实体
        operator=current_user,              # 操作人(用于"不能停用自己"等判断)
        email=payload.email,                # 新邮箱
        full_name=payload.full_name,        # 新姓名
        phone=payload.phone,                # 新手机号
        position=payload.position,          # 新职位(可空)
        role=payload.role,                  # 新角色
        department_id=payload.department_id,  # 新部门(按角色规则校验)
        is_active=payload.is_active,        # 新启用状态
    )


    # 4.返回修改后的详情:前端可以直接用返回值刷新表单与按钮状态
    logger.info("管理员修改用户 operator_id=%s user_id=%s username=%s role=%s is_active=%s department_id=%s",  # 日志模板
                current_user.id, user.id, user.username,  # 操作人/用户ID/用户名
                user.role.value if hasattr(user.role, "value") else user.role,  # 角色
                user.is_active, user.department_id)  # 启用状态/部门ID
    return _build_user_detail(db, user)



# 重置用户密码的接口(仅admin可用)
@router.post(
    "/{user_id}/reset-password",            # 接口路径；拼上总前缀后完整为 /api/v1/admin/user/{user_id}/reset-password
    response_model=AdminUserDetail,         # 返回详情,前端可刷新"待改密"标记
    tags=["管理端"],                         # 在 /api/docs 文档里归到"管理端"分组显示
    summary="重置用户密码(仅admin可用)",       # 接口标题
)
def reset_user_password(
    user_id: int,                                                           # 路径参数，要重置密码的用户ID
    payload: AdminPasswordReset,                                            # 入参：新密码
    db: Session = Depends(get_db),                                          # 注入数据库会话
    current_user: User = Depends(get_current_user),                         # 当前登录人（验签）
):
    """
    管理员重置指定用户的密码（直接设新密码，不校验旧密码）
    权限与边界:
        - 需登录鉴权，且必须拥有 manage_user 权限（当前仅 admin）
        - 不能重置自己的密码：管理员改自己密码应走 /auth/change-password（需要旧密码确认），
          否则等于开了一条"免验证改自己密码"的通道
    业务语义:
        - 管理员设的密码属于"别人设的密码"，重置成功后 must_change_password 置 True；
        - 该用户下次登录后，除修改密码外所有接口都会返回 403，直到他自己把密码改掉；
        - 登录接口会返回 must_change_password=true，前端据此直接跳改密页。
    安全说明:
        - 重置后该用户此前签发的令牌全部立即失效（token_version +1，鉴权时比对不上即拒绝），
          不存在"重置了密码但旧会话还能用"的窗口期
        :param user_id: 路径参数，目标用户ID
        :param payload: 重置入参（新密码 6~128 位）
        :param db: 数据库会话
        :param current_user: 当前登录用户（须为 admin）
        :return: 重置后的用户详情（must_change_password 为 true）
        :raises HTTPException: 403 无权限；404 用户不存在；400 试图重置自己的密码
    """
    # 1.权限校验:管理端接口统一用 manage_user 判断
    if not current_user.has_permission("manage_user"):
        raise HTTPException(status_code=403, detail="仅管理员可重置用户密码")


    # 2.查目标用户,不存在直接 404
    user = db.query(User).filter(User.id == user_id).first()  # 目标用户
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")


    # 3.调用服务层重置:写新密码哈希 + 置 must_change_password=True
    user = reset_password(  # 重置后的用户
        db,                                 # 数据库会话
        user,                               # 被重置密码的用户实体
        operator=current_user,              # 操作人(用于"不能重置自己密码"的判断)
        new_password=payload.new_password,  # 新密码(服务层哈希)
    )


    # 4.返回详情:前端可直接看到 must_change_password=true,并提示用户下次登录必须改密
    logger.info("管理员重置用户密码 operator_id=%s user_id=%s username=%s（对方需下次登录改密）",  # 日志模板
                current_user.id, user.id, user.username)  # 操作人/用户ID/用户名
    return _build_user_detail(db, user)
