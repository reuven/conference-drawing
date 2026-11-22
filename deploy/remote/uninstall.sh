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

# Is it provably safe to purge PostgreSQL $1? Only if this PostgreSQL is a
# single cluster, "<major> main", and nothing but the stock databases, the
# stock roles and empty postgres/template1 databases is left in it. Purging
# postgresql-<major> deletes every cluster of that version, so anything else
# means someone else's data. Every check fails closed: a failed or odd answer
# means "no". Prints the reason when it says no.
safe_to_purge() {
	local major=$1 clusters out db
	if ! clusters=$(pg_lsclusters --no-header 2>/dev/null) ||
		! printf '%s\n' "$clusters" | awk -v m="$major" '
			NF { n++; if ($1 != m || $2 != "main") bad = 1 }
			END { exit !(n == 1 && !bad) }'; then
		say "not removing PostgreSQL: it is not exactly one cluster, $major main"
		return 1
	fi
	if ! out=$(pg_admin -At -c "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres', 'template0', 'template1')") ||
		[ -n "$out" ]; then
		say "not removing PostgreSQL: other databases exist (or the check failed)"
		return 1
	fi
	for db in postgres template1; do
		if ! out=$(pg_admin -At --dbname="$db" -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\_%'") ||
			[ "$out" != 0 ]; then
			say "not removing PostgreSQL: the $db database holds relations of its own (or the check failed)"
			return 1
		fi
	done
	if ! out=$(pg_admin -At -c "SELECT rolname FROM pg_roles WHERE rolname <> 'postgres' AND rolname NOT LIKE 'pg\_%'") ||
		[ -n "$out" ]; then
		say "not removing PostgreSQL: other roles exist (or the check failed)"
		return 1
	fi
}

# The database and role go once nothing can use them. PostgreSQL itself goes
# only if install.sh installed it (the marker) and safe_to_purge agrees; its
# dependencies (postgresql-common, the client) stay for apt autoremove.
if [ -n "$(pg_server_majors)" ]; then
	say "dropping the $DB_NAME database and the $DB_ROLE role"
	pg_admin -f - <<'SQL'
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
DROP ROLE IF EXISTS :"role";
SQL
	no_symlink "$PG_MARKER"
	if [ -f "$PG_MARKER" ]; then
		major=$(cat "$PG_MARKER")
		[[ $major =~ ^[0-9]+$ ]] || die "$PG_MARKER does not hold a PostgreSQL major version"
		dropin="$ROOT/etc/postgresql/$major/main/conf.d/$PG_DROPIN_NAME"
		if safe_to_purge "$major"; then
			say "removing PostgreSQL $major, which install.sh installed"
			rm -f "$dropin"
			DEBIAN_FRONTEND=noninteractive apt-get purge -y -q postgresql "postgresql-$major"
		else
			say "left PostgreSQL $major installed, and its drop-in $dropin with it." \
				"Once nothing else needs it, remove it by hand with:" \
				"apt-get purge -y -q postgresql postgresql-$major"
		fi
	else
		say "left PostgreSQL alone: install.sh did not install it"
	fi
else
	say "PostgreSQL is not installed; no database to drop"
fi

say "removing $APP_DIR and the $APP_USER user"
rm -rf -- "$APP_DIR"
if id -u "$APP_USER" >/dev/null 2>&1; then
	userdel "$APP_USER"
fi
