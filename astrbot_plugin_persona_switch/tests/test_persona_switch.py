"""本地单元测试：用假的 astrbot API 驱动插件，验证指令行为。

运行（在插件目录下，不需要安装 AstrBot）：
    python tests/test_persona_switch.py

真实 AstrBot 的 API 形状（4.x）：
    astrbot.api: logger, sp
    astrbot.api.event: AstrMessageEvent, filter(command)
    astrbot.api.star: Context, Star
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MAIN = ROOT / "main.py"

logging.basicConfig(level=logging.CRITICAL)


# ---------------------------------------------------------------------------
# 假的 astrbot 模块
# ---------------------------------------------------------------------------


class FakeStar:
    def __init__(self, context, config=None):
        self.context = context


class FakeSP:
    def __init__(self):
        self.data = {}

    async def session_get(self, umo, key, default=None):
        return self.data.get((umo, key), default)

    async def session_put(self, umo, key, value):
        self.data[(umo, key)] = value


def build_stubs() -> FakeSP:
    fake_sp = FakeSP()

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.__version__ = "4.29.3"

    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = logging.getLogger("astrbot.test")
    api.sp = fake_sp

    event_pkg = types.ModuleType("astrbot.api.event")
    event_pkg.__path__ = []
    event_pkg.AstrMessageEvent = object

    filter_mod = types.ModuleType("astrbot.api.event.filter")

    def command(name, alias=None, **kwargs):
        def decorator(func):
            func.__command_name__ = name
            func.__command_alias__ = alias
            return func

        return decorator

    filter_mod.command = command
    event_pkg.filter = filter_mod

    star_pkg = types.ModuleType("astrbot.api.star")
    star_pkg.Context = object
    star_pkg.Star = FakeStar

    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_pkg,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star_pkg,
        }
    )
    return fake_sp


# ---------------------------------------------------------------------------
# 假的 AstrBot 对象
# ---------------------------------------------------------------------------


class FakePersona:
    def __init__(self, persona_id: str, system_prompt: str = ""):
        self.persona_id = persona_id
        self.system_prompt = system_prompt


class FakePersonaManager:
    def __init__(self, personas, default: str = "default"):
        self.personas = personas
        self.default = default

    async def get_all_personas(self):
        return list(self.personas)

    async def get_default_persona_v3(self, umo=None):
        return {"name": self.default, "prompt": ""}


class FakeConversationManager:
    def __init__(self):
        self.convs: dict[str, dict] = {}
        self.current: dict[str, str] = {}
        self.counter = 0
        self.new_calls = 0

    async def get_curr_conversation_id(self, umo):
        return self.current.get(umo)

    async def new_conversation(self, umo, platform_id=None, persona_id=None, **kwargs):
        self.counter += 1
        self.new_calls += 1
        cid = f"cid{self.counter}"
        self.convs[cid] = {"cid": cid, "persona_id": persona_id}
        self.current[umo] = cid
        return cid

    async def get_conversation(self, umo, cid, create_if_not_exists=False):
        data = self.convs.get(cid)
        if not data:
            return None
        return types.SimpleNamespace(persona_id=data["persona_id"])

    async def update_conversation(
        self,
        umo,
        conversation_id=None,
        history=None,
        title=None,
        persona_id=None,
        **kwargs,
    ):
        if conversation_id is None:
            conversation_id = self.current.get(umo)
        if not conversation_id or conversation_id not in self.convs:
            return
        if persona_id is not None:
            self.convs[conversation_id]["persona_id"] = persona_id

    async def update_conversation_persona_id(self, umo, persona_id, conversation_id=None):
        await self.update_conversation(umo, conversation_id, persona_id=persona_id)


class FakeContext:
    def __init__(self, persona_manager, conversation_manager):
        self.persona_manager = persona_manager
        self.conversation_manager = conversation_manager


class FakeEvent:
    def __init__(self, text, umo="aiocqhttp:FriendMessage:10001", admin=False):
        self.message_str = text
        self.unified_msg_origin = umo
        self._admin = admin

    def get_message_str(self):
        return self.message_str

    def is_admin(self):
        return self._admin

    def get_platform_id(self):
        return self.unified_msg_origin.split(":", 1)[0]

    def plain_result(self, text):
        return ("MSG", text)


# ---------------------------------------------------------------------------
# 加载插件
# ---------------------------------------------------------------------------


def load_plugin():
    spec = importlib.util.spec_from_file_location("persona_switch_under_test", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PersonaSwitchPlugin


async def run(plugin, text, admin=False, umo="aiocqhttp:FriendMessage:10001"):
    event = FakeEvent(text, umo=umo, admin=admin)
    replies = []
    async for result in plugin.persona(event):
        replies.append(result[1])
    return replies


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def make_plugin(Plugin, config=None, default="default"):
    personas = [
        FakePersona("default", "You are a helpful assistant."),
        FakePersona("猫娘", "你是一只可爱的猫娘，说话要带喵。"),
        FakePersona("傲娇女仆", "你是一名傲娇女仆。"),
        FakePersona("asuka", "You are Asuka."),
    ]
    pm = FakePersonaManager(personas, default=default)
    cm = FakeConversationManager()
    ctx = FakeContext(pm, cm)
    return Plugin(ctx, config or {}), pm, cm


async def main():
    fake_sp = build_stubs()
    Plugin = load_plugin()

    checks = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))
        if not condition:
            print(f"[FAIL] {name} {detail}")

    # 1. 列表
    plugin, pm, cm = make_plugin(Plugin)
    replies = await run(plugin, "persona")
    text = "\n".join(replies)
    check("列表包含标题", "🎭 人格列表" in text, text)
    check("列表标记当前人格", "当前人格：default" in text)
    check("列表序号从 1 开始", "1. default（系统默认人格）" in text)
    check("列表包含 4 个人格", "4. asuka" in text)
    check("列表不创建对话", cm.new_calls == 0)

    # 2. 按名称切换（新会话，无对话）
    replies = await run(plugin, "persona 猫娘")
    check("名称切换成功", replies and "人格已切换为「猫娘」" in replies[0], replies)
    check("自动创建对话", cm.new_calls == 1)
    cid = cm.current["aiocqhttp:FriendMessage:10001"]
    check("对话人格已写入", cm.convs[cid]["persona_id"] == "猫娘", cm.convs)

    # 3. 切换后再看列表
    replies = await run(plugin, "persona")
    check("列表显示新当前人格", "当前人格：猫娘" in replies[0], replies[0])
    check("当前人格带标记", "猫娘（当前）" in replies[0], replies[0])

    # 4. 按序号切换
    replies = await run(plugin, "persona 3")
    check("序号切换成功", "人格已切换为「傲娇女仆」" in replies[0], replies)
    check("序号切换写入", cm.convs[cid]["persona_id"] == "傲娇女仆")

    # 5. 前缀匹配
    replies = await run(plugin, "persona 猫")
    check("前缀匹配成功", "人格已切换为「猫娘」" in replies[0], replies)

    # 6. 大小写不敏感
    replies = await run(plugin, "persona ASUKA")
    check("大小写不敏感", "人格已切换为「asuka」" in replies[0], replies)

    # 7. 序号越界
    replies = await run(plugin, "persona 99")
    check("序号越界提示", "超出范围" in replies[0], replies)

    # 8. 不存在
    replies = await run(plugin, "persona 不存在")
    check("未找到人格提示", "未找到人格「不存在」" in replies[0], replies)

    # 9. 重置
    replies = await run(plugin, "persona reset")
    check("重置成功", "已清除当前对话的人格设置" in replies[0], replies)
    check("重置后为空", not cm.convs[cid]["persona_id"], cm.convs[cid])
    replies = await run(plugin, "persona")
    check("重置后回到默认", "当前人格：default" in replies[0], replies[0])

    # 10. 没有对话时重置
    plugin2, pm2, cm2 = make_plugin(Plugin)
    replies = await run(plugin2, "persona reset")
    check("无对话重置提示", "无需重置" in replies[0], replies)

    # 11. 中文别名
    replies = await run(plugin2, "人格 猫娘")
    check("中文别名切换", "人格已切换为「猫娘」" in replies[0], replies)

    # 12. 带唤醒前缀残留
    replies = await run(plugin2, "/persona asuka")
    check("斜杠前缀解析", "人格已切换为「asuka」" in replies[0], replies)

    # 13. 管理员限制
    plugin3, pm3, cm3 = make_plugin(Plugin, config={"admin_only": True})
    replies = await run(plugin3, "persona 猫娘", admin=False)
    check("非管理员被拒绝", "只有 AstrBot 管理员" in replies[0], replies)
    check("非管理员不写库", cm3.new_calls == 0)
    replies = await run(plugin3, "persona 猫娘", admin=True)
    check("管理员可切换", "人格已切换为「猫娘」" in replies[0], replies)

    # 14. WebUI 会话强制人格
    plugin4, pm4, cm4 = make_plugin(Plugin)
    umo = "aiocqhttp:FriendMessage:20002"
    await fake_sp.session_put(umo, "session_service_config", {"persona_id": "傲娇女仆"})
    replies = await run(plugin4, "persona 猫娘", umo=umo)
    check("强制人格时提示警告", "强制指定为「傲娇女仆」" in replies[0], replies)
    check("强制人格时仍写入对话", cm4.convs[cm4.current[umo]]["persona_id"] == "猫娘")
    replies = await run(plugin4, "persona", umo=umo)
    check("列表当前人格取强制值", "当前人格：傲娇女仆" in replies[0], replies[0])
    replies = await run(plugin4, "persona reset", umo=umo)
    check("重置时提示强制人格", "强制指定" in replies[0], replies)

    # 15. 列表截断
    plugin5, pm5, cm5 = make_plugin(Plugin, config={"max_list_items": 2})
    replies = await run(plugin5, "persona")
    check("列表截断提示", "还有 2 个人格未显示" in replies[0], replies[0])

    # 16. 提示词摘要
    plugin6, pm6, cm6 = make_plugin(Plugin, config={"show_prompt_preview": True})
    replies = await run(plugin6, "persona")
    check("显示提示词摘要", "你是一只可爱的猫娘，说话要带喵。" in replies[0], replies[0])

    # 17. 帮助/列表关键字
    replies = await run(plugin6, "persona help")
    check("help 等同列表", "🎭 人格列表" in replies[0], replies)

    # 18. 别名 "切换人格"
    replies = await run(plugin6, "切换人格 猫娘")
    check("别名 切换人格", "人格已切换为「猫娘」" in replies[0], replies)

    # 19. 旧版本（< 4.14）切换拦截
    astrbot_mod = sys.modules["astrbot"]
    astrbot_mod.__version__ = "4.10.0"
    plugin7, pm7, cm7 = make_plugin(Plugin)
    replies = await run(plugin7, "persona 猫娘")
    check("旧版本切换被拦截", "低于 4.14" in replies[0], replies)
    check("旧版本不写库", cm7.new_calls == 0)
    replies = await run(plugin7, "persona")
    check("旧版本仍可查看列表", "🎭 人格列表" in replies[0], replies)
    astrbot_mod.__version__ = "4.29.3"

    # 汇总
    failed = [item for item in checks if not item[1]]
    print(f"\n共 {len(checks)} 项检查，通过 {len(checks) - len(failed)} 项，失败 {len(failed)} 项")
    for name, ok, detail in checks:
        if not ok:
            print(f"  FAILED: {name} | {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
