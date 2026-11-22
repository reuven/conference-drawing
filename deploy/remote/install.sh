#!/usr/bin/env bash
# Install or upgrade conference-drawing. Runs as root from
# /tmp/conference-drawing-<commit>/remote/, started by deploy/up.sh:
#
#   bash remote/install.sh <domain> [--restore <backup.tar.gz>]
#
# Secrets arrive on stdin as KEY=value lines (DRIP_API_KEY, DRIP_ACCOUNT_ID,
# DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD), never in argv. Safe to run again:
# init only fills in what is missing, then migrates and runs createcachetable.
#
# Layout and permissions (Caddy runs as "caddy" and reaches the app without
# joining any group, so no Caddy restart is needed):
#   /srv/conference-drawing             root 0755: venv/, python/, bin/, version.json
#   /srv/conference-drawing/data        app user 0711: others may pass through,
#                                       not list. The only writable path.
#     .env 0600, db.sqlite3 0660 (UMask=007), snapshots/ 0700
#     static/, media/, media/covers/    app user 0755, files 0644 (public anyway)
#   /run/conference-drawing/web.sock    gunicorn's socket, 0777 in a 0755 dir
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
release=$(dirname "$here")
# shellcheck source=deploy/remote/common.sh
source "$here/common.sh"

usage() {
	echo "usage: install.sh <domain> [--restore <backup.tar.gz>]" >&2
	exit 2
}
[ $# -eq 1 ] || [ $# -eq 3 ] || usage
domain=$1
valid_domain "$domain" || die "not a lowercase domain name: $domain"
restore=""
if [ $# -eq 3 ]; then
	[ "$2" = "--restore" ] || usage
	restore=$3
fi
require_root

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
: >"$work/secrets"
[ -t 0 ] || cat >"$work/secrets"
read_secrets "$work/secrets"
rm -f "$work/secrets"
umask 022

say "packages and users"
if ! command -v sqlite3 >/dev/null; then
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q sqlite3
fi
if ! id -u "$APP_USER" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir "$APP_DIR" --no-create-home \
		--shell /usr/sbin/nologin "$APP_USER"
fi
if [ ! -x /usr/local/bin/uv ]; then
	say "installing uv $UV_VERSION"
	curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" |
		env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv=/usr/local/bin/uv

# Stop the old version before replacing its code; started again below.
for unit in "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"; do
	if [ -e "$UNIT_DIR/$unit" ]; then
		systemctl stop "$unit"
	fi
done

say "python and the app"
install -d -m 0755 -o root -g root "$APP_DIR" "$APP_DIR/bin"
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python" UV_CACHE_DIR="$APP_DIR/.cache/uv"
export UV_PYTHON_PREFERENCE=only-managed
if [ ! -x "$VENV/bin/python" ]; then
	"$uv" venv --quiet --python 3.14 "$VENV"
fi
"$uv" pip install --quiet --python "$VENV/bin/python" -r "$release/requirements.txt"
wheels=("$release"/*.whl)
[ "${#wheels[@]}" -eq 1 ] || die "expected exactly one wheel in $release"
"$uv" pip install --quiet --python "$VENV/bin/python" --no-deps --reinstall "${wheels[0]}"
install -m 0644 "$release/version.json" "$APP_DIR/version.json"
install -m 0755 "$here/common.sh" "$here/snapshot.sh" "$APP_DIR/bin/"

say "data directory"
install -d -m 0711 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR/snapshots"
if [ -n "$restore" ]; then
	[ ! -e "$DATA_DIR/db.sqlite3" ] || die "refusing to restore over an existing database"
	# Only db.sqlite3 and plain files and directories under media/.
	if tar -tzf "$restore" | grep -Ev '^(db\.sqlite3|media/?|media/[A-Za-z0-9._/-]+)$' >/dev/null ||
		tar -tzf "$restore" | grep -E '(^|/)\.\.(/|$)' >/dev/null ||
		tar -tvzf "$restore" | cut -c1 | grep -v '^[-d]$' >/dev/null; then
		die "the restore tarball holds unexpected entries"
	fi
	tar -xzf "$restore" --no-same-owner --no-same-permissions -C "$DATA_DIR"
	[ "$(sqlite3 "$DATA_DIR/db.sqlite3" "PRAGMA integrity_check")" = ok ] ||
		die "the restored database failed integrity_check"
	chown "$APP_USER:$APP_USER" "$DATA_DIR/db.sqlite3"
	chmod 0660 "$DATA_DIR/db.sqlite3"
	say "restored $(basename "$restore")"
fi
install -d -m 0755 -o "$APP_USER" -g "$APP_USER" \
	"$DATA_DIR/static" "$DATA_DIR/media" "$DATA_DIR/media/covers"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR/media"
find "$DATA_DIR/media" -type d -exec chmod 0755 {} +
find "$DATA_DIR/media" -type f -exec chmod 0644 {} +
if [ ! -e "$LOCK_FILE" ]; then
	install -m 0600 -o "$APP_USER" -g "$APP_USER" /dev/null "$LOCK_FILE"
fi

say "settings and database"
umask 077
for pair in "DRIP_API_KEY:$drip_api_key" "DRIP_ACCOUNT_ID:$drip_account_id"; do
	key=${pair%%:*}
	value=${pair#*:}
	if [ -n "$value" ] && ! grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
		env_line "$key" "$value" >>"$ENV_FILE"
		say "wrote $key"
	fi
done
if [ -e "$ENV_FILE" ]; then
	chown "$APP_USER:$APP_USER" "$ENV_FILE"
	chmod 0600 "$ENV_FILE"
fi
umask 022
# init writes the secret key and allowed host on the first run, then (every
# run) migrates, creates the cache table, the superuser if missing, and the
# install settings. The superuser variables are set only in init's
# environment, never in any argv.
DJANGO_SUPERUSER_USERNAME=$su_username DJANGO_SUPERUSER_EMAIL=$su_email \
	DJANGO_SUPERUSER_PASSWORD=$su_password \
	run_app init --non-interactive --domain "$domain"
su_password=""
run_app collectstatic --noinput --verbosity 0

say "systemd units"
for unit in "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_SERVICE" "$SNAPSHOT_TIMER"; do
	install -m 0644 "$release/systemd/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"

say "caddy site for $domain"
install -d -m 0755 "$ROOT/etc/caddy/sites"
# Create the access log as caddy before `caddy validate` (run as root) could.
[ -d "$CADDY_LOG_DIR" ] || install -d -m 0755 -o caddy -g caddy "$CADDY_LOG_DIR"
if [ ! -e "$CADDY_LOG_DIR/conference-drawing.access.log" ]; then
	install -m 0640 -o caddy -g caddy /dev/null "$CADDY_LOG_DIR/conference-drawing.access.log"
fi
install_caddy_site "$domain" "$release/caddy/conference-drawing.caddy.tmpl"
say "installed $(cat "$APP_DIR/version.json")"
