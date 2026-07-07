# Antigravity + OpenCode Stream Guard

A lightweight, zero-dependency terminal wrapper and session resiliency guard for `opencode` sessions.

## What is it?
This tool is a client-side terminal process wrapper. It runs `opencode` inside a local pseudo-terminal (PTY) and intercepts the raw output stream to automatically manage and recover from stalls, idle disconnects, and JSON parser errors.

## Key Features
* **Idle Reconnection**: Automatically detects connection disconnects (e.g. `upstream idle timeout exceeded`) and injects `continue\r\n` after a short safety delay.
* **Inactivity Watchdog**: Monitors the output stream for long periods of silence (90+ seconds) and injects a `continue\r\n` macro if the engine is stuck mid-generation (and not waiting for user input).
* **High-Token Compaction**: Automatically triggers the `/compact` command when input tokens exceed 80k to maintain context window health.
* **TUI Escape Filtering**: Strips out raw terminal capability queries and mouse reports to prevent display cursor garbage.
* **Error Alerts**: Monitors console outputs for Python exceptions and triggers rate-limited desktop notifications.

## How it Differs from Auth Bypasses
Unlike unauthorized proxy scripts or OAuth credential bypasses, this tool:
* **Runs 100% locally**: It operates purely as a local terminal process wrapper (like running under `tmux` or `screen`).
* **Safe & Compliant**: It does not intercept, spoof, or modify authentication flows, access tokens, or Google account credentials.
* **Zero APIs required**: All reconnect macros are standard CLI text injections (like `continue`) that a human developer would type manually in their terminal.
