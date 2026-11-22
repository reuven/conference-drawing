#!/usr/bin/env bash
# Install or upgrade conference-drawing on an Ubuntu server that already runs
# systemd and Caddy. Run it from the laptop, in a clean checkout:
#
#   deploy/up.sh <host> <domain> [--restore <backup.tar.gz>]
#
# It stops at the first failure, in this order:
#   1. build the wheel and requirements.txt for the checked-out commit;
#   2. preflight over ssh: no failed systemd units, the shared Caddyfile is
#      valid and imports /etc/caddy/sites/*.caddy, a caddy group exists, and
#      every A and AAAA record of <domain> is an address of the host;
#   3. if the server's /usr/local/bin/uv is not uv $UV_VERSION, download the
#      release tarball here and check it against deploy/uv-<version>.sha256;
#   4. on a first install, ask for the owner's login (the password is read
#      without echo and travels to the server on ssh's stdin, never in argv);
#   5. copy the release to a new /tmp/conference-drawing-<commit>-up-<random>/
#      and run remote/install.sh from there (see its header for how upgrades
#      keep the old release serving, and how to roll back);
#   6. smoke test /health, /version (must report this commit) and /privacy/.
#
# Every dependency is pinned by hash: requirements.txt carries uv.lock's
# hashes (the server installs with --require-hashes), the wheel's sha256 is
# checked on the server, and so is the uv tarball's.
#
# Stay connected until it finishes: install.sh runs attached to this ssh
# session, and a dropped connection interrupts it (its trap restarts any
# units it had stopped; rerun up.sh).
#
# --restore loads a backup.sh tarball (books, prizes, codes, media) into a
# fresh install, so a new conference starts with the last one's books.
#
# Secrets: DRIP_API_KEY and DRIP_ACCOUNT_ID are copied from this checkout's
# .env (never ~/.env) if present. Test hooks: CD_LAPTOP_ENV_FILE overrides it,
# CD_UV_SHA256_FILE the uv hash file.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
	echo "usage: deploy/up.sh <host> <domain> [--restore <backup.tar.gz>]" >&2
	exit 2
}

# Print KEY's value from a dotenv file, or nothing. Handles KEY=v, KEY='v',
# KEY="v" and a leading "export"; refuses backslashes rather than guess at
# escapes.
env_value() {
	local line value
	[ -f "$1" ] || return 0
	line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=" "$1" | tail -n 1 || true)
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
	*)
		value=${value%%[[:space:]]#*}
		value=${value%"${value##*[![:space:]]}"}
		;;
	esac
	case $value in *\\*) die "$2 in $1 contains a backslash; up.sh cannot copy it" ;; esac
	printf '%s' "$value"
}

# Ask for the owner login on the operator's terminal (stdin) and append it to
# the secrets file. The password is read with echo off.
prompt_owner() {
	local user email pw again
	printf 'Owner username [owner]: ' >&2
	IFS= read -r user || die "no username given"
	user=${user:-owner}
	[[ $user =~ ^[A-Za-z0-9@.+_-]{1,150}$ ]] || die "a username is letters, digits and @.+-_ only"
	printf 'Owner email: ' >&2
	IFS= read -r email || die "no email given"
	[[ $email =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "that is not an email address"
	printf 'Owner password: ' >&2
	IFS= read -r -s pw || die "no password given"
	printf '\nOwner password again: ' >&2
	IFS= read -r -s again || die "no password given"
	printf '\n' >&2
	[ "$pw" = "$again" ] || die "the passwords differ"
	[ "${#pw}" -ge 12 ] || die "use a password of at least 12 characters"
	printf 'DJANGO_SUPERUSER_USERNAME=%s\nDJANGO_SUPERUSER_EMAIL=%s\nDJANGO_SUPERUSER_PASSWORD=%s\n' \
		"$user" "$email" "$pw" >>"$secrets"
}

[ $# -eq 2 ] || [ $# -eq 4 ] || usage
set_target "$1"
domain=$2
valid_domain "$domain" || die "not a lowercase domain name: $domain"
restore=""
if [ $# -eq 4 ]; then
	[ "$3" = "--restore" ] || usage
	restore=$4
	[ -s "$restore" ] || die "no such backup: $restore"
fi
laptop_env=${CD_LAPTOP_ENV_FILE:-$REPO/.env}
uv_hashes=${CD_UV_SHA256_FILE:-$DEPLOY/uv-$UV_VERSION.sha256}
if [ ! -f "$uv_hashes" ] || grep -q PLACEHOLDER "$uv_hashes"; then
	die "$uv_hashes still holds PLACEHOLDER lines. Fill it in once from the
uv $UV_VERSION release: for each tarball named in it, copy the line from its
.sha256 asset, e.g.
    https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz.sha256
then commit the file and run up.sh again."
fi

umask 077
work=$(mktemp -d "${TMPDIR:-/tmp}/conference-drawing-up.XXXXXX")
trap 'rm -rf "$work"' EXIT
payload="$work/payload"
secrets="$work/secrets"
mkdir "$payload"
: >"$secrets"

# 1. Build the commit that is checked out, and nothing else.
dirty=$(git -C "$REPO" status --porcelain)
[ -z "$dirty" ] || die "the checkout has uncommitted changes; commit them first"
local_commit
branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)
describe=$(git -C "$REPO" describe --tags --always --dirty)
[[ $branch =~ ^[A-Za-z0-9._/+-]+$ ]] || branch=""
[[ $describe =~ ^[A-Za-z0-9._/+-]+$ ]] || describe=""

say "building $COMMIT"
(cd "$REPO" && uv build --wheel --out-dir "$payload" --quiet) >&2
wheels=("$payload"/*.whl)
if [ "${#wheels[@]}" -ne 1 ] || [ ! -f "${wheels[0]}" ]; then
	die "expected exactly one wheel from uv build"
fi
# --no-emit-project: the project itself is installed from the wheel. The
# hashes stay in, for the server's --require-hashes.
(cd "$REPO" && uv export --frozen --no-dev --no-emit-project --quiet) \
	>"$payload/requirements.txt"
printf '%s  %s\n' "$(sha256_of "${wheels[0]}")" "$(basename "${wheels[0]}")" \
	>"$payload/wheel.sha256"
cp -R "$DEPLOY/remote" "$DEPLOY/systemd" "$DEPLOY/caddy" "$payload/"
printf '{"commit": "%s", "built_at": "%s", "branch": "%s", "describe": "%s"}\n' \
	"$COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$branch" "$describe" >"$payload/version.json"
[ -z "$restore" ] || cp "$restore" "$payload/restore.tar.gz"

# 2. Preflight: refuse a server that is already unhealthy.
say "preflight on $HOST"
failed=$(remote "systemctl --failed --no-legend --plain")
if [ -n "$failed" ]; then
	die "systemd units have failed on $HOST. Read their logs, fix them, then run
'systemctl reset-failed' and up.sh again:
$failed"
fi
remote "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" >&2 ||
	die "caddy validate fails on $HOST before any change; fix Caddy first"
# shellcheck disable=SC2016  # $ is for the remote grep, not this shell
remote 'grep -Eq "^[[:space:]]*import[[:space:]]+/etc/caddy/sites/[*]([.]caddy)?[[:space:]]*(#.*)?\$" /etc/caddy/Caddyfile' ||
	die "/etc/caddy/Caddyfile does not import the sites directory. Add this line
to it by hand (up.sh never edits the shared Caddyfile), then run up.sh again:
    import /etc/caddy/sites/*.caddy"
remote "getent group caddy" >/dev/null ||
	die "$HOST has no caddy group; the app's socket is shared with Caddy through it"
host_ips=$(remote "hostname -I")
host_ips=" $(printf '%s' "$host_ips" | tr -s '[:space:]' ' ' | tr 'A-F' 'a-f') "
# Every AAAA record must be the host too, or IPv6 clients reach another server.
dns6=$(dig +short -t AAAA "$domain")
for ip in $(printf '%s\n' "$dns6" | grep -E '^[0-9A-Fa-f:]+$' | tr 'A-F' 'a-f' || true); do
	case $host_ips in
	*" $ip "*) ;;
	*) die "DNS: $domain has AAAA record $ip, which is not an address of $HOST:$host_ips" ;;
	esac
done
dns=$(dig +short "$domain" A)
dns_ips=$(printf '%s\n' "$dns" | grep -E '^[0-9]+([.][0-9]+){3}$' || true)
[ -n "$dns_ips" ] || die "DNS: $domain has no A record; point it at $HOST first"
for ip in $dns_ips; do
	case $host_ips in
	*" $ip "*) ;;
	*) die "DNS: $domain resolves to $ip, which is not an address of $HOST:$host_ips" ;;
	esac
done
state=$(remote "test -e /srv/conference-drawing/data/.env && echo installed || echo fresh")
case $state in
installed | fresh) ;;
*) die "unexpected answer from $HOST: $state" ;;
esac
if [ -n "$restore" ] && [ "$state" = installed ]; then
	die "conference-drawing is already installed on $HOST; --restore needs a fresh install (run down.sh first)"
fi

# 3. uv, pinned by version and hash.
machine=$(remote "uname -m")
case $machine in
x86_64) arch=x86_64 ;;
aarch64 | arm64) arch=aarch64 ;;
*) die "unsupported server architecture: $machine" ;;
esac
server_uv=$(remote "/usr/local/bin/uv --version 2>/dev/null || echo none")
if [ "$(printf '%s' "$server_uv" | cut -d' ' -f1-2)" != "uv $UV_VERSION" ]; then
	uv_tarball="uv-$arch-unknown-linux-gnu.tar.gz"
	expected=$(awk -v name="$uv_tarball" '$2 == name { print $1 }' "$uv_hashes")
	[[ $expected =~ ^[0-9a-f]{64}$ ]] || die "$uv_hashes has no sha256 for $uv_tarball"
	say "downloading uv $UV_VERSION for $arch"
	curl -fsSL --proto '=https' --tlsv1.2 -o "$payload/$uv_tarball" \
		"https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_tarball"
	actual=$(sha256_of "$payload/$uv_tarball")
	[ "$actual" = "$expected" ] ||
		die "sha256 mismatch for the downloaded $uv_tarball: $actual, expected $expected"
	printf '%s  %s\n' "$actual" "$uv_tarball" >"$payload/uv.sha256"
fi

# 4. Secrets, collected into a 0600 file that only ever reaches ssh's stdin.
for key in DRIP_API_KEY DRIP_ACCOUNT_ID; do
	value=$(env_value "$laptop_env" "$key")
	[ -z "$value" ] || printf '%s=%s\n' "$key" "$value" >>"$secrets"
done
if [ "$state" = fresh ] && [ -z "$restore" ]; then
	prompt_owner
fi

# 5. Copy and install.
stage up "$payload"/*
say "copied the release to $HOST:$RDIR"
command="bash $RDIR/remote/install.sh $domain"
[ -z "$restore" ] || command="$command --restore $RDIR/restore.tar.gz"
say "installing on $HOST"
remote_stdin "$command" "$secrets"
rm -f "$secrets"

# 6. Smoke test over HTTPS, through Caddy.
say "smoke testing https://$domain/"
health=$(curl -fsS --max-time 10 --retry 10 --retry-delay 3 --retry-all-errors "https://$domain/health")
[[ $health == *'"status": "ok"'* ]] || die "/health answered: $health"
version=$(curl -fsS --max-time 10 "https://$domain/version")
[[ $version == *"\"commit\": \"$COMMIT\""* ]] ||
	die "/version does not report commit $COMMIT: $version"
curl -fsS --max-time 10 -o /dev/null "https://$domain/privacy/"
unstage
say "done: https://$domain/ runs $COMMIT"
