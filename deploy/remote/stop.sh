#!/usr/bin/env bash
# Stop the app for teardown (down.sh), so nothing writes after the backup.
#
# The web app goes first, so no booth action can queue a Drip job; then the
# outbox is checked again while the worker still runs. If a job slipped in, the
# web app comes back and this exits 1; otherwise the worker stops too.
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
# The socket too: while it listens, any request would start the web app again.
systemctl stop "$SNAPSHOT_TIMER" "$WEB_SOCKET" "$WEB_UNIT"
if ! run_app outbox_status; then
	systemctl start "$SNAPSHOT_TIMER" "$WEB_SOCKET" "$WEB_UNIT"
	die "a Drip job is pending; the app is running again. Wait, then rerun down.sh"
fi
systemctl stop "$DRIP_UNIT"
