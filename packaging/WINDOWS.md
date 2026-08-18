# Windows EXE 交付说明

构建命令（在仓库根目录执行）：

```powershell
D:\miniconda3\python.exe -m PyInstaller --clean --noconfirm packaging\ai_harness_windows.spec
```

生成文件：`dist\AI-Harness-0.6.0.exe`。它是单文件 GUI 程序，可以直接复制到另一台 Windows 电脑运行。

API Key 不打包进 EXE。接收方首次使用时，可以在程序的“模型连接”窗口填写模型 API；Tavily 搜索的 Key 放在：

```text
%USERPROFILE%\.ai-harness\.env
```

示例：

```dotenv
AI_HARNESS_API_KEY=接收方自己的模型APIKey
AI_HARNESS_BASE_URL=https://api.deepseek.com
AI_HARNESS_MODEL=deepseek-v4-flash
# 图片附件如需使用另一套视觉服务，可按需增加以下配置：
# AI_HARNESS_VISION_BASE_URL=https://vision-provider.example.com/v1
# AI_HARNESS_VISION_API_KEY=接收方自己的视觉APIKey
# AI_HARNESS_VISION_MODEL=vision-model-name
TAVILY_API_KEY=接收方自己的TavilyAPIKey
TAVILY_TIMEOUT=30
```

不要把个人 API Key 写入仓库、EXE 或发送给他人。
