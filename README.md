# AI Harness

AI Harness 是一个安全、跨平台、可扩展的本地编码 Agent。它通过 OpenAI 兼容接口连接模型，在受控工作区中读取、搜索、创建、修改和删除文件，并在用户审批后执行命令、运行测试和检查 Git 状态。

## 功能

- 连续对话与上下文记忆
- OpenAI 兼容模型端点，默认兼容 DeepSeek 配置
- 文件读取、目录浏览、全文搜索和精确文本编辑
- 文件与空目录的创建、修改和安全删除
- Git 状态与差异读取
- Windows、macOS、Linux 摄像头拍照并保存为 JPEG/PNG
- 原生 Shell 命令审批、超时和输出限制
- 工作区隔离与额外目录显式授权
- 工具执行进度和可选 JSONL 日志
- 全局 `harness` 启动入口

## 快速开始

macOS/Linux：

```console
cd /path/to/harness
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Windows PowerShell：

```powershell
cd C:\path\to\harness
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

编辑当前目录的 `.env`：

```env
DEEPSEEK_API_KEY="你的 API Key"
AI_HARNESS_MODEL="deepseek-chat"
```

安装完成后，三个系统都使用相同命令：

```bash
harness
```

也可以使用不依赖命令入口的通用启动方式：

```bash
python -m ai_harness
```

当前目录会成为默认工作区。`bin/harness` 只作为已有 macOS 安装的兼容启动器保留。

如果希望退出虚拟环境后仍能在任意目录使用，可安装 [pipx](https://pipx.pypa.io/) 后在仓库根目录运行：

```bash
pipx install --editable .
```

模型配置可以放在当前目录的 `.env`，也可以放在跨平台用户配置文件 `~/.ai-harness/.env`（Windows 即 `%USERPROFILE%\.ai-harness\.env`）。还可以显式指定：

```bash
harness --env-file /path/to/config.env
```

## 使用方式

进入交互模式：

```bash
harness
```

执行一次任务：

```bash
harness "检查项目并修复失败的测试"
```

指定工作区和额外授权目录：

```bash
harness --workspace /path/to/project --allow-path "$HOME/Desktop"
```

Shell 命令审批策略：

```bash
harness --approval ask    # 默认，每次询问
harness --approval auto   # 自动允许，仅用于可信工作区
harness --approval never  # 完全禁止命令执行
```

完全访问模式：

```bash
harness --full-access
```

`--full-access` 会允许访问整个文件系统、读取或修改敏感文件，并自动批准 Shell 命令。它等价于主动放弃工作区隔离和逐次审批，只应在可信任务中临时使用。退出该会话后，下次普通运行会恢复默认安全模式。

平台后端会自动选择：

| 系统 | 命令执行 | 摄像头 |
| --- | --- | --- |
| Windows | PowerShell (`pwsh`/Windows PowerShell) | FFmpeg DirectShow |
| macOS | zsh | FFmpeg AVFoundation |
| Linux | bash/sh | FFmpeg V4L2 |

摄像头功能需要先安装 FFmpeg 并确保 `ffmpeg` 位于 `PATH`。Windows 可以传摄像头名称或数字索引，Linux 可以传索引或 `/dev/video*` 路径。

交互命令：

- `/permissions`：打开权限模式菜单
- `/permissions ask` 或 `/ask`：请求批准；限制在工作区和授权目录，敏感操作逐次询问
- `/permissions auto` 或 `/auto`：替我审批；仍限制在工作区和授权目录，但自动批准敏感操作
- `/permissions full-access` 或 `/full-access`：完全访问整个文件系统、敏感文件并自动批准
- `/permissions never` 或 `/never`：限制在工作区和授权目录，并拒绝敏感操作
- `/clear`：清空对话上下文
- `/help`：显示帮助
- `/exit` 或 `Ctrl-D`：退出

权限切换只对当前会话生效，并会立即重建工具权限；退出后不会保存“完全访问权限”。

其他选项：

```bash
harness --model MODEL_NAME
harness --max-turns 20
harness --log-file .ai-harness/session.jsonl
harness --quiet-tools
```

## 模型配置

DeepSeek：

```env
DEEPSEEK_API_KEY="..."
AI_HARNESS_MODEL="deepseek-chat"
```

任意 OpenAI 兼容端点：

```env
AI_HARNESS_API_KEY="..."
AI_HARNESS_BASE_URL="https://example.com/v1"
AI_HARNESS_MODEL="model-name"
AI_HARNESS_TIMEOUT="60"
```

也可以使用 `OPENAI_API_KEY`，并通过 `AI_HARNESS_MODEL` 指定模型。不要提交 `.env`；仓库只应提交 `.env.example`。

## 安全模型

- 相对路径只能落在工作区内。
- 工作区外路径必须通过 `--allow-path` 显式授权。
- 文件修改拒绝经过符号链接路径。
- `delete_file` 不删除目录，`delete_directory` 只删除空目录。
- Shell 命令默认要求确认，并受工作目录、超时和输出长度限制。
- 摄像头访问与命令执行共用审批策略；完全访问模式会自动允许。
- Shell 子进程不会继承常见模型 API Key 环境变量。
- `.env`、私钥和常见证书文件在工具层禁止读取、搜索、覆盖和删除。
- 只有显式使用 `--full-access` 时，以上文件与命令保护才会在该次会话中解除。

## 开发

```bash
python -m pytest
```

架构说明见 [docs/architecture.md](docs/architecture.md)，后续路线见 [docs/roadmap.md](docs/roadmap.md)。

## 当前边界

AI Harness 0.3.0 是一个支持 Windows、macOS 和 Linux 的本地编码 Agent MVP。真正的二进制办公文件生成、模型智能路由、上下文压缩、沙箱容器和分布式执行属于后续版本范围。
