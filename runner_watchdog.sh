#!/bin/bash
# runner_watchdog.sh — Antigravity Autonomous Self-Healing Watchdog
#
# Designed to run as a persistent launchd user service (password-free).
# Monitors the execution runner, applies code fixes when execution fails,
# and automatically restarts the runner as needed.
#
# Architecture:
#   launchd → runner_watchdog.sh (persistent, KeepAlive=true)
#               ├── Waits for run completion (runner_runner.sh / overnight_runner.sh)
#               ├── Reads .relaunch_requested sentinel
#               ├── Applies automated fixes if script exists
#               └── Relaunches runner after delay
#
# The watchdog NEVER uses sudo. It runs entirely as the active user.
# Restart is triggered by the watchdog itself — no password needed.

set -uo pipefail

# macOS compatibility: fallback for GNU timeout
if ! command -v timeout &>/dev/null; then
    if command -v gtimeout &>/dev/null; then
        timeout() { gtimeout "$@"; }
    else
        timeout() {
            local timeout_secs="$1"
            shift
            # Inherit stdin explicitly for the background process
            "$@" <&0 &
            local pid=$!
            ( sleep "$timeout_secs" && kill -TERM "$pid" 2>/dev/null ) &
            local timer_pid=$!
            wait "$pid" 2>/dev/null
            local exit_code=$?
            kill "$timer_pid" 2>/dev/null
            return "$exit_code"
        }
    fi
fi

# Detect paths dynamically
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
VENV_PYTHON="${VENV_PYTHON:-${WORKSPACE_ROOT}/.venv/bin/python}"
RUNNER_SCRIPT="${RUNNER_SCRIPT:-${SCRIPT_DIR}/overnight_runner.sh}"
RELAUNCH_SENTINEL="${RELAUNCH_SENTINEL:-${WORKSPACE_ROOT}/.relaunch_requested}"
LOCK_FILE="${LOCK_FILE:-${WORKSPACE_ROOT}/.guard_lock}"
WATCHDOG_LOG="${WATCHDOG_LOG:-${SCRIPT_DIR}/watchdog.log}"
WATCHDOG_STATE="${WATCHDOG_STATE:-${SCRIPT_DIR}/.watchdog_state}"
AUTO_FIX_SCRIPT="${AUTO_FIX_SCRIPT:-${SCRIPT_DIR}/antigravity_daemon.py}"

# Timing
TOOL_TIMEOUT=180
MAX_CONSECUTIVE_FAILURES=3  # After this many failures, cool down 2h
FAILURE_RESTART_DELAY=300   # 5 min between failure restarts
SUCCESS_SLEEP=43200          # 12h after a successful run before rerun

# ── Consecutive failure counter (persisted across watchdog restarts)
consecutive_failures=0
if [ -f "${WATCHDOG_STATE}" ]; then
    consecutive_failures=$(cat "${WATCHDOG_STATE}" 2>/dev/null || echo 0)
fi

_wlog() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] WATCHDOG: $*" | tee -a "${WATCHDOG_LOG}"
}

_wlog "═══════════════════════════════════════════════════"
_wlog "🤖 Antigravity Autonomous Watchdog started"
_wlog "   Workspace: ${WORKSPACE_ROOT}"
_wlog "   Consecutive failures (from prior sessions): ${consecutive_failures}"
_wlog "═══════════════════════════════════════════════════"

# ──────────────────────────────────────────────────────────
# Helper: apply daemon auto-fixes
# ──────────────────────────────────────────────────────────
run_antigravity_fixes() {
    local reason="$1"
    _wlog "🔧 Running auto-fix: ${reason}"
    if [ -f "${AUTO_FIX_SCRIPT}" ]; then
        timeout "${TOOL_TIMEOUT}" "${VENV_PYTHON}" \
            "${AUTO_FIX_SCRIPT}" \
            --ticker ALL \
            --reason "${reason}" \
            --auto-fix \
            >> "${WATCHDOG_LOG}" 2>&1 || \
            _wlog "⚠️  Auto-fix script timed out or failed"
    else
        _wlog "ℹ️  No auto-fix script found at ${AUTO_FIX_SCRIPT}. Skipping automated fixes."
    fi
}

# ──────────────────────────────────────────────────────────
# Helper: fix snapshot directory permissions
# ──────────────────────────────────────────────────────────
heal_permissions() {
    local snapshot_dir="${HOME}/.local/share/opencode/snapshot"
    if [ -d "${snapshot_dir}" ]; then
        chown -R "$(id -un)" "${snapshot_dir}" 2>/dev/null || true
        chmod -R u+rw "${snapshot_dir}" 2>/dev/null || true
        _wlog "🔐 Snapshot directory permissions healed"
    fi
    # Kill any active stream_guard processes
    pkill -f "stream_guard.py" 2>/dev/null || true
}

# ──────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────
while true; do

    _wlog "────────────────────────────────────────────────────"
    if [ -f "${RUNNER_SCRIPT}" ]; then
        _wlog "🚀 Launching execution runner script: ${RUNNER_SCRIPT}"

        # Pre-run cleanup
        rm -f "${LOCK_FILE}"
        heal_permissions

        # Run the script and capture exit code
        RUN_START=$(date +%s)
        bash "${RUNNER_SCRIPT}" >> "${WATCHDOG_LOG}" 2>&1
        RUN_EXIT=$?
        RUN_ELAPSED=$(( $(date +%s) - RUN_START ))

        _wlog "Runner finished in ${RUN_ELAPSED}s with exit code ${RUN_EXIT}"
    else
        _wlog "⚠️  Runner script not found at ${RUNNER_SCRIPT}. Waiting for lock file or manual trigger..."
        # Sleep for a bit to avoid CPU spin when runner script is absent
        sleep 60
        continue
    fi

    # ── Check outcome ─────────────────────────────────────
    if [ -f "${RELAUNCH_SENTINEL}" ]; then
        SENTINEL_CONTENT=$(cat "${RELAUNCH_SENTINEL}" 2>/dev/null || echo "unknown")
        _wlog "⚠️  Relaunch sentinel detected: ${SENTINEL_CONTENT}"
        rm -f "${RELAUNCH_SENTINEL}"

        consecutive_failures=$(( consecutive_failures + 1 ))
        echo "${consecutive_failures}" > "${WATCHDOG_STATE}"

        _wlog "📊 Consecutive failures: ${consecutive_failures}/${MAX_CONSECUTIVE_FAILURES}"

        if [ ${consecutive_failures} -ge ${MAX_CONSECUTIVE_FAILURES} ]; then
            _wlog "🛑 Max consecutive failures (${MAX_CONSECUTIVE_FAILURES}) reached."
            _wlog "   Cooling down for 2h before reset..."
            echo "" >> "${WATCHDOG_LOG}"
            echo "╔══════════════════════════════════════════════════╗" >> "${WATCHDOG_LOG}"
            echo "║  ⚠️  WATCHDOG COOLDOWN — 3 CONSECUTIVE FAILURES  ║" >> "${WATCHDOG_LOG}"
            echo "║  Action required: check failure logs manually    ║" >> "${WATCHDOG_LOG}"
            echo "║  Resuming at: $(date -v+2H '+%H:%M') (approx)  ║" >> "${WATCHDOG_LOG}"
            echo "╚══════════════════════════════════════════════════╝" >> "${WATCHDOG_LOG}"
            sleep 7200
            consecutive_failures=0
            echo "0" > "${WATCHDOG_STATE}"
            _wlog "🔄 Cooldown complete. Resuming autonomous loop."
        else
            # Apply auto-fixes before relaunching
            run_antigravity_fixes "Watchdog auto-fix: failure ${consecutive_failures}/${MAX_CONSECUTIVE_FAILURES}"

            _wlog "⏳ Waiting ${FAILURE_RESTART_DELAY}s before relaunch..."
            sleep "${FAILURE_RESTART_DELAY}"
            _wlog "🔄 Relaunching runner..."
        fi

    else
        # ── SUCCESS path ─────────────────────────────────────
        _wlog "🎉 Run completed successfully!"
        consecutive_failures=0
        echo "0" > "${WATCHDOG_STATE}"

        _wlog "😴 Sleeping ${SUCCESS_SLEEP}s before next scheduled run..."
        sleep "${SUCCESS_SLEEP}"
        _wlog "⏰ Sleep complete. Starting next scheduled run..."
    fi

done
