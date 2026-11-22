#!/usr/bin/env bash
# Server half of deploy/backup.sh. Runs as root from
# /tmp/conference-drawing-<commit>-backup-<random>/remote/. Prints the tarball's path as its
# last line; the laptop downloads it and then deletes the directory.
#
# Root never opens a file in the app user's directory and never connects to
# the database: the app user runs pg_dump -Fc (peer auth) into its own
# snapshots/ directory, reads the dump back fully with pg_restore -f /dev/null,
# and tars it with media/ to stdout; root only writes that stream into its own
# 0700 temp directory. (tar stores links inside media/ as links.)
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
no_symlink "$DATA_DIR"
umask 077
out=$(mktemp -d "$ROOT/tmp/conference-drawing-backup.XXXXXXXX")
copy="$DATA_DIR/snapshots/backup"
locked=0
# The dump holds personal data and snapshot rotation never looks at it, so it
# goes whatever happens; a failed run leaves no partial tarball either. The
# copy is ours only once we hold the lock (another backup may be using it).
on_exit() {
	local status=$?
	set +e
	[ "$locked" -eq 0 ] || as_app rm -rf -- "$copy" 2>/dev/null
	[ "$status" -eq 0 ] || rm -rf -- "$out"
	return 0
}
trap on_exit EXIT

take_lock 300
locked=1
as_app rm -rf -- "$copy"
as_app mkdir -m 0700 "$copy"
as_app pg_dump -Fc --file "$copy/db.dump" --dbname="$DB_NAME"
as_app pg_restore -f /dev/null "$copy/db.dump" ||
	die "the backup dump failed verification (pg_restore could not read it back)"
media=()
[ ! -d "$DATA_DIR/media" ] || [ -L "$DATA_DIR/media" ] || media=(-C "$DATA_DIR" media)
as_app tar -czf - -C "$copy" db.dump "${media[@]+"${media[@]}"}" >"$out/backup.tar.gz"
as_app rm -rf -- "$copy"
printf '%s\n' "$out/backup.tar.gz"
