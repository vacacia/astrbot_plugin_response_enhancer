# ResponseEnhancer

通过 **function calling** 赋予 LLM 表情回应、跳过回复、屏蔽用户、查询拉黑列表、查询群信息等能力。

LLM 在对话中自主判断何时使用这些工具

## 注册的 LLM 工具

| 工具名 | 说明 | 关键参数 |
|---|---|---|
| `react_emoji` | 对消息添加表情回应（群聊） | `emoji` |
| `skip_reply` | 主动跳过本轮回复 | `reason` |
| `silence_user` | 屏蔽用户请求（不需要平台权限） | `user_id`, `duration_seconds`, `scope` |
| `get_silence_list` | 查询当前拉黑（屏蔽）列表，含截止时间 | `scope` |
| `get_group_owner_info` | 查询当前群主信息 | - |
| `get_group_admins_info` | 查询当前管理员列表 | - |
| `get_group_member_count` | 查询当前群人数 | - |
| `get_user_avatar` | 获取用户头像并在工具内二次视觉识别 | `user_id` |

## 工作原理

1. 插件通过 `@filter.llm_tool` 装饰器将各能力注册为 LLM function calling 工具
2. LLM 在对话中看到这些工具的描述，自主决定是否调用
3. 框架收到 LLM 的 tool_call 后，自动调用插件中的对应方法

## 配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_react_emoji` | 是否启用表情回应工具 | `true` |
| `enable_skip_reply` | 是否启用跳过回复工具 | `true` |
| `enable_silence_user` | 是否启用屏蔽用户工具 | `true` |
| `enable_get_silence_list` | 是否启用拉黑列表查询工具 | `true` |
| `enable_get_group_owner` | 是否启用群主查询工具 | `true` |
| `enable_get_group_admins` | 是否启用管理员查询工具 | `true` |
| `enable_get_group_member_count` | 是否启用群人数查询工具 | `true` |
| `enable_get_user_avatar` | 是否启用获取用户头像工具 | `true` |
| `avatar_size` | 头像尺寸（像素） | 640 |
| `avatar_download_timeout` | 头像下载超时时间（秒） | 10 |
| `avatar_vision_provider_id` | 头像识别模型提供商（Provider） | `""` |
| `mute_max_seconds` | 禁言时长上限（秒） | 86400 |
| `silence_scope_default` | 屏蔽默认作用域（session / global） | session |

## 使用建议

可在 Bot 人格/系统提示词中引导 LLM 使用这些工具，例如：

> - 当用户在群里问"群主是谁"时，使用 `get_group_owner_info`
> - 当用户问"管理员都有谁"时，使用 `get_group_admins_info`
> - 当用户问"群里多少人"时，使用 `get_group_member_count`
> - 当用户问"拉黑了哪些人/到什么时候"时，使用 `get_silence_list`
> - 当用户骚扰 Bot 时，使用 `silence_user`
> - 当用户问"如何评价我的头像"、"看看我的头像"时，使用 `get_user_avatar`

## 注意事项

- `react_emoji` 仅在群聊且平台支持时生效
- `silence_user` 是插件级屏蔽（基于 KV 存储），不依赖平台管理员权限
- 被屏蔽的用户在屏蔽期内的 LLM 请求会被 `on_llm_request` 拦截
- `get_group_owner_info` / `get_group_admins_info` / `get_group_member_count` 依赖群成员 API，平台不支持时会返回失败信息
- `get_user_avatar` 在工具内调用视觉模型识别头像，并把“完整描述 + 识别结论 + 面向用户回答”返回给主对话模型
- `avatar_vision_provider_id` 留空时，默认跟随当前会话使用的模型提供商
