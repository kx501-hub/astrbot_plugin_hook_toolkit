# Hook Toolkit

一个通过 **Hook** 补齐 AstrBot 边角能力的插件，目前包含两个互不影响的功能：

| 功能 | 解决的问题 | 实现方式 |
|---|---|---|
| 子代理内置工具注入 | 子代理默认拿不到 `send_message_to_user` 等**内置工具**（内置工具只由主 Agent 显式注入），因此子代理无法主动发消息、`@` 群成员 | `@filter.on_llm_request()` 里幂等 patch `HandoffTool.agent.tools` |
| 插件显示名规范化 | 部分插件 `metadata.yaml` 没写 `display_name`，WebUI 里只能看到 `astrbot_plugin_xxx` | 启动时改写内存中的 `StarMetadata.display_name`，**不改动插件文件** |

## 安装

把本目录放到 `data/plugins/astrbot_plugin_hook_toolkit`，重启 AstrBot（或在 WebUI 插件页重载）。

要求 AstrBot `>= 4.14.0`（SubAgent 编排与 HandoffTool 在该版本引入）。

## 功能一：让子代理继承内置工具

AstrBot 里 `send_message_to_user`、`get_group_message_history` 等属于 **builtin tools**：它们不在通用工具集 `get_full_tool_set()` 里，而是由主 Agent 在 `astr_main_agent.py` 中按平台能力逐条注入。子代理的工具集来自 `HandoffTool.agent.tools`，默认只包含插件/MCP 工具与运行时电脑工具，所以子代理**看不到也调不到** `send_message_to_user`。

本插件在每次 LLM 请求前把配置中列出的内置工具补进每个委派工具：

- `agent.tools` 为 `None`（"继承全部工具"）时，展开为"通用工具集 + 运行时电脑工具 + 注入的内置工具"，避免丢失 shell/文件等运行时工具；
- `agent.tools` 为显式列表时，直接追加缺失的内置工具；
- 操作幂等，配置变更导致子代理重建后会自动重新注入。

注入后子代理即可调用官方 `send_message_to_user`，行为与主 Agent 完全一致：支持 `plain`/`image`/`record`/`video`/`file`/`mention_user`，可指定 `session` 跨会话发送，并且会写入 respond 阶段的去重标记（主 Agent 不会把同一段纯文本再复述一遍）。

> 注意：`send_message_to_user` 只有在平台支持主动消息时才是 `active` 状态。若该工具处于非激活状态，插件会记录 info 日志并跳过，不会强行注入。

配置项：

- `enable_subagent_tools`：功能开关
- `subagent_tool_names`：默认注入的内置工具名，默认 `["send_message_to_user"]`
- `subagent_tool_map`：按子代理单独配置，键为子代理名（Agent 名称，不带 `transfer_to_` 前缀），值为工具名列表或逗号分隔字符串，例如 `{"search_agent": ["send_message_to_user"], "weather": ["send_message_to_user", "get_group_message_history"], "chat": []}`

映射优先级：命中的子代理**只**用映射里的列表（不再叠加默认列表），`[]` / `none` 表示该子代理不注入任何内置工具；未命中的子代理使用 `subagent_tool_names`。

## 功能二：规范化插件显示名

规则（只处理**没有** `display_name` 的插件）：

1. 去掉 `astrbot_plugin_` / `astrbot_` 前缀；
2. `_`、`-`、`.` 分词；
3. 每个词首字母大写，例外：已经是全大写（`CVE`、`API`）或在 `display_name_uppercase_words` 里的词保持全大写；已经带驼峰的（`MsgProcessor`）原样保留。

示例：

| 插件名 | 生成的显示名 |
|---|---|
| `astrbot_plugin_cve_warning` | `CVE Warning` |
| `astrbot_plugin_pixiv_reborn` | `Pixiv Reborn` |
| `astrbot_plugin_media_parser` | `Media Parser` |
| `astrbot_plugin_galplayer` | `Galplayer`（连写词无法自动分词，建议用 `display_name_overrides`） |

配置项：

- `enable_display_name_fix`：功能开关
- `display_name_overrides`：`{"astrbot_plugin_pixiv_reborn": "Pixiv 重生"}`，命中时强制覆盖
- `display_name_skip`：跳过列表
- `display_name_uppercase_words`：保留全大写的单词表
- `display_name_include_reserved`：是否连 AstrBot 内置插件一起处理（默认否）

**只在运行期生效**，不修改任何插件的 `metadata.yaml`，插件更新/重装都不会冲突；代价是每次启动由本插件重新设置一次（`initialize()` + `on_astrbot_loaded()` 各跑一次，覆盖插件加载顺序问题）。

## 已知限制

- 依赖 AstrBot 内部结构（`HandoffTool.agent.tools`、`StarMetadata.display_name`、`FunctionToolExecutor._get_runtime_computer_tools`），核心大版本升级后可能需要跟进。
- 子代理运行过程不触发 `on_agent_begin` / `on_using_llm_tool` / `on_llm_response` 等钩子：`_execute_handoff()` 调用 `tool_loop_agent()` 时未传 `agent_hooks`，因此这些事件只在主 Agent 触发。
- 规范化只影响 WebUI 等读取 `StarMetadata.display_name` 的地方，不改变磁盘上的元数据。
