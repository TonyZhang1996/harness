# Changelog

## 0.6.1 — 2026-08-18

- 加快网页搜索：默认超时从 45 秒降至 15 秒，取消固定的一秒等待；相同查询短时间内复用结果，并限制单个用户回合最多执行 3 次搜索。
- 涉及 AI Harness 自身配置和供应商端点的问题优先查本地源码与文档，避免把已知配置送去外网重复检索。
- OpenCode Go URL 查询改为内置配置确定性返回，不再因为当前工作区不包含 Harness 源码而误走浏览器搜索。
- 修复 macOS GUI 的 Canvas 气泡布局异常，避免正文卡片只显示第一行并触发“界面发生错误”。

## 0.6.0 — 2026-08-18

### Release

- Cut a Windows single-file GUI release from the current workspace as `AI-Harness-0.6.0.exe`.
- Keep API keys and local sessions outside the executable; connection settings and GUI state remain per-user under `%USERPROFILE%\\.ai-harness\\`.
- Updated the package, GUI title, CLI version, and smoke-test version marker to 0.6.0.
- Bundle the matching Playwright Chromium browsers into the Windows executable so public search works on a clean recipient machine.
- Defer live Session client rebuilds after connection changes so running tasks remain stoppable and steerable.
- Make the Windows source launcher repository-relative and remove obsolete Tavily configuration from the delivery instructions.

## 0.5.0 — 2026-08-17

### Web search

- Added the shared `browser_search` tool, backed by Playwright headless Chromium, to every Session.
- Current/external-information questions are instructed to use the fixed Baidu/Bing search tool instead of creating temporary browser scripts with `run_command`.
- Added approval handling, public-search-only URL boundaries, browser-result truncation, and Playwright setup guidance.

### Model providers

- Added an OpenCode Go connection preset with automatic endpoint detection and support for its Chat Completions models.
- Added clickable model selectors that load model IDs from the configured provider's `/models` endpoint.

### Validation

- `66 passed, 1 skipped` in the test suite.

## 0.4.2 — 2026-08-11

### GUI

- Added cross-platform mouse-wheel scrolling for the project tree and chat area.
- Added right-click actions to delete Sessions and remove projects without deleting disk files.
- Enabled true parallel execution for multiple Sessions with independent workers and contexts.
- Fixed response text being clipped in non-fullscreen windows by refreshing card wrapping as the window resizes.
- Bundled the rabbit application icon and fixed macOS Tk runtime/resource startup issues.

### Execution and reliability

- Added live tool output updates and elapsed-time heartbeats for silent commands such as `curl -s`.
- Repaired incomplete tool-call transcripts after an interrupted App run or network failure.
- Added one retry for transient model transport and response-decompression errors.
- Added coverage for tool progress, Session recovery, and model retry behavior.

### Validation

- `58 passed` in the non-native-GUI test suite.

## 0.4.1 — 2026-08-10

- Fixed macOS GUI presentation and packaged-resource lookup issues.
- Restored the last workspace and Session on GUI startup.
