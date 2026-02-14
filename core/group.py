from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class GroupMixin:
    async def _get_group_info(
        self, event: AstrMessageEvent
    ) -> tuple[dict[str, Any] | None, str | None]:
        group_id = self._to_optional_int(event.get_group_id())
        if group_id is None:
            return None, "当前不是群聊，无法查询群信息"

        bot = getattr(event, "bot", None)
        if bot is None:
            return None, "当前平台不支持群信息查询"

        last_error = None
        if hasattr(bot, "get_group_info"):
            try:
                result = await bot.get_group_info(group_id=group_id)
                payload = self._unwrap_api_payload(result)
                if isinstance(payload, dict):
                    return payload, None
            except Exception as exc:
                last_error = exc

        if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
            try:
                result = await bot.api.call_action(
                    "get_group_info", group_id=group_id, no_cache=True
                )
                payload = self._unwrap_api_payload(result)
                if isinstance(payload, dict):
                    return payload, None
            except Exception as exc:
                last_error = exc

        if last_error:
            logger.warning("[response_enhancer] get_group_info failed: %s", last_error)
            return None, f"获取群信息失败: {last_error}"
        return None, "当前平台不支持群信息查询"

    async def _get_group_members(
        self, event: AstrMessageEvent
    ) -> tuple[list[dict[str, Any]], str | None]:
        group_id = self._to_optional_int(event.get_group_id())
        if group_id is None:
            return [], "当前不是群聊，无法查询群成员信息"

        bot = getattr(event, "bot", None)
        if bot is None:
            return [], "当前平台不支持群成员查询"

        last_error = None
        if hasattr(bot, "get_group_member_list"):
            try:
                result = await bot.get_group_member_list(group_id=group_id)
                members = self._extract_member_list(result)
                if members is not None:
                    return members, None
            except Exception as exc:
                last_error = exc

        if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
            try:
                result = await bot.api.call_action(
                    "get_group_member_list", group_id=group_id
                )
                members = self._extract_member_list(result)
                if members is not None:
                    return members, None
            except Exception as exc:
                last_error = exc

        if last_error:
            logger.warning(
                "[response_enhancer] get_group_member_list failed: %s", last_error
            )
            return [], f"获取群成员信息失败: {last_error}"
        return [], "当前平台不支持群成员查询"

    @staticmethod
    def _unwrap_api_payload(result: Any) -> Any:
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    @classmethod
    def _extract_member_list(cls, result: Any) -> list[dict[str, Any]] | None:
        payload = cls._unwrap_api_payload(result)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return None

    @staticmethod
    def _normalize_member(member: dict[str, Any]) -> dict[str, Any]:
        user_id = str(member.get("user_id", ""))
        nickname = str(member.get("nickname") or "").strip()
        card = str(member.get("card") or "").strip()
        display_name = card or nickname or f"用户{user_id}"
        return {
            "user_id": user_id,
            "nickname": nickname,
            "card": card,
            "display_name": display_name,
            "role": str(member.get("role", "member")),
        }
