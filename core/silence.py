from __future__ import annotations

import time
from typing import Any

from astrbot.api.event import AstrMessageEvent


class SilenceMixin:
    async def _is_silenced(self, event: AstrMessageEvent) -> bool:
        user_id = str(event.get_sender_id())
        now = int(time.time())

        global_key = self._silence_key("global", event, user_id)
        global_exp = await self.get_kv_data(global_key, 0)
        if global_exp and global_exp > now:
            return True
        if global_exp and global_exp <= now:
            await self.delete_kv_data(global_key)
            await self._remove_silence_index_entry("global", event, user_id)

        session_key = self._silence_key("session", event, user_id)
        session_exp = await self.get_kv_data(session_key, 0)
        if session_exp and session_exp > now:
            return True
        if session_exp and session_exp <= now:
            await self.delete_kv_data(session_key)
            await self._remove_silence_index_entry("session", event, user_id)

        return False

    def _silence_key(self, scope: str, event: AstrMessageEvent, user_id: str) -> str:
        scope = scope.lower()
        if scope == "global":
            return f"silence:global:{user_id}"
        return f"silence:session:{event.unified_msg_origin}:{user_id}"

    def _silence_index_key(self, scope: str, event: AstrMessageEvent) -> str:
        scope = scope.lower()
        if scope == "global":
            return "silence:index:global"
        return f"silence:index:session:{event.unified_msg_origin}"

    async def _upsert_silence_index(
        self, scope: str, event: AstrMessageEvent, user_id: str, expire_at: int
    ) -> None:
        index_key = self._silence_index_key(scope, event)
        raw_index = await self.get_kv_data(index_key, {})
        index: dict[str, int] = raw_index if isinstance(raw_index, dict) else {}
        index[str(user_id)] = int(expire_at)
        await self.put_kv_data(index_key, index)

    async def _remove_silence_index_entry(
        self, scope: str, event: AstrMessageEvent, user_id: str
    ) -> None:
        index_key = self._silence_index_key(scope, event)
        raw_index = await self.get_kv_data(index_key, {})
        if not isinstance(raw_index, dict):
            return

        if str(user_id) not in raw_index:
            return

        raw_index.pop(str(user_id), None)
        if raw_index:
            await self.put_kv_data(index_key, raw_index)
        else:
            await self.delete_kv_data(index_key)

    async def _get_active_silence_entries(
        self, scope: str, event: AstrMessageEvent
    ) -> list[dict[str, Any]]:
        now = int(time.time())
        index_key = self._silence_index_key(scope, event)
        raw_index = await self.get_kv_data(index_key, {})
        if not isinstance(raw_index, dict):
            return []

        active_entries: list[dict[str, Any]] = []
        cleaned_index: dict[str, int] = {}

        for raw_user_id, raw_expire_at in raw_index.items():
            user_id = str(raw_user_id)
            expire_at = self._to_optional_int(raw_expire_at)
            if expire_at is None or expire_at <= now:
                await self.delete_kv_data(self._silence_key(scope, event, user_id))
                continue

            cleaned_index[user_id] = expire_at
            active_entries.append(
                {
                    "user_id": user_id,
                    "scope": scope,
                    "scope_desc": "全局" if scope == "global" else "当前会话",
                    "expire_at_timestamp": expire_at,
                    "expire_at": self._format_timestamp(expire_at),
                    "remaining_seconds": max(0, expire_at - now),
                }
            )

        if cleaned_index:
            await self.put_kv_data(index_key, cleaned_index)
        else:
            await self.delete_kv_data(index_key)

        return active_entries
