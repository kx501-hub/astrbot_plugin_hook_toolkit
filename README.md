# Hook Toolkit

一个通过 **Hook** 补齐 AstrBot 边角能力的插件，目前包含三个可独立配置的功能：

| 功能 | 解决的问题 | 实现方式 |
|---|---|---|
| 子代理内置工具注入 | 子代理默认拿不到 `send_message_to_user` 等**内置工具**（内置工具只由主 Agent 显式注入），因此子代理无法主动发消息、`@` 群成员 | `@filter.on_llm_request()` 里幂等 patch `HandoffTool.agent.tools` |
| 插件显示名规范化 | 部分插件 `metadata.yaml` 没写 `display_name`，WebUI 里只能看到 `astrbot_plugin_xxx` | 启动时改写内存中的 `StarMetadata.display_name`，**不改动插件文件** |
| 按插件网络代理 | 目标插件未提供代理配置，需要指定代理出口 | 根据请求调用来源，为 aiohttp、httpx、requests 应用代理规则 |

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
- `subagent_tool_map`：键为子代理名（Agent 名称，不带 `transfer_to_` 前缀），值为该子代理要注入的内置工具名（列表或逗号分隔字符串），例如 `{"search_agent": ["send_message_to_user"], "weather": ["send_message_to_user", "get_group_message_history"], "chat": []}`

每个子代理只拿到映射中为它列出的内置工具；**未列出的子代理不注入任何内置工具**，值写成 `[]` / `none` / `off` 同样表示不注入。

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

## 功能三：按插件网络代理

在 AstrBot 插件配置中开启 `enable_plugin_proxy`，向 `plugin_proxy_rules` 添加规则：

- **插件名称**：目标插件 `metadata.yaml` 中的 `name`，也可填写插件目录名，不使用显示名。
- **代理地址**：例如 `http://127.0.0.1:7890`；支持 HTTP/HTTPS 代理及 URL 中的用户名密码。代理必须能从 AstrBot 所在环境访问，容器中的 `127.0.0.1` 指向容器自身。
- **网络客户端**：默认 `auto`，在实际请求时识别 aiohttp、httpx、requests；也可手动指定其中一种。手动选择只覆盖该客户端，不匹配时保留原行为并记录警告。
- **启用这条规则**：独立开关；同一插件出现多条启用规则时，以最后一条为准。

例如，配置 `plugin=astrbot_plugin_media_parser`、`proxy=http://127.0.0.1:7890`、`client=auto`，随后重载 Hook Toolkit。目标插件首次发出可识别请求时，日志会显示检测到的客户端，不输出代理地址和凭据。规则为空时不安装网络补丁。

规则匹配后覆盖目标请求已有的代理设置，包括它使用的环境代理。采用请求发送时的来源识别，标准客户端已创建的会话也能生效，保留原客户端的请求头、Cookie、认证和超时；同一客户端被其他插件使用时，按当前调用来源决定是否应用代理。httpx 使用独立代理连接池，随客户端关闭或 Hook Toolkit 卸载释放。停止或重载 Hook Toolkit 会撤销它持有的补丁。

覆盖范围与限制：

- 只覆盖通过这三种标准客户端发送、且调用栈能识别到插件来源的请求；「自动」不包含其他客户端、浏览器、命令行程序、外部下载器、直接套接字或 WebSocket。调用 AstrBot 模型提供商及平台服务发出的请求仍使用对应服务自身配置。
- 插件委托第三方后台线程或独立后台任务请求时，如果发送调用栈没有插件代码，无法识别来源，请使用目标插件自身的代理配置。插件自己的异步任务仍可通过插件代码识别。
- aiohttp 自定义连接器、requests 自定义适配器、httpx 自定义传输器（包括 MockTransport、ASGITransport 及自定义子类）不支持；匹配规则后遇到它们会报错，避免悄悄改为直连。标准 httpx 传输器保留 TLS 证书验证、协议和连接数量设置。
- 不支持 SOCKS 代理。缺少客户端库、无效规则会记录警告并跳过；代理连接失败由目标插件按原有异常流程处理，不自动回退直连。
- 插件加载阶段中，早于 Hook Toolkit 初始化发出的请求不受影响。修改配置后重载 Hook Toolkit；网络库内部接口变化可能需要更新本插件。

## 已知限制

- 依赖 AstrBot 内部结构（`HandoffTool.agent.tools`、`StarMetadata.display_name`、`FunctionToolExecutor._get_runtime_computer_tools`），核心大版本升级后可能需要跟进。
- 子代理运行过程不触发 `on_agent_begin` / `on_using_llm_tool` / `on_llm_response` 等钩子：`_execute_handoff()` 调用 `tool_loop_agent()` 时未传 `agent_hooks`，因此这些事件只在主 Agent 触发。
- 规范化只影响 WebUI 等读取 `StarMetadata.display_name` 的地方，不改变磁盘上的元数据。
