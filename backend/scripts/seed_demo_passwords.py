"""
本地演示：把演示账号的密码重置为已知值（仅限本地/演示环境）

    为什么需要：
        init_data.sql 里的密码是 bcrypt 哈希，克隆仓库的人不知道明文，
        无法用 testuser/manager1/fin1 体验“员工提交 → 经理/财务审批”的多角色流程。

    安全边界：
        - 生产环境（APP_ENV=production）默认拒绝执行；
        - 只改 6 个演示账号，不动 admin；
        - 支持 --dry-run 只看名单不写库。

    用法（在 backend/ 下）：
        python scripts/seed_demo_passwords.py --dry-run
        python scripts/seed_demo_passwords.py
        python scripts/seed_demo_passwords.py --force   # 生产慎用
"""
import argparse
import pathlib
import sys

# 允许直接执行 scripts/xxx.py：把 backend 加进导入路径
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings
from app.core.security import hash_password
from app.dependencies import SessionLocal
from app.models import User


# 演示账号（与 init_data.sql 一致）
DEMO_USERS = ("testuser", "manager1", "fin1", "manager2", "emp_p2", "fin2")
# 演示密码（仅本地）
DEMO_PASSWORD = "Demo@123456"


def main(argv: list[str] | None = None) -> int:
    """
    重置演示账号密码
        :param argv: 参数列表（None 取 sys.argv）
        :return: 退出码
    """
    parser = argparse.ArgumentParser(description="重置本地演示账号密码")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--force", action="store_true", help="生产环境也执行（慎用）")
    args = parser.parse_args(argv)

    # 1.生产环境护栏：默认拒绝，除非显式 --force
    if settings.APP_ENV == "production" and not args.force and not args.dry_run:
        print("拒绝执行：当前 APP_ENV=production；本地演示请用 development，或显式 --force")
        return 2

    # 2.只处理存在的演示账号
    with SessionLocal() as db:
        users = db.query(User).filter(User.username.in_(DEMO_USERS)).order_by(User.id).all()
        if not users:
            print("没有找到演示账号，请先执行 app/sql/init_data.sql")
            return 1

        print("将重置以下账号：")
        for user in users:
            print(f"  {user.username} ({user.role.value if hasattr(user.role, 'value') else user.role})")
        print(f"密码：{DEMO_PASSWORD}（仅本地演示；生产请停用或改密）")

        if args.dry_run:
            print("dry-run：未写库")
            return 0

        password_hash = hash_password(DEMO_PASSWORD)  # 新密码哈希
        for user in users:
            user.password_hash = password_hash
            user.must_change_password = False
            user.is_active = True
            user.token_version = (user.token_version or 0) + 1
        db.commit()
        print(f"已重置 {len(users)} 个演示账号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
