from __future__ import annotations

import html
import re
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class ImageContextMixin:
    async def _get_recent_image_context_result(
        self,
        event: AstrMessageEvent,
        target_user_id: str,
        lookback_count: int,
        allow_group_fallback: bool,
        user_request: str,
    ) -> dict[str, Any]:
        lookback_count = self._clamp_int(
            lookback_count,
            min_value=5,
            max_value=200,
            default=30,
        )
        query_rounds = self._clamp_int(
            self.config.get("image_context_history_rounds", 3),
            min_value=1,
            max_value=6,
            default=3,
        )

        candidates: list[dict[str, Any]] = []
        candidates.extend(await self._collect_current_image_candidates(event))

        history_count = 0
        history_error = None
        if event.get_group_id():
            history_messages, history_error = await self._fetch_group_history_messages(
                event=event,
                count=lookback_count,
                query_rounds=query_rounds,
            )
            history_count = len(history_messages)
            candidates.extend(
                self._extract_history_image_candidates(history_messages=history_messages)
            )

        target_candidates_count = sum(
            1
            for candidate in candidates
            if str(candidate.get("sender_id", "")) == str(target_user_id)
        )

        selected = self._pick_best_image_candidate(
            candidates=candidates,
            target_user_id=target_user_id,
            allow_group_fallback=allow_group_fallback,
        )
        if not selected:
            return {
                "target_user_id": target_user_id,
                "lookback_count": lookback_count,
                "history_message_count": history_count,
                "allow_group_fallback": allow_group_fallback,
                "candidate_count": len(candidates),
                "target_candidate_count": target_candidates_count,
                "user_request": user_request,
                "error": self._build_no_image_error(
                    target_user_id=target_user_id,
                    allow_group_fallback=allow_group_fallback,
                    history_error=history_error,
                ),
            }

        analysis_text, vision_provider_id, analysis_error = (
            await self._analyze_recent_image_with_vision_model(
                event=event,
                image_input=selected["image_input"],
                user_request=user_request,
                image_context=selected,
            )
        )

        result = {
            "target_user_id": target_user_id,
            "lookback_count": lookback_count,
            "history_message_count": history_count,
            "allow_group_fallback": allow_group_fallback,
            "candidate_count": len(candidates),
            "target_candidate_count": target_candidates_count,
            "user_request": user_request,
            "image_source": selected.get("source", ""),
            "selected_message_id": selected.get("message_id", ""),
            "selected_message_time": selected.get("message_time"),
            "selected_message_time_str": selected.get("message_time_str", ""),
            "selected_sender_id": selected.get("sender_id", ""),
            "selected_sender_name": selected.get("sender_name", ""),
            "selected_text_preview": selected.get("text_preview", ""),
            "selected_image_ref": selected.get("image_ref", ""),
        }

        if vision_provider_id:
            result["vision_provider_id"] = vision_provider_id

        if history_error:
            result["history_error"] = history_error

        if analysis_error:
            result["note"] = "图片已提取，但视觉识别失败"
            result["error"] = analysis_error
        else:
            result["note"] = "图片提取并视觉识别成功"
            result["image_analysis"] = analysis_text

        return result

    async def _collect_current_image_candidates(
        self,
        event: AstrMessageEvent,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        try:
            message_segments = list(event.get_messages() or [])
        except Exception:
            message_segments = []

        msg_time = self._to_optional_int(getattr(event.message_obj, "timestamp", None))
        msg_time_str = self._format_timestamp(msg_time) if msg_time else ""
        sender_name = str(event.get_sender_name() or "").strip()

        for idx, segment in enumerate(message_segments):
            if not isinstance(segment, Comp.Image):
                continue

            image_input = await self._resolve_image_input_from_component(segment)
            if not image_input:
                continue

            candidates.append(
                {
                    "source": "current_message",
                    "message_id": str(getattr(event.message_obj, "message_id", "")),
                    "message_time": msg_time,
                    "message_time_str": msg_time_str,
                    "sender_id": str(event.get_sender_id() or ""),
                    "sender_name": sender_name,
                    "text_preview": self._extract_user_request(event),
                    "image_input": image_input,
                    "image_ref": self._public_image_ref(image_input),
                    "priority": 1_000_000 - idx,
                }
            )

        return candidates

    async def _resolve_image_input_from_component(self, segment: Comp.Image) -> str:
        try:
            file_path = await segment.convert_to_file_path()
            if file_path:
                return str(file_path)
        except Exception:
            pass

        fallback = str(getattr(segment, "url", "") or getattr(segment, "file", "")).strip()
        return fallback

    async def _fetch_group_history_messages(
        self,
        event: AstrMessageEvent,
        count: int,
        query_rounds: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        group_id = self._to_optional_int(event.get_group_id())
        if group_id is None:
            return [], "当前不是群聊，无法查询群历史消息"

        bot = getattr(event, "bot", None)
        if bot is None:
            return [], "当前平台未提供 bot 客户端，无法查询群历史消息"

        all_messages: list[dict[str, Any]] = []
        seen_message_ids: set[str] = set()
        # 维护备注(坑点):
        # 部分 OneBot/NapCat 实现在不传 message_seq 时会始终返回“最新一页”，
        # 看起来有数据但永远翻不到更早消息。这里必须显式从 0 开始。
        message_seq: int = 0
        last_error: Exception | None = None

        for _ in range(query_rounds):
            payload: dict[str, Any] = {
                "group_id": int(group_id),
                "count": int(count),
                "message_seq": int(message_seq),
                "reverseOrder": True,
            }

            result = None
            try:
                if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    result = await bot.api.call_action("get_group_msg_history", **payload)
                elif hasattr(bot, "call_action"):
                    result = await bot.call_action("get_group_msg_history", **payload)
                elif hasattr(bot, "get_group_msg_history"):
                    result = await bot.get_group_msg_history(**payload)
            except Exception as exc:
                last_error = exc
                break

            payload_data = self._unwrap_api_payload(result)
            messages = []
            if isinstance(payload_data, dict):
                messages = payload_data.get("messages", [])
            elif isinstance(payload_data, list):
                messages = payload_data

            if not isinstance(messages, list) or not messages:
                break

            before_len = len(all_messages)
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("message_id", ""))
                if message_id and message_id in seen_message_ids:
                    continue
                if message_id:
                    seen_message_ids.add(message_id)
                all_messages.append(message)

            # 维护备注(坑点):
            # 如果游标推进失败，接口可能重复返回同一页。
            # 这里一旦“本轮无新增”就立即停止，避免无效循环。
            if len(all_messages) == before_len:
                break

            next_message_seq = self._pick_next_history_message_seq(messages)
            if next_message_seq is None or next_message_seq == message_seq:
                break
            message_seq = next_message_seq

        if all_messages:
            return all_messages, None

        if last_error:
            logger.debug("[response_enhancer] get_group_msg_history failed: %s", last_error)
            return [], f"群历史查询失败: {last_error}"

        return [], "未在最近消息中找到可用的图片记录"

    def _extract_history_image_candidates(
        self,
        history_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        for idx, message in enumerate(history_messages):
            sender = message.get("sender", {})
            sender_id = str((sender or {}).get("user_id", "")).strip()
            sender_name = str(
                (sender or {}).get("card")
                or (sender or {}).get("nickname")
                or (sender or {}).get("nick")
                or ""
            ).strip()
            msg_time = self._to_optional_int(message.get("time"))
            msg_time_str = self._format_timestamp(msg_time) if msg_time else ""
            msg_id = str(message.get("message_id", "")).strip()

            raw_segments = message.get("message", [])
            image_inputs: list[str] = []
            text_preview = ""

            # 维护备注(坑点):
            # get_group_msg_history 的 message 字段并不总是“段数组”。
            # 某些实现/场景会返回 CQ 码字符串，这里必须两种都兼容。
            if isinstance(raw_segments, list):
                text_preview = self._extract_text_preview_from_segments(raw_segments)
                image_inputs = self._extract_image_inputs_from_segments(raw_segments)
            elif isinstance(raw_segments, str):
                text_preview = self._extract_text_preview_from_cq_string(raw_segments)
                image_inputs = self._extract_image_inputs_from_cq_string(raw_segments)
            else:
                continue

            for image_input in image_inputs:
                candidates.append(
                    {
                        "source": "group_history",
                        "message_id": msg_id,
                        "message_time": msg_time,
                        "message_time_str": msg_time_str,
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "text_preview": text_preview,
                        "image_input": image_input,
                        "image_ref": self._public_image_ref(image_input),
                        "priority": 100_000 - idx,
                    }
                )

        return candidates

    @staticmethod
    def _extract_text_preview_from_segments(segments: list[dict[str, Any]]) -> str:
        text_parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type", "")) != "text":
                continue
            data = segment.get("data", {})
            if not isinstance(data, dict):
                continue
            text = str(data.get("text", "")).strip()
            if text:
                text_parts.append(text)

        preview = "".join(text_parts).strip()
        if len(preview) > 120:
            preview = preview[:120] + "...(已截断)"
        return preview

    @staticmethod
    def _extract_text_preview_from_cq_string(message_text: str) -> str:
        raw = str(message_text or "").strip()
        if not raw:
            return ""
        # 去除 CQ 码，仅保留可阅读文本。
        text_only = re.sub(r"\[CQ:[^\]]+\]", "", raw).strip()
        if len(text_only) > 120:
            text_only = text_only[:120] + "...(已截断)"
        return text_only

    def _extract_image_inputs_from_segments(
        self, segments: list[dict[str, Any]]
    ) -> list[str]:
        image_inputs: list[str] = []

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type", "")) != "image":
                continue

            data = segment.get("data", {})
            if not isinstance(data, dict):
                continue

            for key in ("url", "file"):
                value = str(data.get(key, "")).strip()
                if not value:
                    continue
                if self._is_supported_image_input(value):
                    image_inputs.append(value)
                    break

        return image_inputs

    def _extract_image_inputs_from_cq_string(self, message_text: str) -> list[str]:
        image_inputs: list[str] = []
        raw = str(message_text or "")
        if not raw:
            return image_inputs

        for cq_body in re.findall(r"\[CQ:image,([^\]]+)\]", raw):
            decoded_body = html.unescape(cq_body)
            for key in ("url", "file"):
                match = re.search(rf"(?:^|,){key}=([^,\]]+)", decoded_body)
                if not match:
                    continue
                value = str(match.group(1) or "").strip()
                if value and self._is_supported_image_input(value):
                    image_inputs.append(value)
                    break

        return image_inputs

    def _pick_next_history_message_seq(
        self,
        messages: list[dict[str, Any]],
    ) -> int | None:
        if not messages:
            return None

        first_msg = messages[0] if isinstance(messages[0], dict) else {}
        last_msg = messages[-1] if isinstance(messages[-1], dict) else {}

        first_time = self._to_optional_int(first_msg.get("time"))
        last_time = self._to_optional_int(last_msg.get("time"))
        older_msg = first_msg
        # 维护备注(坑点):
        # reverseOrder=True 时，不同实现返回顺序可能不一致。
        # 不要假定 messages[-1] 一定更旧，先根据 time 选更旧消息，再取 seq/id 分页。
        if (
            first_time is not None
            and last_time is not None
            and last_time < first_time
        ):
            older_msg = last_msg

        for key in ("message_seq", "message_id"):
            seq = self._to_optional_int(older_msg.get(key))
            if seq:
                return seq

        return None

    @staticmethod
    def _is_supported_image_input(value: str) -> bool:
        return str(value).startswith(
            ("http://", "https://", "base64://", "file:///", "/")
        )

    def _pick_best_image_candidate(
        self,
        candidates: list[dict[str, Any]],
        target_user_id: str,
        allow_group_fallback: bool,
    ) -> dict[str, Any] | None:
        if not candidates:
            return None

        target_candidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("sender_id", "")) == str(target_user_id)
        ]
        if target_candidates:
            return max(target_candidates, key=lambda item: int(item.get("priority", 0)))

        if allow_group_fallback:
            return max(candidates, key=lambda item: int(item.get("priority", 0)))

        return None

    def _build_no_image_error(
        self,
        target_user_id: str,
        allow_group_fallback: bool,
        history_error: str | None,
    ) -> str:
        if not allow_group_fallback:
            base = f"未在用户 {target_user_id} 的最近消息中找到图片"
        else:
            base = "未在当前消息及最近群聊中找到可用图片"
        if history_error:
            return f"{base}；{history_error}"
        return base

    async def _analyze_recent_image_with_vision_model(
        self,
        event: AstrMessageEvent,
        image_input: str,
        user_request: str,
        image_context: dict[str, Any],
    ) -> tuple[str | None, str | None, str | None]:
        configured_provider_id = str(
            self.config.get("image_context_vision_provider_id", "")
            or self.config.get("avatar_vision_provider_id", "")
            or ""
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
            error_msg = "未配置可用的图片识别模型"
            if session_provider_error:
                error_msg += f"，且获取当前会话模型失败: {session_provider_error}"
            return None, None, error_msg

        primary_prompt = self._build_recent_image_vision_prompt(
            user_request=user_request,
            image_context=image_context,
        )
        safety_prompt = self._build_recent_image_vision_prompt(
            user_request=user_request,
            image_context=image_context,
            safety_fallback=True,
        )

        errors: list[str] = []
        for provider_id in provider_candidates:
            for stage, prompt in [
                ("primary", primary_prompt),
                ("safety_fallback", safety_prompt),
            ]:
                try:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=[image_input],
                    )
                except Exception as exc:
                    error_text = str(exc)
                    errors.append(f"{provider_id}[{stage}]: {error_text}")
                    if stage == "primary" and self._is_policy_block_error_text(error_text):
                        continue
                    break

                if llm_resp.role == "err":
                    error_text = llm_resp.completion_text or "模型返回错误"
                    errors.append(f"{provider_id}[{stage}]: {error_text}")
                    if stage == "primary" and self._is_policy_block_error_text(error_text):
                        continue
                    break

                analysis_text = str(llm_resp.completion_text or "").strip()
                if not analysis_text:
                    errors.append(f"{provider_id}[{stage}]: 识别结果为空")
                    break

                return analysis_text, provider_id, None

        return (
            None,
            provider_candidates[0] if provider_candidates else None,
            "图片视觉识别失败: " + " | ".join(errors) if errors else "图片视觉识别失败",
        )

    @staticmethod
    def _build_recent_image_vision_prompt(
        user_request: str,
        image_context: dict[str, Any],
        safety_fallback: bool = False,
    ) -> str:
        source = str(image_context.get("source", ""))
        sender_name = str(image_context.get("sender_name", "")).strip()
        sender_id = str(image_context.get("sender_id", "")).strip()
        message_time = str(image_context.get("message_time_str", "")).strip()
        text_preview = str(image_context.get("text_preview", "")).strip()

        context_block = (
            "图片来源信息:\n"
            f"- 来源: {source or 'unknown'}\n"
            f"- 发送者: {sender_name or '未知'}({sender_id or '未知'})\n"
            f"- 发送时间: {message_time or '未知'}\n"
            f"- 同消息文本: {text_preview or '（无可用文本）'}\n"
        )

        base_prompt = (
            "你是一个谨慎且详细的群聊图片理解助手。你将收到一张群聊中的图片。\n"
            "请先完整描述图片内容，再结合用户原始问题给出可直接回答用户的结论。\n\n"
            f"用户原始请求: {user_request}\n\n"
            f"{context_block}\n"
            "输出要求:\n"
            "1. 使用中文。\n"
            "2. 【图片完整描述】尽量全面，包含主体、动作、服饰、场景、画风、构图、可见文本等。\n"
            "3. 【与用户问题的关联】只保留和用户问题相关的关键信息。\n"
            "4. 【面向用户的回答】给出自然、直接、可发送的回答。\n"
            "5. 不要编造看不见的细节；不确定时必须明确说明。\n"
            "6. 禁止识别或猜测现实人物身份，不输出个人隐私信息。\n"
        )

        if safety_fallback:
            base_prompt += (
                "额外要求（安全降级模式）:\n"
                "A. 如果是现实人物，只描述外观，不做身份判断。\n"
                "B. 回答尽量中性，避免敏感推断。\n"
            )

        base_prompt += (
            "\n请严格按以下结构输出:\n"
            "【图片完整描述】\n"
            "...\n"
            "【与用户问题的关联】\n"
            "...\n"
            "【面向用户的回答】\n"
            "..."
        )
        return base_prompt

    @staticmethod
    def _is_policy_block_error_text(error_text: str) -> bool:
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
    def _public_image_ref(image_input: str) -> str:
        raw = str(image_input or "").strip()
        if not raw:
            return ""
        if raw.startswith("http"):
            return raw
        if raw.startswith("base64://"):
            return "base64://<omitted>"
        if raw.startswith("file:///"):
            return raw
        if "/" in raw:
            return raw.split("/")[-1]
        return raw
