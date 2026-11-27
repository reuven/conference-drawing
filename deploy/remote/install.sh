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
# The database is PostgreSQL on the local unix socket. If the server has no
# PostgreSQL at all (no cluster under /etc/postgresql, no postgresql-<major>
# package), a first run installs Ubuntu's, records that it did in
# /srv/conference-drawing/.installed-postgresql, and gives it a
# small-footprint conf.d drop-in (which also sets log_min_error_statement =
# panic). A PostgreSQL that was already there belongs to someone else: no apt,
# no marker, no drop-in, no restart, only a warning showing its
# listen_addresses and a one-line suggestion of that logging setting. Either
# way it must be version 15 or later (Django 6.1), and the run creates the
# role conference-drawing (LOGIN, no password: the app user's OS name maps to
# it by peer auth) and the database conference_drawing it owns, idempotently;
# a conference_drawing database that another role owns is refused, never
# taken over.
#
# The units have Requires=postgresql.service, so restarting PostgreSQL also
# restarts the web app and the Drip worker for a moment. That happens only
# when the drop-in's content changed (cmp), which on a server this script set
# up means the first install or a release that changes the drop-in.
#
# Upgrades keep the old release serving for as long as possible:
#   1. a new venv, venv-<UTC time>-<commit>, is built beside the live one
#      (uv pip install --require-hashes, the wheel's sha256 checked first),
#      and collectstatic runs from it, while the old units still run;
#   2. only then are the running units stopped, a pg_dump snapshot is taken
#      (under the maintenance lock, which this script holds throughout), init
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
# taken just before migrate (data/snapshots/, newest): drop and recreate the
# database as postgres, then pg_restore the snapshot as the app user (a failed
# migrate writes the exact commands to the install log).
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
#     .env 0600, snapshots/ 0700 (pg_dump files, 0600)
#     static/, media/, media/{covers,logos}/  app user 0755, files 0644 (public anyway)
#   /run/conference-drawing/web.sock    gunicorn's socket, made by systemd
#                                       (the web .socket unit): app user,
#                                       group caddy, 0660
#   PostgreSQL                          socket only (listen_addresses = ''),
#                                       database conference_drawing owned by
#                                       role conference-drawing (peer auth)
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
restore_dump=""
on_exit() {
	local status=$? started=""
	# The ssh session may be gone: no signal or failed write may stop this.
	trap '' PIPE HUP
	set +e
	# A restore's db.dump holds personal data: it never stays behind.
	[ -z "$restore_dump" ] || as_app rm -f -- "$restore_dump" 2>/dev/null
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
	if [ "$migrating" -eq 1 ] && [ -n "$pre_migrate" ]; then
		log_line "init/migrate failed: the schema may be partly migrated. To go back to the pre-migrate snapshot:" \
			"systemctl stop $WEB_SOCKET $WEB_UNIT $DRIP_UNIT $SNAPSHOT_TIMER &&" \
			"runuser -u postgres -- dropdb --force $DB_NAME &&" \
			"runuser -u postgres -- createdb --owner=$DB_ROLE $DB_NAME &&" \
			"runuser -u $APP_USER -- pg_restore --no-owner --role=$DB_ROLE --exit-on-error --single-transaction --dbname=$DB_NAME $pre_migrate &&" \
			"systemctl start $WEB_SOCKET $WEB_UNIT $DRIP_UNIT $SNAPSHOT_TIMER;" \
			"then rerun up.sh, which grants the database's privileges again"
	elif [ "$migrating" -eq 1 ]; then
		log_line "init/migrate failed with no pre-migrate snapshot (the database had no tables yet), so there is nothing to put back"
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
		for p in snapshots static media media/covers media/logos .env; do
			if [ -L "$data/$p" ]; then
				echo "error: $data/$p is a symbolic link; remove it and run again" >&2
				exit 1
			fi
		done
		mkdir -p "$data/snapshots" "$data/static" "$data/media/covers" "$data/media/logos"
		chmod 0700 "$data/snapshots"
		find -P "$data/static" "$data/media" ! -type l -type d -exec chmod 0755 {} +
		find -P "$data/static" "$data/media" ! -type l -type f -exec chmod 0644 {} +
	' _ "$DATA_DIR"
}

# Bring the Drip keys the operator sent into .env: append a missing key,
# replace every line of a key whose value differs (so a rotated key reaches the
# server), leave the file untouched when it already matches. Root only renders
# the lines; the app user does all the file work (a temp file beside .env,
# mode 0600, then rename), so root never writes through a path the app user
# controls. A symlinked .env is refused. Values are never printed.
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
		tmp=
		trap '"'"'[ -z "$tmp" ] || rm -f -- "$tmp"'"'"' EXIT
		[ -e "$env" ] || : >"$env"
		while IFS= read -r line; do
			key=${line%%=*}
			total=$(grep -c "^$key=" "$env" || true)
			same=$(grep -cxF -- "$line" "$env" || true)
			if [ "$total" -gt 0 ] && [ "$total" = "$same" ]; then
				continue
			fi
			tmp=$(mktemp "$env.XXXXXX")
			if [ "$total" -eq 0 ]; then
				cp -- "$env" "$tmp"
				# A last line without its newline must not swallow the new key.
				[ ! -s "$tmp" ] || [ -z "$(tail -c1 "$tmp")" ] || echo >>"$tmp"
				printf "%s\n" "$line" >>"$tmp"
			else
				NEWLINE=$line KEY=$key awk '"'"'
					index($0, ENVIRON["KEY"] "=") == 1 { print ENVIRON["NEWLINE"]; next }
					{ print }
				'"'"' "$env" >"$tmp"
			fi
			chmod 0600 "$tmp"
			mv -f -- "$tmp" "$env"
			tmp=
			echo "==> wrote $key" >&2
		done
	' _ "$ENV_FILE"
}

# Install Ubuntu's postgresql package only if the server has no PostgreSQL,
# and record that this script did (the marker), so uninstall.sh may purge it
# again. Only a PostgreSQL this script installed (the marker, naming the one
# server there is) gets the small-footprint drop-in: one that was already
# there may serve others, so its configuration is left alone. Restarts
# PostgreSQL only when the drop-in's content changed (its settings need a
# restart, not a reload). Sets pg_ours.
pg_ours=0
ensure_postgresql() {
	local majors major dropin new
	no_symlink "$PG_MARKER"
	majors=$(pg_server_majors)
	if [ -z "$majors" ]; then
		say "installing PostgreSQL"
		DEBIAN_FRONTEND=noninteractive apt-get update -q -o DPkg::Lock::Timeout=300
		DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
			-o DPkg::Lock::Timeout=300 postgresql
		majors=$(pg_server_majors)
		[[ $majors =~ ^[0-9]+$ ]] ||
			die "expected exactly one PostgreSQL version after installing it, found: ${majors:-none}"
		printf '%s\n' "$majors" >"$PG_MARKER"
	fi
	if [ -f "$PG_MARKER" ]; then
		major=$(cat "$PG_MARKER")
		[[ $major =~ ^[0-9]+$ ]] || die "$PG_MARKER does not hold a PostgreSQL major version"
		[ "$majors" != "$major" ] || pg_ours=1
	fi
	if [ "$pg_ours" -eq 0 ]; then
		say "using the PostgreSQL already on this server (version ${majors//$'\n'/, }); its configuration is left alone"
		say "suggestion: set log_min_error_statement = panic in your own PostgreSQL configuration, so a failed statement's values (entrant data) are not written to its log"
		return 0
	fi
	dropin="$ROOT/etc/postgresql/$major/main/conf.d/$PG_DROPIN_NAME"
	no_symlink "$dropin"
	new="$work/$PG_DROPIN_NAME"
	printf '%s\n' \
		"# Managed by conference-drawing (deploy/remote/install.sh)." \
		"# Unix socket only, and small: this server's memory is shared." \
		"listen_addresses = ''" \
		"shared_buffers = 64MB" \
		"max_connections = 20" \
		"# A failed statement's text can carry entrant data: never log it." \
		"log_min_error_statement = panic" >"$new"
	if ! cmp -s "$new" "$dropin"; then
		install -m 0644 "$new" "$dropin"
		systemctl restart postgresql
	fi
}

# Refuse a server older than 15 (Django 6.1's minimum). On someone else's
# server, warn where it listens besides the socket; change nothing.
check_postgresql() {
	local version listen
	version=$(pg_admin -At -c "SHOW server_version_num") ||
		die "cannot reach PostgreSQL as postgres on the local socket"
	[[ $version =~ ^[0-9]+$ ]] || die "unexpected server_version_num from PostgreSQL: $version"
	[ "$version" -ge 150000 ] ||
		die "PostgreSQL $((version / 10000)) is too old: conference-drawing (Django 6.1) needs 15 or later"
	if [ "$pg_ours" -eq 0 ]; then
		listen=$(pg_admin -At -c "SHOW listen_addresses") || listen="(unknown)"
		say "warning: this server's PostgreSQL listens on '$listen' besides its socket; install.sh leaves that alone"
	fi
}

# Create the role and its database if missing, idempotently, as postgres.
# The role logs in by peer auth only: it never gets a password. Nobody else
# may connect to the database.
provision_database() {
	local owner
	owner=$(pg_admin -At -f - <<'SQL'
SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :'db';
SQL
	) || die "cannot read the database list from PostgreSQL"
	if [ -n "$owner" ] && [ "$owner" != "$DB_ROLE" ]; then
		die "the database $DB_NAME already exists, owned by role $owner; refusing to use or change it"
	fi
	pg_admin -f - <<'SQL'
SELECT NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') AS need_role \gset
\if :need_role
CREATE ROLE :"role" LOGIN;
\endif
ALTER ROLE :"role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD NULL;
SELECT NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') AS need_db \gset
\if :need_db
CREATE DATABASE :"db" OWNER :"role";
\endif
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
SQL
}

# How many tables the app's database holds (0 before the first migrate).
app_table_count() {
	as_app psql -X -At -v ON_ERROR_STOP=1 --dbname="$DB_NAME" \
		-c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
}

# settings.py turns DEBUG on for 1, true, yes or on (any case) in DJANGO_DEBUG;
# the last assignment in the .env wins. DEBUG shows tracebacks with settings to
# visitors and drops the Secure flag from cookies, so a server never runs it.
# Refuse rather than edit: the file is the operator's. Never print the file.
refuse_debug_env() {
	local line value
	[ -e "$ENV_FILE" ] || return 0
	no_symlink "$ENV_FILE"
	[ -f "$ENV_FILE" ] || return 0
	line=$(grep -E '^[[:space:]]*(export[[:space:]]+)?DJANGO_DEBUG[[:space:]]*=' "$ENV_FILE" | tail -n 1 || true)
	[ -n "$line" ] || return 0
	value=${line#*=}
	value=${value#"${value%%[![:space:]]*}"}
	case $value in
	\'*)
		value=${value#\'}
		value=${value%%\'*}
		;;
	\"*)
		value=${value#\"}
		value=${value%%\"*}
		;;
	*) value=${value%%[[:space:]]#*} ;;
	esac
	value=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
	case $value in
	1 | true | yes | on)
		die "$ENV_FILE sets DJANGO_DEBUG to a true value. A server must never run with DEBUG on. Remove or blank that line, then run up.sh again."
		;;
	esac
}
refuse_debug_env

say "packages and users"
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
say "PostgreSQL"
ensure_postgresql
check_postgresql
provision_database
prepare_data_dir
if [ -n "$restore" ]; then
	[ "$upgrade" -eq 0 ] || die "refusing to restore over an installed release"
	check_restore_tarball "$restore"
	[ "$(app_table_count)" = 0 ] ||
		die "refusing to restore: the database $DB_NAME already has tables"
	# db.dump holds personal data: the EXIT trap removes it on any failure,
	# a half-finished extraction included.
	restore_dump="$DATA_DIR/db.dump"
	as_app tar -xzf - -C "$DATA_DIR" <"$restore"
	as_app pg_restore -f /dev/null "$restore_dump" ||
		die "the restore dump failed verification (pg_restore could not read it)"
	as_app pg_restore --clean --if-exists --no-owner --role="$DB_ROLE" \
		--exit-on-error --single-transaction --dbname="$DB_NAME" "$restore_dump" ||
		die "the restore failed; the database is left empty (one transaction)"
	as_app rm -f -- "$restore_dump"
	restore_dump=""
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
# The socket first: while it listens, a request would start the old web app
# against a database that is being migrated.
for unit in "$WEB_SOCKET" "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"; do
	if systemctl is-active --quiet "$unit"; then
		systemctl stop "$unit"
		stopped+=("$unit")
	fi
done
install -m 0755 "$here/common.sh" "$here/snapshot.sh" "$APP_DIR/bin/"
# Snapshot before migrate, unless there is nothing to snapshot yet.
if [ "$(app_table_count)" != 0 ]; then
	as_app env CD_LOCK_HELD=1 "$APP_DIR/bin/snapshot.sh"
	snapshots=("$DATA_DIR"/snapshots/db-*.dump)
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
for unit in "$WEB_SOCKET" "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_SERVICE" "$SNAPSHOT_TIMER"; do
	install -m 0644 "$release/systemd/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now "$WEB_SOCKET" "$WEB_UNIT" "$DRIP_UNIT" "$SNAPSHOT_TIMER"
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
