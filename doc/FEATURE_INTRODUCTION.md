# api2cursor 功能介绍

`api2cursor` 是一个面向 Cursor 的 API 代理与协议转换服务。它通过“中转桥接”，把 Cursor 发出的请求与上游中转站实际支持的接口/字段协议对齐，让两端协议天然兼容。

---

## 它解决什么问题

Cursor 会根据你在模型配置中填写的模型名，采用不同的请求协议格式，例如：

- `claude-sonnet-*`、`glm-*`：使用 `/v1/chat/completions`（OpenAI Chat Completions 风格）
- `gpt-*`、`claude-opus-*`：使用 `/v1/responses`（OpenAI Responses 风格）

而很多上游中转站只支持部分接口（常见为 `/v1/chat/completions`、`/v1/messages` 或 `/v1/responses`）。

`api2cursor` 的作用是把这两者“对上”：

- 不管 Cursor 发什么格式，都能正确转发到中转站
- 不管中转站返回什么格式，都能转换成 Cursor 可接收的格式

---

## 架构概览：三种入口协议 + 三种上游协议

项目可以理解为一个协议桥：

- 三种对外入口（给 Cursor 用）：`/v1/chat/completions`、`/v1/responses`、`/v1/messages`
- 三种对内后端协议（面向中转站）：OpenAI Chat Completions、Anthropic Messages、OpenAI Responses（以及兼容 Gemini 的路径）

典型逻辑如下：

- `chat.py`：接住 Cursor 的 Chat Completions 请求，根据模型映射决定发往哪种后端协议
- `responses.py`：接住 Cursor 的 Responses 请求，在需要时做 `Responses ↔ Chat Completions` 或 `Responses ↔ Messages` 桥接
- `messages.py`：提供 Anthropic 原生 Messages 的直通/透传场景

---

## 管理面板：模型映射与路由控制

服务启动后可访问管理面板 `http://localhost:3029/admin`，在其中为每个“Cursor 自定义模型”配置：

- Cursor 模型名：你在 Cursor 的自定义模型里填的名字
- 上游模型名：真正发送到中转站的模型名
- 后端类型：用于选择桥接/转发策略（常见取值包括 `openai` / `anthropic` / `responses` / `gemini` / `auto`）
- 自定义地址/密钥（可选）：用于分流到不同中转站
- 日志模式：`off` / `simple` / `verbose`

这样你可以在 Cursor 侧始终维持统一的模型名与对外协议，同时让代理按映射把请求转到你实际可用的上游接口。

---

## 兼容性修复（字段与工具调用）

为了最大化兼容不同 LLM 平台/中转站实现，代理会自动处理一系列常见差异，例如：

- Cursor 工具 `tools` 扁平格式与 OpenAI 标准嵌套格式的互转
- `reasoningContent` 与 `reasoning_content` 字段命名兼容
- `<think>` 标签内容提取到 `reasoning_content`
- 旧版 `function_call` 与新版 `tool_calls` 的兼容
- `tool_calls` 缺失 `id` / `index` / `type` 等字段时的补全
- 智能引号替换为普通引号，避免精确匹配失败
- `file_path` 与 `path` 字段映射
- `finish_reason` 等返回值修正

---

## 调试日志（可定位协议桥接问题）

支持三档调试模式：

- `off`：关闭调试日志
- `simple`：仅输出控制台调试日志，不写文件
- `verbose`：输出控制台调试日志，并写入详细对话级日志文件

当启用 `verbose` 时，详细日志会写入：

```text
data/conversations/YYYY-MM-DD/{conversation_id}.json
```

内容会包含：

- 客户端请求（client request）
- 上游请求/响应（upstream request/response）
- 客户端响应（client response）
- 错误信息

---

## 如何在 Cursor 中使用（最简步骤）

1. 在 Cursor 的 Settings → Models 添加自定义模型
2. Override OpenAI Base URL 填为 `http://localhost:3029`
3. API Key 填入 `ACCESS_API_KEY`（未启用鉴权则随意填写）
4. 管理面板里为该 Cursor 模型配置映射、后端类型与上游模型名

---

## 许可证

[MIT](../LICENSE)

