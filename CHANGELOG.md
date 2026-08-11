# Changelog

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
