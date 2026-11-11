"""
部门服务（部门写操作的唯一实现，供管理端部门维护使用）
    把部门相关的业务规则收敛在这里，路由层只负责权限与参数：
        - 名称唯一：departments.name 已加唯一索引，代码先查一次给出友好的 409；
        - 系统保留部门：应用开发部 / 财务部 是建号规则依赖的部门（按名称查找），因此禁止改名与停用；
        - 停用保护：部门里还有启用成员、或有待审批单据时不允许停用，避免"人还在、单还在却查不到部门"；
        - 负责人约束：负责人必须指向"本部门的、启用状态的、manager 角色"用户，
          保证 users.role 与 departments.manager_id 两份信息不会互相矛盾。
    约定：写操作直接 commit，失败抛 HTTPException。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models import User, Department, UserRole
from app.models.expense import Expense, ExpenseStatus



# 系统保留部门：建号规则按名称查找这两个部门，改名或停用会让建号直接失败
RESERVED_DEPARTMENT_NAMES = ("应用开发部", "财务部")   # 系统保留部门



# 统计部门成员数
def count_department_users(db: Session, department_ids: list[int]) -> tuple[dict, dict]:
    """
    批量统计多个部门的成员数（一次查询，避免逐个部门数一遍）
        :param db: 数据库会话
        :param department_ids: 部门ID列表
        :return: (全部成员数字典, 启用成员数字典)，形如 ({1: 5}, {1: 3})
    """
    if not department_ids:
        return {}, {}

    # 1.按部门分组统计全部成员
    total_rows = db.query(User.department_id, func.count(User.id)).filter(   # 全部成员统计
        User.department_id.in_(department_ids)
    ).group_by(User.department_id).all()

    # 2.按部门分组统计启用成员
    active_rows = db.query(User.department_id, func.count(User.id)).filter(   # 启用成员统计
        User.department_id.in_(department_ids),
        User.is_active == True,
    ).group_by(User.department_id).all()

    return {row[0]: row[1] for row in total_rows}, {row[0]: row[1] for row in active_rows}



# 统计部门待审批单据数
def count_department_pending_expenses(db: Session, department_id: int) -> int:
    """
    统计"该部门成员提交、且仍处于待审批状态"的单据数
        用途：停用部门前的拦截依据——部门停用后这些单据仍挂在原部门成员名下，
        但部门已不可选，容易出现"单据还在审、部门已经没了"的怪异状态。
        注意：单据没有部门快照，这里按"提交人**当前**部门"归属（与列表筛选口径一致）。
        :param db: 数据库会话
        :param department_id: 部门ID
        :return: 待审批单据数
    """
    return db.query(func.count(Expense.id)).join(
        User, Expense.user_id == User.id          # 通过提交人关联到部门
    ).filter(
        User.department_id == department_id,      # 本部门成员提交的
        Expense.status == ExpenseStatus.PENDING,  # 仅待审批
    ).scalar() or 0



# 新建部门
def create_department(db: Session, *, name: str, description: str | None) -> Department:
    """
    新建一个部门（默认启用、默认没有负责人）
        负责人不能在这里指定：负责人必须是"本部门的 manager 角色用户"，而新部门还没有成员，
        正确做法是先建部门，再用用户管理把 manager 放进来（会自动回写 manager_id）。
        :param db: 数据库会话
        :param name: 部门名称，全局唯一
        :param description: 部门备注，可空
        :return: 新建的部门实体
        :raises HTTPException: 400 名称为空；409 部门名称已存在
    """
    # 1.名称去空格后不能为空
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="部门名称不能为空")

    # 2.名称唯一
    exist = db.query(Department).filter(Department.name == clean_name).first()   # 同名部门
    if exist is not None:
        raise HTTPException(status_code=409, detail="部门名称已存在")

    # 3.入库
    department = Department(                        # 新建部门实体
        name=clean_name,            # 部门名称
        description=description,    # 部门备注(可空)
        is_active=True,             # 新建部门默认启用
    )
    db.add(department)
    db.commit()
    db.refresh(department)

    return department



# 修改部门（PUT 全量语义）
def update_department(
    db: Session,                                    # 数据库会话
    department: Department,                         # 被修改的部门实体
    *,
    name: str,                                      # 新名称
    description: str | None,                        # 新备注
    is_active: bool,                                # 新启用状态
    manager_id: int | None,                         # 新负责人用户ID
) -> Department:
    """
    修改一个部门（名称 / 备注 / 启用状态 / 负责人）
    校验顺序（顺序即安全边界）：
        1. 名称去空格后不能为空；
        2. 系统保留部门不允许改名（建号按名称查找「应用开发部」「财务部」）；
        3. 名称全局唯一（排除自己）；
        4. 停用时：保留部门不允许停用；还有启用成员或有待审批单据时拒绝；
        5. 负责人：必须是本部门、启用状态、manager 角色的用户，否则 400；传 None 表示清空。
    :param db: 数据库会话
    :param department: 被修改的部门实体
    :param name: 新名称
    :param description: 新备注
    :param is_active: 新启用状态
    :param manager_id: 新负责人用户ID，None 表示清空
    :return: 修改后的部门实体
    :raises HTTPException: 400 规则不通过；409 名称重复
    """
    # 1.名称兜底
    clean_name = (name or "").strip()               # 去空格后的名称
    if not clean_name:
        raise HTTPException(status_code=400, detail="部门名称不能为空")

    # 2.系统保留部门禁止改名
    if department.name in RESERVED_DEPARTMENT_NAMES and clean_name != department.name:
        raise HTTPException(status_code=400, detail=f"「{department.name}」是系统保留部门,不允许改名")

    # 3.名称唯一（排除自己）
    exist = db.query(Department).filter(            # 同名部门查询
        Department.name == clean_name,              # 名称匹配
        Department.id != department.id,             # 排除自己
    ).first()
    if exist is not None:
        raise HTTPException(status_code=409, detail="部门名称已存在")

    # 4.停用校验
    if not is_active:
        # 4.1 系统保留部门不允许停用
        if department.name in RESERVED_DEPARTMENT_NAMES:
            raise HTTPException(status_code=400, detail=f"「{department.name}」是系统保留部门,不允许停用")
        # 4.2 还有启用成员时不允许停用
        active_user_count = db.query(func.count(User.id)).filter(   # 启用成员查询
            User.department_id == department.id,    # 本部门
            User.is_active == True,                 # 启用成员
        ).scalar() or 0
        if active_user_count > 0:
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"该部门还有 {active_user_count} 名启用员工,请先调整他们的部门后再停用",   # 错误提示
            )
        # 4.3 还有待审批单据时不允许停用
        pending_count = count_department_pending_expenses(db, department.id)   # 待审批单据数
        if pending_count > 0:
            raise HTTPException(
                status_code=400,                    # 参数不合法
                detail=f"该部门还有 {pending_count} 条待审批单据,请先处理后再停用",   # 错误提示
            )

    # 5.负责人校验（传 None 表示清空，不做校验）
    if manager_id is not None:
        manager = db.query(User).filter(User.id == manager_id).first()   # 负责人用户
        if manager is None:
            raise HTTPException(status_code=400, detail="指定的负责人不存在")
        if not manager.is_active:
            raise HTTPException(status_code=400, detail="指定的负责人已被停用")
        if manager.role != UserRole.MANAGER:
            raise HTTPException(status_code=400, detail="只有 manager 角色的用户可以担任部门负责人")
        if manager.department_id != department.id:
            raise HTTPException(status_code=400, detail="该用户不属于本部门,不能担任本部门负责人")

    # 6.写回字段并提交
    department.name = clean_name                    # 部门名称
    department.description = description            # 部门备注
    department.is_active = is_active                # 启用状态
    department.manager_id = manager_id              # 负责人ID

    db.commit()
    db.refresh(department)

    return department
