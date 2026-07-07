#!/bin/bash
# cleanup_logs.sh - Clean up logs and temporary files

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🧹 Cleaning up logs and temporary files..."

# Delete runtime process logs and lock files
rm -f "${WORKSPACE_ROOT}/stream_guard.log"
rm -f "${WORKSPACE_ROOT}/.guard_lock"
rm -f "${SCRIPT_DIR}/stream_guard.log"
rm -f "${SCRIPT_DIR}/watchdog.log"
rm -f "${SCRIPT_DIR}/watchdog_launchd.log"
rm -f "${SCRIPT_DIR}/watchdog_launchd_err.log"

# Clean up lane logs and reports if they exist
LANES_DIR="${SCRIPT_DIR}/lanes"
if [ -d "${LANES_DIR}" ]; then
    find "${LANES_DIR}" -name "*.log" -delete
    find "${LANES_DIR}" -name "*.md" -type f -exec truncate -s 0 {} + 2>/dev/null || true
fi

echo "✅ Logs successfully cleared."
