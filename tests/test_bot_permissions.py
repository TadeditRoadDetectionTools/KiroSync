"""/summary 權限判定的測試 (純函式, 不接 Discord)。"""

import unittest

import _paths  # noqa: F401
import _stub_discord

_stub_discord.install()

from bot import member_is_admin, member_may_summary


class FakePerms:
    def __init__(self, administrator=False, manage_guild=False):
        self.administrator = administrator
        self.manage_guild = manage_guild


class FakeRole:
    def __init__(self, rid):
        self.id = rid


class FakeMember:
    def __init__(self, perms=None, roles=()):
        if perms is not None:
            self.guild_permissions = perms
        self.roles = list(roles)


class TestMemberIsAdmin(unittest.TestCase):
    def test_administrator(self):
        self.assertTrue(member_is_admin(FakeMember(FakePerms(administrator=True))))

    def test_manage_guild(self):
        self.assertTrue(member_is_admin(FakeMember(FakePerms(manage_guild=True))))

    def test_plain_member(self):
        self.assertFalse(member_is_admin(FakeMember(FakePerms())))

    def test_no_guild_permissions_attr(self):
        # DM 情境下的 User 沒有 guild_permissions — 不能當成管理員
        self.assertFalse(member_is_admin(FakeMember()))


class TestMemberMaySummary(unittest.TestCase):
    def test_admin_always_allowed(self):
        self.assertTrue(member_may_summary(FakeMember(FakePerms(administrator=True)), []))

    def test_whitelisted_role_allowed(self):
        m = FakeMember(FakePerms(), roles=[FakeRole(7), FakeRole(9)])
        self.assertTrue(member_may_summary(m, [9]))

    def test_non_whitelisted_role_denied(self):
        m = FakeMember(FakePerms(), roles=[FakeRole(7)])
        self.assertFalse(member_may_summary(m, [9]))

    def test_no_roles_no_perms_denied(self):
        self.assertFalse(member_may_summary(FakeMember(FakePerms()), [9]))

    def test_empty_whitelist_denies_plain_member(self):
        m = FakeMember(FakePerms(), roles=[FakeRole(7)])
        self.assertFalse(member_may_summary(m, []))

    def test_none_whitelist_tolerated(self):
        self.assertFalse(member_may_summary(FakeMember(FakePerms(), [FakeRole(1)]), None))
        self.assertTrue(member_may_summary(FakeMember(FakePerms(administrator=True)), None))


if __name__ == "__main__":
    unittest.main()
