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

# The database and role go once nothing can use them. PostgreSQL itself goes
# only if install.sh installed it (the marker) and no other database exists;
# its dependencies (postgresql-common, the client) stay for apt autoremove.
if postgresql_installed; then
	say "dropping the $DB_NAME database and the $DB_ROLE role"
	pg_admin -f - <<'SQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
DROP ROLE IF EXISTS :"role";
SQL
	no_symlink "$PG_MARKER"
	if [ -f "$PG_MARKER" ]; then
		major=$(cat "$PG_MARKER")
		[[ $major =~ ^[0-9]+$ ]] || die "$PG_MARKER does not hold a PostgreSQL major version"
		others=$(pg_admin -At -c "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres', 'template0', 'template1')")
		if [ -n "$others" ]; then
			say "left PostgreSQL installed: other databases exist ($(printf '%s\n' "$others" | wc -l | tr -d ' '))"
		else
			say "removing PostgreSQL $major, which install.sh installed"
			rm -f "$ROOT/etc/postgresql/$major/main/conf.d/$PG_DROPIN_NAME"
			DEBIAN_FRONTEND=noninteractive apt-get purge -y -q postgresql "postgresql-$major"
		fi
	fi
else
	say "PostgreSQL is not installed; no database to drop"
fi

say "removing $APP_DIR and the $APP_USER user"
rm -rf -- "$APP_DIR"
if id -u "$APP_USER" >/dev/null 2>&1; then
	userdel "$APP_USER"
fi
