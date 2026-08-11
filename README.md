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
AI_HARNESS_MODEL="deepseek-v4-flash"
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
harness --approval auto   # 帮我批准：独立审查，必要时询问
```

完全访问模式：

```bash
harness --full-access
```

`--full-access` 会允许访问整个文件系统、读取或修改敏感文件，并自动批准 Shell 命令。它等价于主动放弃工作区隔离和逐次审批，只应在可信任务中临时使用。退出该会话后，GUI 会恢复默认的“帮我批准”模式，CLI 会恢复“请求批准”模式。

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
- `/permissions auto` 或 `/auto`：帮我批准；仍限制在工作区和授权目录，由独立审查器自动允许低风险操作、拒绝明确危险操作，并把不确定操作交给你确认
- `/permissions full-access` 或 `/full-access`：完全访问整个文件系统、敏感文件并自动批准
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
AI_HARNESS_MODEL="deepseek-v4-flash"
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
- “帮我批准”通过独立、窄职责的模型调用审查命令；审查失败时回退到人工确认，绝不静默放行。
- 本地硬规则会阻止凭据探测、敏感数据外发、关闭安全防护和大范围不可逆删除；“帮我批准”模式下普通网络访问按低风险自动允许，摄像头、安装、删除及系统状态修改仍需人工确认。
- 摄像头访问与命令执行共用审批策略；完全访问模式会自动允许。
- Windows 下 PowerShell、Git 和 FFmpeg 等工具子进程使用无控制台窗口方式启动，不会反复弹出黑色终端窗口。
- Shell 子进程不会继承常见模型 API Key 环境变量。
- `.env`、私钥和常见证书文件在工具层禁止读取、搜索、覆盖和删除。
- 只有显式使用 `--full-access` 时，以上文件与命令保护才会在该次会话中解除。

## 开发

```bash
python -m pytest
```

架构说明见 [docs/architecture.md](docs/architecture.md)，后续路线见 [docs/roadmap.md](docs/roadmap.md)。

## 0.4.0 桌面 GUI

0.4.0 增加了一个基于 Tkinter 的本地桌面工作台，沿用 Codex 风格的深色三栏布局：左侧是工作区、会话和权限设置，中间是对话与工具活动，底部是任务输入框。GUI 与 CLI 共用同一个 `AgentSession`，不会产生两套工具行为。

启动方式：

```bash
harness --gui
# 或
harness-gui
# 或
python -m ai_harness --gui
```

GUI 以“项目 → Sessions”的树形结构管理任务，支持 Session 新建、切换、删除和本地会话持久化；项目可通过拖曳调整顺序，也可从侧边栏移除（不会删除磁盘文件）。项目树和对话区域支持鼠标滚轮滚动。多个 Session 可以同时运行：每个 Session 拥有独立的工作线程与对话上下文，切换 Session、新建 Session 不会中断其它 Session 正在执行的任务；侧边栏会显示“N 个任务运行中”。首次问答完成后，模型会生成不超过 11 个字的 Session 标题。输入框按 Enter 发送、Shift+Enter 换行；可以通过“＋附件”选择文件，也可以用 Ctrl+V 直接粘贴剪贴板图片或资源管理器中的文件。

GUI 每次启动默认使用“帮我批准”，权限下拉框采用深色显示，并提供“请求批准”“帮我批准”和“完全访问”三个选项。

图片附件会先由本机 `rapidocr_onnxruntime` 做 OCR 识别，再将识别出的文字发送给文本模型；图片本身不会上传到 OCR 服务。若本机未安装 OCR 依赖，界面会在会话中显示识别失败原因。

“请求批准”模式会在 GUI 内弹出中文的“允许/拒绝”确认窗口，不再等待终端输入。任务运行时发送按钮会变成方形停止按钮；停止完成后显示三角形继续按钮，可从已有上下文继续运行。

“模型连接”窗口提供 API Key、API URL 和模型输入框。DeepSeek URL 默认是 `https://api.deepseek.com`，默认模型是 `deepseek-v4-flash`；配置保存在用户目录的 `~/.ai-harness/.env`。

## 0.4.1 修复

- 修复 macOS 上建议卡片文字与背景颜色接近、导致内容显示为白块的问题。
- GUI 启动时恢复上次使用的工作区和当前 Session。
- 修复 macOS 应用打包后图标等资源的查找路径。

## 0.4.2 更新

- 支持项目树和对话区域跨平台鼠标滚轮滚动。
- 支持右键删除 Session、移除项目（不会删除磁盘文件）。
- 支持多个 Session 真正并行运行，每个 Session 拥有独立工作线程和上下文。
- 修复非全屏窗口下回答文字被遮挡的问题，卡片宽度会随窗口动态换行。
- 工具执行期间实时显示输出；静默命令显示运行时间心跳，避免误以为卡住。
- App 使用统一的兔子图标，并修复 macOS 打包后的 Tk 运行时资源和启动问题。
- App 退出或网络中断导致工具调用历史不完整时，自动修复会话并允许继续运行。
- 模型遇到临时网络压缩错误时自动重试一次。
- 增加工具进度、会话恢复和模型重试相关测试；当前测试结果为 58 项通过。

## 当前边界

AI Harness 0.4.2 是一个支持 Windows、macOS 和 Linux 的本地编码 Agent MVP，并提供 Tkinter 桌面 GUI。真正的二进制办公文件生成、模型智能路由、上下文压缩、沙箱容器和分布式执行属于后续版本范围。
