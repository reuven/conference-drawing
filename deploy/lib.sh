# shellcheck shell=bash
# Shared helpers for the laptop-side scripts (up.sh, backup.sh, down.sh).
# Sourced, not run. Written for bash 3.2 (macOS's /bin/bash) as well as bash 5.

# shellcheck disable=SC2034  # REPO and friends are used by the sourcing scripts
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$REPO/deploy"
SSH_USER=${CD_SSH_USER:-root}
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15)

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

local_commit() {
	COMMIT=$(git -C "$REPO" rev-parse HEAD)
	[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] || die "unexpected commit id from git"
	RDIR="/tmp/conference-drawing-$COMMIT"
}

# Copy files to a fresh /tmp/conference-drawing-<commit>/ on the server. Remote
# scripts always run from there, never from a directory an install is changing.
stage() {
	remote "rm -rf $RDIR && mkdir -m 700 $RDIR"
	scp -q -r "${SSH_OPTS[@]}" "$@" "$TARGET:$RDIR/"
}

unstage() { remote "rm -rf $RDIR"; }
