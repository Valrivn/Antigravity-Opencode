#!/usr/bin/env python3
"""
stream_guard.py - Lightweight wrapper for opencode.
- Intercepts raw PTY streams, filters out mouse reports and CPR/DECRPM capability response sequences.
- Alerts on execution errors and sets a local .guard_lock file on traceback or stall.
- Resumes execution by injecting 'continue executing the todo list' when lock is resolved.
- Automatically reconnects on simple idle timeout/disconnect streams.
"""

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

def find_workspace_root() -> str:
    """Attempts to find the workspace root containing .git, or defaults to CWD."""
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.getcwd())

WORKSPACE_ROOT = find_workspace_root()
LOCK_FILE = os.environ.get("GUARD_LOCK_FILE") or os.path.join(WORKSPACE_ROOT, ".guard_lock")
LOG_FILE = os.environ.get("STREAM_GUARD_LOG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream_guard.log")
STALL_TIMEOUT = float(os.environ.get("STALL_TIMEOUT", "900.0"))
PYTEST_TIMEOUT = float(os.environ.get("PYTEST_TIMEOUT", "600.0"))

def get_fallback_file() -> str:
    """Finds a reasonable default Python file to target when traceback extraction fails or watchdog stalls."""
    env_fallback = os.environ.get("GUARD_FALLBACK_FILE")
    if env_fallback:
        return env_fallback
        
    # Check for backward-compatible project default if it exists
    compat_path = os.path.join(WORKSPACE_ROOT, "psychological/scrapers/corp_audit.py")
    if os.path.exists(compat_path):
        return "psychological/scrapers/corp_audit.py"
        
    # Walk the workspace to find any Python file (excluding hidden, venv, and node_modules)
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('venv', '.venv', 'site-packages', 'node_modules')]
        for f in files:
            if f.endswith('.py') and f != "stream_guard.py":
                full_path = os.path.join(root, f)
                return os.path.relpath(full_path, WORKSPACE_ROOT)
                
    return "main.py"

is_reconnecting = False
reconnect_lock = threading.Lock()
log_buffer = ""
buffer_lock = threading.Lock()

is_in_tool_call = False
is_in_alt_screen = False

RECONNECT_TRIGGERS = [
    "idle stream disconnect",
    "upstream idle timeout exceeded",
    "upstream idle timeout",
    "stream disconnected",
    "unexpected end of stream",
    "provider returned error",
]

# Signals indicating the agent is actively working
ACTIVE_AGENT_SIGNALS = [
    "```",
    "applying",
    "+ thought:",
    "preparing write",
    "preparing edit",
    "preparing read",
    "~ preparing",
    "esc to interrupt",
    "esc interrupt",
    "running tool",
    "tool call",
    "writing file",
    "reading file",
    "executing",
    "searching",
    "fetching",
    "installing",
    "compiling",
    "thinking",
    "generating",
]

def is_agent_active(text: str) -> bool:
    """Check if agent is actively outputting code, thoughts, or running tools."""
    lower_text = text.lower()
    return any(sig in lower_text for sig in ACTIVE_AGENT_SIGNALS)

inside_code_block = False
thought_accumulator = ""
in_thought_gathering = False

def log_output_and_thoughts(text: str):
    global inside_code_block, thought_accumulator, in_thought_gathering, is_in_alt_screen
    
    # Track alternate screen buffer transitions statefully
    if "\x1b[?1049h" in text or "\x1b[?1047h" in text or "\x1b[?47h" in text:
        is_in_alt_screen = True
    if "\x1b[?1049l" in text or "\x1b[?1047l" in text or "\x1b[?47l" in text:
        is_in_alt_screen = False

    cleaned = clean_ansi(text)
    lines = cleaned.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        
        # Check if we enter or exit a code block
        if stripped.startswith("```"):
            inside_code_block = not inside_code_block
            # Flush any pending thought block when encountering a code fence
            if in_thought_gathering and thought_accumulator.strip():
                _log(f"🧠 THOUGHT: {thought_accumulator.strip()}")
                thought_accumulator = ""
                in_thought_gathering = False
            continue
            
        if inside_code_block:
            # Ignore code blocks to prevent pollution of the logs
            continue
            
        # Check if a new thought header starts (e.g. "Thought: 294ms")
        if "thought:" in stripped.lower():
            if in_thought_gathering and thought_accumulator.strip():
                # Flush existing thought
                _log(f"🧠 THOUGHT: {thought_accumulator.strip()}")
            thought_accumulator = line
            in_thought_gathering = True
            continue
            
        if in_thought_gathering:
            # Check for ending indicators (tool execution, build status, esc interrupt, code block)
            if stripped.startswith("->") or stripped.startswith("Build") or "esc interrupt" in stripped.lower() or "applying" in stripped.lower():
                if thought_accumulator.strip():
                    _log(f"🧠 THOUGHT: {thought_accumulator.strip()}")
                thought_accumulator = ""
                in_thought_gathering = False
                # Log this boundary line as normal output if not in alt screen
                if not is_in_alt_screen:
                    _log(f"📄 OUTPUT: {stripped}")
            else:
                thought_accumulator += "\n" + line
        else:
            # Skip TUI noise
            if stripped == "esc interrupt" or stripped.startswith("ctrl+p"):
                continue
            
            # Suppress normal layout rendering outputs inside alternate TUI screen
            if is_in_alt_screen:
                continue
                
            # Filter layout borders, blocks, and menu lists
            TUI_BLOCK_CHARS = "┃█▀▄╹▲▼◆◀▶│─┌┐└┘├┤┬┴┼"
            if any(c in TUI_BLOCK_CHARS for c in stripped):
                continue
            if len(stripped) <= 2:
                continue
            lower_line = stripped.lower()
            TUI_KEYWORDS = ["ctrl+p", "tab agents", "opentui-notifications", "capabilities", "ask anything...", "@explore", "@general"]
            if any(kw in lower_line for kw in TUI_KEYWORDS):
                continue
            # Filter multiple files list pattern
            if re.search(r'\w+\.(?:py|md|yaml|sh|log)\s+\w+\.(?:py|md|yaml|sh|log)', stripped):
                continue
                
            _log(f"📄 OUTPUT: {stripped}")

def _log(msg: str) -> None:
    """Logs internally to stream_guard.log; never writes to stdout/stderr."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Terminal Escape Sequence Filters
# ---------------------------------------------------------------------------
_TUI_QUERY_RESPONSE_RE = re.compile(
    rb'(?:'
        rb'\x1b\[\??[\d;]+\$y'                        # DECRPM responses
        rb'|\x1b\[\d+;\d+R'                          # CPR responses
        rb'|\x1b\]\d+;[^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC responses
    rb')',
    re.DOTALL
)

def filter_tui_query_responses(data: bytes) -> bytes:
    """Removes raw query responses to prevent garbage characters rendering on display."""
    return _TUI_QUERY_RESPONSE_RE.sub(b'', data)

_MOUSE_TRACKING_RE = re.compile(rb'\x1b\[\??(?:1000|1001|1002|1003|1004|1005|1006|1015|1016)[hl]')
_SCROLLBACK_CLEAR_RE = re.compile(rb'\x1b\[\??3J')

def filter_alt_screen(data: bytes) -> bytes:
    """Removes alternate screen buffer switches to keep output in main scrollback."""
    alt_screen_re = re.compile(rb'\x1b\[\??(?:1049|1047|47)[hl]')
    return alt_screen_re.sub(b'', data)

def filter_mouse_tracking(data: bytes) -> bytes:
    """Removes mouse tracking enablement/disablement sequences from stdout."""
    return _MOUSE_TRACKING_RE.sub(b'', data)

def filter_scrollback_clear(data: bytes) -> bytes:
    """Removes scrollback buffer clearing sequences (\x1b[3J) to preserve terminal history."""
    return _SCROLLBACK_CLEAR_RE.sub(b'', data)

def clean_ansi(text: str) -> str:
    """Strip standard ANSI escape sequences for text classification."""
    # Matches CSI sequences (e.g. \x1b[?25l, \x1b[24;80R, \x1b[?1016$p)
    csi_re = re.compile(r'\x1b\[[?0-9;><=\+\-\*\$]*[a-zA-Z]')
    # Matches OSC sequences (e.g. \x1b]99;...\x07 or \x1b]99;...\x1b\\)
    osc_re = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|\r?\n)')
    # Matches simple 2-character escape sequences like \x1b(B or \x1b= or \x1b>
    esc_2char_re = re.compile(r'\x1b[()][a-zA-Z0-9]|\x1b[=>]')
    
    cleaned = csi_re.sub('', text)
    cleaned = osc_re.sub('', cleaned)
    cleaned = esc_2char_re.sub('', cleaned)
    return cleaned

# ---------------------------------------------------------------------------
# Error check & Watchdog Target Extraction
# ---------------------------------------------------------------------------
def check_for_traceback(buffer_text: str) -> str:
    """
    Scans buffer_text for python tracebacks.
    Returns the target file path if a traceback is found.
    If a traceback is found but no local file can be extracted, returns a fallback identifier.
    Returns None only if NO traceback is present.
    """
    lines = buffer_text.splitlines()
    inside_code_block = False
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        if stripped.startswith("```"):
            inside_code_block = not inside_code_block
            continue

        if inside_code_block:
            continue

        # Look for the start of a traceback
        # Traceback Echo Shielding: only trigger if the traceback message starts at absolute column 0
        if line.startswith("Traceback (most recent call last):"):
            
            # We found a verified traceback. Let's try to find a local file.
            target_file = None
            workspace_root = WORKSPACE_ROOT
            
            for j in range(i + 1, min(i + 20, len(lines))):
                next_line = lines[j]
                if next_line.startswith(">") or "```" in next_line:
                    continue
                    
                match = re.search(r'File "([^"]+\.py)"', next_line)
                if match:
                    path = match.group(1)
                    if any(x in path for x in ["site-packages", "lib/python", "venv", ".venv", "stream_guard.py"]):
                        continue
                        
                    if not path.startswith(workspace_root):
                        full_local_path = os.path.join(workspace_root, path)
                        if not os.path.exists(full_local_path):
                            continue
                        path = full_local_path
                    
                    target_file = path
                    break  # Stop scanning once we find the first valid local file
            
            # If we found a local file, return it.
            if target_file:
                return target_file
                
            # CRITICAL FIX: If we found a traceback but couldn't parse a local file,
            # we MUST STILL RETURN a target so the interrupt triggers.
            return get_fallback_file()
            
    return None

# ---------------------------------------------------------------------------
# Resiliency & Recovery Execution
# ---------------------------------------------------------------------------
def write_failure_trigger(filename: str, reason: str, fd: int):
    """Logs the structured failure block and sets the execution hold lock."""
    global is_reconnecting, log_buffer
    
    # Reset log buffer to prevent matching the same traceback on next check iteration
    with buffer_lock:
        log_buffer = ""
        
    block = (
        f"\n=== AUTONOMOUS FAILURE TRIGGER ===\n"
        f"File: {filename}\n"
        f"Error: {reason}\n"
        f"==================================\n"
    )
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {block}")
    except Exception:
        pass
        
    _log(f"Autonomous Failure Trigger written for {filename}. Reason: {reason}")

    # Step 1: Send ESC key (\x1b) to interrupt OpenCode loop safely
    try:
        os.write(fd, b"\x1b")
        _log("Sent ESC interrupt signal to stop active agent loop (Tap 1).")
    except OSError as e:
        _log(f"Failed to send ESC Tap 1: {e}")

    # Step 2: Double-tap check: Wait 500ms and check if PTY is still streaming output
    time.sleep(0.5)
    # Check if there is still data to read from the master PTY
    r, _, _ = select.select([fd], [], [], 0.1)
    if fd in r:
        _log("PTY stream still active. Sending ESC interrupt signal (Tap 2 - Double-Tap).")
        try:
            os.write(fd, b"\x1b")
        except OSError as e:
            _log(f"Failed to send ESC Tap 2: {e}")

    # Step 3: Set the local synchronization lock file
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(f"LOCKED: {os.getcwd()}")
    except Exception as e:
        _log(f"Failed to write LOCK_FILE: {e}")

    # Step 4: Spawn background thread to wait for Antigravity patch completion
    threading.Thread(target=wait_for_antigravity_repair, args=(fd,), daemon=True).start()

def interrupt_and_resume_tool(fd: int, reason: str):
    global is_reconnecting, log_buffer, is_in_tool_call
    _log(f"Starting interrupt & resume for: {reason}")
    
    # Reset states
    with buffer_lock:
        log_buffer = ""
    is_in_tool_call = False

    # Log structured event
    block = (
        f"\n=== TOOL HANG INTERRUPT ===\n"
        f"Reason: {reason}\n"
        f"===========================\n"
    )
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {block}")
    except Exception:
        pass

    # Step 1: Send ESC key double-tap to stop the hung tool/process
    try:
        os.write(fd, b"\x1b")
        _log("Sent ESC interrupt (Tap 1).")
    except OSError as e:
        _log(f"Failed to send ESC Tap 1: {e}")
        
    time.sleep(0.5)
    r, _, _ = select.select([fd], [], [], 0.1)
    if fd in r:
        _log("PTY active. Sending ESC interrupt (Tap 2 - Double-Tap).")
        try:
            os.write(fd, b"\x1b")
        except OSError as e:
            _log(f"Failed to send ESC Tap 2: {e}")

    # Step 2: Flush stdin buffer
    time.sleep(1.0)
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        _log("Flushed stdin buffer.")
    except Exception as e:
        _log(f"Warning: termios tcflush failed: {e}")
        
    # Step 3: Inject resume directive
    time.sleep(1.0)
    try:
        os.write(fd, b"continue executing the todo list\r\n")
        _log("Successfully injected: continue executing the todo list")
    except OSError as e:
        _log(f"Failed to inject resume directive: {e}")
        
    with reconnect_lock:
        is_reconnecting = False

def do_simple_reconnect(fd: int, trigger: str):
    global is_reconnecting, log_buffer
    _log(f"Simple reconnect thread started. Trigger: '{trigger}'")
    
    # Wait 5s total (sleep 1s, clear buffer, sleep 4s) to allow connection to reset and check for active agent
    time.sleep(1.0)
    with buffer_lock:
        log_buffer = ""
    time.sleep(4.0)

    # Re-verify that no new code block was started during wait
    with buffer_lock:
        buffer_text = log_buffer

    if is_agent_active(buffer_text):
        _log("Aborting simple reconnect: Agent is active or outputting code.")
        with reconnect_lock:
            is_reconnecting = False
        return

    try:
        os.write(fd, b"continue\r\n")
        _log("Injected: continue")
    except OSError as e:
        _log(f"Failed to inject continue: {e}")

    with reconnect_lock:
        is_reconnecting = False

def wait_for_antigravity_repair(fd: int):
    global is_reconnecting
    _log("Holding input redirection. Waiting for Antigravity cascade to resolve lock...")
    
    # Block loop execution while Antigravity is processing changes
    while os.path.exists(LOCK_FILE):
        time.sleep(1.0)
        
    _log("Antigravity repair verified (.guard_lock removed). Flushing terminal and resuming...")
    time.sleep(2.0) # Safe buffer delay for file-system stabilization
    
    # OS Stdin Buffer Flushing: Discard any queued keystrokes to prevent prompt corruption
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        _log("Successfully flushed stdin buffer.")
    except Exception as e:
        _log(f"Warning: termios tcflush failed: {e}")
        
    try:
        os.write(fd, b"continue executing the todo list\r\n")
        _log("Successfully injected: continue executing the todo list")
    except OSError as e:
        _log(f"Failed to inject resume directive: {e}")
        
    with reconnect_lock:
        is_reconnecting = False

def was_spinner_active(buffer_text: str) -> bool:
    """Checks if a loading spinner (helix or Braille spinner) was active in the buffer using a bulk regex check."""
    spinner_chars = re.findall(r"[\u2800-\u28ff■⬝\u25a0\u22c5]", buffer_text)
    return len(spinner_chars) > 100

def double_escape_and_continue(fd: int, reason: str):
    global is_reconnecting, log_buffer
    _log(f"Inactivity watchdog triggered: {reason}. Running double escape and continue...")
    
    # Reset log buffer
    with buffer_lock:
        log_buffer = ""

    # Step 1: Send ESC key (Tap 1)
    try:
        os.write(fd, b"\x1b")
        _log("Sent ESC interrupt (Tap 1).")
    except OSError as e:
        _log(f"Failed to send ESC Tap 1: {e}")
        
    time.sleep(0.5)
    
    # Step 2: Send ESC key (Tap 2 - Double-Tap)
    try:
        os.write(fd, b"\x1b")
        _log("Sent ESC interrupt (Tap 2 - Double-Tap).")
    except OSError as e:
        _log(f"Failed to send ESC Tap 2: {e}")

    # Step 3: Flush stdin buffer
    time.sleep(1.0)
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        _log("Flushed stdin buffer.")
    except Exception as e:
        _log(f"Warning: termios tcflush failed: {e}")
        
    # Step 4: Inject continue directive
    time.sleep(1.0)
    try:
        os.write(fd, b"continue\r\n")
        _log("Successfully injected: continue")
    except OSError as e:
        _log(f"Failed to inject continue directive: {e}")
        
    with reconnect_lock:
        is_reconnecting = False

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
def parse_arguments() -> None:
    """Manually parse configuration flags meant for stream_guard itself,
    removing them from sys.argv so they do not interfere with the spawned subprocess command.
    """
    global WORKSPACE_ROOT, LOCK_FILE, LOG_FILE, STALL_TIMEOUT, PYTEST_TIMEOUT
    
    args = sys.argv[1:]
    clean_args = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--workspace-root" and i + 1 < len(args):
            WORKSPACE_ROOT = os.path.abspath(args[i+1])
            i += 2
        elif arg == "--lock-file" and i + 1 < len(args):
            LOCK_FILE = os.path.abspath(args[i+1])
            i += 2
        elif arg == "--log-file" and i + 1 < len(args):
            LOG_FILE = os.path.abspath(args[i+1])
            i += 2
        elif arg == "--stall-timeout" and i + 1 < len(args):
            try:
                STALL_TIMEOUT = float(args[i+1])
            except ValueError:
                pass
            i += 2
        elif arg == "--pytest-timeout" and i + 1 < len(args):
            try:
                PYTEST_TIMEOUT = float(args[i+1])
            except ValueError:
                pass
            i += 2
        else:
            clean_args.append(arg)
            i += 1
            
    sys.argv = [sys.argv[0]] + clean_args

def main() -> None:
    parse_arguments()
    if "--test-escalation" in sys.argv:
        run_escalation_test()
        return

    # Clean up stale locks on startup
    lock_removed = False
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            lock_removed = True
        except Exception:
            pass

    # Clear log file on startup to keep it lightweight
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"--- StreamGuard session started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    except Exception:
        pass

    # Write initial logs to the fresh file
    if lock_removed:
        _log("Cleared stale lock file on startup.")
    elif os.path.exists(LOCK_FILE):
        _log("Failed to clear stale lock file on startup.")

    global log_buffer, is_reconnecting, is_in_tool_call

    # Support custom commands via --cmd argument
    if len(sys.argv) > 1 and sys.argv[1] == "--cmd":
        cmd = sys.argv[2:]
    else:
        cmd = ["opencode"] + sys.argv[1:]
    _log(f"StreamGuard starting process: {cmd}")

    try:
        old_tty = termios.tcgetattr(sys.stdin.fileno())
    except termios.error:
        old_tty = None

    try:
        pid, fd = pty.fork()
    except Exception as e:
        sys.exit(1)

    if pid == 0:
        try:
            os.execvp(cmd[0], cmd)
        except Exception:
            sys.exit(127)

    if old_tty:
        tty.setraw(sys.stdin.fileno())

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    last_activity_time = time.time()
    is_pytest_active = False

    try:
        while True:
            # Dynamic timeout bounds: 5 minutes if we are currently waiting in a tool call hang
            if is_in_tool_call:
                current_timeout = 300.0
            else:
                current_timeout = PYTEST_TIMEOUT if is_pytest_active else STALL_TIMEOUT
            
            try:
                r, _, _ = select.select([fd] + ([sys.stdin] if old_tty else []), [], [], 1.0)
            except (ValueError, select.error):
                break

            now = time.time()

            # Watchdog Evaluation Block
            with reconnect_lock:
                currently_reconnecting = is_reconnecting

            if not currently_reconnecting and (now - last_activity_time) > current_timeout:
                # Dynamic target selection: parse log buffer for last modified Python file
                with buffer_lock:
                    target_file = check_for_traceback(log_buffer)
                
                if target_file:
                    reason = "Immediate traceback error"
                    _log(f"🚨 Watchdog triggered: {reason}")
                    with reconnect_lock:
                        is_reconnecting = True
                    write_failure_trigger(target_file, reason, fd)
                elif is_in_tool_call:
                    reason = "Tool execution hang (>5m)"
                    _log(f"🚨 Watchdog triggered: {reason}. Interrupting and resuming...")
                    with reconnect_lock:
                        is_reconnecting = True
                    threading.Thread(target=interrupt_and_resume_tool, args=(fd, reason), daemon=True).start()
                else:
                    # Distinguish between spinner active vs silent stall
                    with buffer_lock:
                        spinner_active = was_spinner_active(log_buffer)
                    
                    if spinner_active:
                        reason = "Pytest execution hang (>10m)" if is_pytest_active else "User-space watchdog stall (>15m, spinner active)"
                        _log(f"🚨 Watchdog triggered: {reason}. Triggering double escape...")
                        with reconnect_lock:
                            is_reconnecting = True
                        threading.Thread(target=double_escape_and_continue, args=(fd, reason), daemon=True).start()
                    else:
                        if not target_file:
                            target_file = get_fallback_file()
                        reason = "Pytest execution hang (>10m)" if is_pytest_active else "User-space watchdog stall (>15m, silent)"
                        _log(f"🚨 Watchdog triggered: {reason}. Triggering Gemini self-healing cascade...")
                        with reconnect_lock:
                            is_reconnecting = True
                        write_failure_trigger(target_file, reason, fd)

            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break

                # Apply TUI Filters to keep rendering clean while preserving alt-screen and mouse interactivity
                display_data = filter_tui_query_responses(data)
                if display_data:
                    sys.stdout.buffer.write(display_data)
                    sys.stdout.buffer.flush()

                text_clean = data.decode("utf-8", errors="replace")
                
                # Extract and log thoughts/outputs to stream_guard.log
                log_output_and_thoughts(text_clean)
                
                # Check for tracebacks immediately on the incoming stream
                with buffer_lock:
                    log_buffer += text_clean
                    if len(log_buffer) > 10000:
                        log_buffer = log_buffer[-10000:]
                    current_log = log_buffer
                
                # Scan for traceback error or simple reconnect
                if not currently_reconnecting:
                    target_file = check_for_traceback(current_log)
                    if target_file:
                        _log(f"🚨 Traceback detected in stream: {target_file}")
                        with reconnect_lock:
                            is_reconnecting = True
                        write_failure_trigger(target_file, "Immediate traceback error", fd)
                    else:
                        # Check for simple reconnect triggers
                        lower_buf = current_log.lower()
                        matched_trigger = None
                        for trigger in RECONNECT_TRIGGERS:
                            if trigger in lower_buf:
                                matched_trigger = trigger
                                break
                        if matched_trigger:
                            _log(f"🚨 Simple reconnect trigger matched: '{matched_trigger}'")
                            with reconnect_lock:
                                is_reconnecting = True
                            threading.Thread(target=do_simple_reconnect, args=(fd, matched_trigger), daemon=True).start()

                # Pytest activation/deactivation logic
                text_lower = text_clean.lower()
                if "platform darwin" in text_lower or "rootdir:" in text_lower:
                    is_pytest_active = True
                    last_activity_time = now

                # Pytest cleanup: reset state if agent becomes active
                if is_pytest_active and is_agent_active(text_clean):
                    _log("Pytest completed. Resetting is_pytest_active to False via agent active signal.")
                    is_pytest_active = False

                # Tool state tracking: check if we just entered or exited a tool call
                tool_starts = [
                    "-> read", "-> write", "-> edit", "-> run", 
                    "running tool", "tool call", "preparing write", 
                    "preparing edit", "preparing read", "executing", 
                    "searching", "fetching"
                ]
                if any(sig in text_lower for sig in tool_starts):
                    is_in_tool_call = True
                    
                tool_ends = [
                    "thought:", "thought ", "```", "build", "esc interrupt", "applying"
                ]
                if any(sig in text_lower for sig in tool_ends):
                    is_in_tool_call = False

                # Filter out spinner/helix/Braille characters to prevent them from resetting the watchdog timer
                cleaned_activity = re.sub(r"[\u2800-\u28ff■⬝\u25a0\u22c5\u25ae\u2588\u2584\u2580\u2022\u2219]", "", text_clean)
                if len(cleaned_activity.strip()) > 0:
                    last_activity_time = now

            if old_tty and sys.stdin in r:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                
                # Only pass down manual stdin input if Antigravity isn't actively running a patch
                if not os.path.exists(LOCK_FILE) and data:
                    try:
                        os.write(fd, data)
                    except OSError:
                        break
    finally:
        if old_tty:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
            except termios.error:
                pass
        _log("StreamGuard exiting.")

# ---------------------------------------------------------------------------
# Test Escalation Mode (Mock Simulation)
# ---------------------------------------------------------------------------
def run_escalation_test() -> None:
    print("=== Starting StreamGuard Reconnect & Escalation Verification ===")
    
    # ---------------------------------------------------------
    # 1. Test TUI Regex Filters
    # ---------------------------------------------------------
    print("Testing TUI Escape Sequence Filters & Regexes...")
    # CPR Response
    cpr = b"Hello\x1b[24;80RWorld"
    assert filter_tui_query_responses(cpr) == b"HelloWorld", "CPR response filtering failed!"
    # DECRPM Response
    decrpm = b"Hello\x1b[1000$yWorld"
    assert filter_tui_query_responses(decrpm) == b"HelloWorld", "DECRPM response filtering failed!"
    # Alt Screen buffers
    alt = b"Hello\x1b[?1049hWorld\x1b[?47l"
    assert filter_alt_screen(alt) == b"HelloWorld", "Alt screen filtering failed!"
    # Mouse tracking enable/disable
    mouse_track = b"Hello\x1b[?1000hWorld\x1b[?1015l"
    assert filter_mouse_tracking(mouse_track) == b"HelloWorld", "Mouse tracking filtering failed!"
    # Scrollback Clear
    clear = b"Hello\x1b[3JWorld"
    assert filter_scrollback_clear(clear) == b"HelloWorld", "Scrollback clear filtering failed!"
    print("✅ TUI Filters: PASSED")

    # ---------------------------------------------------------
    # 2. Test Output & Thought Logger
    # ---------------------------------------------------------
    print("Testing Output & Thought Logger (Code block shielding)...")
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
        
    global inside_code_block, thought_accumulator, in_thought_gathering
    inside_code_block = False
    thought_accumulator = ""
    in_thought_gathering = False
    
    log_output_and_thoughts("Thought: 10ms\nWe need to analyze weights file.\n")
    log_output_and_thoughts("-> Read config.json\n")
    log_output_and_thoughts("```python\nimport os\nimport sys\n```\n")
    log_output_and_thoughts("Build success\n")
    
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        log_content = f.read()
        
    print("Simulated Log:\n", log_content)
    assert "🧠 THOUGHT: Thought: 10ms\nWe need to analyze weights file." in log_content, "Thought log missing!"
    assert "📄 OUTPUT: -> Read config.json" in log_content, "Tool call output log missing!"
    assert "📄 OUTPUT: Build success" in log_content, "Status output log missing!"
    assert "import os" not in log_content, "Code blocks were not shielded from log!"
    print("✅ Output & Thought Logger: PASSED")

    # ---------------------------------------------------------
    # 3. Test Watchdog Timers & Dynamic Thresholds
    # ---------------------------------------------------------
    print("Testing Watchdog Timers & Dynamic Thresholds...")
    def get_timeout(pytest_active: bool, tool_active: bool) -> float:
        if tool_active:
            return 300.0
        return PYTEST_TIMEOUT if pytest_active else STALL_TIMEOUT
        
    assert get_timeout(pytest_active=False, tool_active=False) == 900.0, "Normal timeout must be 15m (900s)"
    assert get_timeout(pytest_active=True, tool_active=False) == 600.0, "Pytest timeout must be 10m (600s)"
    assert get_timeout(pytest_active=False, tool_active=True) == 300.0, "Tool hang timeout must be 5m (300s)"
    assert get_timeout(pytest_active=True, tool_active=True) == 300.0, "Tool hang timeout must override pytest (300s)"
    print("✅ Watchdog Timers: PASSED")

    # ---------------------------------------------------------
    # 3.5 Test Spinner Active Detection (Helix & Braille)
    # ---------------------------------------------------------
    print("Testing Spinner Active Detection...")
    helix_buffer = "■⬝⬝⬝⬝⬝⬝⬝\n" * 105
    braille_buffer = "⠋ Thinking\n" * 105
    silent_buffer = "some random log line\n" * 20
    
    assert was_spinner_active(helix_buffer) is True, "Helix spinner was not detected!"
    assert was_spinner_active(braille_buffer) is True, "Braille spinner was not detected!"
    assert was_spinner_active(silent_buffer) is False, "Silent buffer falsely flagged as active spinner!"
    print("✅ Spinner Active Detection: PASSED")

    # Clean up old locks or logs
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
        
    read_fd, write_fd = os.pipe()
    
    # Set non-blocking on read_fd
    flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
    fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    
    print("Simulating fake traceback injection...")
    
    # Mock traceback strings
    fake_traceback = (
        "Traceback (most recent call last):\n"
        f"  File \"{WORKSPACE_ROOT}/psychological/scrapers/product_intel.py\", line 12, in <module>\n"
        "    scrape_company()\n"
        "NameError: name 'scrape_company' is not defined\n"
    )
    
    # Also dump historical tracebacks to verify echo shielding works
    fake_echo_traceback = (
        f"> Traceback (most recent call last):\n"
        f">   File \"{WORKSPACE_ROOT}/psychological/scrapers/site-packages/selenium/webdriver.py\", line 45\n"
        "```python\n"
        "Traceback (most recent call last):\n"
        "  File \"/usr/lib/python3.11/subprocess.py\", line 100\n"
        "```\n"
    )
    
    # Let's call the parser on the echo traceback and verify it returns None
    echo_result = check_for_traceback(fake_echo_traceback)
    print(f"Echo Shielding Verification: {echo_result} (Expected: None)")
    assert echo_result is None, "Echo shielding failed! Triggered on formatted markdown traceback."
    
    # Let's call the parser on the real traceback
    real_result = check_for_traceback(fake_traceback)
    print(f"Real Traceback Extraction Verification: {real_result} (Expected: {WORKSPACE_ROOT}/psychological/scrapers/product_intel.py)")
    assert real_result == f"{WORKSPACE_ROOT}/psychological/scrapers/product_intel.py", f"Extraction failed! Got: {real_result}"
    
    # Trigger the simulated failure
    write_failure_trigger(real_result, "Simulated name error", write_fd)
    
    # Verify .guard_lock was created
    print(f"Checking if {LOCK_FILE} exists...")
    assert os.path.exists(LOCK_FILE), f"Sync Lock file {LOCK_FILE} was not created!"
    print(f"✅ Verified: {LOCK_FILE} exists.")
    
    # Read log file and verify structure
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        log_content = f.read()
    print("Log Content:\n", log_content)
    assert "=== AUTONOMOUS FAILURE TRIGGER ===" in log_content, "Structured failure block missing from log!"
    assert f"File: {WORKSPACE_ROOT}/psychological/scrapers/product_intel.py" in log_content, "Incorrect target file path in log!"
    print("✅ Verified: Log content structure is correct.")
    
    # Simulate Antigravity deleting the lock file
    print("Simulating Antigravity deleting the lock file...")
    os.remove(LOCK_FILE)
    
    # Read the data written to the read_fd pipe in a loop to handle async timing and pipe chunks
    data = b""
    start_time = time.time()
    while (time.time() - start_time) < 6.0:
        try:
            chunk = os.read(read_fd, 1024)
            if chunk:
                data += chunk
                if b"continue executing the todo list" in data:
                    break
        except OSError:
            pass
        time.sleep(0.5)
        
    print(f"Pipe data received on read end: {repr(data)}")
    assert b"continue executing the todo list" in data, "Resume command was not injected!"
    print("✅ Verified: Resume directive successfully injected.")

    # Test Simple Reconnect
    print("Simulating simple reconnect trigger injection...")
    global is_reconnecting
    # Re-use pipe descriptors
    is_reconnecting = True
    do_simple_reconnect(write_fd, "upstream idle timeout exceeded")
    
    # Read the data written to the read_fd pipe
    data = b""
    start_time = time.time()
    while (time.time() - start_time) < 6.0:
        try:
            chunk = os.read(read_fd, 1024)
            if chunk:
                data += chunk
                if b"continue" in data:
                    break
        except OSError:
            pass
        time.sleep(0.5)
        
    print(f"Simple reconnect pipe data: {repr(data)}")
    assert b"continue" in data, "Simple reconnect continue was not injected!"
    print("✅ Verified: Simple reconnect logic successfully injected continue.")
        
    # Test Tool Hang Interrupt
    print("Testing tool hang interrupt logic...")
    global is_in_tool_call
    is_reconnecting = True
    is_in_tool_call = True
    interrupt_and_resume_tool(write_fd, "Tool execution hang (>5m)")
    
    # Read the data written to the read_fd pipe
    data = b""
    start_time = time.time()
    while (time.time() - start_time) < 6.0:
        try:
            chunk = os.read(read_fd, 1024)
            if chunk:
                data += chunk
                if b"continue executing the todo list" in data:
                    break
        except OSError:
            pass
        time.sleep(0.5)
        
    print(f"Tool hang pipe data: {repr(data)}")
    assert b"continue executing the todo list" in data, "Tool hang resume command was not injected!"
    print("✅ Verified: Tool hang logic successfully injected resume directive.")
        
    # Test Double Escape and Continue
    print("Testing double escape and continue logic...")
    is_reconnecting = True
    double_escape_and_continue(write_fd, "User-space watchdog stall (>5m)")
    
    # Read the data written to the read_fd pipe
    data = b""
    start_time = time.time()
    while (time.time() - start_time) < 6.0:
        try:
            chunk = os.read(read_fd, 1024)
            if chunk:
                data += chunk
                if b"continue" in data:
                    break
        except OSError:
            pass
        time.sleep(0.5)
        
    print(f"Double escape pipe data: {repr(data)}")
    assert data.count(b"\x1b") == 2, f"Expected 2 ESC taps, got: {data.count(b'\x1b')}"
    assert b"continue" in data, "Double escape continue was not injected!"
    print("✅ Verified: Double escape logic successfully injected continue.")

    print("=== Reconnect & Escalation Verification Complete: ALL PASSED ===")

if __name__ == "__main__":
    main()
