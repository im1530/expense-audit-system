"""
账号服务（账号写操作的唯一实现：建号 + 改资料/改角色/启停，注册接口与管理端共用）
    把"创建/修改一个账号"涉及的全部规则收敛到这里，避免注册接口和管理端各写一套、规则慢慢分叉：
        - 唯一性：用户名与邮箱交叉查重（用户名撞别人邮箱也算冲突），数据库唯一索引兜底；
        - 密码：明文只在校验通过后立刻 bcrypt 哈希，任何环节都不落库明文；
        - 部门规则（按角色，见 resolve_department / _resolve_department_for_update）：
            · admin    → 默认挂「应用开发部」，显式传入则用传入值；
            · boss     → 不挂部门，传了直接报错（避免前端以为生效了）；
            · finance  → 固定「财务部」，传了别的部门直接报错；
            · manager  → 必须指定部门，且该部门当前不能已有负责人（负责人以 departments.manager_id 为准）；
            · employee → 必须指定部门；
        - 经理与部门互指：建/改 manager 时同步回写 departments.manager_id，改角色或换部门时解除旧指向；
        - 自锁保护：不能停用自己、不能修改自己的角色、不能把最后一个启用的 admin 停用或降级；
        - 停用保护：有待他审批的单据、或他是某个部门的负责人时，停用会被拒绝（避免单据卡死无人可批）；
        - 密码管理：管理员建号/重置密码统一把 must_change_password 置 True，
          用户必须先用"修改密码"接口把"别人设的密码"换掉，才能使用其他业务接口；
        - 令牌失效：改密/重置密码时 token_version +1，JWT 里带着这个版本号，
          鉴权时比对不一致即拒绝，因此"改密 = 所有旧令牌立刻失效"。
    约定：本模块直接 commit（每次写操作是一次独立事务），失败时抛 HTTPException 由 FastAPI 统一转成错误响应。
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models import User, Department, UserRole
from app.core.security import hash_password, verify_password
from app.services.approval_service import count_pending_approvals_for_user



# 系统约定部门的名称（按名称取，避免把部门ID硬编码在代码里）
ADMIN_DEPARTMENT_NAME = "应用开发部"      # admin 默认归属部门
FINANCE_DEPARTMENT_NAME = "财务部"        # finance 固定归属部门



# 按名称取部门
def _department_by_name(db: Session, name: str) -> Department | None:
    """
    按部门名称查询部门（用于「应用开发部」「财务部」这类约定部门）
        为什么按名称而不是按ID：部门ID随初始化数据而变，硬编码ID换个环境就失效。
        :param db: 数据库会话
        :param name: 部门名称，如 "财务部"
        :return: 部门对象；不存在返回 None
    """
    return db.query(Department).filter(Department.name == name).first()



# 按ID取部门并要求存在且启用
def _get_active_department(db: Session, department_id: int) -> Department:
    """
    按ID取部门，并校验"存在 + 启用"
        停用的部门不能再接新账号，否则新人的单据会落到一个已下线的部门里。
        :param db: 数据库会话
        :param department_id: 部门ID
        :return: 部门对象
        :raises HTTPException: 400 部门不存在或已停用
    """
    # 1.按ID查出部门
    department = db.query(Department).filter(Department.id == department_id).first()   # 部门实体
    # 2.不存在：给 400（属于入参问题）
    if department is None:
        raise HTTPException(status_code=400, detail="所选部门不存在")
    # 3.存在但已停用：同样不能接新账号
    if not department.is_active:
        raise HTTPException(status_code=400, detail="所选部门已停用,不可使用")
    # 4.返回校验通过的部门
    return department



# 按角色解析最终归属部门
def resolve_department(db: Session, role: UserRole, department_id: int | None) -> Department | None:
    """
    按角色规则解析新建用户的最终归属部门
    规则以业务约定为准（admin 应用开发部 / boss 无 / finance 财务部 / manager·employee 必填）：
        - 这里只做"该角色最终属于哪个部门"的判断，不负责写库；
        - 约定部门找不到（或已停用）时抛 500，因为这属于系统初始化数据缺失，不是调用方的问题。
        :param db: 数据库会话
        :param role: 待创建用户的角色
        :param department_id: 调用方传入的部门ID（可能为 None，也可能与规则冲突）
        :return: 部门对象；boss 返回 None
        :raises HTTPException: 400 入参与角色规则冲突 / 部门不存在或停用；500 约定部门未配置
    """
    # 1.boss：不挂部门
    if role == UserRole.BOSS:
        # 显式传了部门说明调用方理解有误，直接报错而不是静默忽略
        if department_id is not None:
            raise HTTPException(status_code=400, detail="boss 角色不挂部门,请不要传入所属部门")
        return None

    # 2.admin：默认应用开发部，传了就用传入的（仍需存在且启用）
    if role == UserRole.ADMIN:
        if department_id is not None:
            return _get_active_department(db, department_id)
        department = _department_by_name(db, ADMIN_DEPARTMENT_NAME)   # 应用开发部
        if department is None or not department.is_active:
            raise HTTPException(status_code=500, detail=f"系统未配置启用中的「{ADMIN_DEPARTMENT_NAME}」部门")
        return department

    # 3.finance：固定财务部，传了别的部门视为冲突
    if role == UserRole.FINANCE:
        department = _department_by_name(db, FINANCE_DEPARTMENT_NAME)   # 财务部
        if department is None or not department.is_active:
            raise HTTPException(status_code=500, detail=f"系统未配置启用中的「{FINANCE_DEPARTMENT_NAME}」部门")
        if department_id is not None and department_id != department.id:
            raise HTTPException(status_code=400, detail="财务人员必须属于财务部,不能选择其他部门")
        return department

    # 4.manager / employee：必须指定部门
    if department_id is None:
        raise HTTPException(status_code=400, detail="该角色必须指定所属部门")
    return _get_active_department(db, department_id)



# 创建账号
def create_user(
    db: Session,                                    # 数据库会话
    *,
    username: str,                                  # 登录用户名
    email: str,                                     # 邮箱
    password: str,                                  # 明文初始密码
    full_name: str | None,                          # 真实姓名
    phone: str | None,                              # 手机号
    position: str | None,                           # 职位
    role: UserRole,                                 # 角色
    department_id: int | None,                      # 部门ID
    must_change_password: bool = True,              # 是否强制改密
) -> User:
    """
    创建一个用户账号（注册接口与管理端建号共用）
    执行顺序：
        1. 必填兜底：full_name / phone 在数据库里是 NOT NULL，这里提前拦住，避免抛 500 IntegrityError；
        2. 唯一性：用户名与邮箱交叉查重，命中即 409；
        3. 部门规则：交给 resolve_department 解析最终部门；
        4. manager 额外校验：目标部门必须没有负责人，否则 400（负责人以 departments.manager_id 为准）；
        5. 入库：bcrypt 哈希后 add/flush/commit/refresh；manager 场景在 flush 拿到 id 后回写部门负责人。
    改密标记：默认 True（管理员设的初始密码属于"别人设的密码"，必须先改掉）；
    用户自己注册时由调用方传 False，因为密码是他自己定的。
    注意：本函数会 commit，调用方不要再重复提交；创建成功后返回带 id 的 User 实体。
        :param db: 数据库会话
        :param username: 登录用户名（唯一）
        :param email: 邮箱（唯一）
        :param password: 明文初始密码，本函数内部哈希
        :param full_name: 真实姓名，必填
        :param phone: 手机号，必填
        :param position: 职位，可空
        :param role: 角色枚举
        :param department_id: 调用方指定的部门ID，按角色规则校验
        :param must_change_password: 是否要求用户下次登录后先改密码（注册传 False）
        :return: 新建的用户 ORM 对象
        :raises HTTPException: 409 用户名或邮箱已存在；400 字段缺失/部门规则冲突/部门已有负责人
    """
    # 1.必填兜底：数据库这两列是 NOT NULL，提前给出 400，比让数据库抛 500 更友好
    if not full_name:
        raise HTTPException(status_code=400, detail="真实姓名不能为空")
    if not phone:
        raise HTTPException(status_code=400, detail="手机号不能为空")

    # 2.唯一性校验：用户名与邮箱交叉查重（四种组合都算冲突）
    exist_user = db.query(User).filter(             # 冲突用户查询
        or_(
            User.username == username,          # 用户名冲突
            User.email == username,             # 邮箱当用户名
            User.username == email,             # 用户名当邮箱
            User.email == email,                # 邮箱冲突
        )
    ).first()                                       # 冲突用户
    if exist_user:
        raise HTTPException(status_code=409, detail="用户名或邮箱已被注册")

    # 3.按角色规则解析最终部门（可能为 None，例如 boss）
    department = resolve_department(db, role, department_id)   # 最终归属部门

    # 4.manager 额外校验：一个部门只能有一个负责人（以 departments.manager_id 为准）
    if role == UserRole.MANAGER and department is not None and department.manager_id is not None:
        raise HTTPException(status_code=400, detail="该部门已有负责人,请先更换或清空部门负责人")

    # 5.构建用户对象：密码只在这里哈希一次，角色/状态显式赋值，不接受前端传 is_superuser
    user = User(                                    # 新建用户实体
        username=username,                                      # 登录用户名
        email=email,                                            # 邮箱
        password_hash=hash_password(password),                  # 密码密文(不存明文)
        full_name=full_name,                                    # 真实姓名
        phone=phone,                                            # 手机号
        position=position,                                      # 职位(可空)
        role=role,                                              # 角色
        department_id=department.id if department is not None else None,   # 所属部门(boss 为空)
        must_change_password=must_change_password,              # 是否强制下次登录先改密
    )

    # 6.先入库拿到自增ID，再把"部门负责人"指向这个新用户，两步在同一事务内完成
    db.add(user)
    if role == UserRole.MANAGER and department is not None:
        db.flush()                       # flush 后 user.id 才有值
        department.manager_id = user.id  # 回写部门负责人，保证 users.role 与 departments.manager_id 一致

    # 7.提交并回读（refresh 才能拿到自增ID与数据库默认值）
    db.commit()
    db.refresh(user)

    return user



# 按角色解析"修改账号"时的最终归属部门
def _resolve_department_for_update(
    db: Session,                                    # 数据库会话
    user: User,                                     # 被修改的用户
    role: UserRole,                                 # 修改后的角色
    department_id: int | None,                      # 传入的部门ID
) -> Department | None:
    """
    按角色规则解析修改后的归属部门（与建号规则基本一致，唯一差别是 admin 默认保留原部门）
        admin 的差别：建号时没传部门会挂「应用开发部」，但改资料时没传部门应当保留原部门，
        否则管理员只是改个手机号，部门却被重置了。
        :param db: 数据库会话
        :param user: 被修改的用户（需要读出现有部门）
        :param role: 修改后的角色
        :param department_id: 调用方传入的部门ID
        :return: 部门对象；boss 返回 None
        :raises HTTPException: 400 入参与角色规则冲突 / 部门不存在或停用；500 约定部门未配置
    """
    # 1.boss：不挂部门
    if role == UserRole.BOSS:
        if department_id is not None:
            raise HTTPException(status_code=400, detail="boss 角色不挂部门,请不要传入所属部门")
        return None

    # 2.admin：传了用传入的，没传保留原部门，原部门也没有才回落到应用开发部
    if role == UserRole.ADMIN:
        if department_id is not None:
            return _get_active_department(db, department_id)
        if user.department_id is not None:
            return _get_active_department(db, user.department_id)
        department = _department_by_name(db, ADMIN_DEPARTMENT_NAME)   # 应用开发部
        if department is None or not department.is_active:
            raise HTTPException(status_code=500, detail=f"系统未配置启用中的「{ADMIN_DEPARTMENT_NAME}」部门")
        return department

    # 3.finance：固定财务部
    if role == UserRole.FINANCE:
        department = _department_by_name(db, FINANCE_DEPARTMENT_NAME)   # 财务部
        if department is None or not department.is_active:
            raise HTTPException(status_code=500, detail=f"系统未配置启用中的「{FINANCE_DEPARTMENT_NAME}」部门")
        if department_id is not None and department_id != department.id:
            raise HTTPException(status_code=400, detail="财务人员必须属于财务部,不能选择其他部门")
        return department

    # 4.manager / employee：必须指定部门
    if department_id is None:
        raise HTTPException(status_code=400, detail="该角色必须指定所属部门")
    return _get_active_department(db, department_id)



# 修改账号（资料 / 角色 / 部门 / 启停）
def update_user(
    db: Session,                                    # 数据库会话
    user: User,                                     # 被修改的用户
    *,
    operator: User,                                 # 操作管理员
    email: str,                                     # 新邮箱
    full_name: str | None,                          # 新姓名
    phone: str | None,                              # 新手机号
    position: str | None,                           # 新职位
    role: UserRole,                                 # 新角色
    department_id: int | None,                      # 新部门ID
    is_active: bool,                                # 新启用状态
) -> User:
    """
    修改一个用户账号（PUT 全量语义：调用方必须把可编辑字段一次发全）
    执行顺序（顺序本身就是安全边界，别随意调整）：
        1. 必填兜底：姓名/手机号在库里 NOT NULL，提前拦成 400；
        2. 邮箱唯一：不能与任何其他账号的邮箱或用户名重复；
        3. 部门规则：按新角色解析最终部门；
        4. 自锁保护：不能停用自己、不能修改自己的角色（否则可能把自己锁在系统外）；
        5. 最后一个启用的 admin 不能停用或降级，否则系统再没人能进管理端；
        6. 待办保护：要停用时，若还有待他审批的单据，直接拒绝（避免单据卡死无人可批）；
        7. 部门负责人处理：他是某部门负责人时——停用直接拒绝（需先指定继任者），
           降级或换部门则自动解除旧部门的负责人指向；
        8. 若新角色是 manager：要求目标部门没有别的负责人，并写入 departments.manager_id。
    注意：本函数会 commit；username 与 password 不在可改范围内（用户名不可变、密码走重置接口）。
        :param db: 数据库会话
        :param user: 被修改的用户实体（已从库里查出）
        :param operator: 执行修改的管理员（用于自锁保护）
        :param email: 新邮箱
        :param full_name: 新真实姓名
        :param phone: 新手机号
        :param position: 新职位，可空
        :param role: 新角色
        :param department_id: 新部门ID，按角色规则校验
        :param is_active: 新启用状态
        :return: 修改后的用户实体
        :raises HTTPException: 409 邮箱重复；400 各类业务规则不通过
    """
    # 1.必填兜底
    if not full_name:
        raise HTTPException(status_code=400, detail="真实姓名不能为空")
    if not phone:
        raise HTTPException(status_code=400, detail="手机号不能为空")

    # 2.邮箱唯一校验：跨字段比一遍，避免"邮箱撞上别人的用户名"
    exist_user = db.query(User).filter(             # 冲突用户查询
        User.id != user.id,                     # 排除自己
        or_(
            User.username == email,             # 与别人的用户名冲突
            User.email == email,                # 与别人的邮箱冲突
        )
    ).first()                                       # 冲突用户
    if exist_user:
        raise HTTPException(status_code=409, detail="邮箱已被其他账号使用")

    # 3.按新角色解析最终部门
    department = _resolve_department_for_update(db, user, role, department_id)   # 最终归属部门

    # 4.自锁保护：不能让自己停用或失去管理员身份
    if user.id == operator.id:
        if not is_active:
            raise HTTPException(status_code=400, detail="不能停用自己的账号")
        if role != user.role:
            raise HTTPException(status_code=400, detail="不能修改自己的角色,请让其他管理员操作")

    # 5.最后一个启用 admin 保护：停用或降级前先数一下还有几个可用管理员
    if user.role == UserRole.ADMIN and user.is_active and (role != UserRole.ADMIN or not is_active):
        active_admin_count = db.query(User).filter(   # 启用管理员数查询
            User.role == UserRole.ADMIN,        # 只数管理员
            User.is_active == True              # 且处于启用状态
        ).count()
        if active_admin_count <= 1:
            raise HTTPException(status_code=400, detail="系统必须保留至少一个启用状态的管理员")

    # 6.待办保护：停用前确认他没有等着的审批（与待办列表同一套资格规则）
    if not is_active:
        pending_count = count_pending_approvals_for_user(db, user)   # 待审批数
        if pending_count > 0:
            raise HTTPException(status_code=400, detail=f"该用户还有 {pending_count} 条待审批单据,请先转交后再停用")

    # 7.部门负责人处理：先看他是不是某个部门的负责人
    managed_department = db.query(Department).filter(Department.manager_id == user.id).first()   # 负责的部门
    if managed_department is not None:
        # 停用负责人会让该部门往后没人能审批，必须先把负责人换成别人
        if not is_active:
            raise HTTPException(status_code=400, detail=f"该用户是「{managed_department.name}」部门的负责人,请先指定继任者再停用")
        # 降级或换部门：自动解除旧部门的负责人指向，保证 manager_id 不会指向一个非经理
        if role != UserRole.MANAGER or department is None or department.id != managed_department.id:
            managed_department.manager_id = None    # 解除负责人

    # 8.新角色是 manager：目标部门不能已有别人，通过后把负责人指向他
    if role == UserRole.MANAGER and department is not None:
        if department.manager_id not in (None, user.id):
            raise HTTPException(status_code=400, detail="该部门已有负责人,请先更换或清空部门负责人")
        department.manager_id = user.id             # 回写负责人

    # 9.写回可编辑字段（username / password 刻意不在其中）
    user.email = email                              # 邮箱
    user.full_name = full_name                      # 真实姓名
    user.phone = phone                              # 手机号
    user.position = position                        # 职位
    user.role = role                                # 角色
    user.department_id = department.id if department is not None else None   # 所属部门
    user.is_active = is_active                      # 启用状态

    # 10.提交并回读
    db.commit()
    db.refresh(user)

    return user



# 管理员重置密码
def reset_password(db: Session, user: User, *, operator: User, new_password: str) -> User:
    """
    管理员重置某个用户的密码（不校验旧密码）
    语义：这是"管理员给用户设了一个他不知道的密码"，所以必须同时把 must_change_password 置 True，
    用户下次登录后只能先调"修改密码"接口，把密码换成自己知道的才能继续用其他功能。
    安全说明：
        - 不允许重置自己的密码：管理员改自己的密码应当走"修改密码"接口，需要旧密码确认，
          否则等于给了管理员一个不需要验证就能改自己密码的通道；
        - 重置后该用户已签发的 token 仍然有效（JWT 无状态，目前没有 token 黑名单/版本号机制），
          若要"改密即踢下线"，后续需要加 token_version 字段并写入 JWT。
        :param db: 数据库会话
        :param user: 被重置密码的用户
        :param operator: 执行操作的管理员
        :param new_password: 新明文密码（本函数内部哈希）
        :return: 更新后的用户实体
        :raises HTTPException: 400 试图重置自己的密码
    """
    # 1.自锁保护：不能通过"重置密码"改自己的密码，必须走需要旧密码的修改密码接口
    if user.id == operator.id:
        raise HTTPException(status_code=400, detail="请使用「修改密码」接口修改自己的密码")

    # 2.写入新密码哈希，并强制用户下次登录先改密
    user.password_hash = hash_password(new_password)   # 新密码密文
    user.must_change_password = True                   # 强制下次改密
    # 令牌版本 +1：该用户手上所有已签发的令牌立即失效（改密=踢下线）
    user.token_version = (user.token_version or 0) + 1   # 令牌版本号

    # 3.提交并回读
    db.commit()
    db.refresh(user)

    return user



# 用户修改自己的密码
def change_password(db: Session, user: User, *, old_password: str, new_password: str) -> User:
    """
    用户修改自己的密码（必须校验旧密码，用于确认是本人操作）
    规则：
        - 旧密码不匹配返回 400（特意不用 401：前端会把 401 当成"登录失效"直接踢回登录页，
          而这里只是密码输错，用户还应停留在当前页面重试）；
        - 新密码不能与旧密码相同，避免"改了等于没改"；
        - 成功后清除 must_change_password，用户即可正常使用其他业务接口。
        :param db: 数据库会话
        :param user: 当前登录用户
        :param old_password: 旧明文密码
        :param new_password: 新明文密码
        :return: 更新后的用户实体
        :raises HTTPException: 400 旧密码不正确 / 新旧密码相同
    """
    # 1.校验旧密码
    if not verify_password(old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码不正确")

    # 2.新旧密码不能相同
    if old_password == new_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")

    # 3.写入新密码哈希，并清除强制改密标记
    user.password_hash = hash_password(new_password)   # 新密码密文
    user.must_change_password = False                  # 清除强制改密
    # 令牌版本 +1：其他设备上的旧令牌全部失效，调用方需要给当前会话重新签发令牌
    user.token_version = (user.token_version or 0) + 1   # 令牌版本号

    # 4.提交并回读
    db.commit()
    db.refresh(user)

    return user
