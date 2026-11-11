"""
API 全局鉴权依赖（所有需登录接口的公共入口）
    提供 JWT 令牌解析与当前登录用户获取能力：
        - oauth2_scheme：声明从请求头 Authorization: Bearer <token> 中提取令牌，
          同时让 Swagger 文档出现"授权"按钮，方便调试时填入 token；
        - _load_current_user：内部基础校验（验签解析 → 取 sub → 查库存在性 → 启用状态），
          失败按语义抛 401（未登录/令牌失效/用户不存在）或 403（账号已停用）；
        - get_current_user：业务接口的标准依赖，在基础校验之上再拦"必须先改密"的账号（403）；
        - get_current_user_allow_pending_password：只有"修改密码"接口使用，
          允许 must_change_password=True 的用户通过，否则被强制改密的用户会连改密接口都进不去。
    使用方式：接口函数声明 `current_user: User = Depends(get_current_user)` 即可拿到登录用户，无需重复解析令牌。
    职责边界：本模块只回答"你是否已登录、账号是否可用"，"这个角色能不能做这件事"由各接口自行判断（如 has_permission）。
"""



from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.dependencies import get_db              # 数据库会话
from app.models import User                        # 用户模型
from app.core.security import decode_access_token, TOKEN_VERSION_CLAIM  # token 验签解析 + 版本声明键名



# 从请求头 Authorization: Bearer <token> 里自动提取 token 的规则
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")  # 令牌提取器
"""
OAuth2PasswordBearer：自动从请求头读 Bearer xxx，并让 Swagger 出现小锁输入框
tokenUrl 只是给 Swagger 文档指示"登录接口在哪"，用于点锁获取 token
"""



# 内部基础校验: 验签 + 查库 + 启用状态
def _load_current_user(token: str, db: Session) -> User:
    """
    解析令牌并加载当前登录用户（只做 401/403 的基础校验，不看是否需要改密）
        三层校验（全部复用同一个 credentials_exception，对外不区分失败原因）：
            1. 验签解析：decode_access_token 校验签名与有效期，失败即 401；
            2. 提取用户标识：载荷中必须带 sub（字符串形式的用户ID），缺失即 401；
            3. 存在性校验：按 ID 查库，用户已被删除或数据异常同样返回 401。
        凭证版本校验：令牌里的 ver 必须等于 users.token_version。
        改密/重置密码时该字段会 +1，因此所有旧令牌立即失效（不必等 24 小时自然过期）。
        老令牌没有 ver 声明时按 0 处理，兼容改造前签发的令牌。
        启用状态校验：查到用户后再看 is_active，被管理员停用的账号立即失效（403），
        这样"停用账号"不用等 token 过期就生效；停用与登录是两个不同的失败语义，所以错误码分开。
        设计目的：不区分"令牌无效 / 用户不存在"，避免攻击者靠错误差异枚举账号。
        :param token: 由 oauth2_scheme 从请求头自动取出的 Bearer 令牌
        :param db: 数据库会话
        :return: 当前登录用户的 ORM 对象
        :raises HTTPException: 401 未登录/令牌失效/用户不存在；403 账号已被停用
    """
    # 统一的未登录异常,后面多处复用
    credentials_exception = HTTPException(  # 未登录异常
        status_code=401,                            # 401 = 未认证（没登录/token失效）
        detail="未登录或登录已失效，请重新登录",         # 返回给前端的错误说明
        headers={"WWW-Authenticate": "Bearer"},     # 告知前端用 Bearer token 方式重新认证
    )

    # 1.验签解析 token
    payload = decode_access_token(token)  # 令牌载荷
    if payload is None:
        raise credentials_exception

    # 2.取出 sub(用户id),取不到视为非法
    user_id = payload.get("sub")  # 用户ID
    if user_id is None:
        raise credentials_exception

    # 3.按 用户id 查库,查不到说明用户已被删除
    # db.query(User) 建查询，.filter(条件) 过滤，.first() 取第一条
    user = db.query(User).filter(User.id == int(user_id)).first()  # 当前用户
    if user is None:
        raise credentials_exception

    # 4.凭证版本校验:改密/重置密码后 token_version 会 +1,旧令牌在这里被拦住
    # 老令牌没有 ver 声明时按 0 处理,兼容本次改造之前签发的令牌
    if payload.get(TOKEN_VERSION_CLAIM, 0) != user.token_version:
        raise HTTPException(status_code=401, detail="登录状态已失效(密码已修改)，请重新登录")

    # 5.账号启用状态校验:被管理员停用后,旧 token 立即失效,不用等它自然过期
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被停用,请联系管理员")

    return user



# 验签依赖: 获取当前登录用户(业务接口标准入口)
def get_current_user(
        request: Request,                               # 请求对象：用于把用户ID挂到 request.state，供访问日志使用
        token: str = Depends(oauth2_scheme),        # 执行依赖：自动从请求头取出 Bearer token
        db: Session = Depends(get_db)               # 注入数据库会话
) -> User:
    """
    获取当前登录用户（所有业务接口的标准鉴权依赖）
        在基础校验之后，额外拦截"必须先修改初始密码"的账号：
        管理员建号或重置密码后，账号处于"密码是别人设的"状态，此时除修改密码外不应做任何操作，
        因此这里直接 403，前端据此跳转到改密页面。
        :param request: 当前请求对象（用于把用户ID写入 request.state，访问日志会读它）
        :param token: 由 oauth2_scheme 从请求头自动取出的 Bearer 令牌
        :param db: 数据库会话
        :return: 当前登录用户的 ORM 对象
        :raises HTTPException: 401 未登录/令牌失效/用户不存在；403 账号停用或需要先改密
    """
    # 1.基础校验（401 / 账号停用 403）
    user = _load_current_user(token, db)  # 当前用户

    # 2.强制改密拦截:必须先调 /auth/change-password 把初始密码换掉
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="请先修改初始密码后再使用其他功能")

    # 3.把用户ID挂到请求上，访问日志中间件会读取它（不记录敏感信息）
    request.state.user_id = user.id  # 记录用户ID

    # 4.通过，返回当前登录用户
    return user



# 验签依赖(允许"待改密"状态通过): 仅供修改密码接口使用
def get_current_user_allow_pending_password(
        request: Request,                               # 请求对象：用于记录访问日志里的用户ID
        token: str = Depends(oauth2_scheme),        # 执行依赖：自动从请求头取出 Bearer token
        db: Session = Depends(get_db)               # 注入数据库会话
) -> User:
    """
    获取当前登录用户（不做强制改密拦截，仅供"修改密码"接口使用）
        为什么需要这个变体：被强制改密的用户除了改密接口什么都调不了，
        如果改密接口自己也用 get_current_user，用户会陷入"改不了密码也出不去"的死锁。
        :param request: 当前请求对象
        :param token: 由 oauth2_scheme 从请求头自动取出的 Bearer 令牌
        :param db: 数据库会话
        :return: 当前登录用户的 ORM 对象（可能 still 处于待改密状态）
        :raises HTTPException: 401 未登录/令牌失效/用户不存在；403 账号已停用
    """
    # 1.先做基础校验（验签/版本/启用状态），失败抛 401 或 403
    user = _load_current_user(token, db)  # 当前用户
    # 2.把用户ID挂到请求上，访问日志中间件会读取它
    request.state.user_id = user.id  # 记录用户ID
    # 3.放行"待改密"用户（本依赖只给修改密码接口用）
    return user



# 可选令牌提取器：没有 Authorization 头时不报错，返回 None
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)  # 可选令牌提取器



# 可选登录依赖: 有令牌就解析，没有就返回 None
def get_current_user_optional(
        request: Request,                                      # 请求对象：用于记录访问日志里的用户ID
        token: str | None = Depends(oauth2_scheme_optional),   # 没有令牌时为 None
        db: Session = Depends(get_db)                          # 注入数据库会话
) -> User | None:
    """
    尝试获取当前登录用户（未登录不报错，返回 None）
        用途：附件下载接口既支持"带 Bearer 头"也支持"带短期签名 token"，
        当请求走签名地址时根本没有 Authorization 头，不能因为缺少令牌直接 401。
        注意：令牌无效时同样返回 None（由调用方决定是 401 还是 403），不做"半吊子"放行。
        :param request: 当前请求对象
        :param token: 由可选提取器解析出的 Bearer 令牌，可能为 None
        :param db: 数据库会话
        :return: 用户对象；未登录/令牌无效/账号停用时返回 None
    """
    # 1.没有 Authorization 头：直接返回 None，由调用方决定是否 401
    if not token:
        return None
    # 2.有令牌就尝试解析；无效/停用等异常一律当"未登录"处理，不半吊子放行
    try:
        user = _load_current_user(token, db)  # 当前用户
    except HTTPException:
        return None
    # 3.记录到 request.state，供访问日志使用
    request.state.user_id = user.id  # 记录用户ID
    return user
