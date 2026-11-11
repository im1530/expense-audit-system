"""
部门模块路由（公共基础数据字典，公开接口）
    只提供一个查询接口：
        - GET /departments/：返回所有 is_active = True 的部门，按 ID 升序排列，无需登录即可访问
    业务用途：用户注册表单的"所属部门"选择、用户/报销单列表的部门筛选器。
    说明：部门是注册前置数据，因此不做鉴权；新增/修改/停用部门暂未开放接口，由 SQL 初始化脚本维护。
"""



from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from app.models.department import Department
from app.dependencies import get_db                                # 数据库会话依赖
from app.schemas.department import DepartmentResponse



# 创建路由器对象
router = APIRouter()  # 路由器对象



# 获取全部启用的部门列表的接口
@router.get(
    "/",                                    # 接口路径；拼上总前缀后完整为 /api/v1/department/ （末尾带斜杠，前端请求也需带 / ，否则会 307 跳转）
    response_model=List[DepartmentResponse],     # 返回值按 List[DepartmentResponse] 格式化
    tags=["部门"],                                # 在 /api/docs 文档里归到"部门"分组显示
    summary="获取全部启用的部门列表"                 # 接口标题
)
def get_department_list(
        db: Session = Depends(get_db)            # 依赖注入:数据库会话
):
    """
    获取所有已启用的部门字典数据（公开接口，无需登录）
    业务用途:
        - 用户注册表单的「所属部门」下拉选择框数据源
        - 用户列表、报销单列表的部门筛选器数据源
        - 属于公共基础数据，前端页面初始化时可直接拉取渲染
    过滤规则:
        - 只返回 is_active = True 的启用部门
        - 按部门 ID 升序排列，保证下拉选项顺序稳定一致
        :param db: 数据库会话
        :return: 部门列表，包含部门ID、名称、负责人ID、备注，由 response_model 统一裁剪字段
    """
    # 1.查询所有启用状态的部门,按部门ID升序排列
    departments = db.query(Department).filter(  # 启用部门查询
        Department.is_active == True  # 仅启用状态
    ).order_by(Department.id.asc()).all()

    # 2.返回:由 response_model=List[DepartmentResponse] 统一转换并过滤字段
    return departments
