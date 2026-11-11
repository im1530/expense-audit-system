"""
v1 版本路由包（app/api/v1）
    按业务模块拆分子路由：每个模块文件内定义 `router = APIRouter()`，再由 router.py 统一挂载前缀与文档分组：
        - auth.py        认证：注册、登录（公开接口，无需鉴权）
        - user.py        用户：已启用用户下拉列表、用户角色字典
        - department.py  部门：已启用部门下拉列表（公开接口）
        - expense.py     报销：草稿增删改查、三级列表（我的/本部门/全公司）、明细管理、状态流转、字典、审批轨迹
        - approval.py    审批：审批执行、我的待办、我的已办、审批结果字典
    子路由自身不带前缀，前缀与 tags 在 router.py 的 include_router 中统一指定，便于统一维护。
"""
