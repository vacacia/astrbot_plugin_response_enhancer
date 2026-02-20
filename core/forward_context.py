from __future__ import annotations

import html
import json
import re
import time
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class ForwardContextMixin:
    # region 转发上下文主流程
    async def _get_forward_context_result(
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
        candidates.extend(await self._collect_current_forward_candidates(event))
        reply_candidates, reply_error = await self._collect_reply_forward_candidates(event)
        candidates.extend(reply_candidates)

        history_count = 0
        history_error = None
        if event.get_group_id():
            history_messages, history_error = (
                await self._fetch_group_history_messages_for_forward(
                    event=event,
                    count=lookback_count,
                    query_rounds=query_rounds,
                )
            )
            history_count = len(history_messages)
            candidates.extend(
                self._extract_history_forward_candidates(history_messages=history_messages)
            )

        target_candidates_count = sum(
            1
            for candidate in candidates
            if str(candidate.get("sender_id", "")) == str(target_user_id)
        )

        selected = self._pick_best_forward_candidate(
            candidates=candidates,
            target_user_id=target_user_id,
            allow_group_fallback=allow_group_fallback,
        )
        if not selected:
            return {
                "target_user_id": target_user_id,
                "lookback_count": lookback_count,
                "allow_group_fallback": allow_group_fallback,
                "history_message_count": history_count,
                "candidate_count": len(candidates),
                "target_candidate_count": target_candidates_count,
                "user_request": user_request,
                "error": self._build_no_forward_error(
                    target_user_id=target_user_id,
                    allow_group_fallback=allow_group_fallback,
                    history_error=history_error or reply_error,
                ),
            }

        ordered_candidates = self._rank_forward_candidates(
            candidates=candidates,
            target_user_id=target_user_id,
            allow_group_fallback=allow_group_fallback,
        )
        max_expand_try_count = self._clamp_int(
            self.config.get("forward_context_expand_try_count", 4),
            min_value=1,
            max_value=20,
            default=4,
        )
        expand_errors: list[str] = []
        entries: list[dict[str, Any]] = []
        image_inputs: list[str] = []
        video_refs: list[str] = []
        selected_candidate: dict[str, Any] | None = None

        for candidate in ordered_candidates[:max_expand_try_count]:
            inline_nodes = candidate.get("inline_nodes")
            forward_id = str(candidate.get("forward_id", "")).strip()
            if not forward_id and not (
                isinstance(inline_nodes, list) and len(inline_nodes) > 0
            ):
                continue
            (
                current_entries,
                current_image_inputs,
                current_video_refs,
                current_expand_errors,
            ) = await self._expand_forward_context(
                event=event,
                forward_id=forward_id,
                outer_message_id=str(candidate.get("message_id", "")).strip(),
                initial_nodes=inline_nodes if isinstance(inline_nodes, list) else None,
            )
            if current_entries:
                selected_candidate = candidate
                entries = current_entries
                image_inputs = current_image_inputs
                video_refs = current_video_refs
                if current_expand_errors:
                    expand_errors.extend(current_expand_errors[:10])
                break

            if current_expand_errors:
                expand_errors.extend(current_expand_errors[:5])

        if not entries:
            cache_candidate = selected_candidate or selected
            cached_result = await self._load_forward_result_cache(
                forward_id=str(cache_candidate.get("forward_id", ""))
            )
            if cached_result:
                cached_result["target_user_id"] = target_user_id
                cached_result["lookback_count"] = lookback_count
                cached_result["allow_group_fallback"] = allow_group_fallback
                cached_result["history_message_count"] = history_count
                cached_result["candidate_count"] = len(candidates)
                cached_result["target_candidate_count"] = target_candidates_count
                cached_result["user_request"] = user_request
                cached_result["cache_hit"] = True
                cached_result["cache_reason"] = "源转发可能已过期，已回退到历史缓存"
                if expand_errors:
                    cached_result["expand_errors"] = expand_errors[:20]
                return cached_result

            return {
                "target_user_id": target_user_id,
                "lookback_count": lookback_count,
                "allow_group_fallback": allow_group_fallback,
                "history_message_count": history_count,
                "candidate_count": len(candidates),
                "target_candidate_count": target_candidates_count,
                "user_request": user_request,
                "selected_forward_id": cache_candidate.get("forward_id", ""),
                "selected_source": cache_candidate.get("source", ""),
                "error": "已找到合并转发入口，但展开失败。源转发可能已过期，请重新发送合并转发后再试",
                "expand_errors": expand_errors[:20],
            }
        selected = selected_candidate or selected

        (
            image_analysis,
            vision_provider_id,
            image_analysis_error,
        ) = await self._analyze_forward_images_with_vision_model(
            event=event,
            image_inputs=image_inputs,
            entries=entries,
            user_request=user_request,
        )
        forward_stats = self._build_forward_stats(entries)
        forward_text = self._build_forward_dialogue(entries, max_chars=5000)

        result = {
            "target_user_id": target_user_id,
            "lookback_count": lookback_count,
            "allow_group_fallback": allow_group_fallback,
            "history_message_count": history_count,
            "candidate_count": len(candidates),
            "target_candidate_count": target_candidates_count,
            "user_request": user_request,
            "selected_source": selected.get("source", ""),
            "selected_message_id": selected.get("message_id", ""),
            "selected_message_time": selected.get("message_time"),
            "selected_message_time_str": selected.get("message_time_str", ""),
            "selected_sender_id": selected.get("sender_id", ""),
            "selected_sender_name": selected.get("sender_name", ""),
            "selected_text_preview": selected.get("text_preview", ""),
            "selected_forward_id": selected.get("forward_id", ""),
            "forward_entry_count": len(entries),
            "forward_text_preview": self._build_forward_text_preview(
                entries,
                max_lines=12,
                max_chars=1000,
            ),
            "forward_dialogue": forward_text,
            "forward_stats": forward_stats,
            "image_count": len(image_inputs),
            "image_refs": [self._public_media_ref(x) for x in image_inputs],
            "video_count": len(video_refs),
            "video_refs": video_refs,
        }
        if history_error:
            result["history_error"] = history_error
        if reply_error:
            result["reply_error"] = reply_error
        if expand_errors:
            result["expand_errors"] = expand_errors[:20]
        if vision_provider_id:
            result["vision_provider_id"] = vision_provider_id
        if self._to_bool(self.config.get("forward_context_debug_entries"), default=False):
            # 维护备注:
            # forward_entries 信息量大、token 开销高。默认不返回，仅在调试时开启。
            result["forward_entries"] = entries

        if image_analysis_error:
            if image_analysis:
                result["image_analysis"] = image_analysis
                result["note"] = "合并转发已展开，图片部分识别成功"
            else:
                result["note"] = "合并转发已展开，但图片识别失败"
            result["error"] = image_analysis_error
        else:
            if image_inputs:
                result["note"] = "合并转发已展开并完成图片识别"
                if image_analysis:
                    result["image_analysis"] = image_analysis
            else:
                result["note"] = "合并转发已展开（无图片，无需识别）"

        await self._save_forward_result_cache(
            forward_id=str(selected.get("forward_id", "")),
            result=result,
        )

        return result
    # endregion 转发上下文主流程

    # region 候选收集
    async def _collect_current_forward_candidates(
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
            if not isinstance(segment, Comp.Forward):
                continue
            forward_id = str(getattr(segment, "id", "")).strip()
            if not forward_id:
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
                    "forward_id": forward_id,
                    "priority": 3_000_000 - idx,
                }
            )
        return candidates

    async def _collect_reply_forward_candidates(
        self,
        event: AstrMessageEvent,
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            message_segments = list(event.get_messages() or [])
        except Exception:
            message_segments = []

        reply_seg = next(
            (segment for segment in message_segments if isinstance(segment, Comp.Reply)),
            None,
        )
        if reply_seg is None:
            return [], None

        reply_msg_id = str(getattr(reply_seg, "id", "")).strip()
        if not reply_msg_id:
            return [], "引用消息缺少 message_id"

        reply_msg, error = await self._fetch_message_by_id(event=event, message_id=reply_msg_id)
        if not isinstance(reply_msg, dict):
            return [], error or "引用消息查询失败"

        forward_items = self._extract_forward_items_from_raw_message(reply_msg.get("message"))
        if not forward_items:
            return [], None

        sender = reply_msg.get("sender", {})
        sender_id = str((sender or {}).get("user_id", "")).strip()
        sender_name = str(
            (sender or {}).get("card")
            or (sender or {}).get("nickname")
            or (sender or {}).get("nick")
            or ""
        ).strip()
        msg_time = self._to_optional_int(reply_msg.get("time"))
        msg_time_str = self._format_timestamp(msg_time) if msg_time else ""
        msg_id = str(reply_msg.get("message_id", reply_msg_id)).strip()

        candidates: list[dict[str, Any]] = []
        for idx, item in enumerate(forward_items):
            forward_id = str(item.get("forward_id", "")).strip()
            inline_nodes = item.get("inline_nodes", [])
            candidates.append(
                {
                    "source": "replied_message",
                    "message_id": msg_id,
                    "message_time": msg_time,
                    "message_time_str": msg_time_str,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "text_preview": self._extract_text_preview_from_raw_message(
                        reply_msg.get("message")
                    ),
                    "forward_id": forward_id,
                    "inline_nodes": inline_nodes if isinstance(inline_nodes, list) else [],
                    "priority": 4_000_000 - idx,
                }
            )

        return candidates, error

    async def _fetch_message_by_id(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return None, "当前平台未提供 bot 客户端"

        payload = {"message_id": message_id}
        for action in ("get_msg",):
            try:
                if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    result = await bot.api.call_action(action, **payload)
                elif hasattr(bot, "call_action"):
                    result = await bot.call_action(action, **payload)
                elif hasattr(bot, action):
                    method = getattr(bot, action)
                    result = await method(**payload)
                else:
                    continue

                data = self._unwrap_api_payload(result)
                if isinstance(data, dict):
                    return data, None
            except Exception as exc:
                return None, f"{action} 失败: {exc}"

        return None, "当前平台不支持 get_msg"

    async def _fetch_group_history_messages_for_forward(
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

            if len(all_messages) == before_len:
                break

            next_message_seq = self._pick_next_history_message_seq_for_forward(messages)
            if next_message_seq is None or next_message_seq == message_seq:
                break
            message_seq = next_message_seq

        if all_messages:
            return all_messages, None
        if last_error:
            logger.debug("[response_enhancer] get_group_msg_history failed: %s", last_error)
            return [], f"群历史查询失败: {last_error}"
        return [], "未在最近消息中找到可用的合并转发记录"

    def _extract_history_forward_candidates(
        self,
        history_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for idx, message in enumerate(history_messages):
            forward_items = self._extract_forward_items_from_raw_message(message.get("message"))
            if not forward_items:
                continue

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
            text_preview = self._extract_text_preview_from_raw_message(message.get("message"))

            for sub_idx, item in enumerate(forward_items):
                forward_id = str(item.get("forward_id", "")).strip()
                inline_nodes = item.get("inline_nodes", [])
                candidates.append(
                    {
                        "source": "group_history",
                        "message_id": msg_id,
                        "message_time": msg_time,
                        "message_time_str": msg_time_str,
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "text_preview": text_preview,
                        "forward_id": forward_id,
                        "inline_nodes": inline_nodes if isinstance(inline_nodes, list) else [],
                        "priority": 100_000 - idx * 10 - sub_idx,
                    }
                )
        return candidates
    # endregion 候选收集

    # region 转发提取
    def _extract_forward_ids_from_raw_message(self, raw_message: Any) -> list[str]:
        ids: list[str] = []
        if isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, dict):
                    continue
                if str(segment.get("type", "")).lower() != "forward":
                    continue
                data = segment.get("data", {})
                forward_id = self._extract_forward_id_from_data(data)
                if forward_id:
                    ids.append(forward_id)
        elif isinstance(raw_message, str):
            decoded = html.unescape(raw_message)
            for cq_body in re.findall(r"\[CQ:forward,([^\]]+)\]", decoded):
                body = html.unescape(cq_body)
                forward_id = self._extract_forward_id_from_cq_body(body)
                if forward_id:
                    ids.append(forward_id)
        return list(dict.fromkeys([x for x in ids if x]))

    def _extract_forward_items_from_raw_message(self, raw_message: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, dict):
                    continue
                if str(segment.get("type", "")).lower() != "forward":
                    continue
                data = segment.get("data", {})
                forward_id = self._extract_forward_id_from_data(data)
                inline_nodes = self._extract_inline_nodes_from_forward_data(data)
                if not forward_id and not inline_nodes:
                    continue
                items.append(
                    {
                        "forward_id": forward_id,
                        "inline_nodes": inline_nodes,
                    }
                )
        elif isinstance(raw_message, str):
            for forward_id in self._extract_forward_ids_from_raw_message(raw_message):
                items.append(
                    {
                        "forward_id": forward_id,
                        "inline_nodes": [],
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in items:
            forward_id = str(item.get("forward_id", "")).strip()
            inline_nodes = item.get("inline_nodes", [])
            if forward_id:
                if forward_id in seen_ids:
                    for existing in deduped:
                        if str(existing.get("forward_id", "")).strip() == forward_id:
                            if not existing.get("inline_nodes") and inline_nodes:
                                existing["inline_nodes"] = inline_nodes
                            break
                    continue
                seen_ids.add(forward_id)
            deduped.append(item)
        return deduped

    def _extract_inline_nodes_from_forward_data(self, data: Any) -> list[Any]:
        if not isinstance(data, dict):
            return []
        for key in ("content", "message", "messages", "nodes"):
            content = data.get(key)
            if isinstance(content, list):
                return content
            if isinstance(content, str):
                decoded = html.unescape(content).strip()
                if not decoded:
                    continue
                try:
                    parsed = json.loads(decoded)
                except Exception:
                    continue
                if isinstance(parsed, list):
                    return parsed
        return []

    @staticmethod
    def _extract_forward_id_from_data(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("id", "forward_id", "res_id", "resid", "message_id"):
            value = str(data.get(key, "")).strip()
            if value:
                return value
        return ""

    def _extract_forward_id_from_cq_body(self, body: str) -> str:
        for key in ("id", "forward_id", "res_id", "resid", "message_id"):
            match = re.search(rf"(?:^|,){key}=([^,\]]+)", body)
            if match:
                value = str(match.group(1) or "").strip()
                if value:
                    return value
        return ""

    def _extract_text_preview_from_raw_message(self, raw_message: Any) -> str:
        text = ""
        if isinstance(raw_message, list):
            text_parts: list[str] = []
            for segment in raw_message:
                if not isinstance(segment, dict):
                    continue
                seg_type = str(segment.get("type", "")).lower()
                data = segment.get("data", {})
                if seg_type == "text" and isinstance(data, dict):
                    value = str(data.get("text", "")).strip()
                    if value:
                        text_parts.append(value)
                elif seg_type == "image":
                    text_parts.append("[图片]")
                elif seg_type == "video":
                    text_parts.append("[视频]")
                elif seg_type == "forward":
                    text_parts.append("[合并转发]")
            text = "".join(text_parts).strip()
        elif isinstance(raw_message, str):
            text = re.sub(r"\[CQ:[^\]]+\]", "", html.unescape(raw_message)).strip()
        if len(text) > 120:
            return text[:120] + "...(已截断)"
        return text
    # endregion 转发提取

    # region 候选策略
    def _pick_best_forward_candidate(
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

    def _rank_forward_candidates(
        self,
        candidates: list[dict[str, Any]],
        target_user_id: str,
        allow_group_fallback: bool,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []

        sorted_candidates = sorted(
            candidates,
            key=lambda item: int(item.get("priority", 0)),
            reverse=True,
        )
        target_candidates = [
            candidate
            for candidate in sorted_candidates
            if str(candidate.get("sender_id", "")) == str(target_user_id)
        ]
        other_candidates = [
            candidate
            for candidate in sorted_candidates
            if str(candidate.get("sender_id", "")) != str(target_user_id)
        ]
        ranked = target_candidates + (other_candidates if allow_group_fallback else [])

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in ranked:
            signature = (
                str(candidate.get("forward_id", "")),
                str(candidate.get("message_id", "")),
            )
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(candidate)
        return deduped

    def _build_no_forward_error(
        self,
        target_user_id: str,
        allow_group_fallback: bool,
        history_error: str | None,
    ) -> str:
        if not allow_group_fallback:
            base = f"未在用户 {target_user_id} 的最近消息中找到合并转发"
        else:
            base = "未在当前消息及最近群聊中找到可用的合并转发"
        if history_error:
            return f"{base}；{history_error}"
        return base
    # endregion 候选策略

    # region 转发展开
    async def _expand_forward_context(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        outer_message_id: str = "",
        initial_nodes: list[Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
        entries: list[dict[str, Any]] = []
        image_inputs: list[str] = []
        video_refs: list[str] = []
        errors: list[str] = []
        visited_forward_ids: set[str] = set()
        seen_images: set[str] = set()
        seen_videos: set[str] = set()

        if isinstance(initial_nodes, list) and initial_nodes:
            for raw_node in initial_nodes:
                node = self._normalize_forward_node(raw_node)
                if node is None:
                    continue
                await self._flatten_forward_node(
                    event=event,
                    node=node,
                    depth=1,
                    visited_forward_ids=visited_forward_ids,
                    entries=entries,
                    image_inputs=image_inputs,
                    video_refs=video_refs,
                    errors=errors,
                    seen_images=seen_images,
                    seen_videos=seen_videos,
                )
        else:
            await self._expand_forward_by_id(
                event=event,
                forward_id=forward_id,
                depth=1,
                source_message_id=outer_message_id,
                visited_forward_ids=visited_forward_ids,
                entries=entries,
                image_inputs=image_inputs,
                video_refs=video_refs,
                errors=errors,
                seen_images=seen_images,
                seen_videos=seen_videos,
            )
        return entries, image_inputs, video_refs, errors

    async def _expand_forward_by_id(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        depth: int,
        source_message_id: str,
        visited_forward_ids: set[str],
        entries: list[dict[str, Any]],
        image_inputs: list[str],
        video_refs: list[str],
        errors: list[str],
        seen_images: set[str],
        seen_videos: set[str],
    ) -> None:
        if not forward_id:
            return
        if forward_id in visited_forward_ids:
            return
        visited_forward_ids.add(forward_id)

        raw_nodes, fetch_error = await self._fetch_forward_messages(
            event=event,
            forward_id=forward_id,
            source_message_id=source_message_id,
        )
        if fetch_error:
            errors.append(fetch_error)
            return

        for raw_node in raw_nodes:
            node = self._normalize_forward_node(raw_node)
            if node is None:
                continue
            await self._flatten_forward_node(
                event=event,
                node=node,
                depth=depth,
                visited_forward_ids=visited_forward_ids,
                entries=entries,
                image_inputs=image_inputs,
                video_refs=video_refs,
                errors=errors,
                seen_images=seen_images,
                seen_videos=seen_videos,
            )

    async def _fetch_forward_messages(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        source_message_id: str = "",
    ) -> tuple[list[Any], str | None]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return [], "当前平台未提供 bot 客户端，无法获取合并转发详情"

        int_source_message_id = self._to_optional_int(source_message_id)
        int_forward_id = self._to_optional_int(forward_id)
        payload_variants: list[dict[str, Any]] = []
        for payload in (
            {"message_id": int_source_message_id}
            if int_source_message_id is not None
            else None,
            {"message_id": source_message_id} if source_message_id else None,
            {"id": int_source_message_id} if int_source_message_id is not None else None,
            {"id": source_message_id} if source_message_id else None,
            {"message_id": int_forward_id} if int_forward_id is not None else None,
            {"message_id": forward_id},
            {"id": int_forward_id} if int_forward_id is not None else None,
            {"id": forward_id},
            {"res_id": int_forward_id} if int_forward_id is not None else None,
            {"res_id": forward_id},
            {"resid": int_forward_id} if int_forward_id is not None else None,
            {"resid": forward_id},
            {"forward_id": int_forward_id} if int_forward_id is not None else None,
            {"forward_id": forward_id},
        ):
            if not payload:
                continue
            if list(payload.values())[0] in (None, ""):
                continue
            payload_variants.append(payload)

        dedup_payload_variants: list[dict[str, Any]] = []
        seen_payloads: set[tuple[str, str]] = set()
        for payload in payload_variants:
            key = str(next(iter(payload.keys())))
            value = str(next(iter(payload.values())))
            signature = (key, value)
            if signature in seen_payloads:
                continue
            seen_payloads.add(signature)
            dedup_payload_variants.append(payload)

        errors: list[str] = []
        for payload in dedup_payload_variants:
            try:
                result = None
                if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                    result = await bot.api.call_action("get_forward_msg", **payload)
                elif hasattr(bot, "call_action"):
                    result = await bot.call_action("get_forward_msg", **payload)
                elif hasattr(bot, "get_forward_msg"):
                    result = await bot.get_forward_msg(**payload)
                if result is None:
                    continue

                data = self._unwrap_api_payload(result)
                messages = None
                if isinstance(data, dict):
                    messages = (
                        data.get("messages")
                        or data.get("message")
                        or data.get("nodes")
                    )
                elif isinstance(data, list):
                    messages = data

                if isinstance(messages, list):
                    return messages, None
            except Exception as exc:
                payload_desc = f"{next(iter(payload.keys()))}={next(iter(payload.values()))}"
                errors.append(f"{payload_desc}: {exc}")
                continue

        if errors:
            return (
                [],
                "get_forward_msg 失败"
                f"(forward_id={forward_id}): "
                + " | ".join(errors[:3]),
            )
        return [], f"未获取到合并转发内容(forward_id={forward_id})"
    # endregion 转发展开

    # region 节点展开
    def _normalize_forward_node(self, raw_node: Any) -> dict[str, Any] | None:
        if not isinstance(raw_node, dict):
            return None

        if str(raw_node.get("type", "")).lower() == "node" and isinstance(
            raw_node.get("data"), dict
        ):
            data = raw_node["data"]
            return {
                "sender_id": str(data.get("user_id", "")).strip(),
                "sender_name": str(data.get("nickname", "")).strip(),
                "time": self._to_optional_int(raw_node.get("time"))
                or self._to_optional_int(data.get("time")),
                "content": data.get("content", data.get("message", [])),
            }

        sender = raw_node.get("sender", {})
        sender_id = str(
            (sender or {}).get("user_id", raw_node.get("user_id", ""))
        ).strip()
        sender_name = str(
            (sender or {}).get("card")
            or (sender or {}).get("nickname")
            or raw_node.get("nickname", "")
            or ""
        ).strip()
        content = raw_node.get("content")
        if content is None:
            content = raw_node.get("message", [])

        return {
            "sender_id": sender_id,
            "sender_name": sender_name,
            "time": self._to_optional_int(raw_node.get("time")),
            "content": content,
        }

    async def _flatten_forward_node(
        self,
        event: AstrMessageEvent,
        node: dict[str, Any],
        depth: int,
        visited_forward_ids: set[str],
        entries: list[dict[str, Any]],
        image_inputs: list[str],
        video_refs: list[str],
        errors: list[str],
        seen_images: set[str],
        seen_videos: set[str],
    ) -> None:
        sender_id = str(node.get("sender_id", "")).strip()
        sender_name = str(node.get("sender_name", "")).strip()
        msg_time = self._to_optional_int(node.get("time"))
        msg_time_str = self._format_timestamp(msg_time) if msg_time else ""

        text_parts: list[str] = []
        nested_forward_ids: list[str] = []
        inline_nodes: list[dict[str, Any]] = []
        node_image_count = 0
        node_video_count = 0

        content = node.get("content", [])
        parsed_segments = self._normalize_forward_segments(content)
        if parsed_segments is not None:
            image_inc, video_inc = self._consume_forward_segments(
                segments=parsed_segments,
                text_parts=text_parts,
                nested_forward_ids=nested_forward_ids,
                inline_nodes=inline_nodes,
                image_inputs=image_inputs,
                video_refs=video_refs,
                seen_images=seen_images,
                seen_videos=seen_videos,
            )
            node_image_count += image_inc
            node_video_count += video_inc
        elif isinstance(content, str):
            decoded = html.unescape(content)
            text_parts.append(re.sub(r"\[CQ:[^\]]+\]", "", decoded).strip())
            for image in self._extract_media_inputs_from_cq_string(decoded, media_type="image"):
                if image not in seen_images:
                    seen_images.add(image)
                    image_inputs.append(image)
                node_image_count += 1
                text_parts.append("[图片]")
            for video in self._extract_media_inputs_from_cq_string(decoded, media_type="video"):
                ref = self._public_media_ref(video)
                if ref not in seen_videos:
                    seen_videos.add(ref)
                    video_refs.append(ref)
                node_video_count += 1
                text_parts.append("[视频]")
            for nested_id in self._extract_forward_ids_from_raw_message(decoded):
                nested_forward_ids.append(nested_id)
                text_parts.append("[合并转发]")

        content_text = "".join([x for x in text_parts if x]).strip()
        if not content_text:
            content_text = "（无文本，仅媒体）"
        if len(content_text) > 500:
            content_text = content_text[:500] + "...(已截断)"

        entries.append(
            {
                "depth": depth,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "time": msg_time,
                "time_str": msg_time_str,
                "content": content_text,
                "image_count": node_image_count,
                "video_count": node_video_count,
            }
        )

        for nested_id in nested_forward_ids:
            await self._expand_forward_by_id(
                event=event,
                forward_id=nested_id,
                depth=depth + 1,
                source_message_id="",
                visited_forward_ids=visited_forward_ids,
                entries=entries,
                image_inputs=image_inputs,
                video_refs=video_refs,
                errors=errors,
                seen_images=seen_images,
                seen_videos=seen_videos,
            )
        for inline_node in inline_nodes:
            await self._flatten_forward_node(
                event=event,
                node=inline_node,
                depth=depth + 1,
                visited_forward_ids=visited_forward_ids,
                entries=entries,
                image_inputs=image_inputs,
                video_refs=video_refs,
                errors=errors,
                seen_images=seen_images,
                seen_videos=seen_videos,
            )

    @staticmethod
    def _normalize_forward_segments(content: Any) -> list[Any] | None:
        if isinstance(content, list):
            return content
        if not isinstance(content, str):
            return None

        decoded = html.unescape(content).strip()
        if not decoded:
            return []

        try:
            parsed = json.loads(decoded)
        except Exception:
            return None

        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("content", "message", "messages", "nodes"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
        return None

    def _consume_forward_segments(
        self,
        segments: list[Any],
        text_parts: list[str],
        nested_forward_ids: list[str],
        inline_nodes: list[dict[str, Any]],
        image_inputs: list[str],
        video_refs: list[str],
        seen_images: set[str],
        seen_videos: set[str],
    ) -> tuple[int, int]:
        node_image_count = 0
        node_video_count = 0

        for segment in segments:
            if isinstance(segment, str):
                text_parts.append(segment)
                continue
            if not isinstance(segment, dict):
                continue

            seg_type = str(segment.get("type", "")).lower()
            data = segment.get("data", {})

            if seg_type == "node":
                normalized_inline_node = self._normalize_forward_node(segment)
                if normalized_inline_node:
                    inline_nodes.append(normalized_inline_node)
                    text_parts.append("[嵌套节点]")
                continue

            if seg_type == "text":
                if isinstance(data, dict):
                    text_value = str(data.get("text", "")).strip()
                    if text_value:
                        text_parts.append(text_value)
                continue

            if seg_type == "image":
                image_input = self._extract_media_input_from_data(data)
                if image_input and image_input not in seen_images:
                    seen_images.add(image_input)
                    image_inputs.append(image_input)
                node_image_count += 1
                text_parts.append("[图片]")
                continue

            if seg_type == "video":
                video_input = self._extract_media_input_from_data(data)
                video_ref = self._public_media_ref(video_input or "video://unknown")
                if video_ref not in seen_videos:
                    seen_videos.add(video_ref)
                    video_refs.append(video_ref)
                node_video_count += 1
                text_parts.append("[视频]")
                continue

            if seg_type == "forward":
                nested_ids, inline_forward_nodes = self._extract_nested_forward_refs_from_data(
                    data
                )
                if inline_forward_nodes:
                    inline_nodes.extend(inline_forward_nodes)
                for nested_id in nested_ids:
                    nested_forward_ids.append(nested_id)
                text_parts.append("[合并转发]")
                continue

            if seg_type in {"json", "xml"}:
                nested_ids, inline_forward_nodes = self._extract_nested_forward_refs_from_data(
                    data
                )
                if inline_forward_nodes:
                    inline_nodes.extend(inline_forward_nodes)
                for nested_id in nested_ids:
                    nested_forward_ids.append(nested_id)
                text_parts.append("[合并转发]" if nested_ids or inline_forward_nodes else "[卡片]")
                continue

            if seg_type in {"face", "mface", "market_face"}:
                text_parts.append("[表情]")
                continue

            if seg_type == "at":
                if isinstance(data, dict):
                    qq = str(data.get("qq", "")).strip()
                    if qq:
                        text_parts.append(f"@{qq}")
                continue

            if seg_type == "reply":
                continue

            text_parts.append(f"[{seg_type or 'unknown'}]")

        return node_image_count, node_video_count

    def _extract_nested_forward_refs_from_data(
        self,
        data: Any,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        ids: list[str] = []
        inline_nodes: list[Any] = []
        inline_nodes.extend(self._extract_inline_nodes_from_forward_data(data))
        if not inline_nodes:
            inline_nodes.extend(self._extract_inline_forward_nodes_from_data(data))
        if inline_nodes:
            # 维护备注(坑点):
            # 某些平台会把内层转发“直接内联成 node 列表”，但同时附带一个不可查询的转发 ID。
            # 这时继续按 ID 调 get_forward_msg 通常失败（如 "message_id is required"/"内层消息不可获取"）。
            # 直接优先展开 inline nodes，避免无意义递归请求。
            normalized_inline_nodes: list[dict[str, Any]] = []
            for raw_node in inline_nodes:
                normalized = self._normalize_forward_node(raw_node)
                if normalized is not None:
                    normalized_inline_nodes.append(normalized)
            if normalized_inline_nodes:
                return [], normalized_inline_nodes

        direct_id = self._extract_forward_id_from_data(data)
        if direct_id:
            ids.append(direct_id)

        ids.extend(self._collect_forward_ids_from_obj(data))
        ids = [x for x in ids if x]
        return list(dict.fromkeys(ids)), []

    def _extract_inline_forward_nodes_from_data(self, data: Any) -> list[dict[str, Any]]:
        normalized_nodes: list[dict[str, Any]] = []

        def _consume_candidate(candidate: Any) -> None:
            if isinstance(candidate, str):
                decoded = html.unescape(candidate).strip()
                if not decoded:
                    return
                try:
                    parsed = json.loads(decoded)
                except Exception:
                    return
                _consume_candidate(parsed)
                return

            if isinstance(candidate, dict):
                if str(candidate.get("type", "")).lower() == "node":
                    normalized = self._normalize_forward_node(candidate)
                    if normalized:
                        normalized_nodes.append(normalized)
                    return

                for key in ("messages", "nodes", "content", "message", "data"):
                    if key in candidate:
                        _consume_candidate(candidate.get(key))
                return

            if isinstance(candidate, list):
                for item in candidate:
                    _consume_candidate(item)

        _consume_candidate(data)

        dedup: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for node in normalized_nodes:
            signature = (
                str(node.get("sender_id", "")),
                str(node.get("time", "")),
                str(node.get("content", ""))[:120],
            )
            if signature in seen:
                continue
            seen.add(signature)
            dedup.append(node)
        return dedup

    def _collect_forward_ids_from_obj(self, obj: Any) -> list[str]:
        ids: list[str] = []

        def _walk(value: Any, parent_type: str = "") -> None:
            if isinstance(value, str):
                decoded = html.unescape(value).strip()
                if not decoded:
                    return
                ids.extend(self._extract_forward_ids_from_raw_message(decoded))
                try:
                    parsed = json.loads(decoded)
                except Exception:
                    return
                _walk(parsed, parent_type=parent_type)
                return

            if isinstance(value, list):
                for item in value:
                    _walk(item, parent_type=parent_type)
                return

            if not isinstance(value, dict):
                return

            current_type = str(value.get("type", "")).lower() or parent_type
            for key, raw in value.items():
                key_lower = str(key).lower()
                if key_lower in {"resid", "res_id", "forward_id"}:
                    candidate_id = str(raw or "").strip()
                    if candidate_id:
                        ids.append(candidate_id)
                elif key_lower == "id" and current_type == "forward":
                    candidate_id = str(raw or "").strip()
                    if candidate_id:
                        ids.append(candidate_id)
                _walk(raw, parent_type=current_type)

        _walk(obj)
        return list(dict.fromkeys([x for x in ids if x]))
    # endregion 节点展开

    # region 结果缓存
    async def _load_forward_result_cache(self, forward_id: str) -> dict[str, Any] | None:
        cache_key = f"forward_context_cache:{str(forward_id).strip()}"
        if not str(forward_id).strip():
            return None
        try:
            payload = await self.get_kv_data(cache_key)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        cached_at = self._to_optional_int(payload.get("cached_at")) or 0
        ttl = self._clamp_int(
            self.config.get("forward_context_cache_ttl_seconds", 259200),
            min_value=60,
            max_value=2592000,
            default=259200,
        )
        if cached_at <= 0 or int(time.time()) - cached_at > ttl:
            return None

        result = payload.get("result")
        if not isinstance(result, dict):
            return None
        return dict(result)

    async def _save_forward_result_cache(self, forward_id: str, result: dict[str, Any]) -> None:
        normalized_forward_id = str(forward_id or "").strip()
        if not normalized_forward_id:
            return

        cache_key = f"forward_context_cache:{normalized_forward_id}"
        cache_payload = {
            "cached_at": int(time.time()),
            "result": result,
        }
        try:
            await self.put_kv_data(cache_key, cache_payload)
        except Exception as exc:
            logger.debug(
                "[response_enhancer] 保存 forward_context 缓存失败(forward_id=%s): %s",
                normalized_forward_id,
                exc,
            )
    # endregion 结果缓存

    # region 图片识别
    @staticmethod
    def _extract_media_inputs_from_cq_string(message_text: str, media_type: str) -> list[str]:
        key = "image" if media_type == "image" else "video"
        values: list[str] = []
        for cq_body in re.findall(rf"\[CQ:{key},([^\]]+)\]", message_text):
            decoded_body = html.unescape(cq_body)
            for field in ("url", "file"):
                match = re.search(rf"(?:^|,){field}=([^,\]]+)", decoded_body)
                if not match:
                    continue
                value = str(match.group(1) or "").strip()
                if value:
                    values.append(value)
                    break
        return values

    @staticmethod
    def _extract_media_input_from_data(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("url", "file"):
            value = str(data.get(key, "")).strip()
            if value:
                return value
        return ""

    async def _analyze_forward_images_with_vision_model(
        self,
        event: AstrMessageEvent,
        image_inputs: list[str],
        entries: list[dict[str, Any]],
        user_request: str,
    ) -> tuple[str | None, str | None, str | None]:
        if not image_inputs:
            return None, None, None

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

        context_excerpt = self._build_forward_text_preview(entries, max_lines=25, max_chars=2000)
        batch_size = 8
        all_batch_results: list[str] = []
        errors: list[str] = []

        for provider_id in provider_candidates:
            all_batch_results.clear()
            errors.clear()
            provider_failed = False

            for start in range(0, len(image_inputs), batch_size):
                end = min(len(image_inputs), start + batch_size)
                batch = image_inputs[start:end]
                prompt_primary = self._build_forward_image_prompt(
                    user_request=user_request,
                    context_excerpt=context_excerpt,
                    batch_start=start + 1,
                    batch_end=end,
                    total_count=len(image_inputs),
                )
                prompt_safety = self._build_forward_image_prompt(
                    user_request=user_request,
                    context_excerpt=context_excerpt,
                    batch_start=start + 1,
                    batch_end=end,
                    total_count=len(image_inputs),
                    safety_fallback=True,
                )

                batch_text = None
                for stage, prompt in [("primary", prompt_primary), ("safety_fallback", prompt_safety)]:
                    try:
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            image_urls=batch,
                        )
                    except Exception as exc:
                        error_text = str(exc)
                        errors.append(
                            f"{provider_id}[{stage}] batch({start + 1}-{end}): {error_text}"
                        )
                        if stage == "primary" and self._is_policy_block_error_text_for_forward(error_text):
                            continue
                        provider_failed = True
                        break

                    if llm_resp.role == "err":
                        error_text = llm_resp.completion_text or "模型返回错误"
                        errors.append(
                            f"{provider_id}[{stage}] batch({start + 1}-{end}): {error_text}"
                        )
                        if stage == "primary" and self._is_policy_block_error_text_for_forward(error_text):
                            continue
                        provider_failed = True
                        break

                    candidate_text = str(llm_resp.completion_text or "").strip()
                    if not candidate_text:
                        errors.append(
                            f"{provider_id}[{stage}] batch({start + 1}-{end}): 识别结果为空"
                        )
                        provider_failed = True
                        break
                    batch_text = candidate_text
                    break

                if batch_text:
                    all_batch_results.append(f"[图片{start + 1}-{end}]\n{batch_text}")
                elif provider_failed:
                    break

            if all_batch_results:
                return "\n\n".join(all_batch_results), provider_id, (
                    "部分图片识别失败: " + " | ".join(errors) if errors else None
                )

        return None, (provider_candidates[0] if provider_candidates else None), (
            "合并转发图片识别失败: " + " | ".join(errors) if errors else "合并转发图片识别失败"
        )

    @staticmethod
    def _build_forward_image_prompt(
        user_request: str,
        context_excerpt: str,
        batch_start: int,
        batch_end: int,
        total_count: int,
        safety_fallback: bool = False,
    ) -> str:
        prompt = (
            "你是一个谨慎的合并转发图片理解助手。\n"
            f"当前需要分析图片批次: 第 {batch_start} 到 {batch_end} 张（总计 {total_count} 张）。\n\n"
            f"用户原始请求: {user_request}\n\n"
            "转发文本摘录（用于理解语境）:\n"
            f"{context_excerpt or '（无可用文本）'}\n\n"
            "输出要求:\n"
            "1. 使用中文。\n"
            "2. 先按图片顺序描述每张图的关键信息。\n"
            "3. 再总结与用户问题相关的要点。\n"
            "4. 最后给出可直接回复用户的一段回答。\n"
            "5. 不确定时明确说明，不得编造。\n"
            "6. 禁止识别或猜测现实人物身份，不输出个人隐私信息。\n"
        )
        if safety_fallback:
            prompt += (
                "额外要求（安全降级模式）:\n"
                "A. 只描述可见外观和场景，不做敏感推断。\n"
                "B. 用词尽量中性。\n"
            )
        prompt += (
            "\n请严格按以下结构输出:\n"
            "【逐图描述】\n"
            "...\n"
            "【与用户问题的关联】\n"
            "...\n"
            "【面向用户的回答】\n"
            "..."
        )
        return prompt

    @staticmethod
    def _is_policy_block_error_text_for_forward(error_text: str) -> bool:
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
    # endregion 图片识别

    # region 文本输出
    @staticmethod
    def _public_media_ref(media_input: str) -> str:
        raw = str(media_input or "").strip()
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

    @staticmethod
    def _build_forward_text_preview(
        entries: list[dict[str, Any]],
        max_lines: int = 20,
        max_chars: int = 1200,
    ) -> str:
        lines: list[str] = []
        for idx, entry in enumerate(entries[:max_lines], start=1):
            sender_name = str(entry.get("sender_name", "")).strip() or "未知发送者"
            sender_id = str(entry.get("sender_id", "")).strip() or "未知ID"
            content = str(entry.get("content", "")).strip()
            depth = entry.get("depth", 0)
            lines.append(f"{idx}. [d{depth}] {sender_name}({sender_id}): {content}")

        preview = "\n".join(lines).strip()
        if len(preview) > max_chars:
            preview = preview[:max_chars] + "...(已截断)"
        return preview

    @staticmethod
    def _build_forward_dialogue(
        entries: list[dict[str, Any]],
        max_chars: int = 5000,
    ) -> str:
        lines: list[str] = []
        for idx, entry in enumerate(entries, start=1):
            depth = entry.get("depth", 0)
            sender_name = str(entry.get("sender_name", "")).strip() or "未知发送者"
            sender_id = str(entry.get("sender_id", "")).strip() or "未知ID"
            time_str = str(entry.get("time_str", "")).strip()
            content = str(entry.get("content", "")).strip()
            image_count = int(entry.get("image_count", 0) or 0)
            video_count = int(entry.get("video_count", 0) or 0)

            media_tag = ""
            if image_count or video_count:
                media_tag = f" [img:{image_count} vid:{video_count}]"

            prefix = f"{idx:02d}. [d{depth}] {sender_name}({sender_id})"
            if time_str:
                prefix += f" {time_str}"
            lines.append(f"{prefix}: {content}{media_tag}")

        dialogue = "\n".join(lines).strip()
        if len(dialogue) > max_chars:
            dialogue = dialogue[:max_chars] + "...(已截断)"
        return dialogue

    @staticmethod
    def _build_forward_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
        participant_counter: dict[str, int] = {}
        depth_max = 0
        times: list[int] = []

        for entry in entries:
            sender_name = str(entry.get("sender_name", "")).strip() or "未知发送者"
            sender_id = str(entry.get("sender_id", "")).strip() or "未知ID"
            key = f"{sender_name}({sender_id})"
            participant_counter[key] = participant_counter.get(key, 0) + 1

            depth = int(entry.get("depth", 0) or 0)
            if depth > depth_max:
                depth_max = depth

            raw_time = entry.get("time")
            try:
                ts = int(raw_time)
            except Exception:
                ts = 0
            if ts > 0:
                times.append(ts)

        participants = sorted(
            participant_counter.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        participant_top = [f"{name} x{count}" for name, count in participants[:10]]

        time_range = ""
        if times:
            import time as _time

            start = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(min(times)))
            end = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(max(times)))
            time_range = f"{start} ~ {end}"

        return {
            "participant_count": len(participant_counter),
            "participants_top": participant_top,
            "max_depth": depth_max,
            "time_range": time_range,
        }

    def _pick_next_history_message_seq_for_forward(
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
    # endregion 文本输出
