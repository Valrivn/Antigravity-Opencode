#!/bin/bash
# install_launchd.sh — One-time setup for Antigravity Autonomous Runner
#
# Installs the launchd plist so the watchdog runs as your user
# without any sudo password requirement for future starts/stops.
#
# Run once:  bash opencode_scripts/install_launchd.sh

set -euo pipefail

# Auto-detect directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PLIST_SRC="${SCRIPT_DIR}/com.antigravity.runner.plist"
LAUNCHD_DIR="${HOME}/Library/LaunchAgents"
PLIST_DST="${LAUNCHD_DIR}/com.antigravity.runner.plist"
SERVICE_ID="com.antigravity.runner"
GUI_DOMAIN="gui/$(id -u)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  🚀 Antigravity Autonomous Runner — launchd Install      ║"
echo "╠══════════════════════════════════════════════════════════╣"

# ── 1. Validate prerequisites ─────────────────────────────
echo "  Checking prerequisites..."

if [ ! -f "${PLIST_SRC}" ]; then
    echo "  ❌ FATAL: plist template not found at ${PLIST_SRC}"
    exit 1
fi

if [ -f "${SCRIPT_DIR}/runner_watchdog.sh" ] && [ ! -x "${SCRIPT_DIR}/runner_watchdog.sh" ]; then
    echo "  Making runner_watchdog.sh executable..."
    chmod +x "${SCRIPT_DIR}/runner_watchdog.sh"
fi

echo "  ✅ Prerequisites OK"

# ── 2. Unload any existing service ───────────────────────
if launchctl print "${GUI_DOMAIN}/${SERVICE_ID}" > /dev/null 2>&1; then
    echo "  Stopping existing service..."
    launchctl bootout "${GUI_DOMAIN}/${PLIST_DST}" 2>/dev/null \
        || launchctl remove "${SERVICE_ID}" 2>/dev/null || true
    sleep 1
fi

# ── 3. Install plist to LaunchAgents with placeholder replacement ──
mkdir -p "${LAUNCHD_DIR}"

CURRENT_USER="$(whoami)"
CURRENT_HOME="${HOME}"

sed -e "s|WORKSPACE_ROOT_PLACEHOLDER|${WORKSPACE_ROOT}|g" \
    -e "s|HOME_DIR_PLACEHOLDER|${CURRENT_HOME}|g" \
    -e "s|USER_NAME_PLACEHOLDER|${CURRENT_USER}|g" \
    "${PLIST_SRC}" > "${PLIST_DST}"

# launchd requires the plist to be owned by root or the current user and not group/world writable
chmod 644 "${PLIST_DST}"
echo "  ✅ Plist installed with system overrides → ${PLIST_DST}"

# ── 4. Bootstrap (register) the service ──────────────────
launchctl bootstrap "${GUI_DOMAIN}" "${PLIST_DST}"
echo "  ✅ Service bootstrapped to ${GUI_DOMAIN}"

# ── 5. Start immediately ──────────────────────────────────
echo ""
echo "  Starting Antigravity Autonomous Watchdog now..."
launchctl kickstart "${GUI_DOMAIN}/${SERVICE_ID}"
sleep 1

# ── 6. Verify ─────────────────────────────────────────────
if launchctl print "${GUI_DOMAIN}/${SERVICE_ID}" > /dev/null 2>&1; then
    echo "  ✅ Service is running!"
else
    echo "  ⚠️  Service may not be running — check: launchctl print ${GUI_DOMAIN}/${SERVICE_ID}"
fi

echo ""
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  📖 Quick Reference                                       ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Start:   launchctl start ${SERVICE_ID}"
echo "║  Stop:    launchctl stop  ${SERVICE_ID}"
echo "║  Status:  launchctl print ${GUI_DOMAIN}/${SERVICE_ID}"
echo "║  Live log: tail -f ${WORKSPACE_ROOT}/opencode_scripts/watchdog.log"
echo "╚══════════════════════════════════════════════════════════╝"
