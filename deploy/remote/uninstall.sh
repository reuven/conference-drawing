#!/usr/bin/env bash
# Remove conference-drawing from the server (down.sh runs it after a backup).
# Runs as root from /tmp/conference-drawing-<commit>-down-<random>/remote/, never from
# /srv/conference-drawing, which it deletes.
#
# /srv/conference-drawing goes only after every unit is confirmed stopped.
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
# Wait for a running backup or snapshot, and keep new ones out.
if [ -e "$LOCK_FILE" ] || [ -L "$LOCK_FILE" ]; then
	take_lock 600
fi
units=("$SNAPSHOT_TIMER" "$SNAPSHOT_SERVICE" "$WEB_UNIT" "$DRIP_UNIT")
for unit in "${units[@]}"; do
	if [ -e "$UNIT_DIR/$unit" ]; then
		systemctl disable --now "$unit"
	fi
done
for unit in "${units[@]}"; do
	if systemctl is-active --quiet "$unit"; then
		die "$unit is still running; nothing was removed"
	fi
done

say "removing the Caddy site"
remove_caddy_site
# Access logs hold client addresses; they go with the site.
rm -f "$CADDY_LOG_DIR"/conference-drawing.access*

for unit in "${units[@]}"; do
	rm -f "$UNIT_DIR/$unit"
done
systemctl daemon-reload
# Forget any failed state so the next preflight sees a clean server.
systemctl reset-failed "${units[@]}" 2>/dev/null || true

say "removing $APP_DIR and the $APP_USER user"
rm -rf -- "$APP_DIR"
if id -u "$APP_USER" >/dev/null 2>&1; then
	userdel "$APP_USER"
fi
