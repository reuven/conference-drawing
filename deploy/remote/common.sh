# shellcheck shell=bash
# shellcheck disable=SC2034  # the constants are used by the sourcing scripts
# Shared constants and steps for the server-side scripts. Sourced, not run.
#
# CD_ROOT prefixes every path. It is empty on a real server; the tests set it to
# a temp dir to run whole scripts on a laptop, with fakes for every privileged
# command.
#
# Root never writes through a path the app user controls: the data directory
# belongs to the app user, who could plant a symbolic link there. Writes inside
# it run as the app user (as_app), root's own steps refuse symlinks, and
# chown is always chown -h.

ROOT=${CD_ROOT:-}
APP_USER=conference-drawing
APP_DIR="$ROOT/srv/conference-drawing"
DATA_DIR="$APP_DIR/data"
VENV="$APP_DIR/venv"
ENV_FILE="$DATA_DIR/.env"
# One lock for every job that dumps, restores or migrates the database from a
# shell: install.sh, uninstall.sh, backup.sh and the hourly snapshot. (pg_dump
# reads one consistent MVCC snapshot whatever the app writes meanwhile; the
# lock keeps these jobs from overlapping each other and migrate. Entrant
# deletion's VACUUM FULL runs inside the app under PostgreSQL's own table
# locks.) It lives in root's directory, root:conference-drawing 0640: the app
# user can open and flock it but never replace it with a link.
LOCK_FILE="$APP_DIR/.maintenance.lock"
CADDY_MAIN="$ROOT/etc/caddy/Caddyfile"
CADDY_SITE="$ROOT/etc/caddy/sites/conference-drawing.caddy"
CADDY_LOG_DIR="$ROOT/var/log/caddy"
UNIT_DIR="$ROOT/etc/systemd/system"
WEB_SOCKET=conference-drawing-web.socket
WEB_UNIT=conference-drawing-web.service
DRIP_UNIT=conference-drawing-drip.service
SNAPSHOT_SERVICE=conference-drawing-snapshot.service
SNAPSHOT_TIMER=conference-drawing-snapshot.timer
UV_VERSION=0.13.9
# PostgreSQL, on the local unix socket only. The app's OS user maps to the role
# of the same name by peer auth, so no database password exists anywhere.
DB_NAME=conference_drawing
DB_ROLE=$APP_USER
# Present only if install.sh installed the postgresql package (it holds the
# major version); uninstall.sh then may purge it again.
PG_MARKER="$APP_DIR/.installed-postgresql"
PG_DROPIN_NAME=conference-drawing.conf
# The distribution's socket directory. The app is told it explicitly: the
# psycopg binary wheel bundles its own libpq, whose compiled-in default may
# differ.
DB_SOCKET_DIR=/var/run/postgresql

say() { printf '==> %s\n' "$*" >&2; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

require_root() {
	[ "$(id -u)" -eq 0 ] || die "run this as root"
}

# Same rule as deploy/lib.sh: a lowercase DNS name with at least one dot.
valid_domain() {
	local label='[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
	[[ ${#1} -le 253 && $1 =~ ^($label\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

# Run a command as the app user, from / (never a directory root is changing),
# without the maintenance lock's descriptor (fd 9).
as_app() {
	(
		cd /
		runuser -u "$APP_USER" -- "$@" 9<&-
	)
}

# Run psql as the postgres superuser (peer auth), for the role and database
# themselves. -X skips any psqlrc; the role and database names reach SQL only
# as the psql variables :"role" and :"db", which psql quotes as identifiers.
pg_admin() {
	(
		cd /
		runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 \
			-v role="$DB_ROLE" -v db="$DB_NAME" "$@" 9<&-
	)
}

# Print the major version of every PostgreSQL server on this machine, one per
# line, sorted: from the cluster directories under /etc/postgresql and from
# installed postgresql-<major> packages (with or without the "postgresql"
# metapackage, Ubuntu's or PGDG's). Prints nothing when there is none.
pg_server_majors() {
	local dir
	{
		for dir in "$ROOT"/etc/postgresql/*/*/; do
			[ -d "$dir" ] || continue
			basename "$(dirname "$dir")"
		done
		{ dpkg-query -W -f='${Package} ${Status}\n' 'postgresql-[0-9]*' 2>/dev/null || true; } |
			awk '$1 ~ /^postgresql-[0-9]+$/ && $NF == "installed" { sub(/^postgresql-/, "", $1); print $1 }'
	} | { grep -E '^[0-9]+$' || true; } | sort -un
}

# Run a conference-drawing management command as the app user, with the same
# environment the systemd units set. APP_VENV picks another venv (install.sh
# runs the new release before switching to it).
run_app() {
	CD_ENV_FILE="$ENV_FILE" CD_DATA_DIR="$DATA_DIR" \
		CD_VERSION_FILE="$APP_DIR/version.json" CD_DB_HOST="$DB_SOCKET_DIR" \
		as_app "${APP_VENV:-$VENV}/bin/conference-drawing" "$@"
}

# Refuse a symbolic link at $1 (root is about to act on that path).
no_symlink() {
	[ ! -L "$1" ] || die "$1 is a symbolic link; remove it and run again"
}

# Check that backup tarball $1 is safe to unpack into the data directory: it
# holds db.dump (pg_dump -Fc), and otherwise only plain files and directories
# under media/ (no links, no absolute paths, no "..", nothing else).
check_restore_tarball() {
	local names types
	names=$(tar -tzf "$1") || die "cannot read the restore tarball"
	types=$(tar -tvzf "$1" | cut -c1) || die "cannot read the restore tarball"
	if printf '%s\n' "$names" | grep -x 'db.sqlite3' >/dev/null; then
		die "this backup is from the SQLite version of conference-drawing and cannot be restored into PostgreSQL"
	fi
	printf '%s\n' "$names" | grep -x 'db.dump' >/dev/null ||
		die "the restore tarball has no db.dump"
	if printf '%s\n' "$names" | grep -Ev '^(db\.dump|media/?|media/[A-Za-z0-9._/-]+)$' >/dev/null ||
		printf '%s\n' "$names" | grep -E '(^|/)\.\.(/|$)' >/dev/null ||
		printf '%s\n' "$types" | grep -v '^[-d]$' >/dev/null; then
		die "the restore tarball holds unexpected entries (links, other paths or ..)"
	fi
}

# Print one KEY='value' line that python-dotenv reads back unchanged, the way
# conference_drawing.cli.format_env_line writes them.
env_line() {
	local key=$1 value=$2
	[[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "invalid env key"
	# shellcheck disable=SC2016  # a literal "${", which dotenv would interpolate
	case $value in
	*'${'* | *$'\n'*) die "$key cannot be written to .env (newline or \${)" ;;
	esac
	value=${value//\\/\\\\}
	value=${value//\'/\\\'}
	printf "%s='%s'\n" "$key" "$value"
}

# Read the KEY=value lines up.sh sends on stdin (saved to file $1) into
# drip_api_key, drip_account_id, su_username, su_email and su_password.
# Splits on the first "=" by hand: IFS='=' read would drop a trailing "=".
read_secrets() {
	local line key value
	drip_api_key="" drip_account_id="" su_username="" su_email="" su_password=""
	while IFS= read -r line || [ -n "$line" ]; do
		key=${line%%=*}
		value=${line#*=}
		case $key in
		DRIP_API_KEY) drip_api_key=$value ;;
		DRIP_ACCOUNT_ID) drip_account_id=$value ;;
		DJANGO_SUPERUSER_USERNAME) su_username=$value ;;
		DJANGO_SUPERUSER_EMAIL) su_email=$value ;;
		DJANGO_SUPERUSER_PASSWORD) su_password=$value ;;
		"") ;;
		*) die "unexpected secret key" ;;
		esac
	done <"$1"
}

# Render the Caddy site for $1 from template $2, put it in place, and reload
# Caddy only if the whole config still validates. On failure the previous site
# file (or none) is put back and Caddy is not touched. Never edits any other
# Caddy file.
install_caddy_site() {
	local domain=$1 template=$2 new saved=""
	valid_domain "$domain" || die "not a lowercase domain name: $domain"
	new="$CADDY_SITE.new"
	sed "s/__DOMAIN__/$domain/g" "$template" >"$new"
	if grep -q '__DOMAIN__' "$new"; then
		rm -f "$new"
		die "the Caddy template did not render"
	fi
	if [ -e "$CADDY_SITE" ]; then
		# Keep the old copy outside sites/, which Caddy imports.
		saved=$(mktemp "${TMPDIR:-/tmp}/conference-drawing-caddy.XXXXXX")
		cp -p "$CADDY_SITE" "$saved"
	fi
	mv "$new" "$CADDY_SITE"
	if ! caddy validate --config "$CADDY_MAIN" --adapter caddyfile >&2; then
		if [ -n "$saved" ]; then
			mv "$saved" "$CADDY_SITE"
		else
			rm -f "$CADDY_SITE"
		fi
		die "caddy validate failed with the new site file; the previous one is back and Caddy was not reloaded"
	fi
	[ -z "$saved" ] || rm -f "$saved"
	systemctl reload caddy
}

# Remove the Caddy site file, validate, reload; put it back if validation fails.
remove_caddy_site() {
	local saved
	[ -e "$CADDY_SITE" ] || return 0
	saved=$(mktemp "${TMPDIR:-/tmp}/conference-drawing-caddy.XXXXXX")
	mv "$CADDY_SITE" "$saved"
	if ! caddy validate --config "$CADDY_MAIN" --adapter caddyfile >&2; then
		mv "$saved" "$CADDY_SITE"
		die "caddy validate fails without the site file; it is back and Caddy was not reloaded"
	fi
	rm -f "$saved"
	systemctl reload caddy
}

# Hold the maintenance lock on fd 9 for the rest of the calling script.
take_lock() {
	no_symlink "$LOCK_FILE"
	exec 9<"$LOCK_FILE"
	flock --wait "$1" 9 || die "another install, backup or snapshot holds $LOCK_FILE"
}
