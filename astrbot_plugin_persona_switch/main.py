"""AstrBot 人格切换插件（Persona Switch）。

指令：
    /persona                查看人格列表与当前人格
    /persona <名称或序号>    切换当前对话使用的人格
    /persona reset          清除当前对话的人格设置，回到默认人格

实现说明：
    AstrBot 4.14+ 中每个人格都有一个 persona_id（即 WebUI「人格」页面里显示的名称），
    当前对话使用的人格保存在 conversation.persona_id 字段中，Agent 请求时会读取并
    注入对应的人格提示词。本插件通过 ConversationManager 更新该字段，因此：
      * 切换只影响当前对话，不影响其他群 / 其他对话；
      * 执行 /new 新建对话时会继承当前人格（AstrBot 4.3.3+ 行为）；
      * 结果写入数据库，重启 AstrBot 后依然有效。
"""

from __future__ import annotations

import inspect
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 系统内置默认人格的 persona_id
DEFAULT_PERSONA_ID = "default"

# 保留关键字：无法与人格重名，重名时请改用序号切换
RESET_KEYWORDS = frozenset(
    {"reset", "default", "clear", "重置", "默认", "恢复", "恢复默认", "复位"},
)
LIST_KEYWORDS = frozenset(
    {"list", "ls", "all", "help", "列表", "全部", "帮助"},
)


class PersonaSwitchPlugin(Star):
    """用 /persona 指令查看、切换、重置当前对话的 AI 人格。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self._sp_api: object | None = None
        self._version: tuple[int, ...] | bool | None = None

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------

    @filter.command("persona", alias={"人格", "切换人格"})
    async def persona(self, event: AstrMessageEvent):
        """查看人格列表，或按名称 / 序号切换当前对话的人格。"""
        try:
            async for result in self._handle(event):
                yield result
        except Exception as exc:  # noqa: BLE001 - 不让插件异常打断整个事件管线
            logger.error(f"[persona] 处理指令失败: {exc}", exc_info=True)
            yield event.plain_result(f"❌ 人格指令执行失败：{exc}")

    async def _handle(self, event: AstrMessageEvent):
        if self._config_bool("admin_only", False) and not event.is_admin():
            yield event.plain_result("❌ 只有 AstrBot 管理员才能切换人格。")
            return

        umo = event.unified_msg_origin
        arg = self._extract_argument(event.get_message_str())
        keyword = arg.casefold()

        pm = self._persona_manager()
        if pm is None:
            yield event.plain_result(
                "❌ 无法获取人格管理器，请确认 AstrBot 版本不低于 4.0。",
            )
            return

        personas = await self._fetch_personas(pm)
        current_id = await self._current_persona_id(umo)

        # 1. 查看列表
        if not arg or keyword in LIST_KEYWORDS:
            yield event.plain_result(self._format_list(personas, current_id))
            return

        # 2. 恢复默认人格
        if keyword in RESET_KEYWORDS:
            cleared = await self._clear_persona(umo)
            default_id = await self._default_persona_id(umo)
            forced = await self._forced_persona_id(umo)

            if not cleared:
                lines = ["ℹ️ 当前还没有对话记录，无需重置。"]
            else:
                lines = [f"✅ 已清除当前对话的人格设置，恢复默认人格「{default_id}」。"]

            if forced and forced != default_id:
                lines.append(
                    f"⚠️ 但该会话在 WebUI「会话管理」中被强制指定为「{forced}」，"
                    "实际仍会使用该人格。",
                )
            yield event.plain_result("\n".join(lines))
            return

        # 3. 切换人格
        target, error = self._resolve_target(personas, arg)
        if error:
            yield event.plain_result(error)
            return

        warning = self._unsupported_warning()
        if warning:
            yield event.plain_result(warning)
            return

        await self._apply_persona(umo, target)
        logger.info(f"[persona] 会话 {umo} 切换到人格「{target}」")

        forced = await self._forced_persona_id(umo)
        lines = [f"✅ 人格已切换为「{target}」。"]
        if forced and forced != target:
            lines.append(
                f"⚠️ 但该会话在 WebUI「会话管理」中被强制指定为「{forced}」，"
                "实际仍会使用该人格；移除该会话规则后本条设置才会生效。",
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 人格数据读取
    # ------------------------------------------------------------------

    def _persona_manager(self):
        """兼容不同版本的人格管理器位置。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            provider_manager = getattr(self.context, "provider_manager", None)
            pm = getattr(provider_manager, "persona_mgr", None)
        return pm

    async def _fetch_personas(self, pm) -> list[tuple[str, str]]:
        """返回 [(persona_id, system_prompt), ...]，按人格管理页顺序排列。"""
        items: list = []
        getter = getattr(pm, "get_all_personas", None)
        if callable(getter):
            try:
                items = await self._await(getter()) or []
            except Exception as exc:  # noqa: BLE001 - 数据库异常时退回缓存列表
                logger.warning(f"[persona] 读取人格列表失败，回退到缓存: {exc}")

        if not items:
            items = list(
                getattr(pm, "personas_v3", None)
                or getattr(pm, "personas", None)
                or [],
            )

        personas: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in items:
            persona_id = self._persona_id_of(item)
            if not persona_id or persona_id in seen:
                continue
            seen.add(persona_id)
            personas.append((persona_id, self._persona_prompt_of(item)))
        return personas

    async def _current_persona_id(self, umo: str) -> str | None:
        """当前实际生效的人格：会话强制规则 > 当前对话设置 > 全局默认。"""
        forced = await self._forced_persona_id(umo)
        if forced:
            return forced

        cm = getattr(self.context, "conversation_manager", None)
        if cm is not None:
            try:
                cid = await cm.get_curr_conversation_id(umo)
                if cid:
                    conv = await cm.get_conversation(umo, cid)
                    persona_id = getattr(conv, "persona_id", None)
                    if persona_id:
                        return persona_id
            except Exception as exc:  # noqa: BLE001 - 读不到时退回默认人格
                logger.debug(f"[persona] 读取当前对话人格失败: {exc}")

        return await self._default_persona_id(umo)

    async def _default_persona_id(self, umo: str) -> str:
        pm = self._persona_manager()
        if pm is not None:
            getter = getattr(pm, "get_default_persona_v3", None)
            if callable(getter):
                for args in ((umo,), ()):
                    try:
                        value = await self._await(getter(*args))
                    except TypeError:
                        # 旧版本 get_default_persona_v3 不接受 umo 参数
                        continue
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[persona] 读取默认人格失败: {exc}")
                        break
                    persona_id = self._persona_id_of(value)
                    if persona_id:
                        return persona_id

            selected = getattr(pm, "selected_default_persona_v3", None) or getattr(
                pm,
                "selected_default_persona",
                None,
            )
            persona_id = self._persona_id_of(selected)
            if persona_id:
                return persona_id

        return DEFAULT_PERSONA_ID

    async def _forced_persona_id(self, umo: str) -> str | None:
        """WebUI「会话管理」中通过会话规则强制指定的人格。"""
        sp_api = self._get_sp()
        if sp_api is None:
            return None

        try:
            session_get = getattr(sp_api, "session_get", None)
            if callable(session_get):
                value = await self._await(
                    session_get(umo, "session_service_config", {}),
                )
            else:
                value = await sp_api.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
        except Exception as exc:  # noqa: BLE001 - 读取失败不影响主流程
            logger.debug(f"[persona] 读取会话规则失败: {exc}")
            return None

        if isinstance(value, dict):
            persona_id = value.get("persona_id")
            if isinstance(persona_id, str) and persona_id.strip():
                return persona_id.strip()
        return None

    def _get_sp(self):
        """延迟导入共享配置对象，兼容不同版本。"""
        if self._sp_api is not None:
            return self._sp_api or None
        try:
            from astrbot.api import sp as sp_api
        except Exception:  # noqa: BLE001
            try:
                from astrbot.core import sp as sp_api
            except Exception:  # noqa: BLE001
                sp_api = None
        self._sp_api = sp_api or False
        return sp_api

    # ------------------------------------------------------------------
    # 人格写入
    # ------------------------------------------------------------------

    async def _apply_persona(self, umo: str, persona_id: str) -> None:
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            raise RuntimeError("当前 AstrBot 版本缺少对话管理器，无法设置人格")

        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            # 还没有任何对话（例如新会话的第一条指令），先建一个空对话
            cid = await cm.new_conversation(umo)

        conv = await cm.get_conversation(umo, cid)
        if conv is None:
            # 当前对话已被删除，重新建一个
            cid = await cm.new_conversation(umo)

        try:
            await cm.update_conversation(umo, cid, persona_id=persona_id)
        except TypeError:
            # 极旧版本不支持 persona_id 关键字参数，下面用兼容方法兜底
            pass

        conv = await cm.get_conversation(umo, cid)
        if getattr(conv, "persona_id", None) != persona_id:
            legacy = getattr(cm, "update_conversation_persona_id", None)
            if callable(legacy):
                await legacy(umo, persona_id, cid)
                conv = await cm.get_conversation(umo, cid)

        if getattr(conv, "persona_id", None) != persona_id:
            raise RuntimeError("写入人格设置失败，请查看 AstrBot 日志")

    async def _clear_persona(self, umo: str) -> bool:
        """清除当前对话的人格设置；没有对话时返回 False。"""
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            raise RuntimeError("当前 AstrBot 版本缺少对话管理器，无法重置人格")

        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return False

        legacy = getattr(cm, "update_conversation_persona_id", None)
        if callable(legacy):
            await legacy(umo, "", cid)
        else:
            await cm.update_conversation(umo, cid, persona_id="")

        conv = await cm.get_conversation(umo, cid)
        if getattr(conv, "persona_id", None):
            # 某些版本的空字符串会被忽略，再用 update_conversation 试一次
            await cm.update_conversation(umo, cid, persona_id="")
            conv = await cm.get_conversation(umo, cid)

        if getattr(conv, "persona_id", None):
            raise RuntimeError("清除人格设置失败，请查看 AstrBot 日志")
        return True

    # ------------------------------------------------------------------
    # 文本处理
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_argument(message: str) -> str:
        """取出指令名之后的参数。唤醒前缀通常已被 AstrBot 剥离。"""
        text = (message or "").strip().lstrip("/／").strip()
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _resolve_target(
        personas: list[tuple[str, str]],
        arg: str,
    ) -> tuple[str | None, str | None]:
        """把用户输入解析成 persona_id，返回 (persona_id, 错误信息)。"""
        query = arg.strip()
        if not query:
            return None, "❌ 未指定人格，发送 /persona 查看列表。"

        ids = [persona_id for persona_id, _ in personas]

        # 序号（1 开始）
        if query.isdigit():
            index = int(query)
            if 1 <= index <= len(ids):
                return ids[index - 1], None
            return None, f"❌ 序号 {index} 超出范围，可用范围是 1-{len(ids)}。"

        # 完全匹配（区分大小写）
        for persona_id in ids:
            if persona_id == query:
                return persona_id, None

        # 忽略大小写匹配
        lower = query.casefold()
        exact = [persona_id for persona_id in ids if persona_id.casefold() == lower]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            return None, "⚠️ 匹配到多个人格：" + "、".join(exact) + "，请发送序号切换。"

        # 前缀匹配
        prefixed = [
            persona_id for persona_id in ids if persona_id.casefold().startswith(lower)
        ]
        if len(prefixed) == 1:
            return prefixed[0], None
        if len(prefixed) > 1:
            shown = "、".join(prefixed[:8])
            more = f" 等 {len(prefixed)} 个" if len(prefixed) > 8 else ""
            return None, f"⚠️ 匹配到多个人格：{shown}{more}，请发送更完整的名称或序号。"

        return None, f"❌ 未找到人格「{query}」，发送 /persona 查看可用列表。"

    def _format_list(
        self,
        personas: list[tuple[str, str]],
        current_id: str | None,
    ) -> str:
        if not personas:
            return "📭 还没有任何人格，请先在 AstrBot WebUI 的「人格」页面创建。"

        max_items = self._config_int("max_list_items", 30)
        show_preview = self._config_bool("show_prompt_preview", False)

        lines = ["🎭 人格列表", f"当前人格：{current_id or DEFAULT_PERSONA_ID}", ""]
        for index, (persona_id, prompt) in enumerate(personas[:max_items], start=1):
            if persona_id == DEFAULT_PERSONA_ID:
                name = "default（系统默认人格）"
            else:
                name = persona_id
            mark = "（当前）" if persona_id == current_id else ""
            lines.append(f"{index}. {name}{mark}")

            if show_preview and prompt:
                snippet = self._prompt_preview(prompt)
                if snippet:
                    lines.append(f"    {snippet}")

        if len(personas) > max_items:
            lines.append(
                f"…… 还有 {len(personas) - max_items} 个人格未显示，可直接发送名称切换。",
            )

        lines.extend(
            [
                "",
                "发送 /persona 名称 或 /persona 序号 切换",
                "发送 /persona reset 恢复默认人格",
            ],
        )
        return "\n".join(lines)

    @staticmethod
    def _prompt_preview(prompt: str) -> str:
        text = re.sub(r"\s+", " ", prompt or "").strip()
        if not text:
            return ""
        return text[:30] + ("…" if len(text) > 30 else "")

    # ------------------------------------------------------------------
    # 配置与类型工具
    # ------------------------------------------------------------------

    @staticmethod
    def _persona_id_of(item) -> str | None:
        if item is None:
            return None
        if isinstance(item, dict):
            value = item.get("persona_id") or item.get("name")
        else:
            value = getattr(item, "persona_id", None) or getattr(item, "name", None)
        return str(value) if value else None

    @staticmethod
    def _persona_prompt_of(item) -> str:
        if isinstance(item, dict):
            return str(item.get("prompt") or item.get("system_prompt") or "")
        return str(getattr(item, "system_prompt", "") or "")

    @staticmethod
    async def _await(value):
        if inspect.isawaitable(value):
            return await value
        return value

    # ------------------------------------------------------------------
    # 版本检查
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_version(text: str) -> tuple[int, int, int] | None:
        numbers = re.findall(r"\d+", text or "")
        if not numbers:
            return None
        parts = [int(number) for number in numbers[:3]]
        while len(parts) < 3:
            parts.append(0)
        return parts[0], parts[1], parts[2]

    def _astrbot_version(self) -> tuple[int, int, int] | None:
        if self._version is None:
            try:
                from astrbot import __version__

                self._version = self._parse_version(__version__) or False
            except Exception:  # noqa: BLE001
                self._version = False
        return self._version or None

    def _unsupported_warning(self) -> str | None:
        """AstrBot 4.14 之前的版本不会读取对话级人格，提前提示避免误解。"""
        version = self._astrbot_version()
        if version is None or version >= (4, 14, 0):
            return None
        version_text = ".".join(str(part) for part in version)
        return (
            f"⚠️ 当前 AstrBot 版本为 {version_text}，低于 4.14，"
            "切换人格不会生效，请先升级 AstrBot。"
        )

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def _config_bool(self, key: str, default: bool = False) -> bool:
        return self._as_bool(self._config_get(key, default), default)

    def _config_int(self, key: str, default: int) -> int:
        try:
            value = int(self._config_get(key, default))
        except (TypeError, ValueError):
            return default
        return max(1, min(100, value))

    def _config_get(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value
