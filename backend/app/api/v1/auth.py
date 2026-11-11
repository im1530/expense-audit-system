"""
认证模块路由（系统身份入口，两个接口均为公开接口，无需鉴权）
    提供"建账号"与"换令牌"两件事：
        - POST /register：创建普通员工账号，内置用户名/邮箱交叉查重、部门存在性校验、bcrypt 哈希存储；
        - POST /login：适配 OAuth2 标准表单格式，支持用户名或邮箱登录，成功后签发 Bearer JWT。
    设计说明：
        - 新用户角色固定为模型默认的 employee，必须归属一个已存在的部门；
        - 注册时角色、激活状态、超级用户标记一律沿用模型默认值，不接受前端指定，防止越权建号；
        - 两个接口都不加 get_current_user 依赖，它们是整条鉴权链路的起点。
"""



import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.dependencies import get_db                                # 数据库会话依赖
from app.config import settings                                    # 授权条款版本
from app.schemas.user import (                                      # 入参/出参模型
    UserCreate,
    UserResponse,
    UserLogin,
    ChangePassword,
    ChangePasswordResponse,
    AIConsentRequest,
)
from app.models import User, UserRole                              # 用户模型与角色枚举
# 密码验密工具、JWT token 签发工具
from app.core.security import verify_password, create_user_token
# 建号/改密逻辑（注册与管理端共用，唯一性/部门规则/密码哈希都在服务层）
from app.services.auth_service import create_user, change_password
# 允许"待改密"状态通过的用户依赖：改密接口自己必须能用，否则死锁
from app.api.deps import get_current_user_allow_pending_password
from sqlalchemy import or_
from fastapi.security import OAuth2PasswordRequestForm



# 创建路由器对象
router = APIRouter()  # 路由器对象

# 日志器：日志里会带 request_id，可据此串联同一次请求的所有记录
logger = logging.getLogger(__name__)  # 日志器



# @router.post(...)：装饰器，声明下面这个函数是一个 POST 接口（POST 常用于 "新建 / 提交"，注册就是创建用户）
# router 是文件顶部的 APIRouter() 分组对象
# 注册接口
@router.post(
    "/register",                # 接口路径；拼上总前缀后完整为 /api/v1/auth/register
    response_model=UserResponse,     # 返回值按 UserResponse 格式化，自动过滤密码等多余字段
    tags=["认证"],                    # 在 /api/docs 文档里归到"认证"分组显示
    summary="用户注册账号"             # 在 /api/docs 文档里显示接口摘要
)
def register(
        user_in: UserCreate,               # 接收前端请求体，并按 UserCreate 自动校验字段
        db: Session = Depends(get_db)      # 依赖注入：自动拿到一个数据库会话
):
    """
    用户注册：创建一个普通员工账户（公开接口）
    访问说明:
        - 无需登录鉴权，任何人可调用；角色固定为 employee，激活状态沿用模型默认值
    核心校验逻辑（都在 services.auth_service.create_user 里，注册与管理端建号共用一套）:
        - 唯一性：username 与 email 交叉查重（四种组合都算冲突），避免"用户名撞别人邮箱"这类脏数据
        - 部门：必须传 department_id，且该部门必须存在并处于启用状态
        - 密码：明文经 bcrypt 哈希后入库，数据库中不保存明文
        - 姓名/手机号：数据库要求非空，服务层会提前拦截并返回 400
    写入流程: add → commit → refresh，commit 后才拿到自增 id，refresh 后返回的对象字段才是完整的
        :param user_in: 注册入参，已由 UserCreate 完成字段校验
        :param db: 数据库会话
        :return: 新建的用户 ORM 对象，经 response_model=UserResponse 过滤掉 password_hash
        :raises HTTPException: 409 用户名或邮箱已被注册；400 未提供部门/部门不存在或已停用/必填项缺失
    """
    # 1.建号逻辑统一收敛到服务层：注册固定 employee 角色，部门仍由用户选择
    user = create_user(  # 新建员工账号
        db,  # 数据库会话
        username=user_in.username,          # 用户名
        email=user_in.email,                # 邮箱
        password=user_in.password,          # 明文密码(服务层哈希)
        full_name=user_in.full_name,        # 姓名
        phone=user_in.phone,                # 手机号
        position=user_in.position,          # 职位(可空)
        role=UserRole.EMPLOYEE,             # 注册只能建普通员工，绝不能由前端指定
        department_id=user_in.department_id,  # 所属部门(employee 必填)
        must_change_password=False,         # 注册的密码是用户自己定的，不需要强制改密
    )

    # 2.记日志：注册成功留痕（日志会自动带 request_id）
    logger.info("用户注册成功 user_id=%s username=%s department_id=%s", user.id, user.username, user.department_id)
    # 3.返回；由 response_model=UserResponse 过滤，不带 password_hash
    return user



# 登录接口
@router.post(
    "/login",        # 接口路径；拼上总前缀后完整为 /api/v1/auth/login
    tags=["认证"],          # 在 /api/docs 文档里归到"认证"分组显示
    summary="用户登录获取token"          # 在 /api/docs 文档里显示接口摘要
)
def login(
        form_data: OAuth2PasswordRequestForm = Depends(),   # 前端表单入参，适配Authorize弹窗
        db: Session = Depends(get_db)                       # 依赖注入：自动拿到一个数据库会话
):
    """
    用户登录：校验账号密码并签发 JWT 访问令牌（公开接口）
    访问说明:
        - 无需登录鉴权；使用 OAuth2 标准表单入参，因此在 Swagger 里点"Authorize"即可直接换取 token
    校验逻辑:
        - 支持用户名或邮箱两种方式登录，任一匹配即可（or_ 条件）
        - 用户不存在与密码错误返回同一句提示，避免被用来枚举系统内的账号
        - 密码用哈希比对，数据库始终不存明文
        - 密码正确但账号已被停用时返回 403，且该判断放在密码校验之后，避免被用来探测账号是否存在
    令牌说明:
        - 载荷放 sub（字符串形式的用户ID）与 ver（令牌版本号），不放角色等可变信息，
          这样改角色无需等旧令牌过期，而改密又能通过 ver 让旧令牌立即失效
        - 返回标准 {"access_token", "token_type": "bearer"}，前端后续请求带 Authorization: Bearer <token>
        - 额外返回 must_change_password：为 true 时前端应立刻跳转改密页，
          因为此时除"修改密码"外的接口都会返回 403
        :param form_data: 用户名/密码表单，由 OAuth2PasswordRequestForm 解析
        :param db: 数据库会话
        :return: 含 access_token、token_type、must_change_password 的字典
        :raises HTTPException: 401 用户名或密码错误；403 账号已停用
    """
    # 1.支持用[用户名]或[邮箱]登录,任一匹配即可
    user = (  # 登录用户查询
        db.query(User)          # 从 User 表建查询
        .filter(or_(User.username == form_data.username, User.email == form_data.username)) # 过滤条件，or_ 表示“或”
        .first()                # 取第一条结果
    )

    # 2.用户不存在,或密码哈希比对失败 -> 统一返回401(不区分"用户不存在/密码错误",防止枚举)
    if not user or not verify_password(form_data.password, user.password_hash):
        # 登录失败只记用户名与原因，绝不记录密码；成功/失败都要留痕，便于安全审计
        logger.warning("登录失败 username=%s 原因=账号或密码不正确", form_data.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 3.账号启用状态校验:被管理员停用后不允许再登录(密码对也不行)
    # 放在密码校验之后,避免"账号已停用"这句提示被用来探测某个用户名是否存在
    if not user.is_active:
        logger.warning("登录失败 username=%s user_id=%s 原因=账号已停用", user.username, user.id)
        raise HTTPException(status_code=403, detail="账号已被停用,请联系管理员")

    # 4.校验通过,签发 token(载荷:sub=用户ID + ver=令牌版本号)
    token = create_user_token(user)  # 访问令牌
    logger.info("登录成功 user_id=%s username=%s role=%s must_change_password=%s",  # 日志模板
                user.id, user.username,  # 用户ID/用户名
                user.role.value if hasattr(user.role, "value") else user.role,  # 角色
                user.must_change_password)  # 是否待改密

    # 5.返回 token 给前端
    #  "token_type": "bearer" 是标准约定:持票人令牌类型，前端后续把 token 放在请求头 Authorization: Bearer <token>
    #  must_change_password: 管理员建号/重置密码后为 True，前端据此强制跳转改密页
    return {
        "access_token": token,  # 访问令牌
        "token_type": "bearer",  # 令牌类型
        "must_change_password": user.must_change_password,  # 是否待改密
    }



# 修改密码接口(登录后调用，无需管理员)
@router.post(
    "/change-password",                 # 接口路径；拼上总前缀后完整为 /api/v1/auth/change-password
    response_model=ChangePasswordResponse,   # 返回用户信息 + 新签发的令牌
    tags=["认证"],                       # 在 /api/docs 文档里归到"认证"分组显示
    summary="修改当前登录用户的密码",       # 接口标题
)
def change_my_password(
        payload: ChangePassword,                                             # 入参：旧密码 + 新密码
        db: Session = Depends(get_db),                                       # 依赖注入：数据库会话
        current_user: User = Depends(get_current_user_allow_pending_password)  # 允许"待改密"状态通过
):
    """
    修改当前登录用户自己的密码（常规改密 + 强制改密都走这里）
    为什么用 get_current_user_allow_pending_password:
        - 被管理员建号/重置密码的账号处于"必须改密"状态，除本接口外全部 403；
        - 若本接口也用标准鉴权依赖，用户会陷入"改不了密码也做不了别的事"的死锁。
    校验规则:
        - 旧密码必须正确（400），新密码不能与旧密码相同（400）
        - 改密成功后 must_change_password 置 False，用户即可正常使用其他接口
    令牌处理（token_version 机制）:
        - 改密会把 users.token_version +1，该账号此前签发的所有令牌立即失效；
        - 为了让当前设备不被自己刚改的密码踢下线，这里用新的版本号重新签发一张令牌一并返回，
          前端直接用返回的 access_token 替换本地旧令牌即可。
        :param payload: 修改密码入参（旧密码、新密码）
        :param db: 数据库会话
        :param current_user: 当前登录用户（允许待改密状态）
        :return: 更新后的用户信息 + 重新签发的访问令牌
        :raises HTTPException: 400 旧密码不正确或新旧密码相同
    """
    # 1.校验旧密码、写新密码哈希、清除强制改密标记、令牌版本 +1，全部在服务层完成
    user = change_password(  # 改密后的用户
        db,                                     # 数据库会话
        current_user,                           # 当前登录用户（要改密的人）
        old_password=payload.old_password,      # 旧密码(用于确认本人操作)
        new_password=payload.new_password,      # 新密码
    )

    # 2.用新版本号重新签发令牌:旧令牌已全部失效,当前会话用这张新令牌继续
    logger.info("用户修改密码成功 user_id=%s（旧令牌全部失效）", user.id)
    # 3.返回更新后的用户信息 + 新令牌
    return {
        "user": user,                       # 更新后的用户信息(不含密码字段)
        "access_token": create_user_token(user),  # 新令牌
        "token_type": "bearer",             # 令牌类型
    }



# AI 数据处理授权接口
@router.post(
    "/ai-consent",                      # 接口路径；拼上总前缀后完整为 /api/v1/auth/ai-consent
    response_model=UserResponse,        # 返回更新后的用户信息
    tags=["认证"],                       # 在 /api/docs 文档里归到"认证"分组
    summary="授权/撤回 AI 数据处理",       # 接口标题
)
def update_ai_consent(
        payload: AIConsentRequest,                                                  # 入参：是否授权
        db: Session = Depends(get_db),                                              # 数据库会话
        current_user: User = Depends(get_current_user_allow_pending_password),      # 允许待改密用户先处理授权
):
    """
    记录或撤回"发票影像发送给模型服务"的授权
        行为：
            - consent=True：记录当前时间与配置的授权版本；
            - consent=False：清空授权时间与版本，后续凭证识别会跳过；
            - 授权只影响凭证识别（图片外发），不影响纯文本的规则/风险预审。
        :param payload: 授权入参
        :param db: 数据库会话
        :param current_user: 当前登录用户
        :return: 更新后的用户信息
    """
    # 1.按入参写入或清空授权字段
    if payload.consent:
        current_user.ai_consent_at = datetime.now()                         # 授权时间
        current_user.ai_consent_version = settings.DOCUMENT_AI_CONSENT_VERSION   # 授权版本
    else:
        current_user.ai_consent_at = None                                   # 撤回授权
        current_user.ai_consent_version = None                              # 清空版本

    # 2.提交并回读
    db.commit()
    db.refresh(current_user)

    # 3.留痕：只记录用户ID、动作与版本，不记录任何影像内容
    logger.info(
        "AI 数据处理授权变更 user_id=%s consent=%s version=%s",
        current_user.id,
        payload.consent,
        current_user.ai_consent_version,
    )
    return current_user

