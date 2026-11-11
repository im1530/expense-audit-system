"""
认证与权限测试
    覆盖：
        - 登录成功/密码错误/账号停用
        - 令牌失效场景：非法令牌、改密后旧令牌（token_version 不匹配）
        - 强制改密：业务接口拦截，但"修改密码"接口允许通过
        - 管理端接口的 admin 专属权限
        - 报表接口的 view_all 权限
"""
import unittest

from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import get_current_user, get_current_user_allow_pending_password
from app.api.v1.admin_user import list_users
from app.api.v1.auth import login, read_current_user
from app.api.v1.report import report_overview
from app.core.security import create_user_token
from app.schemas.common import PageParams
from app.schemas.user import UserResponse
from tests.support import BaseTestCase, TEST_PASSWORD, make_request



class AuthAndPermissionTest(BaseTestCase, unittest.TestCase):
    """认证与权限测试"""

    # 构造 OAuth2 登录表单
    def login_form(self, username: str, password: str) -> OAuth2PasswordRequestForm:
        """
        构造登录表单对象
            :param username: 用户名
            :param password: 明文密码
            :return: OAuth2PasswordRequestForm
        """
        return OAuth2PasswordRequestForm(
            username=username,
            password=password,
            grant_type="password",
            scope="",
            client_id=None,
            client_secret=None,
        )

    # 断言异常状态码
    def assert_http_error(self, context, status_code: int):
        """校验 HTTP 状态码"""
        self.assertEqual(context.exception.status_code, status_code)

    # ---------- 登录 ----------
    def test_login_success(self):
        """正确密码登录：返回令牌，且不需要强制改密"""
        result = login(form_data=self.login_form("employee_e1", TEST_PASSWORD), db=self.db)

        self.assertIn("access_token", result)
        self.assertEqual(result["token_type"], "bearer")
        self.assertFalse(result["must_change_password"])

    def test_login_wrong_password(self):
        """密码错误：401，且提示不区分账号不存在"""
        with self.assertRaises(HTTPException) as ctx:
            login(form_data=self.login_form("employee_e1", "wrong-password"), db=self.db)
        self.assert_http_error(ctx, 401)
        self.assertEqual(ctx.exception.detail, "用户名或密码错误")

    def test_login_inactive_account(self):
        """账号被停用：密码正确也拒绝登录（403）"""
        user = self.fx["users"]["employee"]
        user.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            login(form_data=self.login_form("employee_e1", TEST_PASSWORD), db=self.db)
        self.assert_http_error(ctx, 403)
        self.assertIn("停用", ctx.exception.detail)

    # ---------- 令牌校验 ----------
    def test_valid_token_resolves_user(self):
        """合法令牌可以解析出当前用户，并写入 request.state"""
        user = self.fx["users"]["employee"]
        request = make_request()
        current = get_current_user(request=request, token=create_user_token(user), db=self.db)

        self.assertEqual(current.id, user.id)
        self.assertEqual(request.state.user_id, user.id)

    def test_invalid_token_rejected(self):
        """非法令牌：401"""
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(request=make_request(), token="not-a-jwt", db=self.db)
        self.assert_http_error(ctx, 401)

    def test_token_version_mismatch_rejected(self):
        """改密后旧令牌失效：token_version 不匹配 → 401"""
        user = self.fx["users"]["employee"]
        old_token = create_user_token(user)

        user.token_version = user.token_version + 1
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(request=make_request(), token=old_token, db=self.db)
        self.assert_http_error(ctx, 401)
        self.assertIn("重新登录", ctx.exception.detail)

    def test_inactive_account_token_rejected(self):
        """账号停用后，已签发的令牌立即失效（403）"""
        user = self.fx["users"]["employee"]
        token = create_user_token(user)

        user.is_active = False
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(request=make_request(), token=token, db=self.db)
        self.assert_http_error(ctx, 403)

    # ---------- 强制改密 ----------
    def test_must_change_password_blocks_business_api(self):
        """待改密账号：业务接口 403，但改密接口可用"""
        user = self.fx["users"]["employee"]
        token = create_user_token(user)

        user.must_change_password = True
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(request=make_request(), token=token, db=self.db)
        self.assert_http_error(ctx, 403)
        self.assertIn("修改初始密码", ctx.exception.detail)

        # 改密接口专用的依赖允许通过
        current = get_current_user_allow_pending_password(
            request=make_request(), token=token, db=self.db
        )
        self.assertEqual(current.id, user.id)

    # ---------- 管理端权限 ----------
    def test_admin_endpoint_denied_for_employee(self):
        """员工访问管理端用户列表：403"""
        employee = self.fx["users"]["employee"]

        with self.assertRaises(HTTPException) as ctx:
            list_users(page_params=PageParams(), role=None, department_id=None,
                       is_active=None, keyword=None, db=self.db, current_user=employee)
        self.assert_http_error(ctx, 403)

    def test_admin_endpoint_allowed_for_admin(self):
        """管理员可以访问用户列表，且能查到种子用户"""
        admin = self.fx["users"]["admin"]

        page = list_users(page_params=PageParams(), role=None, department_id=None,
                          is_active=None, keyword=None, db=self.db, current_user=admin)
        self.assertGreaterEqual(page.total, 7)

    # ---------- 报表权限 ----------
    def test_report_denied_for_employee(self):
        """员工没有 view_all 权限，不能看公司报表（403）"""
        employee = self.fx["users"]["employee"]

        with self.assertRaises(HTTPException) as ctx:
            report_overview(start_date=None, end_date=None, department_id=None,
                            db=self.db, current_user=employee)
        self.assert_http_error(ctx, 403)

    def test_report_allowed_for_finance(self):
        """财务可以看报表总览"""
        finance = self.fx["users"]["finance"]

        overview = report_overview(start_date=None, end_date=None, department_id=None,
                                   db=self.db, current_user=finance)
        self.assertEqual(overview["total_count"], 0)     # 种子数据里还没有单据



    # ---------- 当前用户信息 ----------
    def test_me_returns_current_user(self):
        """当前用户接口返回自己的资料（前端登录后渲染角色用）"""
        employee = self.fx["users"]["employee"]

        me = read_current_user(current_user=employee)
        self.assertEqual(me.id, employee.id)
        self.assertEqual(me.username, employee.username)
        self.assertEqual(me.role, employee.role)

    def test_me_hides_password_hash(self):
        """响应模型会剔除密码哈希字段"""
        employee = self.fx["users"]["employee"]

        me = read_current_user(current_user=employee)
        dumped = UserResponse.model_validate(me).model_dump()
        self.assertNotIn("password_hash", dumped)                   # 绝不把密码哈希发到前端
        self.assertEqual(dumped["role"], employee.role.value)        # 角色以字符串返回



if __name__ == "__main__":
    unittest.main()
