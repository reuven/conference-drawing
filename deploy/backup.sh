#!/usr/bin/env bash
# Pull a consistent backup of a conference-drawing install to this laptop:
#
#   deploy/backup.sh <host>
#
# On the server, remote/backup.sh takes the maintenance lock, copies the
# database with sqlite3 .backup, checks it with PRAGMA integrity_check, and
# tars it with media/. The tarball lands in backups/<host>-<UTC time>.tar.gz
# (git-ignored, mode 0600: it holds personal data until entrants are
# deleted). The local path is the last line on stdout, for down.sh.
#
# The tarball is NOT encrypted: keep backups/ on an encrypted disk, and delete
# old ones once entrants are deleted. (Encrypting with age is deferred.)
#
# Run it during a conference as often as you like, and after it. Test hook:
# CD_BACKUP_DIR overrides the backups/ directory.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ $# -eq 1 ] || {
	echo "usage: deploy/backup.sh <host>" >&2
	exit 2
}
set_target "$1"
backups=${CD_BACKUP_DIR:-$REPO/backups}

umask 077
mkdir -p "$backups"
local_file="$backups/$HOST-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
[ ! -e "$local_file" ] || die "$local_file already exists"

local_commit
stage backup "$DEPLOY/remote"
say "backing up $HOST"
remote_file=$(remote "bash $RDIR/remote/backup.sh")
remote_file=$(printf '%s\n' "$remote_file" | tail -n 1)
[[ $remote_file =~ ^/tmp/conference-drawing-backup\.[A-Za-z0-9]+/backup\.tar\.gz$ ]] ||
	die "unexpected backup path from $HOST: $remote_file"

scp -q "${SSH_OPTS[@]}" "$TARGET:$remote_file" "$local_file"
# grep without -q reads all of tar's output, so pipefail sees no SIGPIPE.
if [ ! -s "$local_file" ] || ! tar -tzf "$local_file" 2>/dev/null | grep -x 'db.sqlite3' >/dev/null; then
	rm -f "$local_file"
	die "the downloaded backup is empty or has no database; the server copy is kept at $remote_file"
fi
chmod 600 "$local_file"
remote "rm -rf ${remote_file%/backup.tar.gz}"
unstage
say "backup saved to $local_file"
printf '%s\n' "$local_file"
