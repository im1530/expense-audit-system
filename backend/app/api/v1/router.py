"""
API v1 版本总路由（所有业务子路由的汇总挂载点）
    统一管理各业务模块子路由的前缀与文档分组，全局前缀为 /api/v1，最后由 app/main.py 挂到应用上：
        - auth         /auth              认证：注册、登录（公开接口）
        - expense      /expense           报销：草稿增删改查、三级列表、明细管理、状态流转、字典、审批轨迹
        - approval     /approval          审批：审批执行、我的待办、我的已办、审批结果字典
        - department   /department        部门：已启用部门列表（公开接口）
        - user         /user              用户：已启用用户列表、用户角色字典
        - admin_user   /admin/user        管理端：用户管理（列表/建号/详情/修改/重置密码）
        - admin_dept   /admin/department  管理端：部门管理（列表/新建/详情/修改）
        - admin_rule   /admin/rule        管理端：规则管理（列表/新建/详情/修改/删除）
        - admin_flow   /admin/approval-flow 管理端：审批流管理（列表/新建/详情/修改/删除）
        - attachment   /attachment        附件：发票上传、列表、签名下载、软删除
        - report       /report            报表：总览/按月/按部门/按类别统计 + Excel 导出
        - agent        /agent             AI 预审：触发、查询最新结果、工作流轨迹
    维护约定：
        - 新增业务模块只需在本文件追加"导入 + include_router"两行，前缀与 tags 在这里统一定义；
        - 前缀统一用单数形式（/expense、/approval、/user…），前端对接以本文件为准；
        - 同一个 router 只能挂载一次，重复挂载会让同一路径注册出多个 path operation，
          导致 OpenAPI 文档出现 Duplicate Operation ID 警告、请求被重复匹配。
"""


from fastapi import APIRouter


# 总路由对象，main.py 挂载的就是它
# prefix="/api/v1": 给这组路由统一加的前缀
api_router = APIRouter(prefix="/api/v1")    # 总路由对象：所有业务子路由都挂在它下面


# 1.挂载认证子路由，前缀 /auth，文档归到"认证"组
from app.api.v1 import auth
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])


# 2.挂载报销子路由，前缀 /expense，文档归到"报销"组
from app.api.v1 import expense
api_router.include_router(expense.router, prefix="/expense", tags=["报销"])


# 3.挂载审批子路由，前缀 /approval，文档归到"审批"组
from app.api.v1 import approval
api_router.include_router(approval.router, prefix="/approval", tags=["审批"])


# 4.挂载部门子路由，前缀 /department，文档归到"部门"组
from app.api.v1 import department
api_router.include_router(department.router, prefix="/department", tags=["部门"])


# 5.挂载用户子路由，前缀 /user，文档归到"用户"组
from app.api.v1 import user
api_router.include_router(user.router, prefix="/user", tags=["用户"])


# 6.挂载管理端-用户子路由，前缀 /admin/user，文档归到"管理端"组
from app.api.v1 import admin_user
api_router.include_router(admin_user.router, prefix="/admin/user", tags=["管理端"])


# 7.挂载管理端-部门子路由，前缀 /admin/department，文档归到"管理端"组
from app.api.v1 import admin_department
api_router.include_router(admin_department.router, prefix="/admin/department", tags=["管理端"])


# 8.挂载管理端-规则子路由，前缀 /admin/rule，文档归到"管理端"组
from app.api.v1 import admin_rule
api_router.include_router(admin_rule.router, prefix="/admin/rule", tags=["管理端"])


# 9.挂载管理端-审批流子路由，前缀 /admin/approval-flow，文档归到"管理端"组
from app.api.v1 import admin_flow
api_router.include_router(admin_flow.router, prefix="/admin/approval-flow", tags=["管理端"])


# 10.挂载附件子路由，前缀 /attachment，文档归到"附件"组
from app.api.v1 import attachment
api_router.include_router(attachment.router, prefix="/attachment", tags=["附件"])


# 11.挂载报表子路由，前缀 /report，文档归到"报表"组
from app.api.v1 import report
api_router.include_router(report.router, prefix="/report", tags=["报表"])


# 12.挂载 AI 预审子路由，前缀 /agent，文档归到"AI预审"组
from app.api.v1 import agent
api_router.include_router(agent.router, prefix="/agent", tags=["AI预审"])


# 13.挂载站内通知子路由，前缀 /notifications，文档归到"通知"组
from app.api.v1 import notification
api_router.include_router(notification.router, prefix="/notifications", tags=["通知"])
