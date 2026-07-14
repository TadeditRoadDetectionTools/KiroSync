"""假的 discord 模組, 讓測試不裝 discord.py 也能 import bot/bot.py。

只做到「import bot 與建構 KiroBot 不會爆」的最低限度:
  - discord.Client        : KiroBot 的父類別 (吃任意 kwargs)
  - discord.Intents       : __init__ 會呼叫 Intents.default()
  - discord.app_commands  : CommandTree / describe (裝飾器在 _register_commands 執行)
其餘 discord.* 只在 runtime 方法內用到, 測試不會走到。
"""

from __future__ import annotations

import sys
import types


def install() -> types.ModuleType:
    """把 stub 塞進 sys.modules['discord'] (冪等; 覆蓋既有的以保持測試確定性)。"""
    existing = sys.modules.get("discord")
    if existing is not None and getattr(existing, "_kirosync_stub", False):
        return existing

    d = types.ModuleType("discord")
    d._kirosync_stub = True

    class Client:
        def __init__(self, **kwargs):
            pass

        def get_channel(self, cid):
            return None

    class Intents:
        def __init__(self):
            self.message_content = False

        @classmethod
        def default(cls):
            return cls()

    class _CommandTree:
        def __init__(self, client):
            self.client = client

        def command(self, **kwargs):
            def deco(fn):
                return fn
            return deco

        def copy_global_to(self, guild=None):
            pass

        async def sync(self, guild=None):
            pass

    def _describe(**kwargs):
        def deco(fn):
            return fn
        return deco

    app_commands = types.ModuleType("discord.app_commands")
    app_commands.CommandTree = _CommandTree
    app_commands.describe = _describe

    d.Client = Client
    d.Intents = Intents
    d.app_commands = app_commands

    sys.modules["discord"] = d
    sys.modules["discord.app_commands"] = app_commands
    return d
