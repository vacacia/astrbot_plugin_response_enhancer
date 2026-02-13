# ResponseEnhancer

通过 **function calling** 赋予 LLM 引用回复、@用户、表情回应、禁言、屏蔽等增强能力。

LLM 在对话中自主判断何时使用这些工具

## 注册的 LLM 工具

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `reply_message` | 引用回复消息，可 @用户 | `text`, `reply_to_message_id`, `at_user_ids` |
| `send_message_at` | 发送消息，可 @用户 | `text`, `at_user_ids` |
| `react_emoji` | 对消息添加表情回应（群聊） | `emoji_id`, `message_id` |
| `mute_user` | 群禁言（需 Bot 管理员权限） | `user_id`, `duration_seconds` |
| `silence_user` | 屏蔽用户请求（不需要平台权限） | `user_id`, `duration_seconds`, `scope` |

## 工作原理

1. 插件通过 `@filter.llm_tool` 装饰器将各能力注册为 LLM function calling 工具
2. LLM 在对话中看到这些工具的描述，自主决定是否调用
3. 框架收到 LLM 的 tool_call 后，自动调用插件中的对应方法

## 配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `mute_max_seconds` | 禁言时长上限（秒） | 86400 |
| `silence_scope_default` | 屏蔽默认作用域（session / global） | session |

## 使用建议

可在 Bot 人格/系统提示词中引导 LLM 使用这些工具，例如：

> - 当用户明确要求 @某人时，使用 send_message_at 工具
> - 当用户严重违规时，可以使用 mute_user 或 silence_user 工具
> - 回复特定消息时，使用 reply_message 工具并指定 message_id

## 注意事项

- `react_emoji` 和 `mute_user` 仅在群聊且平台支持时生效
- `silence_user` 是插件级屏蔽（基于 KV 存储），不依赖平台管理员权限
- 被屏蔽的用户在屏蔽期内的 LLM 请求会被 `on_llm_request` 拦截

