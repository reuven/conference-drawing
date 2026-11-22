#!/usr/bin/env bash
# Install or upgrade conference-drawing. Runs as root from the release that
# deploy/up.sh staged in /tmp/conference-drawing-<commit>-up-<random>/:
#
#   bash remote/install.sh <domain> [--restore <backup.tar.gz>]
#
# Secrets arrive on stdin as KEY=value lines (DRIP_API_KEY, DRIP_ACCOUNT_ID,
# DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD), never in argv. Safe to run again:
# init only fills in what is missing, then migrates and runs createcachetable.
#
# Upgrades keep the old release serving for as long as possible:
#   1. a new venv, venv-<UTC time>-<commit>, is built beside the live one
#      (uv pip install --require-hashes, the wheel's sha256 checked first),
#      and collectstatic runs from it, while the old units still run;
#   2. only then are the running units stopped, a snapshot is taken (under
#      the maintenance lock, which this script holds throughout), init
#      migrates, and the venv symlink is switched to the new venv;
#   3. the units start again. If anything fails after they were stopped, the
#      EXIT trap starts the ones this run stopped, on whichever venv the link
#      then names: the old one, unless the failure came after the switch.
# "Upgrade" means the venv link exists; .env alone (a first install that
# failed after writing the Drip keys) is still a fresh install.
# The previous venv is kept for one release. To roll back by hand:
#   ln -sfn venv-<previous> /srv/conference-drawing/venv
#   systemctl restart conference-drawing-web conference-drawing-drip
# and, if the new release migrated the schema, first put back the snapshot
# taken just before migrate (data/snapshots/, newest) as data/db.sqlite3.
#
# up.sh stays attached over ssh for the whole run. If the connection drops
# mid-run, output fails (SIGPIPE) or SIGHUP arrives; either ends the run
# through the EXIT trap, which ignores both, starts the stopped units before
# it prints anything, and records the outcome in
# /var/log/conference-drawing-install.log (root, 0600). Read that file after
# reconnecting, then rerun up.sh.
#
# Layout and permissions (Caddy runs as "caddy"):
#   /srv/conference-drawing             root 0755: venv -> venv-*/, python/,
#                                       bin/, version.json
#   /srv/conference-drawing/data        app user 0711: others may pass through,
#                                       not list. The only writable path.
#     .env 0600, db.sqlite3 0660 (UMask=007), snapshots/ 0700
#     static/, media/, media/covers/    app user 0755, files 0644 (public anyway)
#   /run/conference-drawing/web.sock    gunicorn's socket, in a 0750 directory
#                                       of group caddy (see the web unit)
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

INSTALL_LOG="$ROOT/var/log/conference-drawing-install.log"
# Append one line to the install log. Never fails, never prints.
log_line() {
	{
		mkdir -p "$(dirname "$INSTALL_LOG")"
		[ ! -L "$INSTALL_LOG" ] || return 0
		(umask 077 && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$INSTALL_LOG")
		chmod 0600 "$INSTALL_LOG"
	} 2>/dev/null || true
}

new_venv=""
switched=0
migrating=0
pre_migrate=""
stopped=()
work=""
on_exit() {
	local status=$? started=""
	# The ssh session may be gone: no signal or failed write may stop this.
	trap '' PIPE HUP
	set +e
	if [ "$status" -ne 0 ] && [ "${#stopped[@]}" -gt 0 ]; then
		if systemctl start "${stopped[@]}" >/dev/null 2>&1; then
			started="restarted ${stopped[*]}"
		else
			started="could not restart ${stopped[*]}"
		fi
	fi
	[ -z "$work" ] || rm -rf "$work" 2>/dev/null
	if [ "$status" -eq 0 ]; then
		log_line "installed commit=${commit:-unknown}"
		return 0
	fi
	if [ -n "$new_venv" ] && [ "$switched" -eq 0 ]; then
		rm -rf -- "$new_venv" 2>/dev/null
	fi
	log_line "install failed (exit $status) commit=${commit:-unknown}${started:+; $started}"
	if [ "$migrating" -eq 1 ]; then
		log_line "init/migrate failed: the schema may be partly migrated. To go back to the pre-migrate snapshot:" \
			"systemctl stop $WEB_UNIT $DRIP_UNIT $SNAPSHOT_TIMER &&" \
			"runuser -u $APP_USER -- cp $pre_migrate $DATA_DIR/db.sqlite3 &&" \
			"rm -f $DATA_DIR/db.sqlite3-wal $DATA_DIR/db.sqlite3-shm &&" \
			"systemctl start $WEB_UNIT $DRIP_UNIT $SNAPSHOT_TIMER"
	fi
	say "install failed${started:+; $started}; see $INSTALL_LOG" 2>/dev/null || true
	return 0
}
trap on_exit EXIT
# A dropped ssh session: end the run through the EXIT trap.
trap 'exit 129' HUP
trap 'exit 141' PIPE
umask 077
work=$(mktemp -d)
log_line "install started domain=$domain"
: >"$work/secrets"
[ -t 0 ] || cat >"$work/secrets"
read_secrets "$work/secrets"
rm -f "$work/secrets"
umask 022

uv="$ROOT/usr/local/bin/uv"
uv_version() {
	{ "$uv" --version 2>/dev/null || true; } | cut -d' ' -f2
}

# Put the pinned uv in /usr/local/bin, from the tarball up.sh downloaded and
# checked, unless the right version is already there.
ensure_uv() {
	local name dir
	[ "$(uv_version)" != "$UV_VERSION" ] || return 0
	[ -f "$release/uv.sha256" ] ||
		die "uv $UV_VERSION is not installed and up.sh shipped no uv tarball"
	name=$(awk 'NR == 1 { print $2 }' "$release/uv.sha256")
	[[ $name =~ ^uv-(x86_64|aarch64)-unknown-linux-gnu\.tar\.gz$ ]] ||
		die "unexpected uv tarball name: $name"
	(cd "$release" && sha256sum -c --quiet uv.sha256) >&2 ||
		die "the uv tarball failed its sha256 check"
	say "installing uv $UV_VERSION"
	mkdir "$work/uv"
	tar -xzf "$release/$name" -C "$work/uv"
	dir="$work/uv/${name%.tar.gz}"
	install -d -m 0755 "$ROOT/usr/local/bin"
	install -m 0755 "$dir/uv" "$dir/uvx" "$ROOT/usr/local/bin/"
	[ "$(uv_version)" = "$UV_VERSION" ] || die "the new uv does not report $UV_VERSION"
}

# Create the data directory and, as the app user, everything in it. Root only
# makes the top directory (in root's /srv/conference-drawing, where the app
# user cannot plant a link) and hands it over.
prepare_data_dir() {
	no_symlink "$DATA_DIR"
	mkdir -p "$DATA_DIR"
	chown -h "$APP_USER:$APP_USER" "$DATA_DIR"
	chmod 0711 "$DATA_DIR"
	# shellcheck disable=SC2016  # expanded by the app user's bash
	as_app bash -c '
		set -euo pipefail
		data=$1
		for p in snapshots static media media/covers .env; do
			if [ -L "$data/$p" ]; then
				echo "error: $data/$p is a symbolic link; remove it and run again" >&2
				exit 1
			fi
		done
		mkdir -p "$data/snapshots" "$data/static" "$data/media/covers"
		chmod 0700 "$data/snapshots"
		find -P "$data/static" "$data/media" ! -type l -type d -exec chmod 0755 {} +
		find -P "$data/static" "$data/media" ! -type l -type f -exec chmod 0644 {} +
		[ ! -e "$data/db.sqlite3" ] || chmod 0660 "$data/db.sqlite3"
	' _ "$DATA_DIR"
}

# Append the Drip keys the operator sent that .env does not have yet. Root
# renders the lines; the app user writes the file.
write_env_keys() {
	local lines=""
	[ -z "$drip_api_key" ] || lines+="$(env_line DRIP_API_KEY "$drip_api_key")"$'\n'
	[ -z "$drip_account_id" ] || lines+="$(env_line DRIP_ACCOUNT_ID "$drip_account_id")"$'\n'
	[ -n "$lines" ] || return 0
	# shellcheck disable=SC2016  # expanded by the app user's bash
	printf '%s' "$lines" | as_app bash -c '
		set -euo pipefail
		env=$1
		[ ! -L "$env" ] || { echo "error: $env is a symbolic link" >&2; exit 1; }
		umask 077
		while IFS= read -r line; do
			key=${line%%=*}
			if ! grep -q "^$key=" "$env" 2>/dev/null; then
				printf "%s\n" "$line" >>"$env"
				echo "==> wrote $key" >&2
			fi
		done
		chmod 0600 "$env"
	' _ "$ENV_FILE"
}

say "packages and users"
if ! command -v sqlite3 >/dev/null; then
	DEBIAN_FRONTEND=noninteractive apt-get install -y -q sqlite3
fi
if ! id -u "$APP_USER" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir "$APP_DIR" --no-create-home \
		--shell /usr/sbin/nologin "$APP_USER"
fi
ensure_uv

say "data directory"
no_symlink "$APP_DIR"
install -d -m 0755 "$APP_DIR" "$APP_DIR/bin"
upgrade=0
if [ -L "$VENV" ]; then
	upgrade=1
fi
no_symlink "$LOCK_FILE"
[ -e "$LOCK_FILE" ] || install -m 0640 /dev/null "$LOCK_FILE"
chown -h "root:$APP_USER" "$LOCK_FILE"
chmod 0640 "$LOCK_FILE"
take_lock 600
prepare_data_dir
if [ -n "$restore" ]; then
	[ "$upgrade" -eq 0 ] || die "refusing to restore over an installed release"
	check_restore_tarball "$restore"
	[ ! -e "$DATA_DIR/db.sqlite3" ] || die "refusing to restore over an existing database"
	as_app tar -xzf - -C "$DATA_DIR" <"$restore"
	[ "$(as_app sqlite3 "$DATA_DIR/db.sqlite3" "PRAGMA integrity_check")" = ok ] ||
		die "the restored database failed integrity_check"
	prepare_data_dir
	say "restored $(basename "$restore")"
fi
write_env_keys

say "building the new release beside the running one"
wheels=("$release"/*.whl)
[ "${#wheels[@]}" -eq 1 ] && [ -f "${wheels[0]}" ] || die "expected exactly one wheel in $release"
[ "$(awk '{ print $2 }' "$release/wheel.sha256")" = "$(basename "${wheels[0]}")" ] ||
	die "wheel.sha256 does not name the shipped wheel"
(cd "$release" && sha256sum -c --quiet wheel.sha256) >&2 || die "the wheel failed its sha256 check"
commit=$(sed -n 's/.*"commit": "\([0-9a-f]\{12\}\)[0-9a-f]*".*/\1/p' "$release/version.json")
[ -n "$commit" ] || die "no commit in version.json"
[ ! -e "$VENV" ] || [ -L "$VENV" ] || die "$VENV is a directory, not a link to a venv-* directory"
new_venv="$APP_DIR/venv-$(date -u +%Y%m%dT%H%M%SZ)-$commit"
while [ -e "$new_venv" ]; do # a second run within the same second
	sleep 1
	new_venv="$APP_DIR/venv-$(date -u +%Y%m%dT%H%M%SZ)-$commit"
done
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python" UV_CACHE_DIR="$APP_DIR/.cache/uv"
export UV_PYTHON_PREFERENCE=only-managed
"$uv" venv --quiet --python 3.14 "$new_venv"
"$uv" pip install --require-hashes --quiet --python "$new_venv/bin/python" \
	-r "$release/requirements.txt"
"$uv" pip install --no-deps --quiet --python "$new_venv/bin/python" "${wheels[0]}"
APP_VENV=$new_venv
collected=0
if [ "$upgrade" -eq 1 ]; then # settings exist, and the old app keeps serving
	run_app collectstatic --noinput --verbosity 0
	collected=1
fi

say "switching to the new release"
for unit in "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"; do
	if systemctl is-active --quiet "$unit"; then
		systemctl stop "$unit"
		stopped+=("$unit")
	fi
done
install -m 0755 "$here/common.sh" "$here/snapshot.sh" "$APP_DIR/bin/"
if [ -e "$DATA_DIR/db.sqlite3" ]; then
	as_app env CD_LOCK_HELD=1 "$APP_DIR/bin/snapshot.sh"
	snapshots=("$DATA_DIR"/snapshots/db-*.sqlite3)
	pre_migrate=${snapshots[${#snapshots[@]} - 1]}
fi
# init writes the secret key and allowed host on the first run, then (every
# run) migrates, creates the cache table, the superuser if missing, and the
# install settings. The superuser variables are set only in init's
# environment, never in any argv.
migrating=1
DJANGO_SUPERUSER_USERNAME=$su_username DJANGO_SUPERUSER_EMAIL=$su_email \
	DJANGO_SUPERUSER_PASSWORD=$su_password \
	run_app init --non-interactive --domain "$domain"
migrating=0
su_password=""
[ "$collected" -eq 1 ] || run_app collectstatic --noinput --verbosity 0
previous=""
[ ! -L "$VENV" ] || previous=$(readlink "$VENV")
ln -sfn "$(basename "$new_venv")" "$VENV"
switched=1
install -m 0644 "$release/version.json" "$APP_DIR/version.json"

say "systemd units"
for unit in "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_SERVICE" "$SNAPSHOT_TIMER"; do
	install -m 0644 "$release/systemd/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"
stopped=()
# Keep the new venv and the one it replaced; drop older ones.
for dir in "$APP_DIR"/venv-*; do
	name=$(basename "$dir")
	if [ "$dir" != "$new_venv" ] && [ "$name" != "$previous" ]; then
		rm -rf -- "$dir"
	fi
done

say "caddy site for $domain"
install -d -m 0755 "$ROOT/etc/caddy/sites"
# Create the access log as caddy before `caddy validate` (run as root) could.
access_log="$CADDY_LOG_DIR/conference-drawing.access.log"
if [ ! -d "$CADDY_LOG_DIR" ]; then
	install -d -m 0755 "$CADDY_LOG_DIR"
	chown -h caddy:caddy "$CADDY_LOG_DIR"
fi
if [ ! -e "$access_log" ] && [ ! -L "$access_log" ]; then
	install -m 0640 /dev/null "$access_log"
	chown -h caddy:caddy "$access_log"
fi
install_caddy_site "$domain" "$release/caddy/conference-drawing.caddy.tmpl"
say "installed $(cat "$APP_DIR/version.json")"
