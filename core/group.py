from __future__ import annotations

import json
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class GroupMixin:
    # region 群信息
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
    # endregion 群信息

    # region 群管理动作
    async def _call_group_action(
        self,
        event: AstrMessageEvent,
        action: str,
        **kwargs: Any,
    ) -> tuple[Any | None, str | None]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None, f"当前平台不支持群管理操作: {action}"

        last_error: Exception | None = None

        if hasattr(bot, action):
            try:
                method = getattr(bot, action)
                result = await method(**kwargs)
                return self._unwrap_api_payload(result), None
            except Exception as exc:
                last_error = exc

        if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
            try:
                result = await bot.api.call_action(action, **kwargs)
                return self._unwrap_api_payload(result), None
            except Exception as exc:
                last_error = exc

        if hasattr(bot, "call_action"):
            try:
                result = await bot.call_action(action, **kwargs)
                return self._unwrap_api_payload(result), None
            except Exception as exc:
                last_error = exc

        if last_error:
            return None, f"{action} 调用失败: {last_error}"
        return None, f"当前平台不支持群管理操作: {action}"

    async def _get_group_member_info(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        group_id = self._to_optional_int(event.get_group_id())
        if group_id is None:
            return None, "当前不是群聊，无法查询群成员信息"

        target_user_id = self._to_optional_int(user_id)
        if target_user_id is None:
            return None, "user_id 必须是纯数字 QQ 号"

        payload, error = await self._call_group_action(
            event,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(target_user_id),
            no_cache=True,
        )
        if error:
            logger.warning(
                "[response_enhancer] get_group_member_info failed: %s",
                error,
            )
            return None, f"获取群成员信息失败: {error}"

        if isinstance(payload, dict):
            return payload, None
        return None, "获取群成员信息失败: 返回数据格式异常"

    async def _get_group_member_role(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> tuple[str | None, str | None]:
        member_info, error = await self._get_group_member_info(event, user_id)
        if error:
            return None, error

        role = str(member_info.get("role", "member") or "member").strip().lower()
        if role not in ("owner", "admin", "member"):
            role = "member"
        return role, None

    async def _set_group_ban(
        self,
        event: AstrMessageEvent,
        user_id: str,
        duration_seconds: int,
    ) -> tuple[bool, str | None]:
        group_id = self._to_optional_int(event.get_group_id())
        if group_id is None:
            return False, "当前不是群聊，无法执行群禁言"

        target_user_id = self._to_optional_int(user_id)
        if target_user_id is None:
            return False, "user_id 必须是纯数字 QQ 号"

        duration = self._to_optional_int(duration_seconds)
        if duration is None or duration < 0:
            return False, "duration_seconds 必须是大于等于 0 的整数"

        _, error = await self._call_group_action(
            event,
            "set_group_ban",
            group_id=int(group_id),
            user_id=int(target_user_id),
            duration=int(duration),
        )
        if error:
            logger.warning("[response_enhancer] set_group_ban failed: %s", error)
            return False, f"执行群禁言失败: {error}"
        return True, None
    # endregion 群管理动作

    # region 群禁言结果
    @staticmethod
    def _build_group_mute_response(
        event: AstrMessageEvent,
        *,
        user_id: str,
        reason: str | None,
        ok: bool,
        error_code: str,
        message: str,
        action: str = "",
        target_role: str = "",
        bot_role: str = "",
        requester_role: str = "",
        trigger_mode: str = "",
        duration_requested: int | None = None,
        duration_effective: int | None = None,
        truncated: bool = False,
    ) -> str:
        return json.dumps(
            {
                "ok": ok,
                "error_code": error_code,
                "message": message,
                "action": action,
                "trigger_mode": trigger_mode,
                "group_id": str(event.get_group_id() or ""),
                "requester_user_id": str(event.get_sender_id() or ""),
                "requester_role": requester_role,
                "bot_user_id": str(event.get_self_id() or ""),
                "bot_role": bot_role,
                "target_user_id": str(user_id or event.get_sender_id() or ""),
                "target_role": target_role,
                "duration_requested": duration_requested,
                "duration_effective": duration_effective,
                "truncated": truncated,
                "reason": str(reason or ""),
            },
            ensure_ascii=False,
        )

    async def _group_mute_user_result(
        self,
        event: AstrMessageEvent,
        *,
        user_id: str | None,
        duration_seconds: int | None,
        trigger_mode: str,
        reason: str | None,
        group_mute_max_seconds: int,
    ) -> str:
        if not event.get_group_id():
            return self._build_group_mute_response(
                event,
                user_id=str(user_id or ""),
                reason=reason,
                ok=False,
                error_code="NOT_GROUP",
                message="当前不是群聊，无法执行群禁言",
            )

        mode = str(trigger_mode or "request").strip().lower()
        if mode not in ("request", "auto"):
            return self._build_group_mute_response(
                event,
                user_id=str(user_id or ""),
                reason=reason,
                ok=False,
                error_code="INVALID_TRIGGER_MODE",
                message="trigger_mode 仅支持 request 或 auto",
                trigger_mode=mode,
            )

        if duration_seconds is None:
            duration_seconds = 600

        try:
            requested_duration = int(duration_seconds)
        except Exception:
            return self._build_group_mute_response(
                event,
                user_id=str(user_id or ""),
                reason=reason,
                ok=False,
                error_code="INVALID_DURATION",
                message="duration_seconds 必须是整数",
                trigger_mode=mode,
            )

        if requested_duration < 0:
            return self._build_group_mute_response(
                event,
                user_id=str(user_id or ""),
                reason=reason,
                ok=False,
                error_code="INVALID_DURATION",
                message="duration_seconds 必须大于等于 0",
                trigger_mode=mode,
                duration_requested=requested_duration,
            )

        duration_effective = requested_duration
        duration_truncated = False
        if requested_duration > 0 and requested_duration > group_mute_max_seconds:
            duration_effective = group_mute_max_seconds
            duration_truncated = True

        target_user_id = str(user_id or event.get_sender_id() or "").strip()
        if not target_user_id.isdigit():
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="INVALID_USER_ID",
                message="user_id 必须是纯数字 QQ 号",
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )

        requester_id = str(event.get_sender_id() or "").strip()
        requester_role = ""
        if mode == "request":
            requester_role, error = await self._get_group_member_role(event, requester_id)
            if error:
                return self._build_group_mute_response(
                    event,
                    user_id=target_user_id,
                    reason=reason,
                    ok=False,
                    error_code="API_FAILED",
                    message=f"无法校验发起者身份: {error}",
                    requester_role=requester_role,
                    trigger_mode=mode,
                    duration_requested=requested_duration,
                    duration_effective=duration_effective,
                    truncated=duration_truncated,
                )
            if requester_role not in ("owner", "admin"):
                return self._build_group_mute_response(
                    event,
                    user_id=target_user_id,
                    reason=reason,
                    ok=False,
                    error_code="INVOKER_FORBIDDEN",
                    message="request 模式仅允许群主或管理员发起禁言",
                    requester_role=requester_role,
                    trigger_mode=mode,
                    duration_requested=requested_duration,
                    duration_effective=duration_effective,
                    truncated=duration_truncated,
                )

        bot_user_id = str(event.get_self_id() or "").strip()
        if not bot_user_id.isdigit():
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="BOT_ROLE_INVALID",
                message="无法获取机器人账号信息，无法执行群禁言",
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )

        bot_role, error = await self._get_group_member_role(event, bot_user_id)
        if error:
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="API_FAILED",
                message=f"无法校验机器人身份: {error}",
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )
        if bot_role not in ("owner", "admin"):
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="BOT_ROLE_INVALID",
                message="机器人当前不是群主或管理员，无法执行群禁言",
                bot_role=bot_role,
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )

        target_role, error = await self._get_group_member_role(event, target_user_id)
        if error:
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="API_FAILED",
                message=f"无法校验目标用户身份: {error}",
                bot_role=bot_role,
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )
        if target_role != "member":
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="TARGET_NOT_MEMBER",
                message="仅允许操作普通成员(member)，不能操作群主或管理员",
                target_role=target_role,
                bot_role=bot_role,
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )

        success, error = await self._set_group_ban(
            event=event,
            user_id=target_user_id,
            duration_seconds=duration_effective,
        )
        if not success:
            return self._build_group_mute_response(
                event,
                user_id=target_user_id,
                reason=reason,
                ok=False,
                error_code="API_FAILED",
                message=error or "执行群禁言失败",
                target_role=target_role,
                bot_role=bot_role,
                requester_role=requester_role,
                trigger_mode=mode,
                duration_requested=requested_duration,
                duration_effective=duration_effective,
                truncated=duration_truncated,
            )

        action = "unmute" if duration_effective == 0 else "mute"
        action_text = "解除禁言" if duration_effective == 0 else f"禁言 {duration_effective} 秒"
        message = f"已对用户 {target_user_id} 执行{action_text}"
        if duration_truncated:
            message += (
                f"（请求 {requested_duration} 秒，已按上限截断到 {duration_effective} 秒）"
            )

        return self._build_group_mute_response(
            event,
            user_id=target_user_id,
            reason=reason,
            ok=True,
            error_code="",
            message=message,
            action=action,
            target_role=target_role,
            bot_role=bot_role,
            requester_role=requester_role,
            trigger_mode=mode,
            duration_requested=requested_duration,
            duration_effective=duration_effective,
            truncated=duration_truncated,
        )
    # endregion 群禁言结果

    # region 数据工具
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
    # endregion 数据工具
