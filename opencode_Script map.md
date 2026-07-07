# OpenCode Script Map

## 🚀 Start Command

To run the Stream Guard terminal wrapper:
```bash
python3 opencode_scripts/stream_guard.py
```

> **Note**: Run this command from your workspace root directory.

---

## File Overview

1. [stream_guard.py](stream_guard.py): The self-contained wrapper script that forks `opencode`, intercepts raw PTY streams, filters out mouse reports and capability responses, alerts on errors, and coordinates self-healing locks (`.guard_lock`).

---

## Key Design Decisions & Behaviors

| Feature | Detail |
|---|---|
| **Simple Reconnect** | Detects timeout/idle errors, waits 5 seconds, and injects `continue\r\n` (aborting if the agent starts coding during the delay). |
| **Self-Healing Lock** | Sets a local `.guard_lock` file on tracebacks or watchdog stalls (inactivity > 15m, or > 10m in tests), and pauses the process. An external coordinator/daemon can resolve the file fixes and delete the lock to resume. |
| **Tool Hang Recovery** | Tracks tool call execution states (reading/writing). If silent/inactive for 5 minutes, automatically interrupts the process (ESC double-tap) and injects `continue executing the todo list\r\n` directly. |
| **Thought Logger** | Extracts step-by-step agent thought blocks from stdout and logs them directly to `stream_guard.log`. |
| **Capability Filter** | Removes CPR (`ESC[row;colR`) and DECRPM (`ESC[mode;val$y`) status reports from stdout to prevent garbage rendering on terminal screens. |

---

## Code Skeleton

### `stream_guard.py`
```python
import os
import sys
import pty
import select
import termios
import tty
import fcntl
import re
import time
import threading

# Reconnect triggers
RECONNECT_TRIGGERS = [...]

# Helpers
def _log(msg: str) -> None: ...
def filter_tui_query_responses(data: bytes) -> bytes: ...
def filter_alt_screen(data: bytes) -> bytes: ...
def filter_mouse_tracking(data: bytes) -> bytes: ...
def filter_scrollback_clear(data: bytes) -> bytes: ...
def clean_ansi(text: str) -> str: ...
def check_for_traceback(buffer_text: str) -> str: ...
def write_failure_trigger(filename: str, reason: str, fd: int) -> None: ...
def wait_for_antigravity_repair(fd: int) -> None: ...
def do_simple_reconnect(fd: int, trigger: str) -> None: ...
def interrupt_and_resume_tool(fd: int, reason: str) -> None: ...
def log_thoughts_from_stream(text: str) -> None: ...

# Main loop
def main() -> None: ...
```
