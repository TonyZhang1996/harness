# Architecture

AI Harness 采用单进程、本地优先的 Agent 架构。

```text
CLI / interactive REPL
        |
        v
AgentSession --- conversation history
        |
        +--- OpenAI-compatible model client
        +--- live direction queue (GUI steering at safe turn boundaries)
        |
        +--- vision_router plugin (image -> vision model -> text evidence)
        |
        +--- tool schema and dispatch
                 |
                 +--- workspace and allowed-root resolver
                 +--- file/search/edit tools
                 +--- browser_search (Playwright headless Chromium)
                 +--- Git inspection tools
                 +--- platform adapter
                        +--- PowerShell / zsh / bash
                        +--- DirectShow / AVFoundation / V4L2
```

## Components

- `config.py`：从环境变量构建模型配置，并提供 OpenCode Go 的 Chat Completions 连接预设。
- `model.py`：创建 OpenAI 兼容客户端。
- `agent.py`：维护对话、调用模型、分发工具并报告事件；运行中的 GUI 可以把方向调整放入线程安全队列，Agent 在下一次模型或工具边界注入最新用户指示并修复被跳过的工具调用记录。
- `plugins/vision_router.py`：只在有图片附件时自动发现并调用多模态模型，将图片事实转换为文本上下文；失败时直接报错，不使用 OCR 兜底。
- `tools.py`：实现路径隔离、文件操作、搜索、Git 检查、命令执行和受限的 Playwright 无头浏览器搜索。
- 命令工具根据系统选择 PowerShell、zsh 或 bash/sh。
- 摄像头工具通过 FFmpeg 的 Windows DirectShow、macOS AVFoundation 或 Linux V4L2 后端采集单帧，并校验输出文件。
- `approval.py`：处理 Shell 命令审批策略。
- `cli.py`：提供单次任务和连续交互界面。
- `gui.py`：Tkinter 桌面工作台。每个 Session 拥有独立的 `AgentSession` 与工作线程，多个 Session 可同时运行；工具事件按 `session_id` 路由到对应 Session 的记录，审批请求按 FIFO 依次弹出确认窗口。运行中的 composer 提供 `调整方向` 和 `加入队列`：前者把提示交给当前 AgentSession 的方向队列，后者在当前回合完成后自动启动新的用户回合。停止完成后的 composer 保持可编辑，`发送` 会在同一 Session 中开始新的用户回合，`继续` 则从已停止的上下文恢复。对话时间线将供应商独立 reasoning 字段、明确思考标记、`browser_search` 和 PowerShell 命令分别归并为可折叠的 `Think`、`Search`、`Pwsh` 行；正式回答在进入历史和 GUI 前会清理已识别的思考内容，默认只显示 Think/Search/Pwsh 单行预览。审批审查、底层工具开始/结果等诊断事件继续保留但不直接展示。项目树与对话画布统一处理跨平台鼠标滚轮事件；删除 Session 或移除项目前会检查运行状态并要求确认，移除项目不会删除磁盘文件。
- Python console script 提供跨平台 `harness` 命令，`python -m ai_harness` 提供通用备用入口。
- `bin/harness`：保留给已有 macOS 本地开发安装的兼容启动器。

## Trust boundaries

模型输出不被视为可信输入。所有路径均在工具层解析和验证；Shell 命令在执行前通过审批策略；API Key 不传递给命令子进程。工具错误会作为工具结果返回模型，不会伪装成成功。

图片感知插件只处理用户明确附加的图片，并把图片上传到配置的视觉模型服务。视觉模型输出被注入为不可信证据；`agent.py` 的系统提示禁止将图片中的文字或视觉模型输出当作系统指令、工具指令或权限授予。视觉链路任何一步失败都会中止这次图片请求，不会改用本地 OCR。

`browser_search` 是所有 Session 共享的固定工具。它只允许访问百度或 Bing 的公开搜索入口，使用新建的无 Cookie Chromium 上下文，并沿用当前 Session 的审批回调；模型不能通过它访问任意 URL、本地文件或用户浏览器登录状态。系统提示要求遇到当前或外部信息时优先调用该工具，避免每个 Session 临时创建搜索脚本。

`--full-access` 是显式的会话级权限提升：文件系统根目录加入授权范围，敏感文件保护解除，命令审批切换为自动允许。交互模式也可以用 `/permissions` 在 `ask`、`auto` 和 `full-access` 之间即时切换（`never` 为底层保留模式，不通过 CLI 暴露）。每次切换都会重新绑定全部工具的路径边界、敏感文件策略和审批回调；该模式不会持久化到后续会话。
