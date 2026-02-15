from __future__ import annotations

from typing import Any


class CommonMixin:
    @staticmethod
    def _format_timestamp(ts: int) -> str:
        import time

        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    @staticmethod
    def _to_optional_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _clamp_int(value: Any, min_value: int, max_value: int, default: int) -> int:
        try:
            value = int(value)
        except Exception:
            return default
        if value < min_value:
            return min_value
        if value > max_value:
            return max_value
        return value

    @staticmethod
    def _to_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default

        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "是", "开"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "否", "关"}:
            return False
        return default
