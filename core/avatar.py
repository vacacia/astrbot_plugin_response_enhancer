from __future__ import annotations

import base64
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    import aiohttp
except Exception:
    aiohttp = None


class AvatarMixin:
    # region 头像获取
    async def _get_user_avatar(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> dict[str, Any]:
        avatar_size = self._clamp_int(
            self.config.get("avatar_size", 640),
            min_value=40,
            max_value=1000,
            default=640,
        )
        download_timeout = self._clamp_int(
            self.config.get("avatar_download_timeout", 10),
            min_value=1,
            max_value=30,
            default=10,
        )

        candidates: list[tuple[str, str]] = []
        napcat_avatar_url = await self._try_napcat_avatar_url(event, user_id)
        if napcat_avatar_url:
            candidates.append(("napcat", napcat_avatar_url))

        candidates.append(("qlogo_https", self._build_avatar_url(user_id, avatar_size)))
        candidates.append(
            (
                "qlogo_http_fallback",
                f"http://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec={avatar_size}",
            )
        )

        errors: list[str] = []
        for source, avatar_url in candidates:
            avatar_data, error = await self._download_avatar_data_uri(
                avatar_url,
                timeout_seconds=download_timeout,
            )
            if avatar_data:
                return {
                    "user_id": user_id,
                    "avatar_data": avatar_data,
                    "avatar_url": avatar_url,
                    "source": source,
                    "note": "头像获取成功，可用于视觉分析",
                }
            if error:
                errors.append(f"{source}: {error}")

        fallback_url = candidates[0][1] if candidates else self._build_avatar_url(
            user_id, avatar_size
        )
        return {
            "user_id": user_id,
            "avatar_url": fallback_url,
            "error": "头像获取失败: " + " | ".join(errors) if errors else "头像获取失败",
        }
    # endregion 头像获取

    # region 头像识图
    async def _analyze_avatar_with_vision_model(
        self,
        event: AstrMessageEvent,
        avatar_data: str,
        user_request: str,
    ) -> tuple[str | None, str | None, str | None]:
        image_input = self._to_astrbot_image_input(avatar_data)
        if not image_input:
            return None, None, "头像图片数据格式无效，无法提交给识图模型"

        configured_provider_id = str(
            self.config.get("avatar_vision_provider_id", "") or ""
        ).strip()

        provider_candidates: list[str] = []
        if configured_provider_id:
            provider_candidates.append(configured_provider_id)

        session_provider_error = ""
        try:
            session_provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            if session_provider_id and session_provider_id not in provider_candidates:
                provider_candidates.append(session_provider_id)
        except Exception as exc:
            session_provider_error = str(exc)

        if not provider_candidates:
            error_msg = "未配置可用的头像识别模型"
            if session_provider_error:
                error_msg += f"，且获取当前会话模型失败: {session_provider_error}"
            return None, None, error_msg

        vision_prompt = self._build_avatar_vision_prompt(user_request=user_request)
        vision_prompt_safety_fallback = self._build_avatar_vision_prompt(
            user_request=user_request,
            safety_fallback=True,
        )

        errors: list[str] = []
        for provider_id in provider_candidates:
            prompt_plans = [
                ("primary", vision_prompt),
                ("safety_fallback", vision_prompt_safety_fallback),
            ]
            for stage, current_prompt in prompt_plans:
                try:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=current_prompt,
                        image_urls=[image_input],
                    )
                except Exception as exc:
                    error_text = str(exc)
                    errors.append(f"{provider_id}[{stage}]: {error_text}")
                    if stage == "primary" and self._is_policy_block_error(error_text):
                        continue
                    break

                if llm_resp.role == "err":
                    error_text = llm_resp.completion_text or "模型返回错误"
                    errors.append(f"{provider_id}[{stage}]: {error_text}")
                    if stage == "primary" and self._is_policy_block_error(error_text):
                        continue
                    break

                analysis_text = (llm_resp.completion_text or "").strip()
                if not analysis_text:
                    errors.append(f"{provider_id}[{stage}]: 识别结果为空")
                    break

                return analysis_text, provider_id, None

        return (
            None,
            provider_candidates[0],
            "头像视觉识别失败: " + " | ".join(errors) if errors else "头像视觉识别失败",
        )
    # endregion 头像识图

    # region 提示词与请求
    @staticmethod
    def _build_avatar_vision_prompt(
        user_request: str, safety_fallback: bool = False
    ) -> str:
        base_prompt = (
            "你是一个谨慎且详细的头像识别助手。你会收到一张用户头像图片。\n"
            "请先完整描述头像中可见的画面信息，再结合用户原始请求给出回答。\n\n"
            f"用户原始请求: {user_request}\n\n"
            "输出要求:\n"
            "1. 使用中文。\n"
            "2. 【图片完整描述】必须尽量完整，涵盖人物外观、发色、服饰、姿态、背景、画风、构图和可见标志元素。\n"
            "3. 【角色识别结论】优先给出最可能角色名与作品名；不确定时给出 2-3 个候选并标注置信度。\n"
            "4. 【判断依据】明确写出关键视觉证据，不要只给结论。\n"
            "5. 【面向用户的回答】基于用户原始请求直接作答，语句可被主对话模型直接引用。\n"
            "6. 不要编造看不见的细节；看不清时必须明确说明不确定性。\n\n"
            "7. 禁止识别或猜测现实人物身份，不要输出真实姓名、账号、联系方式等个人信息。\n\n"
        )
        if safety_fallback:
            base_prompt += (
                "额外要求（安全降级模式）:\n"
                "A. 如果头像是现实人物，只描述外观特征，不进行身份判断。\n"
                "B. 如果头像是二次元/游戏/影视角色，可以给出可能角色名；不确定时明确说明。\n"
                "C. 回答尽量中性、简短、避免敏感推断。\n\n"
            )

        base_prompt += (
            "请严格按以下结构输出:\n"
            "【图片完整描述】\n"
            "...\n"
            "【角色识别结论】\n"
            "...\n"
            "【判断依据】\n"
            "...\n"
            "【面向用户的回答】\n"
            "..."
        )
        return base_prompt

    @staticmethod
    def _is_policy_block_error(error_text: str) -> bool:
        text = str(error_text or "").lower()
        policy_keywords = (
            "平台政策",
            "safety",
            "prohibited_content",
            "spii",
            "blocklist",
            "image_safety",
        )
        return any(keyword in text for keyword in policy_keywords)

    @staticmethod
    def _extract_user_request(event: AstrMessageEvent) -> str:
        request_text = ""
        try:
            request_text = str(event.get_message_str() or "").strip()
        except Exception:
            request_text = ""

        if not request_text:
            try:
                request_text = str(event.get_message_outline() or "").strip()
            except Exception:
                request_text = ""

        if not request_text:
            request_text = "（用户未提供可解析的文本请求）"

        if len(request_text) > 500:
            request_text = request_text[:500] + "...(已截断)"
        return request_text

    @staticmethod
    def _to_astrbot_image_input(avatar_data: str) -> str | None:
        raw = str(avatar_data or "").strip()
        if not raw:
            return None

        if raw.startswith("base64://"):
            return raw

        if raw.startswith("data:image/"):
            if "," not in raw:
                return None
            _, encoded = raw.split(",", 1)
            encoded = encoded.strip()
            if not encoded:
                return None
            return "base64://" + encoded

        return None
    # endregion 提示词与请求

    # region NapCat头像
    async def _try_napcat_avatar_url(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> str | None:
        bot = getattr(event, "bot", None)
        target_user_id = self._to_optional_int(user_id)
        if bot is None or target_user_id is None:
            return None

        async def _query_action(action: str, **kwargs: Any) -> dict[str, Any] | None:
            if hasattr(bot, action):
                try:
                    method = getattr(bot, action)
                    result = await method(**kwargs)
                    payload = self._unwrap_api_payload(result)
                    if isinstance(payload, dict):
                        return payload
                except Exception as exc:
                    logger.debug(
                        "[response_enhancer] bot.%s failed: %s",
                        action,
                        exc,
                    )

            if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                try:
                    result = await bot.api.call_action(action, **kwargs)
                    payload = self._unwrap_api_payload(result)
                    if isinstance(payload, dict):
                        return payload
                except Exception as exc:
                    logger.debug(
                        "[response_enhancer] api.call_action(%s) failed: %s",
                        action,
                        exc,
                    )

            return None

        try:
            group_id = self._to_optional_int(event.get_group_id())
            if group_id is not None:
                group_member_info = await _query_action(
                    "get_group_member_info",
                    group_id=group_id,
                    user_id=target_user_id,
                    no_cache=True,
                )
                avatar_url = self._extract_avatar_url(group_member_info)
                if avatar_url:
                    return avatar_url

            stranger_info = await _query_action(
                "get_stranger_info",
                user_id=target_user_id,
                no_cache=True,
            )
            avatar_url = self._extract_avatar_url(stranger_info)
            if avatar_url:
                return avatar_url
        except Exception as exc:
            logger.debug("[response_enhancer] query napcat avatar failed: %s", exc)

        return None
    # endregion NapCat头像

    # region 下载与解析
    async def _download_avatar_data_uri(
        self,
        avatar_url: str,
        timeout_seconds: int,
    ) -> tuple[str | None, str | None]:
        if aiohttp is None:
            return None, "aiohttp 不可用"

        timeout = aiohttp.ClientTimeout(total=float(timeout_seconds))
        headers = {"User-Agent": "AstrBot-ResponseEnhancer/0.2.3"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(avatar_url, headers=headers) as resp:
                    if resp.status != 200:
                        return None, f"HTTP {resp.status}"

                    body = await resp.read()
                    if not body:
                        return None, "响应内容为空"

                    if len(body) > 2 * 1024 * 1024:
                        return None, "头像文件过大"

                    content_type = str(resp.headers.get("Content-Type", "")).lower()
                    mime = content_type.split(";")[0].strip()
                    if not mime.startswith("image/"):
                        mime = "image/jpeg"

                    encoded = base64.b64encode(body).decode("ascii")
                    return f"data:{mime};base64,{encoded}", None
        except Exception as exc:
            return None, str(exc)

    @classmethod
    def _extract_avatar_url(cls, payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None

        nested_payload = payload.get("data")
        if isinstance(nested_payload, dict):
            nested_avatar_url = cls._extract_avatar_url(nested_payload)
            if nested_avatar_url:
                return nested_avatar_url

        for key in (
            "avatar",
            "avatar_url",
            "face",
            "face_url",
            "head_url",
            "img_url",
            "url",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
            if isinstance(value, dict):
                nested_url = value.get("url")
                if isinstance(nested_url, str) and nested_url.startswith(
                    ("http://", "https://")
                ):
                    return nested_url
        return None

    @staticmethod
    def _build_avatar_url(user_id: str, size: int = 640) -> str:
        if size < 40:
            size = 40
        if size > 1000:
            size = 1000
        return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s={size}"
    # endregion 下载与解析
