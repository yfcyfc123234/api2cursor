# Cursor System Prompt 结构分析

> 分析日期：2026-06-05  
> 数据来源：api2cursor 代理捕获的 Cursor → DeepSeek 请求  
> 样本大小：~21,000 字符（约 5,000-7,000 token）

---

## 一、概述

Cursor 在每次对话的第一轮请求中，向 AI 模型发送一份完整的 System Prompt（role=system），约 **21,000 字符**。这份 prompt 定义了 AI 在 Cursor 环境中的行为规范、工具使用方式、代码引用格式等。

**发送者**：Cursor IDE（客户端）  
**发送时机**：每次新对话的第一轮  
**接收者**：上游 AI 模型（通过代理原样转发）  
**Token 占比**：约占总请求 token 的 10-15%

---

## 二、完整结构

```
Cursor System Prompt
│
├── 1. AI 身份定义                         ~200 字符
├── 2. 环境信息说明                         ~4,500 字符
├── 3. 工具调用规范 <tool_calling>          ~400 字符
├── 4. 代码修改规范 <making_code_changes>   ~350 字符
├── 5. 代码引用规范 <citing_code>           ~8,000 字符
│   ├── METHOD 1: 代码引用（引用已有代码）
│   ├── METHOD 2: Markdown 代码块（展示新代码）
│   └── 关键格式规则
├── 6. 任务管理 <task_management>            ~200 字符
├── 7. 提问指南 <ask_question_guidance>      ~300 字符
├── 8. MCP 工具访问                          ~800 字符
├── 9. MCP 资源访问                          ~600 字符
├── 10. 等待策略                             ~400 字符
├── 11. 工具定义（19个工具）                 ~5,000 字符
│   ├── Shell, Glob, Grep, Read, Write, Edit
│   ├── Task, Agent, AskUserQuestion
│   ├── WebFetch, WebSearch, NotebookEdit
│   └── 其他专用工具
└── 12. 安全与行为约束                       ~500 字符
```

---

## 三、各段详细分析

### 1. AI 身份定义（~200 字符）

```text
You are an AI coding assistant, powered by deepseek-v4-pro.
You operate in Cursor.
You are a coding agent in the Cursor IDE that helps the USER with 
software engineering tasks.
```

**作用**：确立 AI 的角色和运行环境。  
**影响**：模型名 `powered by deepseek-v4-pro` 是我们的代理改写过的（原请求模型为 `glm-deepseek-v4-pro`）。代理在标准化请求时替换了模型名。

### 2. 环境信息说明（~4,500 字符）

```text
Each time the USER sends a message, we may automatically attach 
information about their current state, such as:
- what files they have open
- where their cursor is  
- recently viewed files
- edit history in their session so far
- linter errors
- and more.

This information is provided in case it is helpful to the task.
```

**作用**：解释为什么每条 user 消息前面会有 `<user_info>`、`<git_status>`、`<open_files>` 等标签。  
**Token 消耗**：这段说明本身 4,500 字符，加上每次注入的实际数据，是第一大 token 消耗源。

### 3. 工具调用规范 `<tool_calling>`（~400 字符）

**核心规则**：
- 只能用工具完成任务，不能用聊天文字替代工具调用
- 不能在聊天中用"伪代码"模拟工具执行结果
- 工具调用失败时必须报告错误

### 4. 代码修改规范 `<making_code_changes>`（~350 字符）

**核心规则**：
- 代码修改前必须先读取文件
- 使用精确的字符串替换（`old_string` → `new_string`）
- 不能注释掉代码，要直接删除

### 5. 代码引用规范 `<citing_code>`（~8,000 字符）

这是 System Prompt 中最大的一块，教 AI 如何用特定格式引用代码：

**METHOD 1：代码引用**
```text
```startLine:endLine:filepath
// code content here
`` `
```
必须包含：起始行号、结束行号、文件路径。禁止加语言标签。

**METHOD 2：Markdown 代码块**
```text
```language:filepath
// new code
`` `
```
用于展示不在代码库中的新代码。

**关键格式规则**：
- 不能使用空代码块
- 不能用 `...` 省略代码
- 代码块必须有内容

**Token 消耗**：这是第二大 token 消耗源（~8,000 字符），包含大量示例和反例。

### 6. 任务管理 `<task_management>`（~200 字符）

```text
IMPORTANT: Make sure you don't end your turn before you've completed 
all todos.
```

**作用**：强制 AI 使用 TODO 跟踪进度，不能半途而废。  
**影响**：这导致 AI 倾向于多轮工具调用，增加了请求次数和 token 消耗。

### 7. 提问指南 `<ask_question_guidance>`（~300 字符）

**作用**：教 AI 何时使用 `AskUserQuestion` 工具进行多选提问。  
**规则**：当有 2-4 个互斥选项时使用，不能用于"我的计划可以吗？"这类确认性问题。

### 8. MCP 工具访问（~800 字符）

**作用**：列出可用的 MCP (Model Context Protocol) 服务器和工具。  
**内容**：`plugin-datadog-datadog` 等 MCP 插件提供的工具。

### 9. MCP 资源访问（~600 字符）

**作用**：列出 MCP 资源（如文件系统服务器、项目级配置）。  
**内容**：`<mcp_file_system_servers>` 标签内的文件服务器配置。

### 10. 等待策略（~400 字符）

```text
IMPORTANT - Waiting strategy:
- For multi-step tasks, it's OK to wait for one tool result before 
  calling the next
- The environment will inform you when background tasks complete
```

**作用**：教 AI 如何处理异步工具调用（如子代理、后台 Shell）。

### 11. 工具定义（~5,000 字符，19 个工具）

| 工具 | 类型 | 说明 |
|------|------|------|
| Shell | 终端执行 | 运行命令，有超时限制 |
| Glob | 文件搜索 | 按通配符查找文件 |
| Grep | 内容搜索 | 基于 ripgrep 的代码搜索 |
| Read | 文件读取 | 读取文件内容 |
| Write | 文件写入 | 覆盖写入文件 |
| Edit | 精确替换 | 字符串替换编辑 |
| Task | 子代理 | 启动子代理执行复杂任务 |
| Agent | 多步代理 | 启动多步代理 |
| AskUserQuestion | 交互 | 多选提问 |
| WebFetch | 网络 | 抓取网页内容 |
| WebSearch | 搜索 | 网络搜索 |
| NotebookEdit | Jupyter | 编辑 Notebook |
| Bash | Shell 别名 | 执行 bash 命令 |
| Glob | 文件搜索 | 通配符搜索 |
| Grep | 内容搜索 | 正则搜索 |

### 12. 安全与行为约束（~500 字符）

- 不能生成或传播恶意代码
- 不能帮助绕过安全限制
- 代码风格匹配项目规范
- 优先使用项目中已有的工具和库

---

## 四、谁发送、为什么、能改吗

| 问题 | 答案 |
|------|------|
| **谁发送** | Cursor IDE 客户端，每次新对话自动注入 |
| **为什么这么大** | 包含 19 个工具的完整定义 + 2 种代码引用格式的详细教程 + MCP 配置 |
| **为什么每次都要发** | 因为 AI 是无状态的，每次请求都需要完整上下文。DeepSeek 支持 prompt caching（缓存命中率 98%），所以实际增量消耗不大 |
| **代理能改吗** | 可以通过模型映射的「自定义指令」**追加**内容到 system prompt 前面或后面。但**不能删除** Cursor 原有的内容（它在 client_request 里，代理只是转发） |
| **代理应该改吗** | 一般不需要。但如果发现特定工具说明有误（如 AwaitShell 参数说明不清晰），可以在代理层面通过 error_patcher 修复后果 |

---

## 五、Token 消耗分析

| 段落 | 字符数 | 估算 Token | 占比 |
|------|--------|-----------|------|
| 代码引用规范 | 8,000 | 2,000 | 30% |
| 工具定义 | 5,000 | 1,250 | 19% |
| 环境信息说明 | 4,500 | 1,125 | 17% |
| MCP 相关 | 1,400 | 350 | 5% |
| 其他规则 | 2,000 | 500 | 8% |
| **System Prompt 合计** | **21,000** | **~5,200** | — |
| 第一条 user 消息 | 60,000+ | 15,000+ | — |
| **首次请求总 Token** | — | **~20,000-25,000** | 100% |

**DeepSeek 缓存命中率**：~98%（43,776 / 44,349 token 被缓存）  
**实际计费**：每次约 600 token（增量部分）

---

## 六、代理可做的优化

| 优化方向 | 可行性 | 风险 |
|---------|--------|------|
| 剥离环境信息（user_info 等） | 低 | Cursor 强依赖，去掉后 AI 不知道文件在哪 |
| 精简工具定义 | 中 | 可能导致 AI 不会正确使用工具 |
| 注入补充指令 | 高（已支持） | 与 Cursor 指令冲突时可能混乱 |
| 缓存命中监控 | 高（已支持） | 无风险 |
| 错误自动修复 | 高（已支持） | 补丁重试增加延迟 |
