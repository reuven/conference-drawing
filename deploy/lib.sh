# shellcheck shell=bash
# Shared helpers for the laptop-side scripts (up.sh, backup.sh, down.sh).
# Sourced, not run. Written for bash 3.2 (macOS's /bin/bash) as well as bash 5.

# shellcheck disable=SC2034  # REPO and friends are used by the sourcing scripts
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$REPO/deploy"
SSH_USER=${CD_SSH_USER:-root}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)
# Must match remote/common.sh; deploy/uv-$UV_VERSION.sha256 holds its hashes.
UV_VERSION=0.13.9

say() { printf '==> %s\n' "$*" >&2; }
die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

# A host is a DNS name or an IPv4 address; never anything ssh could read as an
# option or a command.
valid_host() {
	[[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]
}

# A domain goes into the Caddy site file and DJANGO_ALLOWED_HOSTS, so it must be
# a lowercase DNS name with at least one dot. Must match remote/common.sh.
valid_domain() {
	local label='[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
	[[ ${#1} -le 253 && $1 =~ ^($label\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

# The owner's login name: what up.sh asks for and Django accepts. Must match
# remote/common.sh.
valid_username() {
	[[ $1 =~ ^[A-Za-z0-9@.+_][A-Za-z0-9@.+_-]{0,149}$ ]]
}

set_target() {
	valid_host "$1" || die "not a host name: $1"
	HOST=$1
	TARGET="$SSH_USER@$1"
}

# Run one fixed command on the server. stdin is closed so no command can read
# the operator's terminal; remote_stdin feeds a file instead.
# shellcheck disable=SC2029  # the command expands here on purpose, then runs there
remote() { ssh "${SSH_OPTS[@]}" "$TARGET" "$1" </dev/null; }
# shellcheck disable=SC2029
remote_stdin() { ssh "${SSH_OPTS[@]}" "$TARGET" "$1" <"$2"; }
# Run one fixed command on the server with a terminal, and keep stdin, so the
# command can prompt the operator (a password typed at the server's own prompt,
# never in argv). Use it only for a command that must prompt.
# shellcheck disable=SC2029
remote_tty() { ssh -t "${SSH_OPTS[@]}" "$TARGET" "$1"; }

local_commit() {
	COMMIT=$(git -C "$REPO" rev-parse HEAD)
	[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] || die "unexpected commit id from git"
}

# Copy files to a new /tmp/conference-drawing-<commit>-<name>-<random>/ on the
# server, one per run, so two runs (or down.sh and the backup.sh it calls)
# never share or delete each other's files. mkdir fails if the name exists,
# so nothing planted in /tmp is reused. Remote scripts always run from there,
# never from a directory an install is changing.
stage() {
	local name=$1 suffix
	shift
	suffix=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')
	[[ $suffix =~ ^[0-9a-f]{8}$ ]] || die "could not make a staging name"
	RDIR="/tmp/conference-drawing-$COMMIT-$name-$suffix"
	remote "mkdir -m 700 $RDIR"
	scp -q -r "${SSH_OPTS[@]}" "$@" "$TARGET:$RDIR/"
}

# Print the sha256 of file $1 (coreutils on Linux, shasum on macOS).
sha256_of() {
	if command -v sha256sum >/dev/null; then
		sha256sum "$1" | cut -d' ' -f1
	else
		shasum -a 256 "$1" | cut -d' ' -f1
	fi
}

unstage() { remote "rm -rf $RDIR"; }
