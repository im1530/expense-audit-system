"""
用户模块路由（人员字典查询，两个接口均需登录鉴权）
    提供两个只读接口：
        - GET /users：已启用用户列表，支持按部门筛选，返回精简人员选项，
          是"选择提交人/审批人"下拉框、人员筛选器的统一数据源；
        - GET /users/roles：用户角色枚举字典，仅 admin 可调用，供用户管理页筛选与角色标签映射。
    数据安全：列表接口用 UserOptionResponse 出参，自动过滤密码、邮箱、手机号等敏感字段；
    两个接口都不分页——人员规模有限，前端一次拉取后本地搜索体验更好。
"""



from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.models.user import User, UserRole
from app.dependencies import get_db                                # 数据库会话依赖
from app.schemas.expense import DictOption
from app.schemas.user import UserOptionResponse
from app.api.deps import get_current_user



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 获取全部已启用的用户列表的接口
@router.get(
    "",                                     # 接口路径；拼上总前缀后完整为 /api/v1/user (和总路由拼接)
    response_model=List[UserOptionResponse],      # 返回值按 List[UserOptionResponse] 格式化
    tags=["用户"],                                # 在 /api/docs 文档里归到"用户"分组显示
    summary="获取全部已启用的用户列表",              # 接口标题
)
def get_user_list(
    department_id: int | None = Query(default=None, description="可选:按部门ID筛选用户"),    # 接收前端入参:部门ID
    db: Session = Depends(get_db),                      # 依赖注入:数据库会话
    current_user: User = Depends(get_current_user),     # 依赖注入:当前登录用户
):
    """
    获取所有已启用的用户字典数据，支持按部门ID过滤
    业务用途:
        - 选择提交人、选择审批人等人员选择下拉框的数据源
        - 报销单列表、审批记录列表按人员筛选的筛选器数据源
        - 传入部门ID可实现「选部门 → 联动展示对应人员」的级联效果
    过滤规则:
        - 只返回 is_active = True 的启用用户，避免前端出现无效可选值
        - 传入 department_id 时仅返回该部门下的启用用户
        - 按用户ID升序排列，保证下拉选项顺序稳定
    权限说明:
        - 需登录鉴权；出参模型已过滤密码、邮箱、手机号等敏感字段
        :param department_id: 可选，按部门ID筛选用户；不传则返回全公司启用用户
        :param db: 数据库会话
        :param current_user: 当前登录用户（本接口只校验登录态，不区分角色）
        :return: 用户选项列表，由 response_model 统一转换与裁剪字段
    """
    # 1.构建基础查询:默认只返回启用状态的用户
    query = db.query(User).filter(User.is_active == True)  # 启用用户查询

    # 2.若前端有传入部门ID，则追加过滤条件,实现级联筛选
    if department_id:
        query = query.filter(User.department_id == department_id)  # 追加部门筛选

    # 3.按用户ID升序排列,执行查询
    users = query.order_by(User.id.asc()).all()  # 用户列表

    # 4.返回:由 response_model=List[UserOptionResponse] 统一转换并过滤字段
    return users



# 获取用户角色枚举列表的接口(仅admin可用)
@router.get(
    path="/roles",                            # 接口路径；拼上总前缀后完整为 /api/v1/user/roles
    response_model=List[DictOption],          # 返回值按 List[DictOption] 格式化
    tags=["用户"],                         # 在 /api/docs 文档里归到"用户管理"分组显示
    summary="获取用户角色枚举列表(仅admin可用)",              # 接口标题
)
def get_user_role_list(
    current_user: User = Depends(get_current_user)  # 登录鉴权
):
    """
    获取所有用户角色枚举选项（仅 admin 可访问）
    业务用途:
        - 用户管理页按角色筛选的筛选器数据源
        - 用户列表、详情页的角色标签展示映射
    说明:
        - 枚举项与 UserRole 定义完全一致，按枚举定义顺序返回，保证选项顺序稳定；
        - 中文名称集中映射在 role_label_map 中，新增角色时必须同步补充，否则会触发 KeyError。
        :param current_user: 当前登录用户，用于角色校验
        :return: 角色字典列表，元素为 {"value": 枚举值, "label": 中文名}
        :raises HTTPException: 403 当前用户不是管理员
    """
    # 1. 角色校验：仅管理员可获取角色字典
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="仅管理员可查看角色字典")

    # 2.枚举值与中文名映射,与枚举定义语义一一对应
    role_label_map = {  # 角色中文名映射
        UserRole.EMPLOYEE: "普通员工",  # 普通员工
        UserRole.MANAGER: "部门经理",  # 部门经理
        UserRole.FINANCE: "财务",  # 财务人员
        UserRole.BOSS: "老板",  # 公司老板
        UserRole.ADMIN: "管理员",  # 系统管理员
    }

    # 3.遍历所有枚举成员,组装成标准选项格式
    role_list = [  # 角色选项列表
        {"value": role_item.value, "label": role_label_map[role_item]} for role_item in UserRole
    ]

    # 4.返回:由 response_model=List[DictOption] 统一转换并过滤字段
    return role_list
