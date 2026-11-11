"""
安全认证工具（密码哈希 + JWT 令牌）
    提供身份认证所需的全部密码学能力，只依赖 config，不访问数据库：
        - 密码哈希：hash_password / verify_password，用 bcrypt 做单向哈希，数据库只保存结果
        - 令牌签发：create_access_token，把用户身份写进载荷并用 SECRET_KEY 做 HS256 签名
        - 令牌验签：decode_access_token，校验签名与有效期后取回载荷，失败统一返回 None
    安全说明：
        - 单向哈希不可逆：hash 之后推不出原密码，就算数据库泄露也拿不到明文；
        - verify 是把用户输入重新算一遍哈希再和存储值比对，不需要解密；
        - 同一密码每次 hash 结果都不同（库自动加盐），但 verify 都能通过；
        - SECRET_KEY 一旦泄露即可伪造任意令牌，上线前必须从 .env 注入足够随机的长串，禁止沿用默认空串。
"""


from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from app.config import settings


# ===== 密码哈希 =====
# 指定用 bcrypt 算法做哈希；deprecated="auto" 自动淘汰旧算法
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")  # 密码哈希器


# 函数：密码哈希
def hash_password(plain: str) -> str:
    """
    明文密码哈希：注册/改密时调用，把明文转成不可逆的密文再入库
        bcrypt 自带盐值，同一明文每次调用得到的密文都不同，这是正常现象，不影响后续验密。
        :param plain: 用户提交的明文密码
        :return: 形如 "$2b$12$..." 的 bcrypt 密文，直接存入 users.password_hash
    """
    return pwd_context.hash(plain)


# 函数：密码验密
def verify_password(plain: str, hashed: str) -> bool:
    """
    密码验密：登录时调用，比对用户输入与数据库里保存的密文是否匹配
        内部先用密文中的盐重新计算哈希，再做等值比较，因此不需要也不可能"解密"。
        :param plain: 用户本次登录输入的明文密码
        :param hashed: 数据库 users.password_hash 字段中保存的密文
        :return: True 表示密码正确；False 表示不匹配（密码错误或密文已损坏）
    """
    return pwd_context.verify(plain, hashed)



# ===== JWT token 签发、验签 =====
# 函数：签发 JWT token
def create_access_token(data: dict, expires_minutes: int | None = None) -> str:
    """
    签发 JWT 访问令牌：登录成功后调用，把用户身份编码成前端可携带的字符串令牌
        处理流程：复制载荷 → 写入 exp 过期时间 → 用 SECRET_KEY + HS256 签名并编码。
        - sub 约定：本系统统一放字符串形式的用户ID（如 {"sub": str(user.id)}），jose 要求 sub 必须是字符串；
        - 过期时间：不传 expires_minutes 时使用配置的 ACCESS_TOKEN_EXPIRE_MINUTES（默认 24 小时）；
        - 载荷是明文可见（Base64）而非加密的，因此不要往里面塞密码、手机号等敏感信息。
        :param data: 要签入令牌的载荷字典，例如 {"sub": "1"}
        :param expires_minutes: 自定义有效期（分钟），不传则用全局配置
        :return: 可直接返回给前端的 JWT 字符串
    """
    # 1.先复制一份载荷，避免修改调用方传入的字典
    to_encode = data.copy()  # 载荷副本

    # 2.计算过期时刻：有传入有效期就用传入值，否则用配置里的默认 24 小时
    minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES  # 有效期分钟数
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)  # 过期时刻

    # 3.把过期时间写进载荷（exp 是 JWT 标准声明，验签时会自动校验）
    to_encode.update({'exp':expire})

    # 4.用 SECRET_KEY 与 HS256 算法签名并编码成 token 字符串
    encoded_jwt = jwt.encode(  # 签名后的令牌
        to_encode,                          # 载荷：要编码的字典（用户身份 + 过期时间）
        settings.SECRET_KEY,                # 签名密钥：用它做 HS256 签名，防篡改
        algorithm=settings.JWT_ALGORITHM    # 允许的签名算法：HS256
    )

    # 5.返回可直接给前端的 token
    return encoded_jwt



# 令牌版本声明键名：与用户表的 token_version 对应，改密后旧令牌失效
TOKEN_VERSION_CLAIM = "ver"  # 令牌版本键



# 为指定用户签发访问令牌
def create_user_token(user) -> str:
    """
    为某个用户签发访问令牌（登录、改密后重新签发都走这里）
        载荷固定两项：
            - sub：用户ID（字符串），jose 要求 sub 必须是字符串；
            - ver：令牌版本号，取自 users.token_version。改密/重置密码时该字段 +1，
              鉴权时拿令牌里的 ver 与库中值比对，不一致即判定为"旧令牌"直接拒绝；
        这样一来 JWT 仍然是"无状态"的（不需要在服务端存会话），但拿到了"改密即踢下线"的能力。
        :param user: 用户 ORM 对象（必须已持久化，能读到 id 与 token_version）
        :return: 可直接返回给前端的 JWT 字符串
    """
    return create_access_token({
        "sub": str(user.id),                # 主体：用户ID（字符串）
        TOKEN_VERSION_CLAIM: user.token_version,   # 凭证版本：改密后旧令牌验不过
    })


# 函数:JWT token 验签解析
def decode_access_token(token: str) -> dict | None:
    """
    验签解析 JWT 令牌：鉴权依赖 get_current_user 调用，用来从令牌中取回用户身份
        校验内容：签名是否被篡改、令牌是否已过期、格式是否合法；只要有一项不通过就返回 None，
        由调用方统一翻译成 401 未登录，避免把底层异常细节暴露给前端。
        :param token: 请求头 Authorization: Bearer <token> 中取出的一段 token 字符串
        :return: 解析成功的载荷字典（含 sub 用户ID、exp 过期时间等）；验签失败返回 None
    """
    try:
        payload = jwt.decode(  # 令牌载荷
            token,                                  # 待验签的 token 字符串
            settings.SECRET_KEY,                    # 签名密钥：用签发时的密钥验签
            algorithms=[settings.JWT_ALGORITHM]     # 允许的签名算法：HS256
        )
        return payload
    except JWTError:    # 签名被改、过期、格式错都归这里
        return None



# 附件下载签名令牌的用途标记：防止下载令牌被当成登录令牌使用（反之亦然）
ATTACHMENT_TOKEN_SCOPE = "attachment"  # 附件令牌用途



# 为附件签发短期下载令牌
def create_attachment_token(attachment_id: int, expires_minutes: int) -> str:
    """
    为某个附件签发短期下载令牌（前端 <img>/<a> 无法带 Authorization 头时用）
        载荷：
            - sub：附件ID；
            - scope：固定 "attachment"，与登录令牌区分开——
              即使这个令牌泄露，也只能下载这一张附件，不能拿去调业务接口。
        有效期由调用方传入（通常取 settings.ATTACHMENT_URL_EXPIRE_MINUTES），过期即失效。
        :param attachment_id: 附件ID
        :param expires_minutes: 有效期（分钟）
        :return: 签名后的令牌字符串，拼在下载地址的 ?token= 后面
    """
    return create_access_token(
        {  # 令牌载荷
            "sub": str(attachment_id),              # 主体：附件ID
            "scope": ATTACHMENT_TOKEN_SCOPE,        # 用途：仅用于附件下载
        },
        expires_minutes=expires_minutes,  # 有效期分钟
    )



# 校验附件下载令牌
def decode_attachment_token(token: str) -> int | None:
    """
    校验附件下载令牌，返回它指向的附件ID
        校验内容：签名/过期时间（decode_access_token 已处理）+ scope 必须是 attachment。
        任何一项不通过都返回 None，由调用方转成 401，避免泄露具体失败原因。
        :param token: 下载地址里的 ?token= 值
        :return: 附件ID；令牌无效/过期/用途不对时返回 None
    """
    # 1.先验签并校验有效期（失败返回 None）
    payload = decode_access_token(token)  # 令牌载荷
    if not payload:
        return None
    # 2.用途必须是附件下载，防止拿登录令牌来换文件
    if payload.get("scope") != ATTACHMENT_TOKEN_SCOPE:
        return None
    # 3.把 sub 转成附件ID；格式不对按无效令牌处理
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None



