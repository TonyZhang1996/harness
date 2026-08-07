# Architecture

AI Harness 采用单进程、本地优先的 Agent 架构。

```text
CLI / interactive REPL
        |
        v
AgentSession --- conversation history
        |
        +--- OpenAI-compatible model client
        |
        +--- tool schema and dispatch
                 |
                 +--- workspace and allowed-root resolver
                 +--- file/search/edit tools
                 +--- Git inspection tools
                 +--- approved shell execution
```

## Components

- `config.py`：从环境变量构建模型配置。
- `model.py`：创建 OpenAI 兼容客户端。
- `agent.py`：维护对话、调用模型、分发工具并报告事件。
- `tools.py`：实现路径隔离、文件操作、搜索、Git 检查和命令执行。
- 摄像头工具通过 macOS AVFoundation 和本机 FFmpeg 采集单帧，并校验输出文件。
- `approval.py`：处理 Shell 命令审批策略。
- `cli.py`：提供单次任务和连续交互界面。
- `bin/harness`：从任意目录启动项目虚拟环境，并让配置层安全解析本地 `.env`。

## Trust boundaries

模型输出不被视为可信输入。所有路径均在工具层解析和验证；Shell 命令在执行前通过审批策略；API Key 不传递给命令子进程。工具错误会作为工具结果返回模型，不会伪装成成功。

`--full-access` 是显式的会话级权限提升：文件系统根目录加入授权范围，敏感文件保护解除，命令审批切换为自动允许。交互模式也可以用 `/permissions` 在 `ask`、`auto`、`never` 和 `full-access` 之间即时切换。每次切换都会重新绑定全部工具的路径边界、敏感文件策略和审批回调；该模式不会持久化到后续会话。
