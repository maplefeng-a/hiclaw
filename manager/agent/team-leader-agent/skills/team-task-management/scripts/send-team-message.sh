#!/bin/bash
# send-team-message.sh - Send a message to a Matrix room with @mentions
#
# Usage:
#   send-team-message.sh --room-id <ROOM_ID> --to <@user:domain> --message <TEXT>
#
# Uses copaw CLI (CoPaw runtime) or openclaw gateway CLI (OpenClaw runtime)
# for proper message formatting (formatted_body).

set -euo pipefail

ROOM_ID=""
TO_USER=""
MESSAGE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --room-id)  ROOM_ID="$2";  shift 2 ;;
        --to)       TO_USER="$2";  shift 2 ;;
        --message)  MESSAGE="$2";  shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [ -z "${ROOM_ID}" ] || [ -z "${TO_USER}" ] || [ -z "${MESSAGE}" ]; then
    echo "Usage: send-team-message.sh --room-id <ROOM_ID> --to <@user:domain> --message <TEXT>" >&2
    exit 1
fi

# ── CoPaw runtime: copaw CLI with proper formatted_body ──
if command -v copaw &>/dev/null; then
    copaw channels send \
        --agent-id default \
        --channel matrix \
        --target-user "${TO_USER}" \
        --target-session "${ROOM_ID}" \
        --text "${TO_USER} ${MESSAGE}" 2>&1
    exit $?
fi

# ── OpenClaw runtime: openclaw gateway CLI ──
if command -v openclaw &>/dev/null; then
    openclaw gateway send \
        --room "${ROOM_ID}" \
        --message "${TO_USER} ${MESSAGE}"
    exit $?
fi

echo "ERROR: Neither copaw nor openclaw CLI found" >&2
exit 1
