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
# One lock for every job that copies or rewrites the database from a shell:
# install.sh, uninstall.sh, backup.sh and the hourly snapshot. (Entrant deletion's VACUUM runs inside
# the app and relies on SQLite's own locking instead.)
LOCK_FILE="$DATA_DIR/.maintenance.lock"
CADDY_MAIN="$ROOT/etc/caddy/Caddyfile"
CADDY_SITE="$ROOT/etc/caddy/sites/conference-drawing.caddy"
CADDY_LOG_DIR="$ROOT/var/log/caddy"
UNIT_DIR="$ROOT/etc/systemd/system"
WEB_UNIT=conference-drawing-web.service
DRIP_UNIT=conference-drawing-drip.service
SNAPSHOT_SERVICE=conference-drawing-snapshot.service
SNAPSHOT_TIMER=conference-drawing-snapshot.timer
UV_VERSION=0.13.9

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

# Run a command as the app user, from / (never a directory root is changing).
as_app() {
	(
		cd /
		runuser -u "$APP_USER" -- "$@"
	)
}

# Run a conference-drawing management command as the app user, with the same
# environment the systemd units set. APP_VENV picks another venv (install.sh
# runs the new release before switching to it).
run_app() {
	CD_ENV_FILE="$ENV_FILE" CD_DATA_DIR="$DATA_DIR" \
		CD_VERSION_FILE="$APP_DIR/version.json" \
		as_app "${APP_VENV:-$VENV}/bin/conference-drawing" "$@"
}

# Refuse a symbolic link at $1 (root is about to act on that path).
no_symlink() {
	[ ! -L "$1" ] || die "$1 is a symbolic link; remove it and run again"
}

# Check that backup tarball $1 is safe to unpack into the data directory: it
# holds db.sqlite3, and otherwise only plain files and directories under
# media/ (no links, no absolute paths, no "..", nothing else).
check_restore_tarball() {
	local names types
	names=$(tar -tzf "$1") || die "cannot read the restore tarball"
	types=$(tar -tvzf "$1" | cut -c1) || die "cannot read the restore tarball"
	printf '%s\n' "$names" | grep -x 'db.sqlite3' >/dev/null ||
		die "the restore tarball has no db.sqlite3"
	if printf '%s\n' "$names" | grep -Ev '^(db\.sqlite3|media/?|media/[A-Za-z0-9._/-]+)$' >/dev/null ||
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
